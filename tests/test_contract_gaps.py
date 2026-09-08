from __future__ import annotations

import csv
import inspect
import json
import os
import sqlite3
import stat
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from conftest import config_in, module


def generated(tmp_path: Path) -> Any:
    return module("generator").generate(
        workspace_root=tmp_path,
        output=tmp_path / "data",
        seed=20260902,
        config_json=config_in(tmp_path),
    )


def matched(tmp_path: Path, result: Any) -> Any:
    return module("matcher").match(
        workspace_root=tmp_path,
        observed_manifest=result.observed_manifest_json,
        observed_root=tmp_path / "data" / "observed",
        database=tmp_path / "state.sqlite",
        predictions_json=tmp_path / "predictions.json",
        config_json=tmp_path / "config" / "matching-v1.json",
    )


def evaluated(tmp_path: Path, result: Any) -> Any:
    return module("evaluator").evaluate(
        workspace_root=tmp_path,
        observed_manifest=result.observed_manifest_json,
        truth_manifest=result.truth_manifest_json,
        predictions_json=tmp_path / "predictions.json",
        truth_root=tmp_path / "data" / "ground_truth",
        database=tmp_path / "state.sqlite",
        report_json=tmp_path / "report.json",
        report_html=tmp_path / "report.html",
    )


def test_frozen_config_is_parsed_completely_and_drives_decisions(tmp_path: Path) -> None:
    scoring = module("scoring")
    matcher = module("matcher")
    assert hasattr(scoring, "load_config"), "matching config loader is required"
    config = scoring.load_config(config_in(tmp_path))
    assert config.match_threshold == Fraction(86, 100)
    assert config.review_threshold == Fraction(62, 100)
    assert config.minimum_bilateral_margin == Fraction(8, 100)
    assert config.weights == {
        "name": Fraction(45, 100),
        "brand": Fraction(20, 100),
        "sku": Fraction(25, 100),
        "price": Fraction(10, 100),
    }
    strict = replace(config, match_threshold=Fraction(99, 100))
    decision = matcher.decide_candidate(
        total=Fraction(90, 100),
        evidence_count=4,
        supported=True,
        contradiction=False,
        admitted=True,
        left_unique=True,
        right_unique=True,
        left_margin=Fraction(1),
        right_margin=Fraction(1),
        duplicate_ambiguous=False,
        blocked_out_rival=False,
        config=strict,
    )
    assert decision.value != "MATCH"


def test_match_ingests_all_four_catalogues_idempotently(tmp_path: Path) -> None:
    result = generated(tmp_path)
    matched(tmp_path, result)
    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        batch_count = conn.execute("SELECT count(*) FROM ingestion_batches").fetchone()[0]
        record_count = conn.execute("SELECT count(*) FROM source_records").fetchone()[0]
    expected_records = 0
    for source in (tmp_path / "data" / "observed").glob("*/*.csv"):
        with source.open(newline="", encoding="utf-8") as handle:
            expected_records += sum(1 for _ in csv.DictReader(handle))
    assert batch_count == 4
    assert record_count == expected_records
    matched(tmp_path, result)
    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        assert conn.execute("SELECT count(*) FROM ingestion_batches").fetchone()[0] == 4
        assert conn.execute("SELECT count(*) FROM source_records").fetchone()[0] == expected_records


def test_predictions_and_sqlite_persist_full_explanations(tmp_path: Path) -> None:
    result = generated(tmp_path)
    matched(tmp_path, result)
    payload = json.loads((tmp_path / "predictions.json").read_text(encoding="utf-8"))
    assert "identity_envelope" in payload
    pair = payload["splits"]["calibration"]["pairs"][0]
    assert {
        "left_id",
        "right_id",
        "decision",
        "admitted",
        "block_reasons",
        "scores",
        "evidence_count",
        "contradictions",
        "explanation",
    } <= set(pair)
    assert {"name", "brand", "sku", "price", "total"} <= set(pair["scores"])
    assert all(
        value is None or {"fraction", "display"} <= set(value) for value in pair["scores"].values()
    )
    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        score_columns = {row[1] for row in conn.execute("PRAGMA table_info(pair_scores)")}
    assert {
        "name_score",
        "brand_score",
        "sku_score",
        "price_score",
        "total_score",
        "evidence_count",
        "block_reasons_json",
        "contradictions_json",
    } <= score_columns


def test_sqlite_schema_has_canonical_entities_lineage_and_foreign_keys(tmp_path: Path) -> None:
    result = generated(tmp_path)
    matched(tmp_path, result)
    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        }
        foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        events = {row[0] for row in conn.execute("SELECT event_type FROM audit_events ORDER BY id")}
    assert "canonical_entities" in tables
    assert not foreign_key_errors and integrity == "ok"
    assert {"ingestion_complete", "run_complete"} <= events


def test_sqlite_pair_rows_keep_their_physical_split(tmp_path: Path) -> None:
    result = generated(tmp_path)
    matched(tmp_path, result)
    expected: dict[str, int] = {}
    for split in ("calibration", "validation"):
        counts = []
        for role in ("supplier_a", "supplier_b"):
            with (tmp_path / "data" / "observed" / split / f"{role}.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                counts.append(sum(1 for _ in csv.DictReader(handle)))
        expected[split] = counts[0] * counts[1]
    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        actual = dict(
            conn.execute(
                "SELECT split, count(*) FROM pair_scores GROUP BY split ORDER BY split"
            ).fetchall()
        )
    assert actual == expected


def test_manifests_bind_versions_paths_counts_and_truth_bundle(tmp_path: Path) -> None:
    result = generated(tmp_path)
    observed = json.loads(result.observed_manifest_json.read_text(encoding="utf-8"))
    truth = json.loads(result.truth_manifest_json.read_text(encoding="utf-8"))
    assert {
        "generator_version",
        "split_version",
        "matching_config_sha256",
        "observed_files",
    } <= set(observed)
    assert len(observed["observed_files"]) == 4
    assert all(
        {"split", "role", "path", "sha256", "rows"} <= set(item)
        for item in observed["observed_files"]
    )
    assert {"truth_files", "truth_bundle_digest", "observed_manifest_sha256"} <= set(truth)
    assert len(truth["truth_files"]) == 2
    assert all({"split", "path", "sha256", "rows"} <= set(item) for item in truth["truth_files"])


def test_run_identity_envelope_binds_declared_code_and_environment(tmp_path: Path) -> None:
    result = generated(tmp_path)
    match_result = matched(tmp_path, result)
    payload = json.loads((tmp_path / "predictions.json").read_text(encoding="utf-8"))
    envelope = payload.get("identity_envelope")
    assert envelope is not None
    assert {
        "schema",
        "observed_manifest_sha256",
        "observed_files",
        "config_sha256",
        "component_versions",
        "python_version",
        "unicode_version",
        "sqlite_version",
        "uv_lock_sha256",
        "source_bundle",
    } <= set(envelope)
    assert module("identity").run_key(envelope) == match_result.run_key


def test_evaluation_rejects_valid_json_prediction_tampering(tmp_path: Path) -> None:
    result = generated(tmp_path)
    matched(tmp_path, result)
    path = tmp_path / "predictions.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["splits"]["calibration"]["pairs"][0]["decision"] = "MATCH"
    path.write_bytes(module("common").canonical_bytes(payload))
    with pytest.raises(module("common").IntegrityError):
        evaluated(tmp_path, result)
    assert not (tmp_path / "report.json").exists()


def test_audit_recalculates_and_rejects_report_or_database_tamper(tmp_path: Path) -> None:
    result = generated(tmp_path)
    matched(tmp_path, result)
    evaluated(tmp_path, result)
    audit = module("audit")
    assert "workspace_root" in inspect.signature(audit.audit).parameters
    kwargs = {
        "workspace_root": tmp_path,
        "database": tmp_path / "state.sqlite",
        "observed_manifest": result.observed_manifest_json,
        "truth_manifest": result.truth_manifest_json,
        "predictions_json": tmp_path / "predictions.json",
        "truth_root": tmp_path / "data" / "ground_truth",
        "report_json": tmp_path / "report.json",
        "report_html": tmp_path / "report.html",
    }
    checks = audit.audit(**kwargs)
    assert checks["ok"] is True
    assert all(value is True for key, value in checks.items() if key != "ok")
    (tmp_path / "report.json").write_bytes(b'{"tampered": true}\n')
    with pytest.raises(module("common").IntegrityError):
        audit.audit(**kwargs)


def test_audit_rejects_missing_canonical_membership(tmp_path: Path) -> None:
    result = generated(tmp_path)
    match_result = matched(tmp_path, result)
    evaluated(tmp_path, result)
    assert "workspace_root" in inspect.signature(module("audit").audit).parameters
    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        conn.execute(
            "DELETE FROM canonical_members WHERE rowid = ("
            "SELECT rowid FROM canonical_members WHERE run_key = ? LIMIT 1)",
            (match_result.run_key,),
        )
        conn.commit()
    with pytest.raises(module("common").IntegrityError):
        module("audit").audit(
            workspace_root=tmp_path,
            database=tmp_path / "state.sqlite",
            observed_manifest=result.observed_manifest_json,
            truth_manifest=result.truth_manifest_json,
            predictions_json=tmp_path / "predictions.json",
            truth_root=tmp_path / "data" / "ground_truth",
            report_json=tmp_path / "report.json",
            report_html=tmp_path / "report.html",
        )


@pytest.mark.parametrize("subcommand", ["generate", "match", "evaluate", "audit", "demo"])
def test_cli_exposes_every_frozen_subcommand(subcommand: str) -> None:
    cli = module("cli")
    with pytest.raises(SystemExit) as stopped:
        cli.main([subcommand, "--help"])
    assert stopped.value.code == 0


def test_generator_has_true_unmatched_and_token_reordering_cases(tmp_path: Path) -> None:
    generated(tmp_path)
    scenarios: dict[str, list[str]] = {}
    for split in ("calibration", "validation"):
        with (tmp_path / "data" / "ground_truth" / f"{split}.csv").open(
            newline="", encoding="utf-8"
        ) as handle:
            for row in csv.DictReader(handle):
                scenarios.setdefault(row["scenario"], []).append(row["label"])
    assert "token_reorder" in scenarios
    assert "unmatched" in scenarios and set(scenarios["unmatched"]) == {"0"}
    assert {"contradiction_sku", "contradiction_brand", "contradiction_price"} <= set(scenarios)


def test_match_is_atomic_when_pair_persistence_fails(tmp_path: Path) -> None:
    result = generated(tmp_path)
    database = module("database")
    first_csv = tmp_path / "data" / "observed" / "calibration" / "supplier_a.csv"
    database.ingest_csv(
        database_path=tmp_path / "state.sqlite",
        catalog_role="calibration:supplier_a",
        csv_path=first_csv,
    )
    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        conn.execute(
            "CREATE TRIGGER fail_pair BEFORE INSERT ON pair_scores "
            "BEGIN SELECT RAISE(ABORT, 'injected pair failure'); END"
        )
    with pytest.raises(sqlite3.DatabaseError):
        matched(tmp_path, result)
    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        assert conn.execute("SELECT count(*) FROM resolution_runs").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0
        conn.execute("DROP TRIGGER fail_pair")
    retried = matched(tmp_path, result)
    assert retried.reused is False
    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        assert conn.execute("SELECT count(*) FROM resolution_runs").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM pair_scores").fetchone()[0] > 0
        assert {row[0] for row in conn.execute("SELECT DISTINCT split FROM pair_scores")} == {
            "calibration",
            "validation",
        }


def test_match_honors_the_real_database_advisory_lock(tmp_path: Path) -> None:
    result = generated(tmp_path)
    database = module("database")
    with (
        database.exclusive_lock(tmp_path / "state.sqlite"),
        pytest.raises(database.ConcurrentWriterError),
    ):
        matched(tmp_path, result)


def test_commands_reject_outputs_outside_workspace_before_write(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = config_in(workspace)
    outside = tmp_path / "outside"
    with pytest.raises(module("paths").PathSafetyError):
        module("generator").generate(
            workspace_root=workspace,
            output=outside,
            seed=20260902,
            config_json=config,
        )
    assert not outside.exists()


def test_match_and_evaluate_reject_real_output_aliases_before_write(tmp_path: Path) -> None:
    result = generated(tmp_path)
    collision = tmp_path / "collision.bin"
    with pytest.raises(module("paths").PathSafetyError):
        module("matcher").match(
            workspace_root=tmp_path,
            observed_manifest=result.observed_manifest_json,
            observed_root=tmp_path / "data" / "observed",
            database=collision,
            predictions_json=collision,
            config_json=tmp_path / "config" / "matching-v1.json",
        )
    assert not collision.exists()
    matched(tmp_path, result)
    report_collision = tmp_path / "same-report"
    with pytest.raises(module("paths").PathSafetyError):
        module("evaluator").evaluate(
            workspace_root=tmp_path,
            observed_manifest=result.observed_manifest_json,
            truth_manifest=result.truth_manifest_json,
            predictions_json=tmp_path / "predictions.json",
            truth_root=tmp_path / "data" / "ground_truth",
            database=tmp_path / "state.sqlite",
            report_json=report_collision,
            report_html=report_collision,
        )
    assert not report_collision.exists()


def test_prediction_publication_fsyncs_file_and_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = module("database")
    publication = module("publication")
    db = tmp_path / "state.sqlite"
    run = database.store_complete_run(db, run_key="f" * 64, prediction_bytes=b"{}\n")
    modes: list[int] = []
    real_fsync = os.fsync

    def record_fsync(fd: int) -> None:
        modes.append(os.fstat(fd).st_mode)
        real_fsync(fd)

    monkeypatch.setattr(publication.os, "fsync", record_fsync)
    publication.publish_verified(
        database_path=db,
        run_key=run.run_key,
        prediction_path=tmp_path / "predictions.json",
    )
    assert any(stat.S_ISREG(mode) for mode in modes)
    assert any(stat.S_ISDIR(mode) for mode in modes)


def test_report_contains_review_rows_decision_counts_and_provenance(tmp_path: Path) -> None:
    result = generated(tmp_path)
    matched(tmp_path, result)
    evaluated(tmp_path, result)
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert {
        "evaluation_key",
        "run_key",
        "metrics_by_split",
        "decision_counts",
        "review_rows",
        "provenance",
    } <= set(report)
    assert report["review_rows"]
    html = (tmp_path / "report.html").read_text(encoding="utf-8").lower()
    assert "review" in html and "score" in html and "decision" in html


def test_self_consistent_but_incomplete_truth_is_rejected_against_predictions(
    tmp_path: Path,
) -> None:
    result = generated(tmp_path)
    matched(tmp_path, result)
    common = module("common")
    truth_path = tmp_path / "data" / "ground_truth" / "calibration.csv"
    with truth_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
        fields = list(rows[0])
    removed_left = rows[0]["left_id"]
    rows = [row for row in rows if row["left_id"] != removed_left]
    with truth_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    manifest_path = result.truth_manifest_json
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = manifest["truth_files"]
    assert all("split" in item for item in items), "truth manifest must name split entries"
    calibration = next(item for item in items if item["split"] == "calibration")
    calibration["sha256"] = common.digest_file(truth_path)
    calibration["rows"] = len(rows)
    bundle = [
        {"split": item["split"], "truth_sha256": item["sha256"]}
        for item in sorted(items, key=lambda item: item["split"])
    ]
    manifest["truth_bundle_digest"] = common.digest_bytes(common.canonical_bytes(bundle))
    manifest_path.write_bytes(common.canonical_bytes(manifest))
    with pytest.raises(module("evaluator").InvalidTruthError):
        evaluated(tmp_path, result)
    assert not (tmp_path / "report.json").exists()
