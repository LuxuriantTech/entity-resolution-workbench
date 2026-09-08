from __future__ import annotations

import csv
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


def evaluated(workspace: Path, result: Any) -> None:
    module("evaluator").evaluate(
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


def test_generated_homonyms_are_real_ambiguities_and_never_auto_match(tmp_path: Path) -> None:
    result = generated(tmp_path)
    normalized = module("normalization").normalize_text
    found = False
    for split in ("calibration", "validation"):
        names: dict[str, list[str]] = {}
        with (tmp_path / "data" / "observed" / split / "supplier_a.csv").open(
            newline="", encoding="utf-8"
        ) as handle:
            for row in csv.DictReader(handle):
                names.setdefault(normalized(row["name"]), []).append(row["source_id"])
        ambiguous = {name for name, identifiers in names.items() if len(identifiers) >= 2}
        if ambiguous:
            found = True
            matched(tmp_path, result)
            prediction = json.loads((tmp_path / "predictions.json").read_text())
            ambiguous_left = {identifier for name in ambiguous for identifier in names[name]}
            assert all(
                pair["decision"] != "MATCH"
                for pair in prediction["splits"][split]["pairs"]
                if pair["left_id"] in ambiguous_left
            )
    assert found, "generator must place at least one same-normalized-name homonym in one split"


def test_audit_requires_each_completed_ingestion_event(tmp_path: Path) -> None:
    result = generated(tmp_path)
    matched(tmp_path, result)
    evaluated(tmp_path, result)
    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        conn.execute("DELETE FROM audit_events WHERE event_type = 'ingestion_complete'")
        conn.commit()
    with pytest.raises(module("common").IntegrityError):
        module("audit").audit(**audit_kwargs(tmp_path, result))


def test_repeated_ingestion_rejects_missing_completion_event(tmp_path: Path) -> None:
    result = generated(tmp_path)
    database = module("database")
    path = result.calibration.left_csv
    database.ingest_csv(
        database_path=tmp_path / "state.sqlite", catalog_role="catalog", csv_path=path
    )
    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        conn.execute("DELETE FROM audit_events WHERE event_type='ingestion_complete'")
        conn.commit()
    with pytest.raises(module("common").InvariantError):
        database.ingest_csv(
            database_path=tmp_path / "state.sqlite", catalog_role="catalog", csv_path=path
        )


def test_repeated_evaluation_rejects_missing_completion_event(tmp_path: Path) -> None:
    result = generated(tmp_path)
    matched(tmp_path, result)
    evaluated(tmp_path, result)
    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        conn.execute("DELETE FROM audit_events WHERE event_type='evaluation_complete'")
        conn.commit()
    with pytest.raises(module("common").InvariantError):
        evaluated(tmp_path, result)


@pytest.mark.parametrize("consumer", ["evaluator", "audit"])
def test_truth_root_symlink_component_is_rejected_before_read(
    tmp_path: Path, consumer: str
) -> None:
    result = generated(tmp_path)
    matched(tmp_path, result)
    evaluated(tmp_path, result)
    real_truth = tmp_path / "data" / "ground_truth"
    linked_truth = tmp_path / "truth-link"
    os.symlink(real_truth, linked_truth, target_is_directory=True)
    kwargs = audit_kwargs(tmp_path, result)
    kwargs["truth_root"] = linked_truth
    with pytest.raises(module("paths").PathSafetyError):
        if consumer == "evaluator":
            module("evaluator").evaluate(**kwargs)
        else:
            module("audit").audit(**kwargs)


@pytest.mark.parametrize("consumer", ["evaluator", "audit"])
def test_truth_file_symlink_is_rejected_before_any_truth_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, consumer: str
) -> None:
    result = generated(tmp_path)
    matched(tmp_path, result)
    evaluated(tmp_path, result)
    calibration = tmp_path / "data" / "ground_truth" / "calibration.csv"
    outside = tmp_path / "outside-calibration.csv"
    outside.write_bytes(calibration.read_bytes())
    calibration.unlink()
    os.symlink(outside, calibration)
    evaluator = module("evaluator")

    def should_not_read(_: Path) -> object:
        pytest.fail("a truth CSV was read before all manifest paths were validated")

    monkeypatch.setattr(evaluator, "_read_truth_csv", should_not_read)
    with pytest.raises(module("paths").PathSafetyError):
        if consumer == "evaluator":
            evaluator.evaluate(**audit_kwargs(tmp_path, result))
        else:
            module("audit").audit(**audit_kwargs(tmp_path, result))


@pytest.mark.parametrize("target", ["config", "observed", "truth"])
def test_boolean_schema_version_is_rejected(target: str, tmp_path: Path) -> None:
    result = generated(tmp_path)
    if target == "config":
        path = tmp_path / "config" / "matching-v1.json"
        loader = module("scoring").load_config
    elif target == "observed":
        path = result.observed_manifest_json

        def loader(value: Path) -> object:
            return module("matcher").match(
                workspace_root=tmp_path,
                observed_manifest=value,
                observed_root=tmp_path / "data" / "observed",
                database=tmp_path / "state.sqlite",
                predictions_json=tmp_path / "predictions.json",
                config_json=tmp_path / "config" / "matching-v1.json",
            )
    else:
        path = result.truth_manifest_json

        def loader(value: Path) -> object:
            return module("evaluator").verify_truth_manifest(
                value, tmp_path / "data" / "ground_truth"
            )

    payload = json.loads(path.read_text())
    payload["schema_version"] = True
    path.write_bytes(module("common").canonical_bytes(payload))
    with pytest.raises(module("common").InvalidDataError):
        loader(path)


def test_truth_family_isolation_rejects_reused_family_between_splits(tmp_path: Path) -> None:
    generated(tmp_path)
    for split in ("calibration", "validation"):
        path = tmp_path / "data" / "ground_truth" / f"{split}.csv"
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
            fields = list(rows[0])
        if split == "calibration":
            shared = rows[0]["family_id"]
        else:
            for row in rows:
                row["family_id"] = shared
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
    rows_by_split: dict[str, list[dict[str, str]]] = {}
    for split in ("calibration", "validation"):
        with (tmp_path / "data" / "ground_truth" / f"{split}.csv").open(
            newline="", encoding="utf-8"
        ) as handle:
            rows_by_split[split] = list(csv.DictReader(handle))
    with pytest.raises(module("evaluator").InvalidTruthError):
        module("evaluator").validate_truth_split_isolation(rows_by_split)


def test_generator_rejects_symlinked_config_before_loading_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generator = module("generator")
    config = config_in(tmp_path)
    linked = tmp_path / "config-link.json"
    os.symlink(config, linked)

    def should_not_load(_: Path) -> object:
        pytest.fail("config loader was reached through a symlink")

    monkeypatch.setattr(generator, "load_config", should_not_load)
    with pytest.raises(module("paths").PathSafetyError):
        generator.generate(
            workspace_root=tmp_path,
            output=tmp_path / "data",
            seed=20260902,
            config_json=linked,
        )
    assert not (tmp_path / "data").exists()


def test_cli_bootstrap_does_not_depend_on_checkout_config_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = module("cli")
    target = tmp_path / "workspace" / "config" / "matching-v1.json"
    target.parents[1].mkdir()
    fake_installed = tmp_path / "site-packages" / "entity_resolution_workbench" / "cli.py"
    fake_installed.parent.mkdir(parents=True)
    monkeypatch.setattr(cli, "__file__", str(fake_installed))
    cli._bootstrap_config(tmp_path / "workspace", target)
    assert target.is_file()
    assert json.loads(target.read_text())["schema_version"] == 1


def test_sqlite_exact_score_storage_does_not_depend_on_integer64(tmp_path: Path) -> None:
    """Near-bound inputs must retain exact score fractions as textual numerators/denominators."""
    matcher = module("matcher")
    scoring = module("scoring")
    pair = matcher.resolve_split(
        [
            {
                "source_id": "left",
                "name": "a" * 4095,
                "brand": "b" * 4093,
                "sku": "",
                "category": "shared",
                "price": "9999999999.99",
            }
        ],
        [
            {
                "source_id": "right",
                "name": "a" * 4094,
                "brand": "b" * 4092,
                "sku": "",
                "category": "shared",
                "price": "9999999999.98",
            }
        ],
        scoring.default_config(),
    )[0]
    assert pair.score.total.denominator > 2**63 - 1
    assert matcher._fraction_columns(pair.score.total) == (
        module("common").fraction_text(pair.score.total),
        str(pair.score.total.numerator),
        str(pair.score.total.denominator),
    )
    result = generated(tmp_path)
    matched(tmp_path, result)
    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        columns = {
            row[1]: row[2].upper()
            for row in conn.execute("PRAGMA table_info(pair_scores)")
            if row[1] in {"total_numerator", "total_denominator"}
        }
        stored = conn.execute(
            "SELECT total_score,total_numerator,total_denominator FROM pair_scores LIMIT 1"
        ).fetchone()
    assert columns == {"total_numerator": "TEXT", "total_denominator": "TEXT"}
    assert stored[0] == f"{stored[1]}/{stored[2]}"


@pytest.mark.parametrize("event_type", ["run_complete", "evaluation_complete"])
def test_audit_rejects_tampered_canonical_completion_event(tmp_path: Path, event_type: str) -> None:
    result = generated(tmp_path)
    matched(tmp_path, result)
    evaluated(tmp_path, result)
    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        conn.execute(
            "UPDATE audit_events SET payload_json = ? WHERE event_type = ?",
            ('{"tampered":true}\n', event_type),
        )
        conn.commit()
    with pytest.raises(module("common").IntegrityError):
        module("audit").audit(**audit_kwargs(tmp_path, result))


def test_audit_rejects_unknown_audit_event(tmp_path: Path) -> None:
    result = generated(tmp_path)
    matched(tmp_path, result)
    evaluated(tmp_path, result)
    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        conn.execute(
            "INSERT INTO audit_events(run_key,evaluation_key,event_type,payload_json) "
            "VALUES(NULL,NULL,'unexpected_event','{}')"
        )
        conn.commit()
    with pytest.raises(module("common").IntegrityError):
        module("audit").audit(**audit_kwargs(tmp_path, result))
