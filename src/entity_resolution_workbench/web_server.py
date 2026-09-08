"""Loopback-only HTTP wrapper for :mod:`web_api`."""

from __future__ import annotations

import io
import json
import os
import socket
import threading
import time
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import cast

from .web_api import MAX_REQUEST_BODY_BYTES, WorkbenchApi

_SINGLETON_HEADERS = {
    "content-length",
    "content-type",
    "host",
    "origin",
    "transfer-encoding",
    "x-erw-request",
    "x-erw-session",
}
_READ_TIMEOUT_SECONDS = 2.0
_WRITE_TIMEOUT_SECONDS = 1.0


class _DeadlineReader(io.BufferedIOBase):
    def __init__(
        self, stream: io.BufferedReader, connection: socket.socket, deadline: float
    ) -> None:
        self._stream = stream
        self._connection = connection
        self._deadline = deadline
        self._buffer = bytearray()

    def _receive(self, maximum: int) -> bytes:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("request framing deadline exceeded")
        self._connection.settimeout(remaining)
        return self._stream.read1(max(1, maximum))

    def _consume(self, count: int) -> bytes:
        result = bytes(self._buffer[:count])
        del self._buffer[:count]
        return result

    def readline(self, limit: int | None = -1) -> bytes:
        if limit is None:
            limit = -1
        if limit == 0:
            return b""
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0 and (limit < 0 or newline < limit):
                return self._consume(newline + 1)
            if limit >= 0 and len(self._buffer) >= limit:
                return self._consume(limit)
            maximum = 65_536 if limit < 0 else limit - len(self._buffer)
            chunk = self._receive(maximum)
            if not chunk:
                return self._consume(len(self._buffer))
            self._buffer.extend(chunk)

    def read(self, size: int | None = -1) -> bytes:
        if size is None:
            size = -1
        if size == 0:
            return b""
        if size < 0:
            result = bytearray(self._buffer)
            self._buffer.clear()
            while chunk := self._receive(65_536):
                result.extend(chunk)
            return bytes(result)
        result = bytearray()
        while len(result) < size:
            if self._buffer:
                take = min(size - len(result), len(self._buffer))
                result.extend(self._consume(take))
                continue
            chunk = self._receive(min(65_536, size - len(result)))
            if not chunk:
                break
            result.extend(chunk)
        return bytes(result)

    def close(self) -> None:
        self._stream.close()


class _Server(ThreadingHTTPServer):
    api: WorkbenchApi


def _handler() -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self) -> None:
            accepted_at = time.monotonic()
            super().setup()
            original = cast(io.BufferedReader, self.rfile)
            self.rfile = _DeadlineReader(
                original,
                self.connection,
                accepted_at + _READ_TIMEOUT_SECONDS,
            )

        def handle_one_request(self) -> None:
            try:
                super().handle_one_request()
            except OSError:
                self.close_connection = True

        def version_string(self) -> str:
            return "EntityResolutionWorkbench"

        def _handle(self) -> None:
            api = cast(_Server, self.server).api
            if any(len(self.headers.get_all(name, [])) > 1 for name in _SINGLETON_HEADERS):
                status, response_headers, payload = api._error(
                    400,
                    "DUPLICATE_HEADER",
                    "Send each request header once.",
                )
                self._write_payload(status, response_headers, payload)
                return
            headers = dict(self.headers.items())
            if not api._valid_host(headers):
                status, response_headers, payload = api._error(
                    403,
                    "ORIGIN_REJECTED",
                    "Use the local workbench address.",
                )
                self._write_payload(status, response_headers, payload)
                return
            transfer_encoding = self.headers.get("Transfer-Encoding")
            length = self.headers.get("Content-Length")
            if transfer_encoding is not None:
                status, response_headers, payload = api._error(
                    400,
                    "TRANSFER_ENCODING_REJECTED",
                    "Send a complete local request.",
                )
                self._write_payload(status, response_headers, payload)
                return
            length_value: int | None = None
            invalid_length = False
            too_large = False
            if length is not None:
                invalid_length = not length.isascii() or not length.isdecimal()
                if not invalid_length:
                    if len(length) > len(str(MAX_REQUEST_BODY_BYTES)):
                        too_large = True
                    else:
                        length_value = int(length)
                        too_large = length_value > MAX_REQUEST_BODY_BYTES
            if invalid_length or too_large:
                status, response_headers, payload = api._error(
                    413 if too_large else 400,
                    "BODY_TOO_LARGE" if too_large else "INVALID_FRAMING",
                    "Use a smaller catalogue." if too_large else "Send a complete local request.",
                )
                self._write_payload(status, response_headers, payload)
                return
            static = self._static_response()
            if static is not None:
                self._write(*static)
                return
            body = b""
            try:
                if length_value is not None:
                    body = self.rfile.read(length_value)
            except TimeoutError:
                status, response_headers, payload = api._error(
                    408,
                    "REQUEST_TIMEOUT",
                    "Send the complete local request and try again.",
                )
                self._write_payload(status, response_headers, payload)
                return
            except OSError:
                self.close_connection = True
                return
            if length_value is not None and len(body) != length_value:
                status, response_headers, payload = api._error(
                    408,
                    "REQUEST_TIMEOUT",
                    "Send the complete local request and try again.",
                )
                self._write_payload(status, response_headers, payload)
                return
            status, response_headers, payload = api.handle(self.command, self.path, headers, body)
            self._write_payload(status, response_headers, payload)

        def _write_payload(
            self, status: int, response_headers: dict[str, str], payload: object
        ) -> None:
            if isinstance(payload, bytes):
                rendered = payload
            elif payload is None:
                rendered = b""
            else:
                rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            self._write(status, response_headers, rendered)

        def _static_response(self) -> tuple[int, dict[str, str], bytes] | None:
            if self.command != "GET":
                return None
            routes = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/assets/favicon.svg": ("favicon.svg", "image/svg+xml"),
                "/assets/styles.css": ("styles.css", "text/css; charset=utf-8"),
                "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
            }
            route = routes.get(self.path)
            if route is None:
                return None
            filename, content_type = route
            payload = (
                resources.files("entity_resolution_workbench")
                .joinpath("web_assets", filename)
                .read_bytes()
            )
            return (
                200,
                cast(_Server, self.server).api._headers({"Content-Type": content_type}),
                payload,
            )

        def _write(self, status: int, response_headers: dict[str, str], rendered: bytes) -> None:
            self.close_connection = True
            try:
                self.connection.settimeout(_WRITE_TIMEOUT_SECONDS)
                self.send_response(status)
                for key, value in response_headers.items():
                    self.send_header(key, value)
                if status != 204:
                    self.send_header("Content-Length", str(len(rendered)))
                self.end_headers()
                if rendered and self.command != "HEAD":
                    self.wfile.write(rendered)
            except OSError:
                self.close_connection = True

        do_GET = _handle
        do_POST = _handle
        do_PUT = _handle
        do_DELETE = _handle
        do_HEAD = _handle
        do_OPTIONS = _handle
        do_PATCH = _handle

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return Handler


def create_server(*, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """Create, but do not start, a local IPv4 loopback server."""
    if host != "127.0.0.1":
        raise ValueError("workbench host must be the IPv4 loopback literal")
    server = _Server((host, port), _handler())
    server.api = WorkbenchApi(port=server.server_port)
    return server


def start_in_thread(server: ThreadingHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def serve(*, host: str = "127.0.0.1", port: int = 8765, ready_fd: int | None = None) -> None:
    server = create_server(host=host, port=port)
    try:
        if ready_fd is not None:
            os.write(ready_fd, f"ERW_READY {server.server_port}\n".encode("ascii"))
            os.close(ready_fd)
            ready_fd = None
        server.serve_forever()
    finally:
        if ready_fd is not None:
            with suppress(OSError):
                os.close(ready_fd)
        server.server_close()
