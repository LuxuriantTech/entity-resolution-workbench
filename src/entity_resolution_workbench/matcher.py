from __future__ import annotations

import platform
import sqlite3
import unicodedata
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import Any, cast

from .common import (
    IntegrityError,
    InvalidDataError,
    InvariantError,
    ResourceBoundError,
    canonical_bytes,
    digest_bytes,
    digest_file,
    fraction_payload,
    fraction_text,
    read_json_object,
    require_sha256,
)
from .database import (
    PreparedCatalog,
    _check_database,
    _connect,
    _ingest_prepared,
    _schema,
    exclusive_lock,
    prepare_catalog,
)
from .identity import run_key
from .normalization import NormalizedRecord, canonical_price, normalize_record
from .paths import validate_paths
from .publication import publish_verified
from .scoring import MatchingConfig, Score, default_config, load_config, score_pair


@dataclass(frozen=True)
class Explanation:
    failed_conditions: tuple[str, ...]
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Decision:
    value: str
    explanation: Explanation


@dataclass(frozen=True)
class Pair:
    left_id: str
    right_id: str
    admitted: bool
    block_reasons: tuple[str, ...]
    score: Score
    decision: Decision
    left_rank: int
    right_rank: int
    left_margin: Fraction
    right_margin: Fraction


@dataclass(frozen=True)
class MatchResult:
    run_key: str
    prediction_digest: str
    reused: bool


@dataclass(frozen=True)
class VerifiedObserved:
    manifest: dict[str, Any]
    manifest_digest: str
    entries: tuple[dict[str, object], ...]
    catalogs: dict[tuple[str, str], PreparedCatalog]


def decide_candidate(
    *,
    total: Fraction,
    evidence_count: int,
    supported: bool,
    contradiction: bool,
    admitted: bool,
    left_unique: bool,
    right_unique: bool,
    left_margin: Fraction,
    right_margin: Fraction,
    duplicate_ambiguous: bool,
    blocked_out_rival: bool,
    config: MatchingConfig | None = None,
    homonym_ambiguous: bool = False,
) -> Decision:
    config = config or default_config()
    failed: list[str] = []
    if not left_unique or not right_unique:
        failed.append("equal_top_score")
    if (
        left_margin <= config.minimum_bilateral_margin
        or right_margin <= config.minimum_bilateral_margin
    ):
        failed.append("insufficient_bilateral_margin")
    if duplicate_ambiguous:
        failed.append("duplicate_fingerprint")
    if homonym_ambiguous:
        failed.append("homonym_name")
    if blocked_out_rival:
        failed.append("blocked_out_rival")
    if contradiction:
        failed.append("contradiction")
    if not admitted:
        failed.append("not_admitted_by_blocking")
    if total < config.match_threshold:
        failed.append("below_match_threshold")
    if evidence_count < config.minimum_evidence_fields:
        failed.append("insufficient_evidence")
    if not supported:
        failed.append("missing_support")
    reasons = ("no_comparable_components",) if evidence_count == 0 else ()
    if not failed:
        return Decision("MATCH", Explanation((), reasons))
    value = "REVIEW" if admitted and total >= config.review_threshold else "NO_MATCH"
    return Decision(value, Explanation(tuple(failed), reasons))


def decide_scored_pair(score: Score, config: MatchingConfig | None = None) -> Decision:
    config = config or default_config()
    return decide_candidate(
        total=score.total,
        evidence_count=score.evidence_count,
        supported=score.sku == 1 or (score.name or Fraction()) >= config.name_support_at_least,
        contradiction=bool(score.contradictions),
        admitted=True,
        left_unique=True,
        right_unique=True,
        left_margin=Fraction(1),
        right_margin=Fraction(1),
        duplicate_ambiguous=False,
        blocked_out_rival=False,
        config=config,
    )


def _block_reasons(left: NormalizedRecord, right: NormalizedRecord) -> tuple[str, ...]:
    reasons: list[str] = []
    if left.category is not None and left.category == right.category:
        reasons.append("category_exact")
    if left.sku is not None and left.sku == right.sku:
        reasons.append("sku_exact")
    if left.brand is not None and left.brand == right.brand:
        reasons.append("brand_exact")
    if left.name is not None and right.name is not None:
        shared = sorted(
            token for token in set(left.name.split()) & set(right.name.split()) if len(token) >= 4
        )
        reasons.extend(f"name_token:{token}" for token in shared)
    return tuple(reasons)


def _normalize_records(
    rows: list[dict[str, str]], config: MatchingConfig
) -> list[NormalizedRecord]:
    normalized = [
        normalize_record(
            row,
            max_source_id_characters=config.max_source_id_characters,
            max_normalized_field_characters=config.max_normalized_field_characters,
            max_price_integer_digits=config.max_price_integer_digits,
            max_price_fraction_digits=config.max_price_fraction_digits,
        )
        for row in rows
    ]
    source_ids = [record.source_id for record in normalized]
    if len(source_ids) != len(set(source_ids)):
        raise InvalidDataError("duplicate source ID")
    return normalized


def _candidate_map(
    left: list[NormalizedRecord], right: list[NormalizedRecord], config: MatchingConfig
) -> dict[tuple[str, str], tuple[str, ...]]:
    if (
        len(left) > config.max_catalog_records
        or len(right) > config.max_catalog_records
        or len(left) * len(right) > config.max_pair_count
    ):
        raise ResourceBoundError("catalogue or pair bound exceeded")
    reasons = {
        (left_record.source_id, right_record.source_id): _block_reasons(left_record, right_record)
        for left_record in left
        for right_record in right
    }
    for left_record in left:
        candidate_count = sum(
            bool(reasons[left_record.source_id, right_record.source_id]) for right_record in right
        )
        if candidate_count > config.max_candidates_per_left:
            raise ResourceBoundError("candidate bound exceeded")
    return reasons


def build_candidates(
    left: list[dict[str, str]],
    right: list[dict[str, str]],
    config: MatchingConfig | None = None,
) -> set[tuple[str, str]]:
    config = config or default_config()
    normalized_left = _normalize_records(left, config)
    normalized_right = _normalize_records(right, config)
    return {
        pair
        for pair, reasons in _candidate_map(normalized_left, normalized_right, config).items()
        if reasons
    }


def _rank(own: Fraction, rivals: list[Fraction]) -> tuple[int, bool, Fraction]:
    rank = 1 + sum(rival > own for rival in rivals)
    unique_top = rank == 1 and sum(rival == own for rival in rivals) == 0
    margin = own - max(rivals) if rivals else Fraction(1)
    return rank, unique_top, margin


def resolve_split(
    left: list[dict[str, str]], right: list[dict[str, str]], config: MatchingConfig
) -> list[Pair]:
    normalized_left = _normalize_records(left, config)
    normalized_right = _normalize_records(right, config)
    reasons = _candidate_map(normalized_left, normalized_right, config)
    scores = {
        (left_record.source_id, right_record.source_id): score_pair(
            left_record, right_record, config
        )
        for left_record in normalized_left
        for right_record in normalized_right
    }
    duplicate_left = {
        record.fingerprint
        for record in normalized_left
        if sum(other.fingerprint == record.fingerprint for other in normalized_left) > 1
    }
    duplicate_right = {
        record.fingerprint
        for record in normalized_right
        if sum(other.fingerprint == record.fingerprint for other in normalized_right) > 1
    }
    homonym_left = {
        record.name
        for record in normalized_left
        if record.name is not None
        and sum(other.name == record.name for other in normalized_left) > 1
    }
    homonym_right = {
        record.name
        for record in normalized_right
        if record.name is not None
        and sum(other.name == record.name for other in normalized_right) > 1
    }
    output: list[Pair] = []
    for left_record in normalized_left:
        for right_record in normalized_right:
            pair_key = (left_record.source_id, right_record.source_id)
            score = scores[pair_key]
            left_rivals = [
                scores[left_record.source_id, rival.source_id].total
                for rival in normalized_right
                if rival.source_id != right_record.source_id
            ]
            right_rivals = [
                scores[rival.source_id, right_record.source_id].total
                for rival in normalized_left
                if rival.source_id != left_record.source_id
            ]
            left_rank, left_unique, left_margin = _rank(score.total, left_rivals)
            right_rank, right_unique, right_margin = _rank(score.total, right_rivals)
            blocked_out_rival = any(
                not reasons[left_record.source_id, rival.source_id]
                and scores[left_record.source_id, rival.source_id].total >= config.review_threshold
                for rival in normalized_right
                if rival.source_id != right_record.source_id
            ) or any(
                not reasons[rival.source_id, right_record.source_id]
                and scores[rival.source_id, right_record.source_id].total >= config.review_threshold
                for rival in normalized_left
                if rival.source_id != left_record.source_id
            )
            admitted = bool(reasons[pair_key])
            decision = decide_candidate(
                total=score.total,
                evidence_count=score.evidence_count,
                supported=score.sku == 1
                or (score.name or Fraction()) >= config.name_support_at_least,
                contradiction=bool(score.contradictions),
                admitted=admitted,
                left_unique=left_unique,
                right_unique=right_unique,
                left_margin=left_margin,
                right_margin=right_margin,
                duplicate_ambiguous=left_record.fingerprint in duplicate_left
                or right_record.fingerprint in duplicate_right,
                blocked_out_rival=blocked_out_rival,
                config=config,
                homonym_ambiguous=left_record.name in homonym_left
                or right_record.name in homonym_right,
            )
            output.append(
                Pair(
                    left_id=left_record.source_id,
                    right_id=right_record.source_id,
                    admitted=admitted,
                    block_reasons=reasons[pair_key],
                    score=score,
                    decision=decision,
                    left_rank=left_rank,
                    right_rank=right_rank,
                    left_margin=left_margin,
                    right_margin=right_margin,
                )
            )
    return output


def _manifest_entry_path(manifest_path: Path, observed_root: Path, item: dict[str, Any]) -> Path:
    raw_path = item.get("path")
    if not isinstance(raw_path, str):
        raise IntegrityError("observed manifest path is invalid")
    posix = PurePosixPath(raw_path)
    if posix.is_absolute() or ".." in posix.parts or "." in posix.parts:
        raise IntegrityError("observed manifest path is not a safe relative path")
    path = manifest_path.parent.joinpath(*posix.parts)
    expected = observed_root / str(item["split"]) / f"{item['role']}.csv"
    if path.absolute() != expected.absolute():
        raise IntegrityError("observed manifest path differs from requested root")
    return path


def verify_observed_bundle(
    *,
    workspace_root: Path,
    observed_manifest: Path,
    observed_root: Path,
    config_json: Path,
    output_paths: tuple[Path, ...] = (),
    lock_paths: tuple[Path, ...] = (),
) -> VerifiedObserved:
    validate_paths(
        workspace_root,
        inputs=(observed_manifest, config_json),
        outputs=output_paths,
        locks=lock_paths,
    )
    manifest = read_json_object(observed_manifest)
    if set(manifest) != {
        "schema_version",
        "seed",
        "generator_version",
        "split_version",
        "matching_config_sha256",
        "observed_files",
    }:
        raise IntegrityError("observed manifest schema mismatch")
    if (
        isinstance(manifest["schema_version"], bool)
        or manifest["schema_version"] != 1
        or isinstance(manifest["seed"], bool)
        or not isinstance(manifest["seed"], int)
        or manifest["generator_version"] != "generator-v1"
        or manifest["split_version"] != "split-v1"
    ):
        raise IntegrityError("observed manifest version or seed is invalid")
    config_digest = require_sha256(
        manifest["matching_config_sha256"], field="matching_config_sha256"
    )
    if digest_file(config_json) != config_digest:
        raise IntegrityError("matching config differs from observed manifest")
    items = manifest["observed_files"]
    if not isinstance(items, list) or len(items) != 4:
        raise IntegrityError("observed manifest must contain four files")
    expected_order = [
        (split, role)
        for split in ("calibration", "validation")
        for role in ("supplier_a", "supplier_b")
    ]
    paths: list[Path] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or set(item) != {"split", "role", "path", "sha256", "rows"}:
            raise IntegrityError("observed manifest entry schema mismatch")
        if (item["split"], item["role"]) != expected_order[index]:
            raise IntegrityError("observed manifest entries are not in canonical order")
        if isinstance(item["rows"], bool) or not isinstance(item["rows"], int) or item["rows"] < 0:
            raise IntegrityError("observed manifest row count is invalid")
        require_sha256(item["sha256"], field="observed file sha256")
        paths.append(_manifest_entry_path(observed_manifest, observed_root, item))
    validate_paths(
        workspace_root,
        inputs=(observed_manifest, config_json, *paths),
        outputs=output_paths,
        locks=lock_paths,
    )
    config = load_config(config_json)
    catalogs: dict[tuple[str, str], PreparedCatalog] = {}
    verified_entries: list[dict[str, object]] = []
    all_source_ids: set[str] = set()
    for item, path in zip(items, paths, strict=True):
        catalog = prepare_catalog(path, config)
        if catalog.content_sha256 != item["sha256"] or len(catalog.records) != item["rows"]:
            raise IntegrityError("observed file differs from manifest digest or row count")
        source_ids = {record.normalized.source_id for record in catalog.records}
        if all_source_ids & source_ids:
            raise IntegrityError("source IDs are reused across physical catalogues")
        all_source_ids.update(source_ids)
        key = (str(item["split"]), str(item["role"]))
        catalogs[key] = catalog
        verified_entries.append(
            {
                "path": str(item["path"]),
                "role": key[1],
                "rows": len(catalog.records),
                "sha256": catalog.content_sha256,
                "split": key[0],
            }
        )
    return VerifiedObserved(
        manifest=manifest,
        manifest_digest=digest_file(observed_manifest),
        entries=tuple(verified_entries),
        catalogs=catalogs,
    )


def source_bundle() -> list[dict[str, str]]:
    package = Path(__file__).resolve().parent
    checkout_root = package.parent.parent
    checkout_metadata = (checkout_root / "pyproject.toml", checkout_root / "uv.lock")
    if all(path.is_file() for path in checkout_metadata):
        metadata = checkout_metadata
    else:
        provenance = package / "_provenance"
        metadata = (provenance / "pyproject.toml", provenance / "uv.lock")
    if not all(path.is_file() for path in metadata):
        raise IntegrityError("build provenance is missing")
    sources = [
        (f"src/entity_resolution_workbench/{path.relative_to(package).as_posix()}", path)
        for path in package.glob("**/*.py")
    ]
    paths = [*sources, ("pyproject.toml", metadata[0]), ("uv.lock", metadata[1])]
    return [
        {"path": logical, "sha256": digest_file(path)}
        for logical, path in sorted(paths, key=lambda item: item[0])
    ]


def build_identity_envelope(
    verified: VerifiedObserved, *, config: MatchingConfig, config_digest: str
) -> dict[str, object]:
    bundle = source_bundle()
    uv_lock_digest = next(item["sha256"] for item in bundle if item["path"] == "uv.lock")
    return {
        "schema": "erw-run-key-v1",
        "observed_manifest_sha256": verified.manifest_digest,
        "observed_files": list(verified.entries),
        "config_sha256": config_digest,
        "component_versions": {
            "blocking": config.blocking_version,
            "decision": config.decision_version,
            "normalization": config.normalization_version,
            "scoring": config.scoring_version,
            "schema": config.schema_version,
        },
        "python_version": platform.python_version(),
        "unicode_version": unicodedata.unidata_version,
        "sqlite_version": sqlite3.sqlite_version,
        "uv_lock_sha256": uv_lock_digest,
        "source_bundle": bundle,
    }


def _raw_rows(catalog: PreparedCatalog) -> list[dict[str, str]]:
    return [record.raw for record in catalog.records]


def _normalization_payload(record: NormalizedRecord) -> dict[str, str | None]:
    return {
        "name": record.name,
        "brand": record.brand,
        "sku": record.sku,
        "category": record.category,
        "price": canonical_price(record.price),
    }


def _pair_payload(pair: Pair, left: NormalizedRecord, right: NormalizedRecord) -> dict[str, object]:
    return {
        "left_id": pair.left_id,
        "right_id": pair.right_id,
        "decision": pair.decision.value,
        "admitted": pair.admitted,
        "block_reasons": list(pair.block_reasons),
        "scores": {
            "name": fraction_payload(pair.score.name),
            "brand": fraction_payload(pair.score.brand),
            "sku": fraction_payload(pair.score.sku),
            "price": fraction_payload(pair.score.price),
            "total": fraction_payload(pair.score.total),
        },
        "evidence_count": pair.score.evidence_count,
        "contradictions": list(pair.score.contradictions),
        "explanation": {
            "block_reasons": list(pair.block_reasons),
            "failed_conditions": list(pair.decision.explanation.failed_conditions),
            "left_margin": fraction_text(pair.left_margin),
            "left_rank": pair.left_rank,
            "missing_components": list(pair.score.missing_components),
            "normalization": {
                "left": _normalization_payload(left),
                "right": _normalization_payload(right),
            },
            "reasons": list(pair.decision.explanation.reasons),
            "right_margin": fraction_text(pair.right_margin),
            "right_rank": pair.right_rank,
        },
    }


def _canonical_projection(
    pairs_by_split: dict[str, list[Pair]], catalogs: dict[tuple[str, str], PreparedCatalog]
) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    matched_by_member: dict[tuple[str, str, str], str] = {}
    for split, pairs in pairs_by_split.items():
        for pair in pairs:
            if pair.decision.value != "MATCH":
                continue
            key = (
                "match:" + digest_bytes(canonical_bytes([split, pair.left_id, pair.right_id]))[:24]
            )
            matched_by_member[split, "supplier_a", pair.left_id] = key
            matched_by_member[split, "supplier_b", pair.right_id] = key
    members: list[dict[str, str]] = []
    kinds: dict[str, str] = {}
    for split in ("calibration", "validation"):
        for role in ("supplier_a", "supplier_b"):
            for prepared in catalogs[split, role].records:
                source_id = prepared.normalized.source_id
                member_key = (split, role, source_id)
                canonical_key = matched_by_member.get(member_key)
                if canonical_key is None:
                    canonical_key = (
                        "single:" + digest_bytes(canonical_bytes([split, role, source_id]))[:24]
                    )
                    kinds[canonical_key] = "singleton"
                else:
                    kinds[canonical_key] = "matched"
                members.append(
                    {
                        "canonical_key": canonical_key,
                        "role": role,
                        "source_id": source_id,
                        "split": split,
                    }
                )
    members.sort(key=lambda item: (item["split"], item["role"], item["source_id"]))
    grouped: dict[str, list[dict[str, str]]] = {}
    for member in members:
        grouped.setdefault(member["canonical_key"], []).append(
            {
                "role": member["role"],
                "source_id": member["source_id"],
                "split": member["split"],
            }
        )
    entities: list[dict[str, object]] = [
        {"canonical_key": key, "kind": kinds[key], "members": grouped[key]}
        for key in sorted(grouped)
    ]
    if any(len(grouped[key]) != (2 if kinds[key] == "matched" else 1) for key in grouped):
        raise InvariantError("canonical entity cardinality is invalid")
    return entities, members


def _prediction_payload(
    *,
    run: str,
    envelope: dict[str, object],
    config_raw: dict[str, Any],
    config: MatchingConfig,
    verified: VerifiedObserved,
    pairs_by_split: dict[str, list[Pair]],
) -> dict[str, object]:
    split_payloads: dict[str, object] = {}
    for split in ("calibration", "validation"):
        left_catalog = verified.catalogs[split, "supplier_a"]
        right_catalog = verified.catalogs[split, "supplier_b"]
        left_by_id = {
            record.normalized.source_id: record.normalized for record in left_catalog.records
        }
        right_by_id = {
            record.normalized.source_id: record.normalized for record in right_catalog.records
        }
        pairs = sorted(pairs_by_split[split], key=lambda pair: (pair.left_id, pair.right_id))
        split_payloads[split] = {
            "catalogue_counts": {
                "supplier_a": len(left_catalog.records),
                "supplier_b": len(right_catalog.records),
            },
            "pairs": [
                _pair_payload(pair, left_by_id[pair.left_id], right_by_id[pair.right_id])
                for pair in pairs
            ],
        }
    entities, members = _canonical_projection(pairs_by_split, verified.catalogs)
    decision_counts = {
        name: sum(
            pair.decision.value == name for pairs in pairs_by_split.values() for pair in pairs
        )
        for name in ("MATCH", "REVIEW", "NO_MATCH")
    }
    return {
        "schema": "erw-predictions-v1",
        "component_versions": envelope["component_versions"],
        "run_key": run,
        "observed_manifest_sha256": verified.manifest_digest,
        "config_digest": str(envelope["config_sha256"]),
        "frozen_config": config_raw,
        "identity_envelope": envelope,
        "splits": split_payloads,
        "canonical_entities": entities,
        "canonical_members": members,
        "audit_summary": {
            "decision_counts": decision_counts,
            "ingestion_batches": 4,
            "source_records": len(members),
        },
    }


def _json_text(value: object) -> str:
    return canonical_bytes(value).decode("utf-8")


def _fraction_columns(value: Fraction | None) -> tuple[str | None, str | None, str | None]:
    if value is None:
        return None, None, None
    return fraction_text(value), str(value.numerator), str(value.denominator)


def _score_values(pair: Pair) -> tuple[object, ...]:
    return (
        *_fraction_columns(pair.score.name),
        *_fraction_columns(pair.score.brand),
        *_fraction_columns(pair.score.sku),
        *_fraction_columns(pair.score.price),
        *_fraction_columns(pair.score.total),
        pair.score.evidence_count,
        _json_text(list(pair.block_reasons)),
        _json_text(list(pair.score.contradictions)),
    )


def _decision_values(pair: Pair) -> tuple[object, ...]:
    explanation = {
        "failed_conditions": list(pair.decision.explanation.failed_conditions),
        "reasons": list(pair.decision.explanation.reasons),
    }
    return (
        pair.decision.value,
        pair.left_rank,
        pair.right_rank,
        fraction_text(pair.left_margin),
        fraction_text(pair.right_margin),
        _json_text(explanation),
    )


def _verify_reused_run(
    conn: sqlite3.Connection,
    *,
    run: str,
    prediction_bytes: bytes,
    envelope: dict[str, object],
    config_digest: str,
    verified: VerifiedObserved,
    pairs_by_split: dict[str, list[Pair]],
    entities: list[dict[str, object]],
    members: list[dict[str, str]],
) -> None:
    row = conn.execute(
        "SELECT observed_manifest_sha256,config_sha256,source_bundle_digest,"
        "identity_envelope_json,prediction_digest,prediction_bytes,state "
        "FROM resolution_runs WHERE run_key=?",
        (run,),
    ).fetchone()
    if (
        row is None
        or row[0] != verified.manifest_digest
        or row[1] != config_digest
        or row[2] != digest_bytes(canonical_bytes(envelope["source_bundle"]))
        or row[3] != _json_text(envelope)
        or row[4] != digest_bytes(prediction_bytes)
    ):
        raise InvariantError("reused run identity differs")
    if row[5] != prediction_bytes or row[6] != "complete":
        raise InvariantError("reused prediction payload differs")
    expected_pairs = sum(len(pairs) for pairs in pairs_by_split.values())
    counts = {
        "pairs": conn.execute(
            "SELECT count(*) FROM pair_scores WHERE run_key=?", (run,)
        ).fetchone()[0],
        "decisions": conn.execute(
            "SELECT count(*) FROM decisions WHERE run_key=?", (run,)
        ).fetchone()[0],
        "entities": conn.execute(
            "SELECT count(*) FROM canonical_entities WHERE run_key=?", (run,)
        ).fetchone()[0],
        "members": conn.execute(
            "SELECT count(*) FROM canonical_members WHERE run_key=?", (run,)
        ).fetchone()[0],
        "batches": conn.execute(
            "SELECT count(*) FROM run_batches WHERE run_key=?", (run,)
        ).fetchone()[0],
    }
    expected_counts = {
        "pairs": expected_pairs,
        "decisions": expected_pairs,
        "entities": len(entities),
        "members": len(members),
        "batches": 4,
    }
    if counts != expected_counts:
        raise InvariantError("reused run relational projection is incomplete")
    batches = conn.execute(
        "SELECT rb.split,rb.role,b.content_sha256,b.row_count FROM run_batches rb "
        "JOIN ingestion_batches b ON b.id=rb.batch_id WHERE rb.run_key=? "
        "ORDER BY rb.split,rb.role",
        (run,),
    ).fetchall()
    expected_batches = [
        (
            split,
            role,
            verified.catalogs[split, role].content_sha256,
            len(verified.catalogs[split, role].records),
        )
        for split in ("calibration", "validation")
        for role in ("supplier_a", "supplier_b")
    ]
    if batches != expected_batches:
        raise InvariantError("reused run lineage differs")
    for split, role, content_digest, row_count in expected_batches:
        payload = _json_text(
            {
                "catalog_role": f"{split}:{role}",
                "content_sha256": content_digest,
                "row_count": row_count,
            }
        )
        event_count = conn.execute(
            "SELECT count(*) FROM audit_events WHERE run_key IS NULL "
            "AND evaluation_key IS NULL AND event_type='ingestion_complete' "
            "AND payload_json=?",
            (payload,),
        ).fetchone()[0]
        if event_count != 1:
            raise InvariantError("reused ingestion audit event differs")
    expected_pair_rows: dict[tuple[str, str, str], tuple[object, ...]] = {}
    for split, pairs in pairs_by_split.items():
        for pair in pairs:
            score_values = _score_values(pair)
            expected_pair_rows[split, pair.left_id, pair.right_id] = (
                int(pair.admitted),
                score_values[16],
                *score_values[:16],
                score_values[17],
                *_decision_values(pair),
            )
    actual_pair_rows = {
        (str(row[0]), str(row[1]), str(row[2])): tuple(row[3:])
        for row in conn.execute(
            "SELECT p.split,p.left_id,p.right_id,p.candidate,p.block_reasons_json,"
            "p.name_score,p.name_numerator,p.name_denominator,"
            "p.brand_score,p.brand_numerator,p.brand_denominator,"
            "p.sku_score,p.sku_numerator,p.sku_denominator,"
            "p.price_score,p.price_numerator,p.price_denominator,"
            "p.total_score,p.total_numerator,p.total_denominator,"
            "p.evidence_count,p.contradictions_json,d.decision,d.left_rank,d.right_rank,"
            "d.left_margin,d.right_margin,d.explanation_json FROM pair_scores p "
            "JOIN decisions d ON d.run_key=p.run_key AND d.split=p.split "
            "AND d.left_record_id=p.left_record_id AND d.right_record_id=p.right_record_id "
            "WHERE p.run_key=?",
            (run,),
        )
    }
    if actual_pair_rows != expected_pair_rows:
        raise InvariantError("reused pair or decision provenance differs")
    actual_entities = conn.execute(
        "SELECT canonical_key,kind FROM canonical_entities WHERE run_key=? ORDER BY canonical_key",
        (run,),
    ).fetchall()
    expected_entities = sorted(
        (str(entity["canonical_key"]), str(entity["kind"])) for entity in entities
    )
    actual_members = conn.execute(
        "SELECT canonical_key,source_id,split,role FROM canonical_members WHERE run_key=? "
        "ORDER BY split,role,source_id",
        (run,),
    ).fetchall()
    expected_members = [
        (member["canonical_key"], member["source_id"], member["split"], member["role"])
        for member in members
    ]
    if actual_entities != expected_entities or actual_members != expected_members:
        raise InvariantError("reused canonical projection differs")
    run_events = conn.execute(
        "SELECT count(*) FROM audit_events WHERE run_key=? AND event_type='run_complete'",
        (run,),
    ).fetchone()[0]
    if run_events != 1:
        raise InvariantError("reused run audit event differs")


def match(
    *,
    workspace_root: Path,
    observed_manifest: Path,
    observed_root: Path,
    database: Path,
    predictions_json: Path,
    config_json: Path,
) -> MatchResult:
    lock_path = database.with_suffix(database.suffix + ".lock")
    verified = verify_observed_bundle(
        workspace_root=workspace_root,
        observed_manifest=observed_manifest,
        observed_root=observed_root,
        config_json=config_json,
        output_paths=(database, predictions_json),
        lock_paths=(lock_path,),
    )
    config = load_config(config_json)
    config_raw = read_json_object(config_json)
    envelope = build_identity_envelope(
        verified, config=config, config_digest=digest_file(config_json)
    )
    key = run_key(envelope)
    pairs_by_split = {
        split: resolve_split(
            _raw_rows(verified.catalogs[split, "supplier_a"]),
            _raw_rows(verified.catalogs[split, "supplier_b"]),
            config,
        )
        for split in ("calibration", "validation")
    }
    payload = _prediction_payload(
        run=key,
        envelope=envelope,
        config_raw=config_raw,
        config=config,
        verified=verified,
        pairs_by_split=pairs_by_split,
    )
    prediction_bytes = canonical_bytes(payload)
    entities = cast(list[dict[str, object]], payload["canonical_entities"])
    members = cast(list[dict[str, str]], payload["canonical_members"])
    reused = False
    with exclusive_lock(database), _connect(database) as conn:
        _schema(conn)
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT 1 FROM resolution_runs WHERE run_key=?", (key,)
            ).fetchone()
            if existing is not None:
                _verify_reused_run(
                    conn,
                    run=key,
                    prediction_bytes=prediction_bytes,
                    envelope=envelope,
                    config_digest=digest_file(config_json),
                    verified=verified,
                    pairs_by_split=pairs_by_split,
                    entities=entities,
                    members=members,
                )
                reused = True
                conn.commit()
            else:
                stored_batches = {
                    (split, role): _ingest_prepared(
                        conn,
                        catalog_role=f"{split}:{role}",
                        catalog=verified.catalogs[split, role],
                    )
                    for split in ("calibration", "validation")
                    for role in ("supplier_a", "supplier_b")
                }
                bundle_digest = digest_bytes(canonical_bytes(envelope["source_bundle"]))
                conn.execute(
                    "INSERT INTO resolution_runs("
                    "run_key,observed_manifest_sha256,config_sha256,source_bundle_digest,"
                    "identity_envelope_json,prediction_digest,prediction_bytes,state) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        key,
                        verified.manifest_digest,
                        digest_file(config_json),
                        bundle_digest,
                        _json_text(envelope),
                        digest_bytes(prediction_bytes),
                        prediction_bytes,
                        "complete",
                    ),
                )
                for (split, role), stored in stored_batches.items():
                    conn.execute(
                        "INSERT INTO run_batches(run_key,batch_id,split,role) VALUES(?,?,?,?)",
                        (key, stored.batch_id, split, role),
                    )
                for split in ("calibration", "validation"):
                    left_ids = stored_batches[split, "supplier_a"].record_ids
                    right_ids = stored_batches[split, "supplier_b"].record_ids
                    for pair in pairs_by_split[split]:
                        left_record_id = left_ids[pair.left_id]
                        right_record_id = right_ids[pair.right_id]
                        score_values = _score_values(pair)
                        conn.execute(
                            "INSERT INTO pair_scores("
                            "run_key,split,left_record_id,right_record_id,left_id,right_id,"
                            "candidate,block_reasons_json,"
                            "name_score,name_numerator,name_denominator,"
                            "brand_score,brand_numerator,brand_denominator,"
                            "sku_score,sku_numerator,sku_denominator,"
                            "price_score,price_numerator,price_denominator,"
                            "total_score,total_numerator,total_denominator,"
                            "evidence_count,contradictions_json) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                key,
                                split,
                                left_record_id,
                                right_record_id,
                                pair.left_id,
                                pair.right_id,
                                int(pair.admitted),
                                score_values[16],
                                *score_values[:16],
                                score_values[17],
                            ),
                        )
                        conn.execute(
                            "INSERT INTO decisions("
                            "run_key,split,left_record_id,right_record_id,left_id,right_id,"
                            "decision,left_rank,right_rank,left_margin,right_margin,"
                            "explanation_json) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                key,
                                split,
                                left_record_id,
                                right_record_id,
                                pair.left_id,
                                pair.right_id,
                                *_decision_values(pair),
                            ),
                        )
                for entity in entities:
                    conn.execute(
                        "INSERT INTO canonical_entities(run_key,canonical_key,kind) VALUES(?,?,?)",
                        (key, entity["canonical_key"], entity["kind"]),
                    )
                for member in members:
                    stored = stored_batches[member["split"], member["role"]]
                    conn.execute(
                        "INSERT INTO canonical_members("
                        "run_key,canonical_key,source_record_id,source_id,split,role) "
                        "VALUES(?,?,?,?,?,?)",
                        (
                            key,
                            member["canonical_key"],
                            stored.record_ids[member["source_id"]],
                            member["source_id"],
                            member["split"],
                            member["role"],
                        ),
                    )
                conn.execute(
                    "INSERT INTO audit_events(run_key,evaluation_key,event_type,payload_json) "
                    "VALUES(?,?,?,?)",
                    (
                        key,
                        None,
                        "run_complete",
                        _json_text(
                            {
                                "prediction_digest": digest_bytes(prediction_bytes),
                                "run_key": key,
                            }
                        ),
                    ),
                )
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        _check_database(conn)
    publish_verified(database_path=database, run_key=key, prediction_path=predictions_json)
    return MatchResult(key, digest_bytes(prediction_bytes), reused)
