from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from fractions import Fraction
from pathlib import Path
from typing import Any

from .common import InvalidDataError, read_json_object
from .normalization import NormalizedRecord, normalize_record

_CONFIG_KEYS = {
    "schema_version",
    "normalization_version",
    "blocking_version",
    "scoring_version",
    "decision_version",
    "max_catalog_records",
    "max_pair_count",
    "max_candidates_per_left",
    "max_csv_file_bytes",
    "max_csv_field_characters",
    "max_normalized_field_characters",
    "max_source_id_characters",
    "max_price_integer_digits",
    "max_price_fraction_digits",
    "match_threshold",
    "review_threshold",
    "minimum_bilateral_margin",
    "minimum_evidence_fields",
    "weights",
    "price_zero_score_relative_delta",
    "price_contradiction_relative_delta",
    "brand_contradiction_below",
    "name_support_at_least",
}
_WEIGHT_KEYS = {"name", "brand", "sku", "price"}


@dataclass(frozen=True)
class MatchingConfig:
    schema_version: int
    normalization_version: str
    blocking_version: str
    scoring_version: str
    decision_version: str
    max_catalog_records: int
    max_pair_count: int
    max_candidates_per_left: int
    max_csv_file_bytes: int
    max_csv_field_characters: int
    max_normalized_field_characters: int
    max_source_id_characters: int
    max_price_integer_digits: int
    max_price_fraction_digits: int
    match_threshold: Fraction
    review_threshold: Fraction
    minimum_bilateral_margin: Fraction
    minimum_evidence_fields: int
    weights: dict[str, Fraction]
    price_zero_score_relative_delta: Fraction
    price_contradiction_relative_delta: Fraction
    brand_contradiction_below: Fraction
    name_support_at_least: Fraction


def _positive_int(raw: dict[str, Any], key: str) -> int:
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidDataError(f"{key} must be a positive integer")
    return value


def _probability(raw: dict[str, Any], key: str, *, positive: bool = False) -> Fraction:
    value = raw[key]
    if not isinstance(value, str):
        raise InvalidDataError(f"{key} must be a decimal string")
    if not re.fullmatch(r"(?:0(?:\.\d+)?|1(?:\.0+)?)", value):
        raise InvalidDataError(f"{key} is not a plain unit-interval decimal")
    try:
        decimal = Decimal(value)
    except InvalidOperation as exc:
        raise InvalidDataError(f"{key} is not a decimal") from exc
    exponent = decimal.as_tuple().exponent
    if not decimal.is_finite() or not isinstance(exponent, int) or exponent > 0:
        raise InvalidDataError(f"{key} is not a canonical decimal")
    result = Fraction(decimal)
    if result < 0 or result > 1 or (positive and result == 0):
        raise InvalidDataError(f"{key} is outside its allowed range")
    return result


def config_from_mapping(raw: dict[str, Any]) -> MatchingConfig:
    if set(raw) != _CONFIG_KEYS:
        raise InvalidDataError("matching configuration keys differ from schema")
    if isinstance(raw["schema_version"], bool) or raw["schema_version"] != 1:
        raise InvalidDataError("unsupported matching configuration schema")
    versions = {}
    for key in (
        "normalization_version",
        "blocking_version",
        "scoring_version",
        "decision_version",
    ):
        value = raw[key]
        if not isinstance(value, str) or not value:
            raise InvalidDataError(f"{key} must be a non-empty string")
        versions[key] = value
    if versions != {
        "normalization_version": "norm-v1",
        "blocking_version": "block-v1",
        "scoring_version": "score-v1",
        "decision_version": "decision-v1",
    }:
        raise InvalidDataError("unsupported matching component version")
    integer_keys = (
        "max_catalog_records",
        "max_pair_count",
        "max_candidates_per_left",
        "max_csv_file_bytes",
        "max_csv_field_characters",
        "max_normalized_field_characters",
        "max_source_id_characters",
        "max_price_integer_digits",
        "max_price_fraction_digits",
        "minimum_evidence_fields",
    )
    integers = {key: _positive_int(raw, key) for key in integer_keys}
    if (
        integers["max_candidates_per_left"] > integers["max_catalog_records"]
        or integers["minimum_evidence_fields"] > 4
        or integers["max_source_id_characters"] > integers["max_csv_field_characters"]
    ):
        raise InvalidDataError("integer bounds are mutually inconsistent")
    weights_raw = raw["weights"]
    if not isinstance(weights_raw, dict) or set(weights_raw) != _WEIGHT_KEYS:
        raise InvalidDataError("weight keys differ from schema")
    weights = {key: _probability(weights_raw, key, positive=True) for key in sorted(_WEIGHT_KEYS)}
    if sum(weights.values(), Fraction()) != 1:
        raise InvalidDataError("weights must sum exactly to one")
    match_threshold = _probability(raw, "match_threshold")
    review_threshold = _probability(raw, "review_threshold")
    if match_threshold < review_threshold:
        raise InvalidDataError("MATCH threshold must be at least REVIEW threshold")
    price_zero = _probability(raw, "price_zero_score_relative_delta", positive=True)
    price_contradiction = _probability(raw, "price_contradiction_relative_delta")
    if price_contradiction <= price_zero:
        raise InvalidDataError("price contradiction bound must exceed zero-score bound")
    return MatchingConfig(
        schema_version=1,
        normalization_version=versions["normalization_version"],
        blocking_version=versions["blocking_version"],
        scoring_version=versions["scoring_version"],
        decision_version=versions["decision_version"],
        max_catalog_records=integers["max_catalog_records"],
        max_pair_count=integers["max_pair_count"],
        max_candidates_per_left=integers["max_candidates_per_left"],
        max_csv_file_bytes=integers["max_csv_file_bytes"],
        max_csv_field_characters=integers["max_csv_field_characters"],
        max_normalized_field_characters=integers["max_normalized_field_characters"],
        max_source_id_characters=integers["max_source_id_characters"],
        max_price_integer_digits=integers["max_price_integer_digits"],
        max_price_fraction_digits=integers["max_price_fraction_digits"],
        match_threshold=match_threshold,
        review_threshold=review_threshold,
        minimum_bilateral_margin=_probability(raw, "minimum_bilateral_margin"),
        minimum_evidence_fields=integers["minimum_evidence_fields"],
        weights=weights,
        price_zero_score_relative_delta=price_zero,
        price_contradiction_relative_delta=price_contradiction,
        brand_contradiction_below=_probability(raw, "brand_contradiction_below"),
        name_support_at_least=_probability(raw, "name_support_at_least"),
    )


def load_config(path: Path) -> MatchingConfig:
    return config_from_mapping(read_json_object(path))


_DEFAULT_CONFIG_JSON = """{
  "schema_version": 1,
  "normalization_version": "norm-v1",
  "blocking_version": "block-v1",
  "scoring_version": "score-v1",
  "decision_version": "decision-v1",
  "max_catalog_records": 100,
  "max_pair_count": 10000,
  "max_candidates_per_left": 20,
  "max_csv_file_bytes": 1000000,
  "max_csv_field_characters": 4096,
  "max_normalized_field_characters": 4096,
  "max_source_id_characters": 128,
  "max_price_integer_digits": 10,
  "max_price_fraction_digits": 2,
  "match_threshold": "0.86",
  "review_threshold": "0.62",
  "minimum_bilateral_margin": "0.08",
  "minimum_evidence_fields": 2,
  "weights": {
    "name": "0.45",
    "brand": "0.20",
    "sku": "0.25",
    "price": "0.10"
  },
  "price_zero_score_relative_delta": "0.20",
  "price_contradiction_relative_delta": "0.35",
  "brand_contradiction_below": "0.40",
  "name_support_at_least": "0.85"
}
"""


def default_config_bytes() -> bytes:
    return _DEFAULT_CONFIG_JSON.encode("utf-8")


def default_config() -> MatchingConfig:
    raw = json.loads(_DEFAULT_CONFIG_JSON)
    if not isinstance(raw, dict):  # pragma: no cover - constant guarded by tests
        raise AssertionError("default configuration is not an object")
    return config_from_mapping(raw)


@dataclass(frozen=True)
class Score:
    name: Fraction | None
    brand: Fraction | None
    sku: Fraction | None
    price: Fraction | None
    total: Fraction
    missing_components: tuple[str, ...]
    contradictions: tuple[str, ...]

    @property
    def evidence_count(self) -> int:
        return sum(value is not None for value in (self.name, self.brand, self.sku, self.price))


def exact_sequence_ratio(left: str, right: str) -> Fraction:
    blocks = SequenceMatcher(None, left, right, autojunk=False).get_matching_blocks()
    matching_characters = sum(block.size for block in blocks)
    denominator = len(left) + len(right)
    return Fraction(2 * matching_characters, denominator) if denominator else Fraction(1)


def _normalized(
    record: dict[str, str] | NormalizedRecord, config: MatchingConfig
) -> NormalizedRecord:
    if isinstance(record, NormalizedRecord):
        return record
    return normalize_record(
        record,
        max_source_id_characters=config.max_source_id_characters,
        max_normalized_field_characters=config.max_normalized_field_characters,
        max_price_integer_digits=config.max_price_integer_digits,
        max_price_fraction_digits=config.max_price_fraction_digits,
    )


def score_pair(
    left: dict[str, str] | NormalizedRecord,
    right: dict[str, str] | NormalizedRecord,
    config: MatchingConfig | None = None,
) -> Score:
    config = config or default_config()
    a, b = _normalized(left, config), _normalized(right, config)
    name: Fraction | None = None
    if a.name is not None and b.name is not None:
        left_tokens, right_tokens = set(a.name.split()), set(b.name.split())
        union = left_tokens | right_tokens
        overlap = Fraction(len(left_tokens & right_tokens), len(union)) if union else Fraction()
        name = Fraction(3, 5) * exact_sequence_ratio(a.name, b.name) + Fraction(2, 5) * overlap
    brand = (
        exact_sequence_ratio(a.brand, b.brand)
        if a.brand is not None and b.brand is not None
        else None
    )
    sku = Fraction(int(a.sku == b.sku)) if a.sku is not None and b.sku is not None else None
    price: Fraction | None = None
    relative_delta: Fraction | None = None
    if a.price is not None and b.price is not None:
        left_price, right_price = Fraction(a.price), Fraction(b.price)
        relative_delta = abs(left_price - right_price) / max(
            abs(left_price), abs(right_price), Fraction(1)
        )
        price = max(
            Fraction(), Fraction(1) - relative_delta / config.price_zero_score_relative_delta
        )
    components = {"name": name, "brand": brand, "sku": sku, "price": price}
    available = [
        (value, config.weights[key]) for key, value in components.items() if value is not None
    ]
    total = (
        sum((value * weight for value, weight in available), Fraction())
        / sum((weight for _, weight in available), Fraction())
        if available
        else Fraction()
    )
    contradictions: list[str] = []
    if a.sku is not None and b.sku is not None and a.sku != b.sku:
        contradictions.append("sku_disagreement")
    if brand is not None and brand < config.brand_contradiction_below:
        contradictions.append("brand_contradiction")
    if relative_delta is not None and relative_delta > config.price_contradiction_relative_delta:
        contradictions.append("price_contradiction")
    if not 0 <= total <= 1:
        raise InvalidDataError("score outside finite unit interval")
    return Score(
        name=name,
        brand=brand,
        sku=sku,
        price=price,
        total=total,
        missing_components=tuple(key for key, value in components.items() if value is None),
        contradictions=tuple(contradictions),
    )
