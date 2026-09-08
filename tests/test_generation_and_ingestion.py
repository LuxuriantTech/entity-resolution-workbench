from __future__ import annotations

import csv
import inspect
import json
import sqlite3
from pathlib import Path

import pytest
from conftest import config_in, module, record, write_csv


def test_ac1_seeded_generation_is_byte_reproducible_and_config_anchored(tmp_path: Path) -> None:
    generator = module("generator")
    config = config_in(tmp_path)
    first, second, changed = (tmp_path / name for name in ("first", "second", "changed"))
    generator.generate(
        workspace_root=tmp_path,
        output=first,
        seed=20260902,
        config_json=config,
    )
    generator.generate(
        workspace_root=tmp_path,
        output=second,
        seed=20260902,
        config_json=config,
    )
    generator.generate(workspace_root=tmp_path, output=changed, seed=7, config_json=config)
    assert sorted(p.relative_to(first) for p in first.rglob("*") if p.is_file()) == sorted(
        p.relative_to(second) for p in second.rglob("*") if p.is_file()
    )
    for file in (p for p in first.rglob("*") if p.is_file()):
        assert file.read_bytes() == (second / file.relative_to(first)).read_bytes()
    assert (first / "observed-manifest.json").read_bytes() != (
        changed / "observed-manifest.json"
    ).read_bytes()
    assert "matching_config_sha256" in json.loads((first / "observed-manifest.json").read_text())


def test_ac2_observed_and_truth_are_physically_separate_and_match_has_no_truth_parameter(
    tmp_path: Path,
) -> None:
    generator = module("generator")
    matcher = module("matcher")
    config = config_in(tmp_path)
    result = generator.generate(
        workspace_root=tmp_path,
        output=tmp_path / "data",
        seed=20260902,
        config_json=config,
    )
    observed = json.loads(result.observed_manifest_json.read_text())
    forbidden = {"truth", "label", "family", "scenario", "metric"}
    assert not any(word in json.dumps(observed).lower() for word in forbidden)
    assert not any("truth" in name.lower() for name in inspect.signature(matcher.match).parameters)
    split_ids: dict[str, set[str]] = {}
    for split in ("calibration", "validation"):
        split_ids[split] = set()
        for role in ("supplier_a", "supplier_b"):
            with (tmp_path / "data" / "observed" / split / f"{role}.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                reader = csv.DictReader(handle)
                assert not (
                    {"family_id", "label", "scenario", "split"} & set(reader.fieldnames or [])
                )
                split_ids[split].update(row["source_id"] for row in reader)
        with (tmp_path / "data" / "ground_truth" / f"{split}.csv").open(
            newline="", encoding="utf-8"
        ) as handle:
            truth = list(csv.DictReader(handle))
        assert truth and {row["split"] for row in truth} == {split}
        assert all(row["family_id"] and row["scenario"] for row in truth)
    assert split_ids["calibration"].isdisjoint(split_ids["validation"])


def test_fr1_required_synthetic_scenarios_and_complete_truth_are_present(tmp_path: Path) -> None:
    result = module("generator").generate(
        workspace_root=tmp_path,
        output=tmp_path / "data",
        seed=20260902,
        config_json=config_in(tmp_path),
    )
    assert result.truth_manifest_json.is_file()
    scenarios: set[str] = set()
    labels: set[str] = set()
    for split in ("calibration", "validation"):
        observed_counts: list[int] = []
        for role in ("supplier_a", "supplier_b"):
            with (tmp_path / "data" / "observed" / split / f"{role}.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                observed_counts.append(sum(1 for _ in csv.DictReader(handle)))
        with (tmp_path / "data" / "ground_truth" / f"{split}.csv").open(
            newline="", encoding="utf-8"
        ) as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == observed_counts[0] * observed_counts[1]
        assert len({(row["left_id"], row["right_id"]) for row in rows}) == len(rows)
        scenarios.update(row["scenario"] for row in rows)
        labels.update(row["label"] for row in rows)
    required = {"exact", "accent", "typo", "missing", "homonym", "contradiction", "unmatched"}
    assert required <= scenarios
    assert labels == {"0", "1"}


def test_ac3_repeated_identical_ingestion_reuses_batch_and_records(tmp_path: Path) -> None:
    database = module("database")
    csv_path = write_csv(tmp_path / "a.csv", [record("left-opaque", name="Café", brand="Acme")])
    db = tmp_path / "state.sqlite"
    first = database.ingest_csv(database_path=db, catalog_role="supplier_a", csv_path=csv_path)
    second = database.ingest_csv(database_path=db, catalog_role="supplier_a", csv_path=csv_path)
    assert first.batch_id == second.batch_id
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM ingestion_batches").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM source_records").fetchone()[0] == 1


@pytest.mark.parametrize(
    "payload",
    [
        b"source_id,name,brand,sku,category,price\n\xff,x,,,,\n",
        b"source_id,name,brand,sku,category,price\na,x,,,,\na,y,,,,\n",
        b"source_id,name,name,sku,category,price\na,x,y,,z,1\n",
        b"source_id,name,brand,sku,category,price,unknown\na,x,,,,z\n",
        b"source_id,name,brand,sku,category,price\na,x,,,,1e2\n",
        b"source_id,name,brand,sku,category,price\na,,,,,\n",
        b"source_id,name,brand,sku,category,price\na,x\x00y,,,,\n",
    ],
)
def test_ac4_ec3_ec4_invalid_csv_rolls_back_atomically(tmp_path: Path, payload: bytes) -> None:
    database = module("database")
    source, db = tmp_path / "bad.csv", tmp_path / "state.sqlite"
    source.write_bytes(payload)
    with pytest.raises(database.InvalidDataError):
        database.ingest_csv(database_path=db, catalog_role="supplier_a", csv_path=source)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM ingestion_batches").fetchone()[0] == 0


def test_ac20_ec1_ec2_ec22_normalization_makes_empty_values_missing(tmp_path: Path) -> None:
    normalization = module("normalization")
    assert normalization.normalize_text("  Café---MIXED\u0301  ") == "cafe mixed"
    assert normalization.normalize_text("---\u0301") is None
    assert normalization.normalize_sku("--") is None
    normalized = normalization.normalize_record(
        record("opaque", name="Widget", brand="", sku="---")
    )
    assert normalized.brand is None and normalized.sku is None
    with pytest.raises(normalization.InvalidDataError):
        normalization.normalize_record(record("opaque", sku="---"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_id", "x" * 129),
        ("name", "x" * 4097),
        ("price", "1" * 11),
        ("price", "1.001"),
    ],
)
def test_ac22_ec23_resource_bounds_fail_before_any_score(
    tmp_path: Path, field: str, value: str
) -> None:
    database = module("database")
    row = record("id", name="ok")
    row[field] = value
    source = write_csv(tmp_path / "bound.csv", [row])
    with pytest.raises(database.ResourceBoundError):
        database.ingest_csv(
            database_path=tmp_path / "state.sqlite", catalog_role="supplier_a", csv_path=source
        )


def test_ac22_ec23_csv_file_byte_bound_is_enforced(tmp_path: Path) -> None:
    database = module("database")
    source = tmp_path / "too-large.csv"
    source.write_bytes(
        b"source_id,name,brand,sku,category,price\n" + b"id," + b"x" * 1_000_001 + b",,,,\n"
    )
    with pytest.raises(database.ResourceBoundError):
        database.ingest_csv(
            database_path=tmp_path / "state.sqlite", catalog_role="supplier_a", csv_path=source
        )
