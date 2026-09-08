from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from .common import (
    IntegrityError,
    canonical_bytes,
    digest_bytes,
    digest_file,
    json_object_from_bytes,
    parse_fraction_text,
    read_bounded_bytes,
    read_json_object,
    require_sha256,
)
from .evaluator import (
    _load_committed_prediction,
    _overall_counts,
    _prediction_pairs,
    metrics_from_labels,
    validate_truth_matrix,
    validate_truth_split_isolation,
    verify_truth_manifest,
)
from .identity import run_key
from .matcher import (
    _raw_rows,
    build_identity_envelope,
    resolve_split,
    verify_observed_bundle,
)
from .normalization import canonical_price
from .paths import validate_paths
from .reporting import render_report
from .scoring import config_from_mapping


def _expected_pair_rows(
    prediction: dict[str, Any],
) -> dict[tuple[str, str, str], tuple[object, ...]]:
    expected: dict[tuple[str, str, str], tuple[object, ...]] = {}
    for split in ("calibration", "validation"):
        for pair in _prediction_pairs(prediction, split):
            scores = pair["scores"]
            explanation = pair["explanation"]
            score_columns: list[object] = []
            for name in ("name", "brand", "sku", "price", "total"):
                score = scores[name]
                if score is None:
                    score_columns.extend((None, None, None))
                else:
                    exact = parse_fraction_text(score["fraction"])
                    score_columns.extend(
                        (score["fraction"], str(exact.numerator), str(exact.denominator))
                    )
            expected[split, pair["left_id"], pair["right_id"]] = (
                int(pair["admitted"]),
                pair["block_reasons"],
                *score_columns,
                pair["evidence_count"],
                pair["contradictions"],
                pair["decision"],
                explanation["left_rank"],
                explanation["right_rank"],
                explanation["left_margin"],
                explanation["right_margin"],
                {
                    "failed_conditions": explanation["failed_conditions"],
                    "reasons": explanation["reasons"],
                },
            )
    return expected


def _verify_pair_projection(
    connection: sqlite3.Connection, run: str, prediction: dict[str, Any]
) -> None:
    expected = _expected_pair_rows(prediction)
    actual_rows = connection.execute(
        "SELECT p.split,p.left_id,p.right_id,p.candidate,p.block_reasons_json,"
        "p.name_score,p.name_numerator,p.name_denominator,"
        "p.brand_score,p.brand_numerator,p.brand_denominator,"
        "p.sku_score,p.sku_numerator,p.sku_denominator,"
        "p.price_score,p.price_numerator,p.price_denominator,"
        "p.total_score,p.total_numerator,p.total_denominator,p.evidence_count,"
        "p.contradictions_json,d.decision,d.left_rank,d.right_rank,d.left_margin,d.right_margin,"
        "d.explanation_json FROM pair_scores p JOIN decisions d ON "
        "d.run_key=p.run_key AND d.split=p.split AND d.left_record_id=p.left_record_id "
        "AND d.right_record_id=p.right_record_id WHERE p.run_key=? "
        "ORDER BY p.split,p.left_id,p.right_id",
        (run,),
    ).fetchall()
    actual: dict[tuple[str, str, str], tuple[object, ...]] = {}
    for row in actual_rows:
        actual[str(row[0]), str(row[1]), str(row[2])] = (
            int(row[3]),
            json.loads(row[4]),
            *row[5:21],
            json.loads(row[21]),
            row[22],
            int(row[23]),
            int(row[24]),
            row[25],
            row[26],
            json.loads(row[27]),
        )
    if actual != expected:
        raise IntegrityError("SQLite pair or decision projection differs from prediction")


def _verify_ingestion_projection(connection: sqlite3.Connection, run: str, observed: Any) -> None:
    batches = connection.execute(
        "SELECT rb.split,rb.role,rb.batch_id,b.content_sha256,b.row_count FROM run_batches rb "
        "JOIN ingestion_batches b ON b.id=rb.batch_id WHERE rb.run_key=? "
        "ORDER BY rb.split,rb.role",
        (run,),
    ).fetchall()
    if len(batches) != 4:
        raise IntegrityError("run does not reference exactly four ingestion batches")
    for split, role, batch_id, content_digest, row_count in batches:
        catalog = observed.catalogs[str(split), str(role)]
        if content_digest != catalog.content_sha256 or row_count != len(catalog.records):
            raise IntegrityError("run batch lineage differs from observed input")
        stored = connection.execute(
            "SELECT source_id,raw_json,normalized_name,normalized_brand,normalized_sku,"
            "normalized_category,normalized_price,fingerprint FROM source_records "
            "WHERE batch_id=? ORDER BY source_id",
            (batch_id,),
        ).fetchall()
        expected = sorted(
            (
                record.normalized.source_id,
                canonical_bytes(record.raw).decode("utf-8"),
                record.normalized.name,
                record.normalized.brand,
                record.normalized.sku,
                record.normalized.category,
                canonical_price(record.normalized.price),
                record.normalized.fingerprint,
            )
            for record in catalog.records
        )
        if stored != expected:
            raise IntegrityError("stored source lineage differs from normalized observed input")
    expected_events = Counter(
        (
            None,
            None,
            canonical_bytes(
                {
                    "catalog_role": f"{split}:{role}",
                    "content_sha256": observed.catalogs[split, role].content_sha256,
                    "row_count": len(observed.catalogs[split, role].records),
                }
            ).decode("utf-8"),
        )
        for split in ("calibration", "validation")
        for role in ("supplier_a", "supplier_b")
    )
    relevant_payloads = {item[2] for item in expected_events}
    actual_events = Counter(
        (event_run, evaluation_key, payload)
        for event_run, evaluation_key, payload in connection.execute(
            "SELECT run_key,evaluation_key,payload_json FROM audit_events "
            "WHERE event_type='ingestion_complete'"
        )
        if payload in relevant_payloads
    )
    if actual_events != expected_events:
        raise IntegrityError("ingestion audit trail is missing or duplicated")


def _verify_audit_events(connection: sqlite3.Connection) -> None:
    allowed = {"ingestion_complete", "run_complete", "evaluation_complete"}
    for event_run, evaluation_key, event_type, payload_json in connection.execute(
        "SELECT run_key,evaluation_key,event_type,payload_json FROM audit_events ORDER BY id"
    ):
        if event_type not in allowed or not isinstance(payload_json, str):
            raise IntegrityError("audit trail contains an unknown event")
        payload = json_object_from_bytes(payload_json.encode("utf-8"))
        if canonical_bytes(payload).decode("utf-8") != payload_json:
            raise IntegrityError("audit event payload is not canonical JSON")
        if event_type == "ingestion_complete":
            if (
                event_run is not None
                or evaluation_key is not None
                or set(payload)
                != {
                    "catalog_role",
                    "content_sha256",
                    "row_count",
                }
            ):
                raise IntegrityError("ingestion audit event schema differs")
            role, row_count = payload["catalog_role"], payload["row_count"]
            require_sha256(payload["content_sha256"], field="ingestion event digest")
            if (
                not isinstance(role, str)
                or not role
                or isinstance(row_count, bool)
                or not isinstance(row_count, int)
                or row_count < 0
            ):
                raise IntegrityError("ingestion audit event value is invalid")
        elif event_type == "run_complete":
            if (
                not isinstance(event_run, str)
                or evaluation_key is not None
                or set(payload) != {"prediction_digest", "run_key"}
                or payload["run_key"] != event_run
            ):
                raise IntegrityError("run audit event schema differs")
            prediction_digest = require_sha256(
                payload["prediction_digest"], field="run event prediction digest"
            )
            stored = connection.execute(
                "SELECT prediction_digest FROM resolution_runs WHERE run_key=?", (event_run,)
            ).fetchone()
            if stored != (prediction_digest,):
                raise IntegrityError("run audit event differs from stored run")
        else:
            if (
                not isinstance(event_run, str)
                or not isinstance(evaluation_key, str)
                or set(payload) != {"evaluation_key", "report_bundle_digest", "truth_bundle_digest"}
                or payload["evaluation_key"] != evaluation_key
            ):
                raise IntegrityError("evaluation audit event schema differs")
            report_digest = require_sha256(
                payload["report_bundle_digest"], field="evaluation event report digest"
            )
            truth_digest = require_sha256(
                payload["truth_bundle_digest"], field="evaluation event truth digest"
            )
            stored = connection.execute(
                "SELECT run_key,report_bundle_digest,truth_bundle_digest FROM evaluations "
                "WHERE evaluation_key=?",
                (evaluation_key,),
            ).fetchone()
            if stored != (event_run, report_digest, truth_digest):
                raise IntegrityError("evaluation audit event differs from stored evaluation")


def _verify_canonical_projection(
    connection: sqlite3.Connection, run: str, prediction: dict[str, Any]
) -> None:
    entities = connection.execute(
        "SELECT canonical_key,kind FROM canonical_entities WHERE run_key=? ORDER BY canonical_key",
        (run,),
    ).fetchall()
    expected_entities = sorted(
        (entity["canonical_key"], entity["kind"]) for entity in prediction["canonical_entities"]
    )
    members = connection.execute(
        "SELECT canonical_key,source_id,split,role FROM canonical_members WHERE run_key=? "
        "ORDER BY split,role,source_id",
        (run,),
    ).fetchall()
    expected_members = [
        (member["canonical_key"], member["source_id"], member["split"], member["role"])
        for member in prediction["canonical_members"]
    ]
    if entities != expected_entities or members != expected_members:
        raise IntegrityError("canonical SQLite projection differs from prediction")
    sizes = connection.execute(
        "SELECT e.kind,count(m.source_record_id) FROM canonical_entities e "
        "LEFT JOIN canonical_members m ON m.run_key=e.run_key AND m.canonical_key=e.canonical_key "
        "WHERE e.run_key=? GROUP BY e.canonical_key,e.kind",
        (run,),
    ).fetchall()
    if any(size != (2 if kind == "matched" else 1) for kind, size in sizes):
        raise IntegrityError("canonical entity cardinality is invalid")


def _recalculated_report(
    prediction: dict[str, Any],
    truth: Any,
    observed_manifest: Path,
    predictions_json: Path,
) -> tuple[dict[str, object], dict[str, dict[str, int | float | None]]]:
    metrics: dict[str, dict[str, int | float | None]] = {}
    review_rows: list[dict[str, object]] = []
    all_decisions: list[str] = []
    for split in ("calibration", "validation"):
        pairs = _prediction_pairs(prediction, split)
        decisions = {
            (str(pair["left_id"]), str(pair["right_id"])): str(pair["decision"]) for pair in pairs
        }
        left_ids = {left for left, _ in decisions}
        right_ids = {right for _, right in decisions}
        validate_truth_matrix(
            truth.rows[split], left_ids=left_ids, right_ids=right_ids, split=split
        )
        metric = metrics_from_labels(
            predictions=[decisions[row["left_id"], row["right_id"]] for row in truth.rows[split]],
            labels=[int(row["label"]) for row in truth.rows[split]],
        )
        metrics[split] = metric.as_payload()
        for pair in pairs:
            all_decisions.append(str(pair["decision"]))
            if pair["decision"] == "REVIEW":
                review_rows.append({"split": split, **pair})
    prediction_digest = digest_file(predictions_json)
    evaluation_envelope: dict[str, object] = {
        "schema": "erw-evaluation-key-v1",
        "run_key": str(prediction["run_key"]),
        "prediction_digest": prediction_digest,
        "truth_manifest_digest": truth.manifest_digest,
        "truth_bundle_digest": truth.bundle_digest,
        "truth_files": list(truth.entries),
        "evaluator_source_digest": digest_file(Path(__file__).with_name("evaluator.py")),
        "config_digest": str(prediction["config_digest"]),
    }
    evaluation_key = run_key(evaluation_envelope)
    report: dict[str, object] = {
        "schema": "erw-report-v1",
        "evaluation_key": evaluation_key,
        "run_key": str(prediction["run_key"]),
        "metrics_by_split": metrics,
        "overall_descriptive_counts": _overall_counts(metrics),
        "decision_counts": {
            decision: all_decisions.count(decision) for decision in ("MATCH", "REVIEW", "NO_MATCH")
        },
        "review_rows": sorted(
            review_rows,
            key=lambda row: (str(row["split"]), str(row["left_id"]), str(row["right_id"])),
        ),
        "provenance": {
            "config_sha256": str(prediction["config_digest"]),
            "evaluation_identity": evaluation_envelope,
            "observed_manifest_sha256": digest_file(observed_manifest),
            "prediction_sha256": prediction_digest,
            "truth_bundle_digest": truth.bundle_digest,
            "truth_manifest_sha256": truth.manifest_digest,
        },
    }
    return report, metrics


def audit(
    *,
    workspace_root: Path,
    database: Path,
    observed_manifest: Path,
    truth_manifest: Path,
    predictions_json: Path,
    truth_root: Path,
    report_json: Path,
    report_html: Path,
) -> dict[str, bool]:
    config_json = workspace_root / "config" / "matching-v1.json"
    lock_path = database.with_suffix(database.suffix + ".lock")
    validate_paths(
        workspace_root,
        inputs=(
            observed_manifest,
            truth_manifest,
            predictions_json,
            report_json,
            report_html,
            config_json,
            truth_root,
        ),
        outputs=(database,),
        locks=(lock_path,),
    )
    truth = verify_truth_manifest(
        truth_manifest,
        truth_root,
        workspace_root=workspace_root,
        additional_inputs=(
            observed_manifest,
            predictions_json,
            report_json,
            report_html,
            config_json,
        ),
        outputs=(database,),
        locks=(lock_path,),
    )
    validate_truth_split_isolation(truth.rows)
    observed = verify_observed_bundle(
        workspace_root=workspace_root,
        observed_manifest=observed_manifest,
        observed_root=observed_manifest.parent / "observed",
        config_json=config_json,
        output_paths=(database, predictions_json, report_json, report_html),
        lock_paths=(lock_path,),
    )
    if truth.manifest["observed_manifest_sha256"] != observed.manifest_digest:
        raise IntegrityError("truth and observed manifests are not bound")
    prediction, prediction_bytes, run_row = _load_committed_prediction(database, predictions_json)
    config_raw = prediction.get("frozen_config")
    if not isinstance(config_raw, dict):
        raise IntegrityError("prediction does not contain frozen configuration")
    config = config_from_mapping(config_raw)
    expected_envelope = build_identity_envelope(
        observed, config=config, config_digest=digest_file(config_json)
    )
    expected_run = run_key(expected_envelope)
    if (
        prediction["run_key"] != expected_run
        or prediction["identity_envelope"] != expected_envelope
    ):
        raise IntegrityError("run identity does not reproduce from current inputs and code")
    pairs_by_split = {
        split: resolve_split(
            _raw_rows(observed.catalogs[split, "supplier_a"]),
            _raw_rows(observed.catalogs[split, "supplier_b"]),
            config,
        )
        for split in ("calibration", "validation")
    }
    from .matcher import _prediction_payload

    expected_prediction = _prediction_payload(
        run=expected_run,
        envelope=expected_envelope,
        config_raw=config_raw,
        config=config,
        verified=observed,
        pairs_by_split=pairs_by_split,
    )
    if canonical_bytes(expected_prediction) != prediction_bytes:
        raise IntegrityError("prediction does not reproduce from observed inputs")
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise IntegrityError("SQLite integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise IntegrityError("SQLite foreign-key check failed")
        _verify_audit_events(connection)
        _verify_ingestion_projection(connection, expected_run, observed)
        _verify_pair_projection(connection, expected_run, prediction)
        _verify_canonical_projection(connection, expected_run, prediction)
        run_event = connection.execute(
            "SELECT count(*) FROM audit_events WHERE run_key=? AND event_type='run_complete'",
            (expected_run,),
        ).fetchone()[0]
        if run_event != 1:
            raise IntegrityError("run audit trail is missing or duplicated")
    expected_report, metrics = _recalculated_report(
        prediction, truth, observed_manifest, predictions_json
    )
    report_payload = read_json_object(report_json, max_bytes=20_000_000)
    if report_payload != expected_report:
        raise IntegrityError("report metrics or provenance do not recalculate")
    rendered = render_report(expected_report)
    if (
        read_bounded_bytes(
            report_json, max_bytes=len(rendered.json_bytes), description="JSON report"
        )
        != rendered.json_bytes
        or read_bounded_bytes(
            report_html, max_bytes=len(rendered.html_bytes), description="HTML report"
        )
        != rendered.html_bytes
    ):
        raise IntegrityError("report files are non-canonical or divergent")
    evaluation_key = str(expected_report["evaluation_key"])
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT run_key,truth_manifest_digest,truth_bundle_digest,config_digest,"
            "prediction_digest,identity_envelope_json,metrics_json,report_json_digest,"
            "report_html_digest,report_bundle_digest,report_json,report_html FROM evaluations "
            "WHERE evaluation_key=?",
            (evaluation_key,),
        ).fetchone()
        evaluation_events = connection.execute(
            "SELECT count(*) FROM audit_events WHERE evaluation_key=? "
            "AND event_type='evaluation_complete'",
            (evaluation_key,),
        ).fetchone()[0]
    provenance = expected_report["provenance"]
    if not isinstance(provenance, dict):
        raise IntegrityError("report provenance is invalid")
    identity = provenance["evaluation_identity"]
    expected_json_digest = digest_bytes(rendered.json_bytes)
    expected_html_digest = digest_bytes(rendered.html_bytes)
    expected_bundle_digest = digest_bytes(
        canonical_bytes({"html_sha256": expected_html_digest, "json_sha256": expected_json_digest})
    )
    expected_row = (
        expected_run,
        truth.manifest_digest,
        truth.bundle_digest,
        prediction["config_digest"],
        digest_bytes(prediction_bytes),
        canonical_bytes(identity).decode("utf-8"),
        canonical_bytes(metrics).decode("utf-8"),
        expected_json_digest,
        expected_html_digest,
        expected_bundle_digest,
        rendered.json_bytes,
        rendered.html_bytes,
    )
    if row != expected_row or evaluation_events != 1 or run_row[6] != "complete":
        raise IntegrityError("evaluation SQLite projection or audit trail differs")
    checks = {
        "observed_manifest": True,
        "truth_manifest": True,
        "prediction": True,
        "run_identity": True,
        "ingestion_lineage": True,
        "scores_and_decisions": True,
        "canonical_membership": True,
        "metrics_recalculated": True,
        "report_json": True,
        "report_html": True,
        "sqlite_integrity": True,
        "sqlite_foreign_keys": True,
        "audit_trail": True,
    }
    return {"ok": True, **checks}
