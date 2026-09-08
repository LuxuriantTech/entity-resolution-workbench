from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .common import InvalidDataError, ResourceBoundError, canonical_bytes


def normalize_text(value: str | None, *, max_characters: int = 4096) -> str | None:
    if not value:
        return None
    decomposed = unicodedata.normalize("NFKD", value)
    characters: list[str] = []
    pending_space = False
    for character in decomposed.casefold():
        if unicodedata.combining(character):
            continue
        if character.isalnum():
            if pending_space and characters:
                characters.append(" ")
            characters.append(character)
            pending_space = False
        else:
            pending_space = True
        if len(characters) > max_characters:
            raise ResourceBoundError("normalized field exceeds configured bound")
    normalized = "".join(characters)
    if len(normalized) > max_characters:
        raise ResourceBoundError("normalized field exceeds configured bound")
    return normalized or None


def normalize_sku(value: str | None, *, max_characters: int = 4096) -> str | None:
    if not value:
        return None
    normalized = "".join(character for character in value.casefold() if character.isalnum())
    if len(normalized) > max_characters:
        raise ResourceBoundError("normalized SKU exceeds configured bound")
    return normalized or None


def normalize_price(
    value: str | None, *, max_integer_digits: int = 10, max_fraction_digits: int = 2
) -> Decimal | None:
    if not value:
        return None
    grammar = rf"\d{{1,{max_integer_digits}}}(?:\.\d{{1,{max_fraction_digits}}})?"
    if not re.fullmatch(grammar, value):
        if re.fullmatch(r"\d+(?:\.\d+)?", value):
            raise ResourceBoundError("price exceeds configured digit bounds")
        raise InvalidDataError("price is not a non-negative plain decimal")
    try:
        price = Decimal(value)
    except InvalidOperation as exc:
        raise InvalidDataError("invalid price") from exc
    if not price.is_finite() or price < 0:
        raise InvalidDataError("invalid price")
    return price


def canonical_price(value: Decimal | None) -> str | None:
    if value is None:
        return None
    rendered = format(value.normalize(), "f")
    return "0" if Decimal(rendered) == 0 else rendered


@dataclass(frozen=True)
class NormalizedRecord:
    source_id: str
    name: str | None
    brand: str | None
    sku: str | None
    category: str | None
    price: Decimal | None
    fingerprint: str

    def as_payload(self) -> dict[str, str | None]:
        return {
            "source_id": self.source_id,
            "name": self.name,
            "brand": self.brand,
            "sku": self.sku,
            "category": self.category,
            "price": canonical_price(self.price),
            "fingerprint": self.fingerprint,
        }


def normalize_record(
    row: dict[str, str],
    *,
    max_source_id_characters: int = 128,
    max_normalized_field_characters: int = 4096,
    max_price_integer_digits: int = 10,
    max_price_fraction_digits: int = 2,
) -> NormalizedRecord:
    source_id = row.get("source_id", "")
    if not source_id:
        raise InvalidDataError("source_id is required")
    if len(source_id) > max_source_id_characters:
        raise ResourceBoundError("source_id exceeds configured bound")
    name = normalize_text(row.get("name"), max_characters=max_normalized_field_characters)
    brand = normalize_text(row.get("brand"), max_characters=max_normalized_field_characters)
    sku = normalize_sku(row.get("sku"), max_characters=max_normalized_field_characters)
    category = normalize_text(row.get("category"), max_characters=max_normalized_field_characters)
    price = normalize_price(
        row.get("price"),
        max_integer_digits=max_price_integer_digits,
        max_fraction_digits=max_price_fraction_digits,
    )
    if not any((name, brand, sku, category, price is not None)):
        raise InvalidDataError("blank normalized descriptive row")
    fingerprint_values = [name, brand, sku, category, canonical_price(price)]
    fingerprint = hashlib.sha256(canonical_bytes(fingerprint_values)).hexdigest()
    return NormalizedRecord(source_id, name, brand, sku, category, price, fingerprint)
