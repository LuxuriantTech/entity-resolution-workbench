from __future__ import annotations

import hashlib
import json
from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any


class InvalidDataError(ValueError):
    """Input does not satisfy the frozen protocol."""


class ResourceBoundError(InvalidDataError):
    """Input exceeds a pre-registered resource bound."""


class IntegrityError(InvalidDataError):
    """Content-addressed data does not match its recorded identity."""


class InvariantError(IntegrityError):
    """Persisted state violates an internal invariant."""


def canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidDataError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_bounded_bytes(path: Path, *, max_bytes: int, description: str) -> bytes:
    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ResourceBoundError(f"{description} exceeds byte bound")
    return data


def json_object_from_bytes(data: bytes, *, max_bytes: int = 1_000_000) -> dict[str, Any]:
    if len(data) > max_bytes:
        raise ResourceBoundError("JSON exceeds byte bound")
    if b"\0" in data:
        raise InvalidDataError("JSON contains NUL")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs)
    except UnicodeDecodeError as exc:
        raise InvalidDataError("JSON is not UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise InvalidDataError("malformed JSON") from exc
    if not isinstance(value, dict):
        raise InvalidDataError("JSON root must be an object")
    return value


def read_json_object(path: Path, *, max_bytes: int = 1_000_000) -> dict[str, Any]:
    data = read_bounded_bytes(path, max_bytes=max_bytes, description="JSON")
    return json_object_from_bytes(data, max_bytes=max_bytes)


def require_sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise InvalidDataError(f"{field} must be a SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise InvalidDataError(f"{field} must be a SHA-256 hex digest") from exc
    return value.lower()


def fraction_text(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def fraction_display(value: Fraction) -> str:
    decimal = Decimal(value.numerator) / Decimal(value.denominator)
    return format(decimal.quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN), "f")


def fraction_payload(value: Fraction | None) -> dict[str, str] | None:
    if value is None:
        return None
    return {"fraction": fraction_text(value), "display": fraction_display(value)}


def parse_fraction_text(value: str) -> Fraction:
    try:
        result = Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise IntegrityError("invalid exact fraction") from exc
    if not 0 <= result <= 1:
        raise IntegrityError("exact fraction outside [0,1]")
    if fraction_text(result) != value:
        raise IntegrityError("non-canonical exact fraction")
    return result
