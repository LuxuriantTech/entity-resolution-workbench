"""Truth-free adapter from validated browser inputs to the real matcher."""

from __future__ import annotations

import csv
import io
from fractions import Fraction
from typing import Any

from . import matcher
from .common import (
    ResourceBoundError,
    canonical_bytes,
    digest_bytes,
    fraction_display,
    fraction_text,
)
from .matcher import Pair
from .normalization import normalize_record
from .scoring import default_config

MAX_EXPORT_BYTES = 16_000_000
MAX_SIMILARITY_WORK_UNITS = 5_000_000


class _BoundedUtf8Sink:
    def __init__(self) -> None:
        self._stream = io.BytesIO()

    def write(self, text: str) -> int:
        encoded = text.encode("utf-8")
        if self._stream.tell() + len(encoded) > MAX_EXPORT_BYTES:
            raise ResourceBoundError("export exceeds byte bound")
        self._stream.write(encoded)
        return len(text)

    def getvalue(self) -> bytes:
        return self._stream.getvalue()


def _score(value: Fraction | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "fraction": fraction_text(value),
        "display": fraction_display(value),
        "numerator": value.numerator,
        "denominator": value.denominator,
    }


def _record_payload(row: dict[str, str]) -> dict[str, dict[str, str | None]]:
    normalized = normalize_record(row)
    normalized_payload = normalized.as_payload()
    normalized_payload.pop("fingerprint")
    return {"raw": dict(row), "normalized": normalized_payload}


def _validate_similarity_work_budget(
    left: list[dict[str, dict[str, str | None]]],
    right: list[dict[str, dict[str, str | None]]],
) -> None:
    work_units = 0
    for field in ("name", "brand"):
        left_characters = sum(
            len(value)
            for record in left
            if isinstance(value := record["normalized"].get(field), str)
        )
        right_characters = sum(
            len(value)
            for record in right
            if isinstance(value := record["normalized"].get(field), str)
        )
        work_units += left_characters * right_characters
        if work_units > MAX_SIMILARITY_WORK_UNITS:
            raise ResourceBoundError("similarity work exceeds local bound")


def _pair_payload(
    pair: Pair,
    left_records: dict[str, dict[str, dict[str, str | None]]],
    right_records: dict[str, dict[str, dict[str, str | None]]],
) -> dict[str, Any]:
    return {
        "pair_id": "pair-" + digest_bytes(canonical_bytes([pair.left_id, pair.right_id]))[:24],
        "left_id": pair.left_id,
        "right_id": pair.right_id,
        "left": left_records[pair.left_id],
        "right": right_records[pair.right_id],
        "decision": pair.decision.value,
        "scores": {
            key: _score(getattr(pair.score, key))
            for key in ("name", "brand", "sku", "price", "total")
        },
        "ranks": {"left": pair.left_rank, "right": pair.right_rank},
        "margins": {
            "left": fraction_text(pair.left_margin),
            "right": fraction_text(pair.right_margin),
        },
        "block_reasons": list(pair.block_reasons),
        "missing_components": list(pair.score.missing_components),
        "contradictions": list(pair.score.contradictions),
        "failed_conditions": list(pair.decision.explanation.failed_conditions),
        "human_review": "UNREVIEWED",
        "review_revision": 0,
    }


def resolve_catalogues(
    left: list[dict[str, str]], right: list[dict[str, str]]
) -> list[dict[str, Any]]:
    """Run the frozen engine; the adapter has neither truth nor thresholds."""
    left_payloads = [_record_payload(row) for row in left]
    right_payloads = [_record_payload(row) for row in right]
    _validate_similarity_work_budget(left_payloads, right_payloads)
    pairs = matcher.resolve_split(left, right, default_config())
    left_records = {
        row["source_id"]: payload for row, payload in zip(left, left_payloads, strict=True)
    }
    right_records = {
        row["source_id"]: payload for row, payload in zip(right, right_payloads, strict=True)
    }
    return [_pair_payload(pair, left_records, right_records) for pair in pairs]


def _neutralize(value: object) -> str:
    text = "" if value is None else str(value)
    dangerous_prefix = text.startswith(("\t", "\r")) or text.lstrip().startswith(
        ("=", "+", "-", "@")
    )
    return "'" + text if dangerous_prefix else text


def export_csv(pairs: list[dict[str, Any]]) -> bytes:
    canonical = ("source_id", "name", "brand", "sku", "category", "price")
    fields = (
        "pair_id",
        "decision",
        "human_review",
        "review_revision",
        *(f"left_raw_{field}" for field in canonical),
        *(f"right_raw_{field}" for field in canonical),
        "name_score",
        "brand_score",
        "sku_score",
        "price_score",
        "total_score",
        "left_rank",
        "right_rank",
        "left_margin",
        "right_margin",
        "block_reasons",
        "missing_components",
        "contradictions",
        "failed_conditions",
    )
    stream = _BoundedUtf8Sink()
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for pair in pairs:
        row: dict[str, object] = {
            "pair_id": _neutralize(pair.get("pair_id")),
            "decision": _neutralize(pair.get("decision")),
            "human_review": _neutralize(pair.get("human_review")),
            "review_revision": pair.get("review_revision", 0),
            "left_rank": pair.get("ranks", {}).get("left", ""),
            "right_rank": pair.get("ranks", {}).get("right", ""),
            "left_margin": _neutralize(pair.get("margins", {}).get("left", "")),
            "right_margin": _neutralize(pair.get("margins", {}).get("right", "")),
            "block_reasons": _neutralize(";".join(pair.get("block_reasons", []))),
            "missing_components": _neutralize(";".join(pair.get("missing_components", []))),
            "contradictions": _neutralize(";".join(pair.get("contradictions", []))),
            "failed_conditions": _neutralize(";".join(pair.get("failed_conditions", []))),
        }
        for side in ("left", "right"):
            raw = pair.get(side, {}).get("raw", {})
            for field in canonical:
                row[f"{side}_raw_{field}"] = _neutralize(raw.get(field, ""))
        for component in ("name", "brand", "sku", "price", "total"):
            score = pair.get("scores", {}).get(component)
            row[f"{component}_score"] = _neutralize(score.get("fraction", "") if score else "")
        writer.writerow(row)
    return stream.getvalue()
