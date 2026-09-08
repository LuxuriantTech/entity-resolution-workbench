from __future__ import annotations

import csv
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from conftest import PROJECT_ROOT, canonical_json, config_in, module


def generated(tmp_path: Path) -> Any:
    return module("generator").generate(
        workspace_root=tmp_path,
        output=tmp_path / "data",
        seed=20260902,
        config_json=config_in(tmp_path),
    )


def run_match(
    tmp_path: Path,
    result: Any,
    predictions: Path | None = None,
    database: Path | None = None,
) -> Any:
    return module("matcher").match(
        workspace_root=tmp_path,
        observed_manifest=result.observed_manifest_json,
        observed_root=tmp_path / "data" / "observed",
        database=database or tmp_path / "state.sqlite",
        predictions_json=predictions or tmp_path / "predictions.json",
        config_json=tmp_path / "config" / "matching-v1.json",
    )


def test_ac9_ec20_canonical_membership_is_unique_and_only_matches_merge(tmp_path: Path) -> None:
    result = generated(tmp_path)
    match = run_match(tmp_path, result)
    database = module("database")
    members = database.canonical_members(tmp_path / "state.sqlite", match.run_key)
    assert len({member.source_record_id for member in members}) == len(members)
    assert all(member.split in {"calibration", "validation"} for member in members)
    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        group_sizes = conn.execute(
            "SELECT canonical_key, count(*) FROM canonical_members "
            "WHERE run_key = ? GROUP BY canonical_key ORDER BY canonical_key",
            (match.run_key,),
        ).fetchall()
        match_count = conn.execute(
            "SELECT count(*) FROM decisions WHERE run_key = ? AND decision = 'MATCH'",
            (match.run_key,),
        ).fetchone()[0]
    assert all(size in {1, 2} for _, size in group_sizes)
    assert sum(size == 2 for _, size in group_sizes) == match_count


def test_ac10_ac17_ac19_ec21_evaluation_cannot_change_frozen_predictions_or_read_truth_in_match(
    tmp_path: Path,
) -> None:
    result = generated(tmp_path)
    predictions = tmp_path / "predictions.json"
    match_result = run_match(tmp_path, result, predictions)
    before = predictions.read_bytes()
    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        frozen_before = {
            table: conn.execute(
                f"SELECT * FROM {table} WHERE run_key = ? ORDER BY rowid",
                (match_result.run_key,),
            ).fetchall()
            for table in ("pair_scores", "decisions", "canonical_members")
        }
    module("evaluator").evaluate(
        workspace_root=tmp_path,
        observed_manifest=result.observed_manifest_json,
        truth_manifest=result.truth_manifest_json,
        predictions_json=predictions,
        truth_root=tmp_path / "data" / "ground_truth",
        database=tmp_path / "state.sqlite",
        report_json=tmp_path / "report.json",
        report_html=tmp_path / "report.html",
    )
    assert predictions.read_bytes() == before
    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        frozen_after = {
            table: conn.execute(
                f"SELECT * FROM {table} WHERE run_key = ? ORDER BY rowid",
                (match_result.run_key,),
            ).fetchall()
            for table in ("pair_scores", "decisions", "canonical_members")
        }
    assert frozen_after == frozen_before


def test_ac19_ec21_truth_changes_cannot_change_match_run_key_or_predictions(tmp_path: Path) -> None:
    result = generated(tmp_path)
    first_prediction = tmp_path / "first-predictions.json"
    first = run_match(
        tmp_path, result, predictions=first_prediction, database=tmp_path / "first.sqlite"
    )
    truth = tmp_path / "data" / "ground_truth" / "calibration.csv"
    with truth.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    rows[1][2] = "1" if rows[1][2] == "0" else "0"
    with truth.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle, lineterminator="\n").writerows(rows)
    second_prediction = tmp_path / "second-predictions.json"
    second = run_match(
        tmp_path, result, predictions=second_prediction, database=tmp_path / "second.sqlite"
    )
    assert second.run_key == first.run_key
    assert second_prediction.read_bytes() == first_prediction.read_bytes()


def test_generated_homonyms_and_contradictions_are_never_auto_matched(tmp_path: Path) -> None:
    result = generated(tmp_path)
    run_match(tmp_path, result)
    predictions = json.loads((tmp_path / "predictions.json").read_text(encoding="utf-8"))
    decisions = {
        (split, row["left_id"], row["right_id"]): row["decision"]
        for split, payload in predictions["splits"].items()
        for row in payload["pairs"]
    }
    checked: set[str] = set()
    for split in ("calibration", "validation"):
        with (tmp_path / "data" / "ground_truth" / f"{split}.csv").open(
            newline="", encoding="utf-8"
        ) as handle:
            for truth in csv.DictReader(handle):
                if truth["label"] == "1" and truth["scenario"] in {
                    "homonym",
                    "contradiction",
                }:
                    checked.add(truth["scenario"])
                    assert decisions[(split, truth["left_id"], truth["right_id"])] != "MATCH"
    assert checked == {"homonym", "contradiction"}


def test_ac11_ec11_metrics_are_recomputed_and_zero_denominators_are_null() -> None:
    evaluator = module("evaluator")
    metrics = evaluator.metrics_from_labels(
        predictions=["MATCH", "REVIEW", "NO_MATCH"], labels=[0, 0, 0]
    )
    assert (metrics.tp, metrics.fp, metrics.tn, metrics.fn) == (0, 1, 2, 0)
    assert metrics.recall is None and metrics.precision == 0 and metrics.f1 == 0
    balanced = evaluator.metrics_from_labels(
        predictions=["MATCH", "MATCH", "REVIEW", "NO_MATCH"], labels=[1, 0, 1, 0]
    )
    assert (balanced.tp, balanced.fp, balanced.tn, balanced.fn) == (1, 1, 1, 1)
    assert balanced.precision == balanced.recall == balanced.f1 == 0.5
    empty = evaluator.metrics_from_labels(predictions=["NO_MATCH"], labels=[0])
    assert empty.precision is None and empty.recall is None and empty.f1 is None


def test_ac11_pipeline_report_counts_equal_truth_and_frozen_predictions(tmp_path: Path) -> None:
    result = generated(tmp_path)
    run_match(tmp_path, result)
    module("evaluator").evaluate(
        workspace_root=tmp_path,
        observed_manifest=result.observed_manifest_json,
        truth_manifest=result.truth_manifest_json,
        predictions_json=tmp_path / "predictions.json",
        truth_root=tmp_path / "data" / "ground_truth",
        database=tmp_path / "state.sqlite",
        report_json=tmp_path / "report.json",
        report_html=tmp_path / "report.html",
    )
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    predictions = json.loads((tmp_path / "predictions.json").read_text(encoding="utf-8"))
    for split in ("calibration", "validation"):
        predicted = {
            (row["left_id"], row["right_id"]): row["decision"]
            for row in predictions["splits"][split]["pairs"]
        }
        with (tmp_path / "data" / "ground_truth" / f"{split}.csv").open(
            newline="", encoding="utf-8"
        ) as handle:
            truth = list(csv.DictReader(handle))
        counts = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
        for row in truth:
            gold = row["label"] == "1"
            positive = predicted[(row["left_id"], row["right_id"])] == "MATCH"
            counts[("t" if gold == positive else "f") + ("p" if positive else "n")] += 1
        metrics = report["metrics_by_split"][split]
        assert {key: metrics[key] for key in counts} == counts
        assert sum(counts.values()) == len(truth)


def test_ac12_ec15_reports_are_deterministic_self_contained_and_escaped(tmp_path: Path) -> None:
    reporting = module("reporting")
    payload = {"review_rows": [{"name": "<script>alert('x') & y</script>"}]}
    first = reporting.render_report(payload)
    second = reporting.render_report(payload)
    assert first.json_bytes == second.json_bytes and first.html_bytes == second.html_bytes
    assert b"<script>" not in first.html_bytes and b"&lt;script&gt;" in first.html_bytes
    assert b"http://" not in first.html_bytes and b"https://" not in first.html_bytes


def test_ac13_ac18_ac24_ec12_ec13_ec14_ec16_ec19_ec25_transaction_retry_and_tamper_are_closed(
    tmp_path: Path,
) -> None:
    database = module("database")
    publisher = module("publication")
    db, prediction = tmp_path / "state.sqlite", tmp_path / "prediction.json"
    with pytest.raises(database.ConcurrentWriterError):
        database.with_exclusive_lock(db, lambda: database.with_exclusive_lock(db, lambda: None))
    run = database.store_complete_run(
        db, run_key="a" * 64, prediction_bytes=canonical_json({"pairs": []})
    )
    publisher.publish_verified(database_path=db, run_key=run.run_key, prediction_path=prediction)
    prediction.unlink()
    publisher.publish_verified(database_path=db, run_key=run.run_key, prediction_path=prediction)
    prediction.write_bytes(b"tampered")
    with pytest.raises(publisher.IntegrityError):
        publisher.publish_verified(
            database_path=db, run_key=run.run_key, prediction_path=prediction
        )

    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TRIGGER fail_audit BEFORE INSERT ON audit_events "
            "BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END"
        )
    with pytest.raises(sqlite3.DatabaseError):
        database.store_complete_run(
            db, run_key="d" * 64, prediction_bytes=canonical_json({"pairs": ["partial"]})
        )
    with sqlite3.connect(db) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM resolution_runs WHERE run_key = ?", ("d" * 64,)
            ).fetchone()[0]
            == 0
        )

    evaluation = database.store_complete_evaluation(
        db,
        evaluation_key="b" * 64,
        truth_bundle_digest="c" * 64,
        report_json_bytes=canonical_json({"metrics": {}}),
        report_html_bytes=b"<!doctype html><title>report</title>\n",
    )
    report_json, report_html = tmp_path / "report.json", tmp_path / "report.html"
    publisher.publish_evaluation_verified(
        database_path=db,
        evaluation_key=evaluation.evaluation_key,
        report_json_path=report_json,
        report_html_path=report_html,
    )
    report_html.unlink()
    publisher.publish_evaluation_verified(
        database_path=db,
        evaluation_key=evaluation.evaluation_key,
        report_json_path=report_json,
        report_html_path=report_html,
    )
    assert report_html.read_bytes() == b"<!doctype html><title>report</title>\n"


def test_ac14_cli_demo_is_local_and_idempotent(tmp_path: Path) -> None:
    cli = module("cli")
    assert cli.main(["demo", "--workspace", str(tmp_path), "--seed", "20260902"]) == 0
    snapshot = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert cli.main(["demo", "--workspace", str(tmp_path), "--seed", "20260902"]) == 0
    assert snapshot == {
        p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }


def test_ac15_local_quality_commands_are_locked_and_documented() -> None:
    if not (PROJECT_ROOT / "README.md").is_file():
        pytest.fail("README quality commands are not implemented", pytrace=False)
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    for command in (
        "pytest",
        "ruff format --check",
        "ruff check",
        "mypy",
        "gitleaks protect --staged --redact --verbose",
    ):
        assert command in readme
    assert (PROJECT_ROOT / "uv.lock").is_file()
    # Public clones may have an origin; this test checks documented local tooling.


def test_ac23_ec24_run_identity_binds_all_declared_environment_inputs() -> None:
    identities = module("identity")
    base = {
        "python_version": "3.12.1",
        "unicode_version": "15.0",
        "sqlite_version": "3.47",
        "uv_lock_sha256": "a",
        "source_bundle": [["src/x.py", "b"]],
    }
    assert identities.run_key(base) != identities.run_key({**base, "python_version": "3.12.2"})
    assert identities.run_key(base) != identities.run_key(
        {**base, "source_bundle": [["src/x.py", "c"]]}
    )


@pytest.mark.parametrize("kind", ["observed", "truth"])
def test_ac25_ac26_ec26_ec27_manifest_rebinding_rejects_tampered_input_before_write(
    tmp_path: Path, kind: str
) -> None:
    result = generated(tmp_path)
    target = (
        tmp_path
        / "data"
        / (
            "observed/calibration/supplier_a.csv"
            if kind == "observed"
            else "ground_truth/calibration.csv"
        )
    )
    with target.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    assert len(rows) > 1
    column = 1 if kind == "observed" else 2
    rows[1][column] = rows[1][column] + "x"
    with target.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle, lineterminator="\n").writerows(rows)
    if kind == "observed":
        with pytest.raises(module("matcher").IntegrityError):
            module("matcher").match(
                workspace_root=tmp_path,
                observed_manifest=result.observed_manifest_json,
                observed_root=tmp_path / "data" / "observed",
                database=tmp_path / "state.sqlite",
                predictions_json=tmp_path / "predictions.json",
                config_json=tmp_path / "config" / "matching-v1.json",
            )
    else:
        with pytest.raises(module("evaluator").IntegrityError):
            module("evaluator").verify_truth_manifest(
                result.truth_manifest_json, tmp_path / "data" / "ground_truth"
            )


def test_ac27_ec28_path_aliases_symlinks_and_escapes_fail_before_output(tmp_path: Path) -> None:
    paths = module("paths")
    escaped = tmp_path.parent / "outside.sqlite"
    with pytest.raises(paths.PathSafetyError):
        paths.validate_artifact_paths(
            tmp_path,
            database=escaped,
            predictions=tmp_path / "out.json",
            report_json=tmp_path / "report.json",
            report_html=tmp_path / "report.html",
        )
    real = tmp_path / "real.json"
    real.write_text("{}\n", encoding="utf-8")
    hardlink = tmp_path / "hardlink.json"
    os.link(real, hardlink)
    with pytest.raises(paths.PathSafetyError):
        paths.validate_artifact_paths(
            tmp_path,
            database=tmp_path / "state.sqlite",
            predictions=real,
            report_json=hardlink,
            report_html=tmp_path / "report.html",
        )
    symlink_dir = tmp_path / "symlinked"
    symlink_dir.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(paths.PathSafetyError):
        paths.validate_artifact_paths(
            tmp_path,
            database=tmp_path / "state.sqlite",
            predictions=symlink_dir / "prediction.json",
            report_json=tmp_path / "report.json",
            report_html=tmp_path / "report.html",
        )
    alias = tmp_path / "same.json"
    with pytest.raises(paths.PathSafetyError):
        paths.validate_artifact_paths(
            tmp_path,
            database=tmp_path / "state.sqlite",
            predictions=alias,
            report_json=alias,
            report_html=tmp_path / "report.html",
        )


@pytest.mark.parametrize(
    "truth_rows",
    [
        [
            # The only Cartesian row is missing.
        ],
        [
            {
                "left_id": "l",
                "right_id": "r",
                "label": "2",
                "split": "validation",
                "family_id": "f",
                "scenario": "x",
            },
            {
                "left_id": "l",
                "right_id": "r",
                "label": "0",
                "split": "validation",
                "family_id": "f",
                "scenario": "x",
            },
        ],
    ],
)
def test_ec10_incomplete_or_invalid_truth_matrix_fails_before_report(
    truth_rows: list[dict[str, str]], tmp_path: Path
) -> None:
    evaluator = module("evaluator")
    with pytest.raises(evaluator.InvalidTruthError):
        evaluator.validate_truth_matrix(
            truth_rows, left_ids={"l"}, right_ids={"r"}, split="validation"
        )


def test_ec16_inconsistent_idempotent_rows_raise_not_ignore(tmp_path: Path) -> None:
    database = module("database")
    db = tmp_path / "state.sqlite"
    database.store_complete_run(db, run_key="b" * 64, prediction_bytes=b"{}\n")
    with pytest.raises(database.InvariantError):
        database.store_complete_run(db, run_key="b" * 64, prediction_bytes=b'{"changed":true}\n')
