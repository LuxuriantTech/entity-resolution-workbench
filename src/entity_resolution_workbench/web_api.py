"""Small same-origin API state machine for the local workbench."""

from __future__ import annotations

import base64
import copy
import hashlib
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .common import InvalidDataError, ResourceBoundError, json_object_from_bytes
from .web_adapter import export_csv, resolve_catalogues
from .web_inputs import (
    UploadPreview,
    WebInputError,
    mapped_rows,
    parse_upload,
    validate_mapping,
)

MAX_REQUEST_BODY_BYTES = 2_700_000
MAX_ACTIVE_SESSIONS = 4
SESSION_TTL_SECONDS = 30 * 60
_ERROR_HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; connect-src 'self'; "
        "font-src 'none'; frame-ancestors 'none'; form-action 'self'; "
        "img-src 'self'; media-src 'none'; object-src 'none'; "
        "script-src 'self'; style-src 'self'; worker-src 'none'"
    ),
    "Connection": "close",
}


@dataclass
class _Session:
    left: UploadPreview
    right: UploadPreview
    source_kind: str
    last_accessed: float
    mapping: dict[str, dict[str, str | None]] | None = None
    pairs: list[dict[str, Any]] | None = None


class WorkbenchApi:
    def __init__(self, *, port: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.port = port
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.RLock()
        self._clock = clock

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {**_ERROR_HEADERS, **(extra or {})}

    @staticmethod
    def _header(headers: dict[str, str], name: str) -> str | None:
        wanted = name.casefold()
        return next((value for key, value in headers.items() if key.casefold() == wanted), None)

    def _error(
        self, status: int, code: str, message: str, *, field: str | None = None
    ) -> tuple[int, dict[str, str], dict[str, object]]:
        body: dict[str, object] = {"error": {"code": code, "message": message, "recoverable": True}}
        if field:
            body["error"] = {**body["error"], "field": field}  # type: ignore[dict-item]
        return status, self._headers(), body

    def _valid_host(self, headers: dict[str, str]) -> bool:
        return self._header(headers, "Host") == f"127.0.0.1:{self.port}"

    def _json_body(self, headers: dict[str, str], body: bytes) -> dict[str, Any]:
        if (self._header(headers, "Content-Type") or "").split(";", 1)[
            0
        ].strip() != "application/json":
            raise WebInputError("expected JSON request")
        length = self._header(headers, "Content-Length")
        if length is None:
            raise LookupError("length required")
        if not length.isascii() or not length.isdecimal() or int(length) != len(body):
            raise WebInputError("request length is invalid")
        return json_object_from_bytes(body, max_bytes=MAX_REQUEST_BODY_BYTES)

    @staticmethod
    def _preview(preview: UploadPreview) -> dict[str, object]:
        return {
            "display_name": preview.display_name,
            "byte_count": preview.byte_count,
            "sha256": preview.sha256,
            "headers": list(preview.headers),
            "row_count": preview.row_count,
        }

    @staticmethod
    def _fixture_previews() -> tuple[UploadPreview, UploadPreview]:
        root = resources.files("entity_resolution_workbench").joinpath(
            "web_fixtures/catalogue-desk-v1"
        )
        manifest = json_object_from_bytes(root.joinpath("manifest.json").read_bytes())
        if (
            set(manifest) != {"description", "files", "fixture_id", "schema_version", "source"}
            or manifest["fixture_id"] != "catalogue-desk-v1"
        ):
            raise WebInputError("fixture manifest is invalid")
        files = manifest["files"]
        if not isinstance(files, dict) or set(files) != {"left", "right"}:
            raise WebInputError("fixture manifest is invalid")
        previews: list[UploadPreview] = []
        for side in ("left", "right"):
            entry = files[side]
            if (
                not isinstance(entry, dict)
                or set(entry) != {"path", "rows", "sha256"}
                or entry["path"] != f"{side}.csv"
            ):
                raise WebInputError("fixture manifest is invalid")
            content = root.joinpath(entry["path"]).read_bytes()
            if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise WebInputError("fixture integrity check failed")
            preview = parse_upload(
                filename=entry["path"], content_base64=base64.b64encode(content).decode("ascii")
            )
            if preview.row_count != entry["rows"]:
                raise WebInputError("fixture manifest is invalid")
            previews.append(preview)
        return previews[0], previews[1]

    def _session(self, headers: dict[str, str]) -> tuple[str, _Session] | None:
        self._purge_expired_sessions()
        value = self._header(headers, "X-ERW-Session")
        if not value or len(value) > 128:
            return None
        session = self._sessions.get(value)
        if session is not None:
            session.last_accessed = self._clock()
        return (value, session) if session else None

    def _purge_expired_sessions(self) -> None:
        cutoff = self._clock() - SESSION_TTL_SECONDS
        expired = [
            identifier
            for identifier, session in self._sessions.items()
            if session.last_accessed < cutoff
        ]
        for identifier in expired:
            del self._sessions[identifier]

    def handle(
        self, method: str, target: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, str], Any]:
        with self._lock:
            status, response_headers, payload = self._handle_locked(method, target, headers, body)
            return status, dict(response_headers), copy.deepcopy(payload)

    def _handle_locked(
        self, method: str, target: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, str], Any]:
        if not self._valid_host(headers):
            return self._error(403, "ORIGIN_REJECTED", "Use the local workbench address.")
        if method not in {"GET", "POST", "PUT", "DELETE"}:
            status, response_headers, payload = self._error(
                405, "METHOD_NOT_ALLOWED", "Use a supported local workbench action."
            )
            response_headers["Allow"] = "GET, POST, PUT, DELETE"
            return status, response_headers, payload
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or not parsed.path.startswith("/api/v1/"):
            return self._error(404, "NOT_FOUND", "This local route does not exist.")
        if len(body) > MAX_REQUEST_BODY_BYTES:
            return self._error(413, "BODY_TOO_LARGE", "Use smaller catalogues.")
        if self._header(headers, "Transfer-Encoding"):
            return self._error(400, "TRANSFER_ENCODING_REJECTED", "Send a complete local request.")
        if method in {"POST", "PUT", "DELETE"}:
            origin = self._header(headers, "Origin")
            if self._header(headers, "X-ERW-Request") != "local-workbench-v1" or (
                origin is not None and origin != f"http://127.0.0.1:{self.port}"
            ):
                return self._error(403, "ORIGIN_REJECTED", "Use the local workbench address.")
        try:
            return self._dispatch(
                method, parsed.path, parse_qs(parsed.query, keep_blank_values=True), headers, body
            )
        except LookupError:
            return self._error(411, "LENGTH_REQUIRED", "Send a complete JSON request.")
        except ResourceBoundError:
            return self._error(413, "INPUT_TOO_LARGE", "Use a smaller catalogue.")
        except (WebInputError, InvalidDataError, ValueError):
            return self._error(
                422, "INVALID_INPUT", "Correct the highlighted catalogue or mapping and try again."
            )
        except Exception:  # no local details in browser responses
            return self._error(500, "SERVER_ERROR", "Reset the local session and try again.")

    def _dispatch(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        headers: dict[str, str],
        body: bytes,
    ) -> tuple[int, dict[str, str], Any]:
        if method == "GET" and path == "/api/v1/fixtures":
            return (
                200,
                self._headers(),
                {
                    "fixtures": [
                        {
                            "fixture_id": "catalogue-desk-v1",
                            "description": "Versioned synthetic product catalogue sample.",
                        }
                    ]
                },
            )
        if method == "POST" and path == "/api/v1/session":
            self._purge_expired_sessions()
            if len(self._sessions) >= MAX_ACTIVE_SESSIONS:
                return self._error(
                    429,
                    "SESSION_LIMIT",
                    (
                        "Return to an existing workbench tab and choose Reset, or restart the "
                        "local workbench to discard its temporary sessions."
                    ),
                )
            request = self._json_body(headers, body)
            if request.get("synthetic_only") is not True:
                raise WebInputError("session request is invalid")
            if (
                set(request) == {"synthetic_only", "fixture_id"}
                and request["fixture_id"] == "catalogue-desk-v1"
            ):
                left, right = self._fixture_previews()
                source_kind = "fixture"
            elif set(request) == {"synthetic_only", "uploads"}:
                uploads = request["uploads"]
                if not isinstance(uploads, dict) or set(uploads) != {"left", "right"}:
                    raise WebInputError("two catalogues are required")

                def preview(side: str) -> UploadPreview:
                    entry = uploads[side]
                    if not isinstance(entry, dict) or set(entry) != {"filename", "content_base64"}:
                        raise WebInputError("upload is invalid")
                    return parse_upload(
                        filename=entry["filename"], content_base64=entry["content_base64"]
                    )

                left, right = preview("left"), preview("right")
                source_kind = "upload"
            else:
                raise WebInputError("session request is invalid")
            identifier = secrets.token_urlsafe(24)
            self._sessions[identifier] = _Session(
                left,
                right,
                source_kind,
                last_accessed=self._clock(),
            )
            previews = {"left": self._preview(left), "right": self._preview(right)}
            suggestions = {
                side: self._suggest_mapping(preview.headers)
                for side, preview in (("left", left), ("right", right))
            }
            return (
                201,
                self._headers(),
                {
                    "session_id": identifier,
                    "state": "FILES_READY",
                    "previews": previews,
                    "suggested_mapping": suggestions,
                    "synthetic_warning": (
                        "Use synthetic product catalogue data only. "
                        "This tool does not detect personal data."
                    ),
                },
            )
        current = self._session(headers)
        if current is None:
            return self._error(404, "SESSION_NOT_FOUND", "Start a new local session.")
        identifier, session = current
        if method == "PUT" and path == "/api/v1/session/mapping":
            request = self._json_body(headers, body)
            if (
                set(request) != {"mapping"}
                or not isinstance(request["mapping"], dict)
                or set(request["mapping"]) != {"left", "right"}
            ):
                raise WebInputError("mapping is invalid")
            session.mapping = {
                "left": validate_mapping(request["mapping"]["left"], session.left.headers),
                "right": validate_mapping(request["mapping"]["right"], session.right.headers),
            }
            session.pairs = None
            return 200, self._headers(), {"session_id": identifier, "state": "MAPPED"}
        if method == "POST" and path == "/api/v1/session/run":
            if self._json_body(headers, body) != {}:
                raise WebInputError("run request must be empty")
            if session.mapping is None:
                return self._error(409, "MAPPING_REQUIRED", "Map both catalogues before matching.")
            session.pairs = resolve_catalogues(
                mapped_rows(session.left, session.mapping["left"]),
                mapped_rows(session.right, session.mapping["right"]),
            )
            return 200, self._headers(), self._result_page(session, query)
        if method == "GET" and path == "/api/v1/session/results":
            if session.pairs is None:
                return self._error(
                    409, "RESULTS_REQUIRED", "Run matching before filtering results."
                )
            return 200, self._headers(), self._result_page(session, query)
        if method == "GET" and path.startswith("/api/v1/session/pairs/"):
            pair_id = unquote(
                path.removeprefix("/api/v1/session/pairs/"), encoding="utf-8", errors="strict"
            )
            pair = self._pair(session, pair_id)
            if pair is None:
                return self._error(404, "PAIR_NOT_FOUND", "Choose a current result pair.")
            return 200, self._headers(), pair
        if method == "PUT" and path.startswith("/api/v1/session/reviews/"):
            request = self._json_body(headers, body)
            pair_id = unquote(
                path.removeprefix("/api/v1/session/reviews/"), encoding="utf-8", errors="strict"
            )
            pair = self._pair(session, pair_id)
            if pair is None:
                return self._error(404, "PAIR_NOT_FOUND", "Choose a current result pair.")
            if pair["decision"] != "REVIEW":
                return self._error(
                    422, "REVIEW_ONLY", "Only ambiguous engine results can be reviewed."
                )
            if set(request) != {"review"} or request["review"] not in {
                "UNREVIEWED",
                "SAME_ENTITY",
                "DIFFERENT_ENTITY",
            }:
                raise WebInputError("review is invalid")
            pair["human_review"] = request["review"]
            pair["review_revision"] = int(pair["review_revision"]) + 1
            return 200, self._headers(), pair
        if method == "GET" and path == "/api/v1/session/export.csv":
            if session.pairs is None:
                return self._error(409, "RESULTS_REQUIRED", "Run matching before exporting.")
            return (
                200,
                self._headers(
                    {
                        "Content-Type": "text/csv; charset=utf-8",
                        "Content-Disposition": "attachment; filename=workbench-results.csv",
                    }
                ),
                export_csv(session.pairs),
            )
        if method == "DELETE" and path == "/api/v1/session":
            del self._sessions[identifier]
            return 200, self._headers(), {"state": "RESET"}
        return self._error(404, "NOT_FOUND", "This local route does not exist.")

    @staticmethod
    def _pair(session: _Session, pair_id: str) -> dict[str, Any] | None:
        if session.pairs is None or not pair_id or "/" in pair_id:
            return None
        return next((pair for pair in session.pairs if pair["pair_id"] == pair_id), None)

    @staticmethod
    def _suggest_mapping(headers: tuple[str, ...]) -> dict[str, str | None]:
        choices = {header.casefold(): header for header in headers}
        return {
            field: choices.get(field.casefold())
            for field in ("source_id", "name", "brand", "sku", "category", "price")
        }

    def _result_page(self, session: _Session, query: dict[str, list[str]]) -> dict[str, object]:
        assert session.pairs is not None
        decision = query.get("decision", [""])[0]
        if decision and decision not in {"MATCH", "REVIEW", "NO_MATCH"}:
            raise WebInputError("decision filter is invalid")
        text = query.get("query", [""])[0].casefold().strip()
        if len(text) > 128:
            raise WebInputError("query is invalid")
        matches = [
            pair
            for pair in session.pairs
            if (not decision or pair["decision"] == decision) and self._matches_query(pair, text)
        ]
        counts = {
            value: sum(pair["decision"] == value for pair in session.pairs)
            for value in ("MATCH", "REVIEW", "NO_MATCH")
        }
        page = self._positive_query(query, "page", 1)
        page_size = self._positive_query(query, "page_size", 25)
        if page_size > 100:
            raise WebInputError("page size is invalid")
        start = (page - 1) * page_size
        return {
            "state": "RESULTS",
            "counts": counts,
            "pairs": matches[start : start + page_size],
            "total": len(matches),
            "page": page,
            "page_size": page_size,
        }

    @staticmethod
    def _positive_query(query: dict[str, list[str]], key: str, default: int) -> int:
        raw = query.get(key, [str(default)])
        if len(raw) != 1 or not raw[0].isdecimal() or int(raw[0]) < 1:
            raise WebInputError("pagination is invalid")
        return int(raw[0])

    @staticmethod
    def _matches_query(pair: dict[str, Any], text: str) -> bool:
        if not text:
            return True
        values = [pair["pair_id"], pair["decision"]]
        for side in ("left", "right"):
            values.extend(str(value or "") for value in pair[side]["raw"].values())
            values.extend(str(value or "") for value in pair[side]["normalized"].values())
        return any(text in value.casefold() for value in values)
