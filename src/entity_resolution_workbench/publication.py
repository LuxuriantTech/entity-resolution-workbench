from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

from .common import IntegrityError, canonical_bytes, digest_bytes


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_parent(path: Path) -> None:
    missing: list[Path] = []
    cursor = path.parent
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    for directory in reversed(missing):
        _fsync_directory(directory)
        _fsync_directory(directory.parent)


def _preflight(path: Path, data: bytes) -> bool:
    if not path.exists():
        return False
    if path.stat().st_size != len(data):
        raise IntegrityError(f"existing artifact differs: {path.name}")
    with path.open("rb") as handle:
        existing = handle.read(len(data) + 1)
    if existing != data:
        raise IntegrityError(f"existing artifact differs: {path.name}")
    return True


def publish_bytes(path: Path, data: bytes) -> None:
    if _preflight(path, data):
        return
    _ensure_parent(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def publish_many(artifacts: tuple[tuple[Path, bytes], ...]) -> None:
    existing = [_preflight(path, data) for path, data in artifacts]
    for already_present, (path, data) in zip(existing, artifacts, strict=True):
        if not already_present:
            publish_bytes(path, data)


def publish_verified(*, database_path: Path, run_key: str, prediction_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT prediction_digest,prediction_bytes FROM resolution_runs WHERE run_key=?",
            (run_key,),
        ).fetchone()
    if row is None or not isinstance(row[1], bytes) or digest_bytes(row[1]) != row[0]:
        raise IntegrityError("stored prediction payload is invalid")
    publish_bytes(prediction_path, row[1])


def publish_evaluation_verified(
    *, database_path: Path, evaluation_key: str, report_json_path: Path, report_html_path: Path
) -> None:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT report_json_digest,report_html_digest,report_bundle_digest,"
            "report_json,report_html FROM evaluations WHERE evaluation_key=?",
            (evaluation_key,),
        ).fetchone()
    if row is None or not isinstance(row[3], bytes) or not isinstance(row[4], bytes):
        raise IntegrityError("stored evaluation payload is missing")
    json_digest, html_digest = digest_bytes(row[3]), digest_bytes(row[4])
    bundle_digest = digest_bytes(
        canonical_bytes({"html_sha256": html_digest, "json_sha256": json_digest})
    )
    if (json_digest, html_digest, bundle_digest) != row[:3]:
        raise IntegrityError("stored evaluation payload is invalid")
    publish_many(((report_json_path, row[3]), (report_html_path, row[4])))


_publish = publish_bytes
