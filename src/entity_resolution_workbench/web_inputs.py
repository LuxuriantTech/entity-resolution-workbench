"""Bounded, path-free CSV inputs for the local workbench."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import re
import unicodedata
from dataclasses import dataclass

from .common import InvalidDataError, ResourceBoundError
from .scoring import default_config

CANONICAL_FIELDS = ("source_id", "name", "brand", "sku", "category", "price")
MAX_SOURCE_COLUMNS = 32


class WebInputError(InvalidDataError):
    """A safe public validation failure for local CSV input."""


@dataclass(frozen=True)
class UploadPreview:
    display_name: str
    byte_count: int
    sha256: str
    headers: tuple[str, ...]
    rows: tuple[dict[str, str], ...]

    @property
    def row_count(self) -> int:
        return len(self.rows)


def neutral_display_name(value: object) -> str:
    if not isinstance(value, str):
        return "file.csv"
    leaf = re.split(r"[\\/]", value)[-1]
    leaf = "".join(
        char for char in leaf if char.isprintable() and unicodedata.category(char) != "Cf"
    )
    if not leaf or leaf in {".", ".."} or any(char in leaf for char in "<>:\\/"):
        return "file.csv"
    return leaf[:96]


def parse_upload(*, filename: object, content_base64: object) -> UploadPreview:
    if not isinstance(content_base64, str):
        raise WebInputError("upload content is invalid")
    try:
        payload = base64.b64decode(content_base64, validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise WebInputError("upload content is not valid base64") from exc
    config = default_config()
    if len(payload) > config.max_csv_file_bytes:
        raise ResourceBoundError("upload exceeds byte bound")
    if b"\0" in payload:
        raise WebInputError("upload contains NUL")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise WebInputError("upload is not UTF-8") from exc
    previous_limit = csv.field_size_limit()
    csv.field_size_limit(config.max_csv_field_characters)
    clean_rows: list[dict[str, str]] = []
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        headers = tuple(reader.fieldnames or ())
        if not headers or any(not header or not header.strip() for header in headers):
            raise WebInputError("upload header is empty")
        if len(headers) != len(set(headers)):
            raise WebInputError("upload headers are not unique")
        if len(headers) > MAX_SOURCE_COLUMNS:
            raise ResourceBoundError("upload has too many columns")
        for row_index, row in enumerate(reader):
            if row_index >= config.max_catalog_records:
                raise ResourceBoundError("upload has too many rows")
            if set(row) != set(headers) or any(value is None for value in row.values()):
                raise WebInputError("upload row structure is invalid")
            clean_rows.append({header: row[header] for header in headers})
    except csv.Error as exc:
        raise WebInputError("upload CSV structure is invalid") from exc
    finally:
        csv.field_size_limit(previous_limit)
    return UploadPreview(
        neutral_display_name(filename),
        len(payload),
        hashlib.sha256(payload).hexdigest(),
        headers,
        tuple(clean_rows),
    )


def validate_mapping(mapping: object, headers: tuple[str, ...]) -> dict[str, str | None]:
    if not isinstance(mapping, dict) or set(mapping) != set(CANONICAL_FIELDS):
        raise WebInputError("mapping fields are invalid")
    result: dict[str, str | None] = {}
    used: set[str] = set()
    for field in CANONICAL_FIELDS:
        value = mapping[field]
        if value is not None and (
            not isinstance(value, str) or value not in headers or value in used
        ):
            raise WebInputError("mapping values are invalid")
        if isinstance(value, str):
            used.add(value)
        result[field] = value
    if result["source_id"] is None or not any(
        result[field] is not None for field in CANONICAL_FIELDS[1:]
    ):
        raise WebInputError("mapping requires source ID and descriptive field")
    return result


def mapped_rows(preview: UploadPreview, mapping: dict[str, str | None]) -> list[dict[str, str]]:
    return [
        {field: row[column] if column is not None else "" for field, column in mapping.items()}
        for row in preview.rows
    ]
