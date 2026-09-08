from __future__ import annotations

import csv
import fcntl
import hashlib
import io
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .common import (
    InvalidDataError,
    InvariantError,
    ResourceBoundError,
    canonical_bytes,
    digest_bytes,
)
from .normalization import NormalizedRecord, canonical_price, normalize_record

if TYPE_CHECKING:
    from .scoring import MatchingConfig


ConcurrentWriterError = ResourceBoundError
_HELD_LOCKS: set[Path] = set()
_HEADERS = ("source_id", "name", "brand", "sku", "category", "price")


def _fraction_check(prefix: str, *, nullable: bool) -> str:
    score = f"{prefix}_score"
    numerator = f"{prefix}_numerator"
    denominator = f"{prefix}_denominator"
    missing = f"{score} IS NULL AND {numerator} IS NULL AND {denominator} IS NULL"
    valid = (
        f"{score} IS NOT NULL AND {numerator} IS NOT NULL AND {denominator} IS NOT NULL "
        f"AND typeof({numerator})='text' AND typeof({denominator})='text' "
        f"AND length({numerator})>0 AND {numerator} NOT GLOB '*[^0-9]*' "
        f"AND ({numerator}='0' OR substr({numerator},1,1)!='0') "
        f"AND length({denominator})>0 AND {denominator} NOT GLOB '*[^0-9]*' "
        f"AND {denominator}!='0' AND substr({denominator},1,1)!='0' "
        f"AND (length({numerator})<length({denominator}) OR "
        f"(length({numerator})=length({denominator}) AND "
        f"{numerator} COLLATE BINARY<={denominator} COLLATE BINARY)) "
        f"AND {score}={numerator}||'/'||{denominator}"
    )
    return f"CHECK((({missing}) OR ({valid})))" if nullable else f"CHECK(({valid}))"


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path, isolation_level=None)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=0")
    return connection


def _schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=0")
    statements = (
        "CREATE TABLE IF NOT EXISTS ingestion_batches ("
        "id INTEGER PRIMARY KEY, catalog_role TEXT NOT NULL, content_sha256 TEXT NOT NULL, "
        "row_count INTEGER NOT NULL CHECK(row_count >= 0), "
        "UNIQUE(catalog_role, content_sha256))",
        "CREATE TABLE IF NOT EXISTS source_records ("
        "id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES ingestion_batches(id), "
        "source_id TEXT NOT NULL, raw_json TEXT NOT NULL, normalized_name TEXT, "
        "normalized_brand TEXT, normalized_sku TEXT, normalized_category TEXT, "
        "normalized_price TEXT, fingerprint TEXT NOT NULL, UNIQUE(batch_id, source_id))",
        "CREATE TABLE IF NOT EXISTS resolution_runs ("
        "run_key TEXT PRIMARY KEY, observed_manifest_sha256 TEXT, config_sha256 TEXT, "
        "source_bundle_digest TEXT, identity_envelope_json TEXT NOT NULL, "
        "prediction_digest TEXT NOT NULL UNIQUE, prediction_bytes BLOB NOT NULL, "
        "state TEXT NOT NULL CHECK(state='complete'))",
        "CREATE TABLE IF NOT EXISTS run_batches ("
        "run_key TEXT NOT NULL REFERENCES resolution_runs(run_key), "
        "batch_id INTEGER NOT NULL REFERENCES ingestion_batches(id), "
        "split TEXT NOT NULL CHECK(split IN ('calibration','validation')), "
        "role TEXT NOT NULL CHECK(role IN ('supplier_a','supplier_b')), "
        "PRIMARY KEY(run_key, split, role), UNIQUE(run_key, batch_id))",
        "CREATE TABLE IF NOT EXISTS pair_scores ("
        "run_key TEXT NOT NULL REFERENCES resolution_runs(run_key), "
        "split TEXT NOT NULL CHECK(split IN ('calibration','validation')), "
        "left_record_id INTEGER NOT NULL REFERENCES source_records(id), "
        "right_record_id INTEGER NOT NULL REFERENCES source_records(id), "
        "left_id TEXT NOT NULL, right_id TEXT NOT NULL, candidate INTEGER NOT NULL "
        "CHECK(candidate IN (0,1)), block_reasons_json TEXT NOT NULL, "
        "name_score TEXT, name_numerator TEXT, name_denominator TEXT, "
        "brand_score TEXT, brand_numerator TEXT, brand_denominator TEXT, "
        "sku_score TEXT, sku_numerator TEXT, sku_denominator TEXT, "
        "price_score TEXT, price_numerator TEXT, price_denominator TEXT, "
        "total_score TEXT NOT NULL, total_numerator TEXT NOT NULL, "
        "total_denominator TEXT NOT NULL, evidence_count INTEGER NOT NULL "
        "CHECK(evidence_count BETWEEN 0 AND 4), "
        "contradictions_json TEXT NOT NULL, "
        f"{_fraction_check('name', nullable=True)}, "
        f"{_fraction_check('brand', nullable=True)}, "
        f"{_fraction_check('sku', nullable=True)}, "
        f"{_fraction_check('price', nullable=True)}, "
        f"{_fraction_check('total', nullable=False)}, "
        "PRIMARY KEY(run_key,split,left_record_id,right_record_id), "
        "UNIQUE(run_key,split,left_id,right_id))",
        "CREATE TABLE IF NOT EXISTS decisions ("
        "run_key TEXT NOT NULL, split TEXT NOT NULL, left_record_id INTEGER NOT NULL, "
        "right_record_id INTEGER NOT NULL, left_id TEXT NOT NULL, right_id TEXT NOT NULL, "
        "decision TEXT NOT NULL CHECK(decision IN ('MATCH','REVIEW','NO_MATCH')), "
        "left_rank INTEGER NOT NULL CHECK(left_rank >= 1), "
        "right_rank INTEGER NOT NULL CHECK(right_rank >= 1), "
        "left_margin TEXT NOT NULL, right_margin TEXT NOT NULL, explanation_json TEXT NOT NULL, "
        "PRIMARY KEY(run_key,split,left_record_id,right_record_id), "
        "FOREIGN KEY(run_key,split,left_record_id,right_record_id) "
        "REFERENCES pair_scores(run_key,split,left_record_id,right_record_id))",
        "CREATE TABLE IF NOT EXISTS canonical_entities ("
        "run_key TEXT NOT NULL REFERENCES resolution_runs(run_key), canonical_key TEXT NOT NULL, "
        "kind TEXT NOT NULL CHECK(kind IN ('matched','singleton')), "
        "PRIMARY KEY(run_key,canonical_key))",
        "CREATE TABLE IF NOT EXISTS canonical_members ("
        "run_key TEXT NOT NULL, canonical_key TEXT NOT NULL, "
        "source_record_id INTEGER NOT NULL REFERENCES source_records(id), "
        "source_id TEXT NOT NULL, split TEXT NOT NULL "
        "CHECK(split IN ('calibration','validation')), "
        "role TEXT NOT NULL CHECK(role IN ('supplier_a','supplier_b')), "
        "PRIMARY KEY(run_key,source_record_id), "
        "FOREIGN KEY(run_key,canonical_key) REFERENCES canonical_entities(run_key,canonical_key))",
        "CREATE TABLE IF NOT EXISTS evaluations ("
        "evaluation_key TEXT PRIMARY KEY, run_key TEXT REFERENCES resolution_runs(run_key), "
        "truth_manifest_digest TEXT, truth_bundle_digest TEXT NOT NULL UNIQUE, "
        "config_digest TEXT, prediction_digest TEXT, identity_envelope_json TEXT NOT NULL, "
        "metrics_json TEXT NOT NULL, report_json_digest TEXT NOT NULL, "
        "report_html_digest TEXT NOT NULL, report_bundle_digest TEXT NOT NULL, "
        "report_json BLOB NOT NULL, report_html BLOB NOT NULL)",
        "CREATE TABLE IF NOT EXISTS audit_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, run_key TEXT REFERENCES resolution_runs(run_key), "
        "evaluation_key TEXT REFERENCES evaluations(evaluation_key), event_type TEXT NOT NULL, "
        "payload_json TEXT NOT NULL)",
    )
    for statement in statements:
        conn.execute(statement)


@dataclass(frozen=True)
class Batch:
    batch_id: int


@dataclass(frozen=True)
class CompleteRun:
    run_key: str


@dataclass(frozen=True)
class CompleteEvaluation:
    evaluation_key: str
    report_json_digest: str = ""
    report_html_digest: str = ""
    report_bundle_digest: str = ""
    metrics_by_split: dict[str, Any] | None = None


@dataclass(frozen=True)
class PreparedRecord:
    raw: dict[str, str]
    normalized: NormalizedRecord


@dataclass(frozen=True)
class PreparedCatalog:
    path: Path
    content_sha256: str
    records: tuple[PreparedRecord, ...]


@dataclass(frozen=True)
class StoredBatch:
    batch_id: int
    record_ids: dict[str, int]
    inserted: bool


def _default_config() -> MatchingConfig:
    from .scoring import default_config

    return default_config()


def prepare_catalog(path: Path, config: MatchingConfig | None = None) -> PreparedCatalog:
    config = config or _default_config()
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    byte_count = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            byte_count += len(chunk)
            if byte_count > config.max_csv_file_bytes:
                raise ResourceBoundError("CSV exceeds configured byte bound")
            digest.update(chunk)
            chunks.append(chunk)
    data = b"".join(chunks)
    if b"\0" in data:
        raise InvalidDataError("CSV contains NUL")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidDataError("CSV is not UTF-8") from exc
    previous_limit = csv.field_size_limit()
    csv.field_size_limit(config.max_csv_field_characters)
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if tuple(reader.fieldnames or ()) != _HEADERS:
            raise InvalidDataError("CSV headers differ from the frozen schema")
        raw_rows = list(reader)
    except (csv.Error, OverflowError) as exc:
        raise ResourceBoundError("CSV field exceeds configured bound") from exc
    finally:
        csv.field_size_limit(previous_limit)
    if len(raw_rows) > config.max_catalog_records:
        raise ResourceBoundError("catalogue exceeds configured record bound")
    prepared: list[PreparedRecord] = []
    for raw in raw_rows:
        if set(raw) != set(_HEADERS) or any(value is None for value in raw.values()):
            raise InvalidDataError("CSV row does not match the frozen schema")
        row = {key: raw[key] for key in _HEADERS}
        if any(len(value) > config.max_csv_field_characters for value in row.values()):
            raise ResourceBoundError("CSV field exceeds configured character bound")
        normalized = normalize_record(
            row,
            max_source_id_characters=config.max_source_id_characters,
            max_normalized_field_characters=config.max_normalized_field_characters,
            max_price_integer_digits=config.max_price_integer_digits,
            max_price_fraction_digits=config.max_price_fraction_digits,
        )
        prepared.append(PreparedRecord(row, normalized))
    source_ids = [record.normalized.source_id for record in prepared]
    if len(set(source_ids)) != len(source_ids):
        raise InvalidDataError("duplicate source ID")
    return PreparedCatalog(path, digest.hexdigest(), tuple(prepared))


def _stored_record_payload(record: PreparedRecord) -> tuple[object, ...]:
    normalized = record.normalized
    return (
        normalized.source_id,
        canonical_bytes(record.raw).decode("utf-8"),
        normalized.name,
        normalized.brand,
        normalized.sku,
        normalized.category,
        canonical_price(normalized.price),
        normalized.fingerprint,
    )


def _ingest_prepared(
    conn: sqlite3.Connection, *, catalog_role: str, catalog: PreparedCatalog
) -> StoredBatch:
    event_payload = canonical_bytes(
        {
            "catalog_role": catalog_role,
            "content_sha256": catalog.content_sha256,
            "row_count": len(catalog.records),
        }
    ).decode("utf-8")
    existing = conn.execute(
        "SELECT id,row_count FROM ingestion_batches WHERE catalog_role=? AND content_sha256=?",
        (catalog_role, catalog.content_sha256),
    ).fetchone()
    if existing is not None:
        batch_id, row_count = int(existing[0]), int(existing[1])
        rows = conn.execute(
            "SELECT source_id,raw_json,normalized_name,normalized_brand,normalized_sku,"
            "normalized_category,normalized_price,fingerprint,id FROM source_records "
            "WHERE batch_id=? ORDER BY id",
            (batch_id,),
        ).fetchall()
        expected = [_stored_record_payload(record) for record in catalog.records]
        if row_count != len(expected) or [row[:-1] for row in rows] != expected:
            raise InvariantError("existing ingestion batch differs from its content identity")
        event_count = conn.execute(
            "SELECT count(*) FROM audit_events WHERE run_key IS NULL "
            "AND evaluation_key IS NULL AND event_type='ingestion_complete' "
            "AND payload_json=?",
            (event_payload,),
        ).fetchone()[0]
        if event_count != 1:
            raise InvariantError("existing ingestion completion event differs")
        return StoredBatch(batch_id, {str(row[0]): int(row[-1]) for row in rows}, False)
    cursor = conn.execute(
        "INSERT INTO ingestion_batches(catalog_role,content_sha256,row_count) VALUES(?,?,?)",
        (catalog_role, catalog.content_sha256, len(catalog.records)),
    )
    if cursor.lastrowid is None:
        raise InvariantError("ingestion batch insert returned no id")
    batch_id = int(cursor.lastrowid)
    record_ids: dict[str, int] = {}
    for record in catalog.records:
        normalized = record.normalized
        values = _stored_record_payload(record)
        inserted = conn.execute(
            "INSERT INTO source_records("
            "batch_id,source_id,raw_json,normalized_name,normalized_brand,normalized_sku,"
            "normalized_category,normalized_price,fingerprint) VALUES(?,?,?,?,?,?,?,?,?)",
            (batch_id, *values),
        )
        if inserted.lastrowid is None:
            raise InvariantError("source record insert returned no id")
        record_ids[normalized.source_id] = int(inserted.lastrowid)
    conn.execute(
        "INSERT INTO audit_events(run_key,evaluation_key,event_type,payload_json) VALUES(?,?,?,?)",
        (
            None,
            None,
            "ingestion_complete",
            event_payload,
        ),
    )
    return StoredBatch(batch_id, record_ids, True)


def _check_database(conn: sqlite3.Connection) -> None:
    integrity = conn.execute("PRAGMA integrity_check").fetchone()
    if integrity is None or integrity[0] != "ok":
        raise InvariantError("SQLite integrity check failed")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise InvariantError("SQLite foreign key check failed")


def ingest_csv(
    *, database_path: Path, catalog_role: str, csv_path: Path, config: MatchingConfig | None = None
) -> Batch:
    with exclusive_lock(database_path), _connect(database_path) as conn:
        _schema(conn)
        catalog = prepare_catalog(csv_path, config)
        try:
            conn.execute("BEGIN IMMEDIATE")
            stored = _ingest_prepared(conn, catalog_role=catalog_role, catalog=catalog)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        _check_database(conn)
    return Batch(stored.batch_id)


@contextmanager
def exclusive_lock(database_path: Path) -> Iterator[None]:
    lock_path = database_path.with_suffix(database_path.suffix + ".lock").absolute()
    if lock_path in _HELD_LOCKS:
        raise ConcurrentWriterError("database is locked")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ConcurrentWriterError("database is locked") from exc
        _HELD_LOCKS.add(lock_path)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            _HELD_LOCKS.discard(lock_path)


def with_exclusive_lock(database_path: Path, callback: Callable[[], Any]) -> Any:
    with exclusive_lock(database_path):
        return callback()


def store_complete_run(
    database_path: Path, *, run_key: str, prediction_bytes: bytes
) -> CompleteRun:
    digest = digest_bytes(prediction_bytes)
    with _connect(database_path) as conn:
        _schema(conn)
        try:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute(
                "SELECT prediction_digest,prediction_bytes FROM resolution_runs WHERE run_key=?",
                (run_key,),
            ).fetchone()
            if old is not None:
                if old != (digest, prediction_bytes):
                    raise InvariantError("inconsistent idempotent run")
                conn.commit()
                return CompleteRun(run_key)
            conn.execute(
                "INSERT INTO resolution_runs("
                "run_key,observed_manifest_sha256,config_sha256,source_bundle_digest,"
                "identity_envelope_json,prediction_digest,prediction_bytes,state) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (run_key, None, None, None, "{}\n", digest, prediction_bytes, "complete"),
            )
            conn.execute(
                "INSERT INTO audit_events(run_key,evaluation_key,event_type,payload_json) "
                "VALUES(?,?,?,?)",
                (run_key, None, "run_complete", canonical_bytes({"run_key": run_key}).decode()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        _check_database(conn)
    return CompleteRun(run_key)


def _report_bundle_digest(json_digest: str, html_digest: str) -> str:
    return digest_bytes(canonical_bytes({"html_sha256": html_digest, "json_sha256": json_digest}))


def store_complete_evaluation(
    database_path: Path,
    *,
    evaluation_key: str,
    truth_bundle_digest: str,
    report_json_bytes: bytes,
    report_html_bytes: bytes,
    run_key: str | None = None,
    truth_manifest_digest: str | None = None,
    config_digest: str | None = None,
    prediction_digest: str | None = None,
    identity_envelope: dict[str, object] | None = None,
    metrics_by_split: dict[str, Any] | None = None,
    record_audit_event: bool = False,
) -> CompleteEvaluation:
    json_digest = digest_bytes(report_json_bytes)
    html_digest = digest_bytes(report_html_bytes)
    bundle_digest = _report_bundle_digest(json_digest, html_digest)
    envelope_json = canonical_bytes(identity_envelope or {}).decode("utf-8")
    metrics_json = canonical_bytes(metrics_by_split or {}).decode("utf-8")
    event_payload = canonical_bytes(
        {
            "evaluation_key": evaluation_key,
            "report_bundle_digest": bundle_digest,
            "truth_bundle_digest": truth_bundle_digest,
        }
    ).decode("utf-8")
    with _connect(database_path) as conn:
        _schema(conn)
        try:
            conn.execute("BEGIN IMMEDIATE")
            same_truth = conn.execute(
                "SELECT evaluation_key FROM evaluations WHERE truth_bundle_digest=?",
                (truth_bundle_digest,),
            ).fetchone()
            if same_truth is not None and same_truth[0] != evaluation_key:
                raise InvariantError("truth bundle already evaluated with different inputs")
            existing = conn.execute(
                "SELECT run_key,truth_manifest_digest,config_digest,prediction_digest,"
                "identity_envelope_json,metrics_json,report_json_digest,report_html_digest,"
                "report_bundle_digest,report_json,report_html FROM evaluations "
                "WHERE evaluation_key=?",
                (evaluation_key,),
            ).fetchone()
            values = (
                run_key,
                truth_manifest_digest,
                config_digest,
                prediction_digest,
                envelope_json,
                metrics_json,
                json_digest,
                html_digest,
                bundle_digest,
                report_json_bytes,
                report_html_bytes,
            )
            if existing is not None:
                if existing != values:
                    raise InvariantError("inconsistent idempotent evaluation")
                if record_audit_event:
                    event_count = conn.execute(
                        "SELECT count(*) FROM audit_events WHERE run_key IS ? "
                        "AND evaluation_key=? AND event_type='evaluation_complete' "
                        "AND payload_json=?",
                        (run_key, evaluation_key, event_payload),
                    ).fetchone()[0]
                    if event_count != 1:
                        raise InvariantError("existing evaluation completion event differs")
                conn.commit()
                return CompleteEvaluation(
                    evaluation_key, json_digest, html_digest, bundle_digest, metrics_by_split
                )
            conn.execute(
                "INSERT INTO evaluations("
                "evaluation_key,run_key,truth_manifest_digest,truth_bundle_digest,config_digest,"
                "prediction_digest,identity_envelope_json,metrics_json,report_json_digest,"
                "report_html_digest,report_bundle_digest,report_json,report_html) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    evaluation_key,
                    run_key,
                    truth_manifest_digest,
                    truth_bundle_digest,
                    config_digest,
                    prediction_digest,
                    envelope_json,
                    metrics_json,
                    json_digest,
                    html_digest,
                    bundle_digest,
                    report_json_bytes,
                    report_html_bytes,
                ),
            )
            if record_audit_event:
                conn.execute(
                    "INSERT INTO audit_events(run_key,evaluation_key,event_type,payload_json) "
                    "VALUES(?,?,?,?)",
                    (
                        run_key,
                        evaluation_key,
                        "evaluation_complete",
                        event_payload,
                    ),
                )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise InvariantError("evaluation persistence constraint failed") from exc
        except Exception:
            conn.rollback()
            raise
        _check_database(conn)
    return CompleteEvaluation(
        evaluation_key, json_digest, html_digest, bundle_digest, metrics_by_split
    )


@dataclass(frozen=True)
class CanonicalMember:
    source_record_id: str
    split: str


def canonical_members(database_path: Path, run_key: str) -> list[CanonicalMember]:
    with _connect(database_path) as conn:
        return [
            CanonicalMember(str(row[0]), str(row[1]))
            for row in conn.execute(
                "SELECT source_id,split FROM canonical_members WHERE run_key=? "
                "ORDER BY split,role,source_id",
                (run_key,),
            )
        ]
