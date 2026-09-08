from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .common import (
    IntegrityError,
    InvalidDataError,
    ResourceBoundError,
    canonical_bytes,
    digest_bytes,
    digest_file,
    json_object_from_bytes,
    read_bounded_bytes,
    read_json_object,
    require_sha256,
)
from .database import CompleteEvaluation, exclusive_lock, store_complete_evaluation
from .identity import run_key
from .paths import validate_paths
from .publication import publish_evaluation_verified
from .reporting import render_report


class InvalidTruthError(InvalidDataError):
    pass


_TRUTH_HEADERS = ("left_id", "right_id", "label", "split", "family_id", "scenario")


@dataclass(frozen=True)
class Metrics:
    tp: int
    fp: int
    tn: int
    fn: int
    precision: float | None
    recall: float | None
    f1: float | None

    def as_payload(self) -> dict[str, int | float | None]:
        return asdict(self)


@dataclass(frozen=True)
class VerifiedTruth:
    manifest: dict[str, Any]
    manifest_digest: str
    bundle_digest: str
    entries: tuple[dict[str, object], ...]
    rows: dict[str, list[dict[str, str]]]
    paths: tuple[Path, ...]


def metrics_from_labels(*, predictions: list[str], labels: list[int]) -> Metrics:
    tp = fp = tn = fn = 0
    for prediction, label in zip(predictions, labels, strict=True):
        if prediction not in {"MATCH", "REVIEW", "NO_MATCH"} or label not in {0, 1}:
            raise InvalidTruthError("metrics input has an invalid decision or label")
        predicted_positive = prediction == "MATCH"
        gold_positive = label == 1
        if predicted_positive and gold_positive:
            tp += 1
        elif predicted_positive:
            fp += 1
        elif gold_positive:
            fn += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None
    return Metrics(tp, fp, tn, fn, precision, recall, f1)


def validate_truth_matrix(
    rows: list[dict[str, str]], *, left_ids: set[str], right_ids: set[str], split: str
) -> None:
    expected = {(left_id, right_id) for left_id in left_ids for right_id in right_ids}
    found: list[tuple[str, str]] = []
    left_metadata: dict[str, tuple[str, str]] = {}
    positives: list[tuple[str, str]] = []
    for row in rows:
        if set(row) != set(_TRUTH_HEADERS):
            raise InvalidTruthError("truth row fields differ from schema")
        if row["label"] not in {"0", "1"} or row["split"] != split:
            raise InvalidTruthError("truth label or split is invalid")
        if not row["family_id"] or not row["scenario"]:
            raise InvalidTruthError("truth family and scenario are required")
        pair = (row["left_id"], row["right_id"])
        found.append(pair)
        metadata = (row["family_id"], row["scenario"])
        old_metadata = left_metadata.setdefault(row["left_id"], metadata)
        if old_metadata != metadata:
            raise InvalidTruthError("truth metadata changes within one left record")
        if row["label"] == "1":
            positives.append(pair)
    if set(found) != expected or len(found) != len(set(found)):
        raise InvalidTruthError("truth is not the complete Cartesian matrix")
    if len({left for left, _ in positives}) != len(positives):
        raise InvalidTruthError("one left record has multiple positive labels")
    if len({right for _, right in positives}) != len(positives):
        raise InvalidTruthError("one right record has multiple positive labels")


def validate_truth_split_isolation(rows_by_split: dict[str, list[dict[str, str]]]) -> None:
    identifiers: dict[str, set[str]] = {}
    families: dict[str, set[str]] = {}
    for split in ("calibration", "validation"):
        rows = rows_by_split[split]
        identifiers[split] = {
            identifier for row in rows for identifier in (row["left_id"], row["right_id"])
        }
        families[split] = {row["family_id"] for row in rows}
    if identifiers["calibration"] & identifiers["validation"]:
        raise InvalidTruthError("observed identifiers cross split boundary")
    if families["calibration"] & families["validation"]:
        raise InvalidTruthError("truth families cross split boundary")


def _read_truth_csv(path: Path) -> list[dict[str, str]]:
    data = read_bounded_bytes(path, max_bytes=1_000_000, description="truth CSV")
    if b"\0" in data:
        raise InvalidTruthError("truth CSV contains NUL")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidTruthError("truth CSV is not UTF-8") from exc
    previous_limit = csv.field_size_limit()
    csv.field_size_limit(4096)
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if tuple(reader.fieldnames or ()) != _TRUTH_HEADERS:
            raise InvalidTruthError("truth CSV headers differ from schema")
        raw_rows = list(reader)
    except csv.Error as exc:
        raise InvalidTruthError("malformed truth CSV") from exc
    finally:
        csv.field_size_limit(previous_limit)
    rows: list[dict[str, str]] = []
    for row in raw_rows:
        if set(row) != set(_TRUTH_HEADERS) or any(value is None for value in row.values()):
            raise InvalidTruthError("truth CSV row differs from schema")
        if any(len(value) > 4096 for value in row.values()):
            raise ResourceBoundError("truth field exceeds character bound")
        rows.append({key: row[key] for key in _TRUTH_HEADERS})
    return rows


def verify_truth_manifest(
    manifest_path: Path,
    truth_root: Path,
    *,
    workspace_root: Path | None = None,
    additional_inputs: tuple[Path, ...] = (),
    outputs: tuple[Path, ...] = (),
    locks: tuple[Path, ...] = (),
) -> VerifiedTruth:
    manifest = read_json_object(manifest_path)
    if set(manifest) != {
        "schema_version",
        "truth_files",
        "truth_bundle_digest",
        "observed_manifest_sha256",
    }:
        raise IntegrityError("truth manifest schema mismatch")
    if isinstance(manifest["schema_version"], bool) or manifest["schema_version"] != 1:
        raise IntegrityError("unsupported truth manifest schema")
    require_sha256(manifest["observed_manifest_sha256"], field="observed_manifest_sha256")
    claimed_bundle = require_sha256(manifest["truth_bundle_digest"], field="truth_bundle_digest")
    items = manifest["truth_files"]
    if not isinstance(items, list) or len(items) != 2:
        raise IntegrityError("truth manifest must contain two files")
    verified_entries: list[dict[str, object]] = []
    rows_by_split: dict[str, list[dict[str, str]]] = {}
    paths: list[Path] = []
    plans: list[tuple[str, str, Path, str, int]] = []
    for index, split in enumerate(("calibration", "validation")):
        item = items[index]
        if not isinstance(item, dict) or set(item) != {"split", "path", "sha256", "rows"}:
            raise IntegrityError("truth manifest entry schema mismatch")
        if item["split"] != split:
            raise IntegrityError("truth manifest entries are not in canonical order")
        raw_path = item["path"]
        if not isinstance(raw_path, str):
            raise IntegrityError("truth manifest path is invalid")
        posix = PurePosixPath(raw_path)
        if posix.is_absolute() or "." in posix.parts or ".." in posix.parts:
            raise IntegrityError("truth manifest path is unsafe")
        path = manifest_path.parent.joinpath(*posix.parts)
        expected_path = truth_root / f"{split}.csv"
        if path.absolute() != expected_path.absolute():
            raise IntegrityError("truth manifest path differs from requested root")
        claimed_digest = require_sha256(item["sha256"], field="truth file sha256")
        if isinstance(item["rows"], bool) or not isinstance(item["rows"], int) or item["rows"] < 0:
            raise IntegrityError("truth manifest row count is invalid")
        plans.append((split, raw_path, path, claimed_digest, item["rows"]))
        paths.append(path)
    validate_paths(
        workspace_root or manifest_path.parent,
        inputs=(manifest_path, truth_root, *additional_inputs, *paths),
        outputs=outputs,
        locks=locks,
    )
    for split, raw_path, path, claimed_digest, claimed_rows in plans:
        rows = _read_truth_csv(path)
        actual_digest = digest_file(path)
        if actual_digest != claimed_digest or len(rows) != claimed_rows:
            raise IntegrityError("truth file differs from manifest digest or row count")
        rows_by_split[split] = rows
        verified_entries.append(
            {
                "path": raw_path,
                "rows": len(rows),
                "sha256": actual_digest,
                "split": split,
            }
        )
    bundle = [
        {"split": entry["split"], "truth_sha256": entry["sha256"]} for entry in verified_entries
    ]
    actual_bundle = digest_bytes(canonical_bytes(bundle))
    if actual_bundle != claimed_bundle:
        raise IntegrityError("truth bundle digest mismatch")
    return VerifiedTruth(
        manifest=manifest,
        manifest_digest=digest_file(manifest_path),
        bundle_digest=actual_bundle,
        entries=tuple(verified_entries),
        rows=rows_by_split,
        paths=tuple(paths),
    )


def _load_committed_prediction(
    database: Path, predictions_json: Path
) -> tuple[dict[str, Any], bytes, tuple[Any, ...]]:
    prediction_bytes = read_bounded_bytes(
        predictions_json, max_bytes=10_000_000, description="prediction JSON"
    )
    prediction = json_object_from_bytes(prediction_bytes, max_bytes=10_000_000)
    run = prediction.get("run_key")
    if not isinstance(run, str):
        raise IntegrityError("prediction run key is missing")
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT observed_manifest_sha256,config_sha256,source_bundle_digest,"
            "identity_envelope_json,prediction_digest,prediction_bytes,state "
            "FROM resolution_runs WHERE run_key=?",
            (run,),
        ).fetchone()
    if row is None:
        raise IntegrityError("prediction run is absent from SQLite")
    if canonical_bytes(prediction) != prediction_bytes:
        raise IntegrityError("prediction JSON is not canonical")
    if (
        row[4] != digest_bytes(prediction_bytes)
        or row[5] != prediction_bytes
        or row[6] != "complete"
    ):
        raise IntegrityError("prediction differs from committed SQLite payload")
    envelope = prediction.get("identity_envelope")
    if not isinstance(envelope, dict) or run_key(envelope) != run:
        raise IntegrityError("prediction identity envelope does not reproduce run key")
    if canonical_bytes(envelope).decode("utf-8") != row[3]:
        raise IntegrityError("prediction identity envelope differs from SQLite")
    if prediction.get("observed_manifest_sha256") != row[0]:
        raise IntegrityError("prediction observed-manifest identity differs from SQLite")
    if prediction.get("config_digest") != row[1] or envelope.get("config_sha256") != row[1]:
        raise IntegrityError("prediction config identity differs from SQLite")
    source_bundle = envelope.get("source_bundle")
    if (
        not isinstance(source_bundle, list)
        or digest_bytes(canonical_bytes(source_bundle)) != row[2]
    ):
        raise IntegrityError("prediction source bundle differs from SQLite")
    return prediction, prediction_bytes, row


def _prediction_pairs(prediction: dict[str, Any], split: str) -> list[dict[str, Any]]:
    try:
        pairs = prediction["splits"][split]["pairs"]
    except (KeyError, TypeError) as exc:
        raise IntegrityError("prediction split structure is invalid") from exc
    if not isinstance(pairs, list) or any(not isinstance(pair, dict) for pair in pairs):
        raise IntegrityError("prediction pair list is invalid")
    keys = [(pair.get("left_id"), pair.get("right_id")) for pair in pairs]
    if len(keys) != len(set(keys)):
        raise IntegrityError("prediction contains duplicate pairs")
    return pairs


def _overall_counts(metrics: dict[str, dict[str, int | float | None]]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for name in ("tp", "fp", "tn", "fn"):
        values = [split_metrics[name] for split_metrics in metrics.values()]
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise IntegrityError("confusion-matrix count is not an integer")
        totals[name] = sum(value for value in values if isinstance(value, int))
    return totals


def evaluate(
    *,
    workspace_root: Path,
    observed_manifest: Path,
    truth_manifest: Path,
    predictions_json: Path,
    truth_root: Path,
    database: Path,
    report_json: Path,
    report_html: Path,
) -> CompleteEvaluation:
    lock_path = database.with_suffix(database.suffix + ".lock")
    validate_paths(
        workspace_root,
        inputs=(observed_manifest, truth_manifest, predictions_json, truth_root),
        outputs=(database, report_json, report_html),
        locks=(lock_path,),
    )
    truth = verify_truth_manifest(
        truth_manifest,
        truth_root,
        workspace_root=workspace_root,
        additional_inputs=(observed_manifest, predictions_json),
        outputs=(database, report_json, report_html),
        locks=(lock_path,),
    )
    if truth.manifest["observed_manifest_sha256"] != digest_file(observed_manifest):
        raise IntegrityError("truth manifest is not bound to supplied observed manifest")
    prediction, prediction_bytes, _run_row = _load_committed_prediction(database, predictions_json)
    if prediction.get("observed_manifest_sha256") != digest_file(observed_manifest):
        raise IntegrityError("prediction is not bound to supplied observed manifest")
    metrics: dict[str, dict[str, int | float | None]] = {}
    review_rows: list[dict[str, object]] = []
    all_decisions: list[str] = []
    validate_truth_split_isolation(truth.rows)
    for split in ("calibration", "validation"):
        pairs = _prediction_pairs(prediction, split)
        decision_by_pair: dict[tuple[str, str], str] = {}
        for pair in pairs:
            left_id, right_id, decision = (
                pair.get("left_id"),
                pair.get("right_id"),
                pair.get("decision"),
            )
            if (
                not isinstance(left_id, str)
                or not isinstance(right_id, str)
                or decision
                not in {
                    "MATCH",
                    "REVIEW",
                    "NO_MATCH",
                }
            ):
                raise IntegrityError("prediction pair identifiers or decision are invalid")
            decision_by_pair[left_id, right_id] = decision
            all_decisions.append(decision)
            if decision == "REVIEW":
                review_rows.append({"split": split, **pair})
        left_ids = {left for left, _ in decision_by_pair}
        right_ids = {right for _, right in decision_by_pair}
        if not left_ids or not right_ids or len(decision_by_pair) != len(left_ids) * len(right_ids):
            raise IntegrityError("prediction is not a complete Cartesian matrix")
        validate_truth_matrix(
            truth.rows[split], left_ids=left_ids, right_ids=right_ids, split=split
        )
        split_metrics = metrics_from_labels(
            predictions=[
                decision_by_pair[row["left_id"], row["right_id"]] for row in truth.rows[split]
            ],
            labels=[int(row["label"]) for row in truth.rows[split]],
        )
        metrics[split] = split_metrics.as_payload()
    prediction_digest = digest_bytes(prediction_bytes)
    config_digest = str(prediction["config_digest"])
    evaluation_envelope: dict[str, object] = {
        "schema": "erw-evaluation-key-v1",
        "run_key": str(prediction["run_key"]),
        "prediction_digest": prediction_digest,
        "truth_manifest_digest": truth.manifest_digest,
        "truth_bundle_digest": truth.bundle_digest,
        "truth_files": list(truth.entries),
        "evaluator_source_digest": digest_file(Path(__file__)),
        "config_digest": config_digest,
    }
    evaluation_key = run_key(evaluation_envelope)
    report_payload: dict[str, object] = {
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
            "config_sha256": config_digest,
            "evaluation_identity": evaluation_envelope,
            "observed_manifest_sha256": digest_file(observed_manifest),
            "prediction_sha256": prediction_digest,
            "truth_bundle_digest": truth.bundle_digest,
            "truth_manifest_sha256": truth.manifest_digest,
        },
    }
    rendered = render_report(report_payload)
    with exclusive_lock(database):
        stored = store_complete_evaluation(
            database,
            evaluation_key=evaluation_key,
            truth_bundle_digest=truth.bundle_digest,
            report_json_bytes=rendered.json_bytes,
            report_html_bytes=rendered.html_bytes,
            run_key=str(prediction["run_key"]),
            truth_manifest_digest=truth.manifest_digest,
            config_digest=config_digest,
            prediction_digest=prediction_digest,
            identity_envelope=evaluation_envelope,
            metrics_by_split=metrics,
            record_audit_event=True,
        )
    publish_evaluation_verified(
        database_path=database,
        evaluation_key=stored.evaluation_key,
        report_json_path=report_json,
        report_html_path=report_html,
    )
    return CompleteEvaluation(
        stored.evaluation_key,
        stored.report_json_digest,
        stored.report_html_digest,
        stored.report_bundle_digest,
        metrics,
    )
