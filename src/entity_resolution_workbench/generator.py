from __future__ import annotations

import csv
import hashlib
import io
import random
from dataclasses import dataclass
from pathlib import Path

from .common import canonical_bytes, digest_bytes, digest_file
from .paths import validate_paths
from .publication import publish_many
from .scoring import load_config

_OBSERVED_FIELDS = ("source_id", "name", "brand", "sku", "category", "price")
_TRUTH_FIELDS = ("left_id", "right_id", "label", "split", "family_id", "scenario")
_GENERATOR_VERSION = "generator-v1"
_SPLIT_VERSION = "split-v1"


@dataclass(frozen=True)
class SplitFiles:
    left_csv: Path
    right_csv: Path
    truth_csv: Path


@dataclass(frozen=True)
class GenerateResult:
    observed_manifest_json: Path
    truth_manifest_json: Path
    calibration: SplitFiles
    validation: SplitFiles


def _opaque(seed: int, family_id: str, role: str) -> str:
    return hashlib.sha256(f"{seed}|source|{family_id}|{role}".encode()).hexdigest()[:24]


def _csv_bytes(rows: list[dict[str, str]], fields: tuple[str, ...]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _families(seed: int) -> list[tuple[str, str, str, str, str, str]]:
    scenarios = [
        "exact",
        "accent",
        "typo",
        "missing",
        "homonym",
        "token_reorder",
        "contradiction",
        "contradiction_sku",
        "contradiction_brand",
        "contradiction_price",
        "unmatched",
    ] * 2
    nouns = [
        "Cafetière Éclair",
        "Perceuse Atlas",
        "Lampe Boréale",
        "Balance Cobalt",
        "Marteau Delta",
        "Bouilloire Émeraude",
        "Casque Fjord",
        "Clavier Galaxie",
        "Mélangeur Horizon",
        "Scie Ivoire",
        "Thermomètre Junon",
    ]
    brands = ("Acme", "Boréal", "Cobalt", "Dynamo")
    categories = ("cuisine", "outillage", "éclairage", "mesure", "audio")
    families = []
    for index, scenario in enumerate(scenarios):
        cycle, base = divmod(index, len(nouns))
        families.append(
            (
                f"family-{index:02d}",
                scenario,
                f"{nouns[base]} {cycle + 1}",
                brands[index % len(brands)],
                categories[index % len(categories)],
                f"{19 + index}.{(index * 7) % 100:02d}",
            )
        )
    if len(families) < 2:  # pragma: no cover - frozen fixture guard
        raise AssertionError(f"seed {seed} has insufficient families")
    return families


def _right_variant(
    *,
    scenario: str,
    name: str,
    brand: str,
    sku: str,
    category: str,
    price: str,
    rng: random.Random,
) -> dict[str, str]:
    row = {"name": name, "brand": brand, "sku": sku, "category": category, "price": price}
    if scenario == "accent":
        row["name"] = name.replace("É", "E").replace("é", "e").upper().replace(" ", "---")
    elif scenario == "typo":
        characters = list(name)
        positions = [index for index, value in enumerate(characters) if value.isalpha()]
        position = positions[rng.randrange(len(positions))]
        characters[position] = "x" if characters[position].casefold() != "x" else "z"
        row["name"] = "".join(characters)
    elif scenario == "missing":
        row["brand"] = ""
        row["price"] = ""
    elif scenario == "homonym":
        row["sku"] = "HOMONYM-OTHER"
    elif scenario == "token_reorder":
        row["name"] = " ".join(reversed(name.split()))
    elif scenario in {"contradiction", "contradiction_sku"}:
        row["sku"] = "CONTRADICTORY-SKU"
    elif scenario == "contradiction_brand":
        row["brand"] = "Zyxwvu"
    elif scenario == "contradiction_price":
        row["price"] = "999.00"
    elif scenario == "unmatched":
        row.update(
            name="Objet sans relation",
            brand="Indépendant",
            sku="UNMATCHED-RIGHT",
            category="divers",
            price="3.00",
        )
    return row


def generate(*, workspace_root: Path, output: Path, seed: int, config_json: Path) -> GenerateResult:
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    planned_outputs = (
        output / "observed" / "calibration" / "supplier_a.csv",
        output / "observed" / "calibration" / "supplier_b.csv",
        output / "observed" / "validation" / "supplier_a.csv",
        output / "observed" / "validation" / "supplier_b.csv",
        output / "ground_truth" / "calibration.csv",
        output / "ground_truth" / "validation.csv",
        output / "observed-manifest.json",
        output / "truth-manifest.json",
    )
    validate_paths(workspace_root, inputs=(config_json,), outputs=planned_outputs)
    load_config(config_json)
    rng = random.Random(seed)
    families = _families(seed)
    ordered = sorted(
        families,
        key=lambda family: hashlib.sha256(f"{seed}|split|{family[0]}".encode()).hexdigest(),
    )
    cut = max(1, min(len(ordered) - 1, (60 * len(ordered)) // 100))
    split_for = {
        family[0]: "calibration" if index < cut else "validation"
        for index, family in enumerate(ordered)
    }
    name_override: dict[str, str] = {}
    for split in ("calibration", "validation"):
        split_families = [family for family in families if split_for[family[0]] == split]
        homonyms = [family for family in split_families if family[1] == "homonym"]
        if len(homonyms) > 1:
            shared_name = homonyms[0][2]
            name_override.update({family[0]: shared_name for family in homonyms})
        elif homonyms:
            peer = next(family for family in split_families if family[0] != homonyms[0][0])
            name_override[homonyms[0][0]] = peer[2]
    artifacts: dict[Path, bytes] = {}
    observed_entries: list[dict[str, object]] = []
    truth_entries: list[dict[str, object]] = []
    split_files: dict[str, SplitFiles] = {}
    for split in ("calibration", "validation"):
        left_rows: list[dict[str, str]] = []
        right_rows: list[dict[str, str]] = []
        truth_metadata: dict[str, tuple[str, str, str]] = {}
        for family_id, scenario, name, brand, category, price in families:
            if split_for[family_id] != split:
                continue
            name = name_override.get(family_id, name)
            sku = "SKU-" + family_id.removeprefix("family-")
            left_id = _opaque(seed, family_id, "supplier_a")
            right_id = _opaque(seed, family_id, "supplier_b")
            left_rows.append(
                {
                    "source_id": left_id,
                    "name": name,
                    "brand": brand,
                    "sku": sku,
                    "category": category,
                    "price": price,
                }
            )
            right_rows.append(
                {
                    "source_id": right_id,
                    **_right_variant(
                        scenario=scenario,
                        name=name,
                        brand=brand,
                        sku=sku,
                        category=category,
                        price=price,
                        rng=rng,
                    ),
                }
            )
            truth_metadata[left_id] = (family_id, scenario, right_id)
        left_path = output / "observed" / split / "supplier_a.csv"
        right_path = output / "observed" / split / "supplier_b.csv"
        left_bytes = _csv_bytes(left_rows, _OBSERVED_FIELDS)
        right_bytes = _csv_bytes(right_rows, _OBSERVED_FIELDS)
        artifacts[left_path] = left_bytes
        artifacts[right_path] = right_bytes
        for role, path, data, rows in (
            ("supplier_a", left_path, left_bytes, left_rows),
            ("supplier_b", right_path, right_bytes, right_rows),
        ):
            observed_entries.append(
                {
                    "split": split,
                    "role": role,
                    "path": path.relative_to(output).as_posix(),
                    "sha256": digest_bytes(data),
                    "rows": len(rows),
                }
            )
        truth_rows: list[dict[str, str]] = []
        for left in left_rows:
            family_id, scenario, matching_right = truth_metadata[left["source_id"]]
            for right in right_rows:
                truth_rows.append(
                    {
                        "left_id": left["source_id"],
                        "right_id": right["source_id"],
                        "label": "1"
                        if right["source_id"] == matching_right and scenario != "unmatched"
                        else "0",
                        "split": split,
                        "family_id": family_id,
                        "scenario": scenario,
                    }
                )
        truth_path = output / "ground_truth" / f"{split}.csv"
        truth_bytes = _csv_bytes(truth_rows, _TRUTH_FIELDS)
        artifacts[truth_path] = truth_bytes
        truth_entries.append(
            {
                "split": split,
                "path": truth_path.relative_to(output).as_posix(),
                "sha256": digest_bytes(truth_bytes),
                "rows": len(truth_rows),
            }
        )
        split_files[split] = SplitFiles(left_path, right_path, truth_path)
    observed_manifest = {
        "schema_version": 1,
        "seed": seed,
        "generator_version": _GENERATOR_VERSION,
        "split_version": _SPLIT_VERSION,
        "matching_config_sha256": digest_file(config_json),
        "observed_files": observed_entries,
    }
    observed_manifest_path = output / "observed-manifest.json"
    observed_manifest_bytes = canonical_bytes(observed_manifest)
    artifacts[observed_manifest_path] = observed_manifest_bytes
    truth_bundle = [
        {"split": entry["split"], "truth_sha256": entry["sha256"]} for entry in truth_entries
    ]
    truth_manifest = {
        "schema_version": 1,
        "truth_files": truth_entries,
        "truth_bundle_digest": digest_bytes(canonical_bytes(truth_bundle)),
        "observed_manifest_sha256": digest_bytes(observed_manifest_bytes),
    }
    truth_manifest_path = output / "truth-manifest.json"
    artifacts[truth_manifest_path] = canonical_bytes(truth_manifest)
    validate_paths(
        workspace_root,
        inputs=(config_json,),
        outputs=tuple(artifacts),
    )
    publish_many(tuple(artifacts.items()))
    return GenerateResult(
        observed_manifest_json=observed_manifest_path,
        truth_manifest_json=truth_manifest_path,
        calibration=split_files["calibration"],
        validation=split_files["validation"],
    )
