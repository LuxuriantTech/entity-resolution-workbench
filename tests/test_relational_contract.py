from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from conftest import PROJECT_ROOT, config_in, module


def generate_and_match(workspace: Path) -> tuple[Any, Any]:
    generated = module("generator").generate(
        workspace_root=workspace,
        output=workspace / "data",
        seed=20260902,
        config_json=config_in(workspace),
    )
    matched = module("matcher").match(
        workspace_root=workspace,
        observed_manifest=generated.observed_manifest_json,
        observed_root=workspace / "data" / "observed",
        database=workspace / "state.sqlite",
        predictions_json=workspace / "predictions.json",
        config_json=workspace / "config" / "matching-v1.json",
    )
    return generated, matched


def test_run_source_bundle_contains_every_declared_file_and_real_digest(tmp_path: Path) -> None:
    _, matched = generate_and_match(tmp_path)
    prediction = json.loads((tmp_path / "predictions.json").read_text(encoding="utf-8"))
    envelope = prediction["identity_envelope"]
    expected_paths = sorted(
        [
            path.relative_to(PROJECT_ROOT).as_posix()
            for path in (PROJECT_ROOT / "src" / "entity_resolution_workbench").glob("**/*.py")
        ]
        + ["pyproject.toml", "uv.lock"]
    )
    bundle = envelope["source_bundle"]
    assert [entry["path"] for entry in bundle] == expected_paths
    assert all(
        entry["sha256"] == hashlib.sha256((PROJECT_ROOT / entry["path"]).read_bytes()).hexdigest()
        for entry in bundle
    )
    assert module("identity").run_key(envelope) == matched.run_key


@pytest.mark.parametrize("manifest_kind", ["observed_count", "truth_count", "truth_bundle"])
def test_manifest_counts_and_truth_bundle_are_recomputed_before_output(
    tmp_path: Path, manifest_kind: str
) -> None:
    generated, _ = generate_and_match(tmp_path)
    common = module("common")
    if manifest_kind == "observed_count":
        path = generated.observed_manifest_json
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["observed_files"][0]["rows"] += 1
        path.write_bytes(common.canonical_bytes(payload))
        (tmp_path / "state.sqlite").unlink()
        (tmp_path / "predictions.json").unlink()
        with pytest.raises(common.IntegrityError):
            module("matcher").match(
                workspace_root=tmp_path,
                observed_manifest=path,
                observed_root=tmp_path / "data" / "observed",
                database=tmp_path / "state.sqlite",
                predictions_json=tmp_path / "predictions.json",
                config_json=tmp_path / "config" / "matching-v1.json",
            )
        assert not (tmp_path / "state.sqlite").exists()
        return

    path = generated.truth_manifest_json
    payload = json.loads(path.read_text(encoding="utf-8"))
    if manifest_kind == "truth_count":
        payload["truth_files"][0]["rows"] += 1
    else:
        payload["truth_bundle_digest"] = "0" * 64
    path.write_bytes(common.canonical_bytes(payload))
    with pytest.raises(common.IntegrityError):
        module("evaluator").evaluate(
            workspace_root=tmp_path,
            observed_manifest=generated.observed_manifest_json,
            truth_manifest=path,
            predictions_json=tmp_path / "predictions.json",
            truth_root=tmp_path / "data" / "ground_truth",
            database=tmp_path / "state.sqlite",
            report_json=tmp_path / "report.json",
            report_html=tmp_path / "report.html",
        )
    assert not (tmp_path / "report.json").exists()


def test_sqlite_relations_and_canonical_entities_match_the_frozen_prediction(
    tmp_path: Path,
) -> None:
    _, matched = generate_and_match(tmp_path)
    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        relation_tables = (
            "source_records",
            "pair_scores",
            "decisions",
            "canonical_entities",
            "canonical_members",
        )
        assert all(
            conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            for table in relation_tables
        )
        source_columns = {row[1] for row in conn.execute("PRAGMA table_info(source_records)")}
        assert {
            "raw_json",
            "normalized_name",
            "normalized_brand",
            "normalized_sku",
            "normalized_category",
            "normalized_price",
            "fingerprint",
        } <= source_columns
        dangling = conn.execute(
            "SELECT count(*) FROM canonical_members m "
            "LEFT JOIN canonical_entities e ON e.run_key=m.run_key "
            "AND e.canonical_key=m.canonical_key WHERE e.canonical_key IS NULL"
        ).fetchone()[0]
        entity_count = conn.execute(
            "SELECT count(*) FROM canonical_entities WHERE run_key=?", (matched.run_key,)
        ).fetchone()[0]
        group_count = conn.execute(
            "SELECT count(DISTINCT canonical_key) FROM canonical_members WHERE run_key=?",
            (matched.run_key,),
        ).fetchone()[0]
        unused = conn.execute(
            "SELECT count(*) FROM canonical_entities e WHERE run_key=? AND NOT EXISTS ("
            "SELECT 1 FROM canonical_members m WHERE m.run_key=e.run_key "
            "AND m.canonical_key=e.canonical_key)",
            (matched.run_key,),
        ).fetchone()[0]
    assert dangling == 0 and unused == 0 and entity_count == group_count


def test_pair_and_decision_rows_preserve_exact_explanations_and_nulls(tmp_path: Path) -> None:
    _, matched = generate_and_match(tmp_path)
    payload = json.loads((tmp_path / "predictions.json").read_text(encoding="utf-8"))
    pairs = [pair for split in payload["splits"].values() for pair in split["pairs"]]
    assert any(pair["admitted"] and pair["block_reasons"] for pair in pairs)
    null_pair = next(
        pair for pair in pairs if any(value is None for value in pair["scores"].values())
    )
    split = next(name for name, data in payload["splits"].items() if null_pair in data["pairs"])
    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        score = conn.execute(
            "SELECT name_score,brand_score,sku_score,price_score,total_score,evidence_count,"
            "block_reasons_json,contradictions_json FROM pair_scores "
            "WHERE run_key=? AND split=? AND left_id=? AND right_id=?",
            (matched.run_key, split, null_pair["left_id"], null_pair["right_id"]),
        ).fetchone()
        decision_columns = {row[1] for row in conn.execute("PRAGMA table_info(decisions)")}
    assert score is not None and any(value is None for value in score[:4])
    assert score[4] == null_pair["scores"]["total"]["fraction"]
    assert json.loads(score[6]) == null_pair["block_reasons"]
    assert json.loads(score[7]) == null_pair["contradictions"]
    assert {
        "left_rank",
        "right_rank",
        "left_margin",
        "right_margin",
        "explanation_json",
    } <= decision_columns


def test_failed_match_rolls_back_ingestion_batches_added_by_that_attempt(tmp_path: Path) -> None:
    generated = module("generator").generate(
        workspace_root=tmp_path,
        output=tmp_path / "data",
        seed=20260902,
        config_json=config_in(tmp_path),
    )
    database = module("database")
    first = tmp_path / "data" / "observed" / "calibration" / "supplier_a.csv"
    database.ingest_csv(
        database_path=tmp_path / "state.sqlite",
        catalog_role="calibration:supplier_a",
        csv_path=first,
    )
    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        baseline = {
            table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("ingestion_batches", "source_records", "audit_events")
        }
        conn.execute(
            "CREATE TRIGGER fail_pair_again BEFORE INSERT ON pair_scores "
            "BEGIN SELECT RAISE(ABORT, 'injected pair failure'); END"
        )
    with pytest.raises(sqlite3.DatabaseError):
        module("matcher").match(
            workspace_root=tmp_path,
            observed_manifest=generated.observed_manifest_json,
            observed_root=tmp_path / "data" / "observed",
            database=tmp_path / "state.sqlite",
            predictions_json=tmp_path / "predictions.json",
            config_json=tmp_path / "config" / "matching-v1.json",
        )
    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        actual = {
            table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("ingestion_batches", "source_records", "audit_events")
        }
    assert actual == baseline


def test_truth_digest_reuse_with_different_evaluation_fails_as_invariant(tmp_path: Path) -> None:
    database = module("database")
    db = tmp_path / "state.sqlite"
    database.store_complete_evaluation(
        db,
        evaluation_key="a" * 64,
        truth_bundle_digest="c" * 64,
        report_json_bytes=b"{}\n",
        report_html_bytes=b"<!doctype html>one\n",
    )
    with pytest.raises(module("common").InvariantError):
        database.store_complete_evaluation(
            db,
            evaluation_key="b" * 64,
            truth_bundle_digest="c" * 64,
            report_json_bytes=b'{"different":true}\n',
            report_html_bytes=b"<!doctype html>two\n",
        )


def test_matcher_module_has_no_evaluator_or_truth_dependency() -> None:
    matcher_source = Path(module("matcher").__file__).read_text(encoding="utf-8")
    assert "from .evaluator" not in matcher_source
    assert "import evaluator" not in matcher_source
    assert "truth_manifest" not in matcher_source
