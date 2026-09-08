from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from conftest import config_in, module


def generated(workspace: Path) -> Any:
    return module("generator").generate(
        workspace_root=workspace,
        output=workspace / "data",
        seed=20260902,
        config_json=config_in(workspace),
    )


def matched(workspace: Path, result: Any) -> Any:
    return module("matcher").match(
        workspace_root=workspace,
        observed_manifest=result.observed_manifest_json,
        observed_root=workspace / "data" / "observed",
        database=workspace / "state.sqlite",
        predictions_json=workspace / "predictions.json",
        config_json=workspace / "config" / "matching-v1.json",
    )


def evaluated(workspace: Path, result: Any) -> Any:
    return module("evaluator").evaluate(
        workspace_root=workspace,
        observed_manifest=result.observed_manifest_json,
        truth_manifest=result.truth_manifest_json,
        predictions_json=workspace / "predictions.json",
        truth_root=workspace / "data" / "ground_truth",
        database=workspace / "state.sqlite",
        report_json=workspace / "report.json",
        report_html=workspace / "report.html",
    )


def audit_kwargs(workspace: Path, result: Any) -> dict[str, Path]:
    return {
        "workspace_root": workspace,
        "database": workspace / "state.sqlite",
        "observed_manifest": result.observed_manifest_json,
        "truth_manifest": result.truth_manifest_json,
        "predictions_json": workspace / "predictions.json",
        "truth_root": workspace / "data" / "ground_truth",
        "report_json": workspace / "report.json",
        "report_html": workspace / "report.html",
    }


@pytest.mark.parametrize("mutation", ["missing", "unknown", "bad_weights", "bad_bound"])
def test_matching_config_schema_fails_closed(tmp_path: Path, mutation: str) -> None:
    config = config_in(tmp_path)
    payload = json.loads(config.read_text(encoding="utf-8"))
    if mutation == "missing":
        del payload["decision_version"]
    elif mutation == "unknown":
        payload["threshold_learned_from_validation"] = True
    elif mutation == "bad_weights":
        payload["weights"]["name"] = "0.46"
    else:
        payload["max_pair_count"] = 0
    config.write_bytes(module("common").canonical_bytes(payload))
    with pytest.raises(module("common").InvalidDataError):
        module("scoring").load_config(config)


def test_real_match_recovers_missing_prediction_and_rejects_divergence(tmp_path: Path) -> None:
    result = generated(tmp_path)
    first = matched(tmp_path, result)
    prediction = tmp_path / "predictions.json"
    expected = prediction.read_bytes()
    prediction.unlink()
    retry = matched(tmp_path, result)
    assert retry.reused is True and retry.run_key == first.run_key
    assert prediction.read_bytes() == expected
    prediction.write_bytes(b'{"tampered":true}\n')
    with pytest.raises(module("common").IntegrityError):
        matched(tmp_path, result)


def test_real_evaluation_recovers_one_missing_report_and_rejects_divergence(
    tmp_path: Path,
) -> None:
    result = generated(tmp_path)
    matched(tmp_path, result)
    first = evaluated(tmp_path, result)
    report_json = tmp_path / "report.json"
    report_html = tmp_path / "report.html"
    expected_json = report_json.read_bytes()
    expected_html = report_html.read_bytes()
    report_html.unlink()
    retry = evaluated(tmp_path, result)
    assert retry.evaluation_key == first.evaluation_key
    assert report_json.read_bytes() == expected_json
    assert report_html.read_bytes() == expected_html
    report_json.write_bytes(b'{"tampered":true}\n')
    with pytest.raises(module("common").IntegrityError):
        evaluated(tmp_path, result)


@pytest.mark.parametrize("alias_kind", ["database", "lock"])
def test_match_rejects_output_or_lock_hardlinked_to_an_input(
    tmp_path: Path, alias_kind: str
) -> None:
    result = generated(tmp_path)
    database = tmp_path / "state.sqlite"
    alias = database if alias_kind == "database" else database.with_suffix(".sqlite.lock")
    observed_input = tmp_path / "data" / "observed" / "calibration" / "supplier_a.csv"
    original = observed_input.read_bytes()
    os.link(observed_input, alias)
    with pytest.raises(module("paths").PathSafetyError):
        matched(tmp_path, result)
    assert observed_input.read_bytes() == original
    assert not (tmp_path / "predictions.json").exists()


def test_truth_manifest_must_bind_the_supplied_observed_manifest(tmp_path: Path) -> None:
    result = generated(tmp_path)
    matched(tmp_path, result)
    manifest = json.loads(result.truth_manifest_json.read_text(encoding="utf-8"))
    manifest["observed_manifest_sha256"] = "0" * 64
    result.truth_manifest_json.write_bytes(module("common").canonical_bytes(manifest))
    with pytest.raises(module("common").IntegrityError):
        evaluated(tmp_path, result)
    assert not (tmp_path / "report.json").exists()


def test_seeded_family_split_uses_the_frozen_digest_order(tmp_path: Path) -> None:
    generated(tmp_path)
    families: dict[str, set[str]] = {}
    for split in ("calibration", "validation"):
        with (tmp_path / "data" / "ground_truth" / f"{split}.csv").open(
            newline="", encoding="utf-8"
        ) as handle:
            families[split] = {row["family_id"] for row in csv.DictReader(handle)}
    assert families["calibration"].isdisjoint(families["validation"])
    all_families = families["calibration"] | families["validation"]
    ordered = sorted(
        all_families,
        key=lambda family: hashlib.sha256(f"20260902|split|{family}".encode()).hexdigest(),
    )
    cut = max(1, min(len(ordered) - 1, (60 * len(ordered)) // 100))
    assert families["calibration"] == set(ordered[:cut])
    assert families["validation"] == set(ordered[cut:])


@pytest.mark.parametrize(
    "positive_pairs", [[("l1", "r1"), ("l1", "r2")], [("l1", "r1"), ("l2", "r1")]]
)
def test_truth_positive_pairs_are_bipartite_one_to_one(
    positive_pairs: list[tuple[str, str]],
) -> None:
    rows = []
    positives = set(positive_pairs)
    for left in ("l1", "l2"):
        for right in ("r1", "r2"):
            rows.append(
                {
                    "left_id": left,
                    "right_id": right,
                    "label": "1" if (left, right) in positives else "0",
                    "split": "validation",
                    "family_id": f"family-{left}",
                    "scenario": "exact",
                }
            )
    with pytest.raises(module("evaluator").InvalidTruthError):
        module("evaluator").validate_truth_matrix(
            rows,
            left_ids={"l1", "l2"},
            right_ids={"r1", "r2"},
            split="validation",
        )


def test_audit_detects_persisted_score_provenance_tampering(tmp_path: Path) -> None:
    result = generated(tmp_path)
    matched(tmp_path, result)
    evaluated(tmp_path, result)
    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        conn.execute(
            "UPDATE pair_scores SET block_reasons_json = ? "
            "WHERE rowid = (SELECT rowid FROM pair_scores ORDER BY rowid LIMIT 1)",
            ('["tampered"]',),
        )
        conn.commit()
    with pytest.raises(module("common").IntegrityError):
        module("audit").audit(**audit_kwargs(tmp_path, result))


def test_provenance_sql_never_uses_insert_or_ignore() -> None:
    package_root = Path(module("database").__file__).resolve().parent
    source = "\n".join(path.read_text(encoding="utf-8") for path in package_root.glob("*.py"))
    assert "insert or ignore" not in source.casefold()
