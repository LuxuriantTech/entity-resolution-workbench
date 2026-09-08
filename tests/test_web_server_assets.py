from __future__ import annotations

import json
import socket
import struct
import threading
import time
from http.client import HTTPConnection, HTTPResponse
from typing import cast

import pytest
from conftest import module


def _get(connection: HTTPConnection, path: str) -> tuple[HTTPResponse, bytes]:
    connection.request("GET", path)
    response = connection.getresponse()
    return response, response.read()


def test_loopback_server_serves_only_explicit_packaged_assets() -> None:
    server_module = module("web_server")
    server = server_module.create_server(port=0)
    thread = server_module.start_in_thread(server)
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        response, html = _get(connection, "/")
        assert response.status == 200
        assert response.getheader("Content-Type") == "text/html; charset=utf-8"
        assert b"Entity Resolution Workbench" in html

        response, css = _get(connection, "/assets/styles.css")
        assert response.status == 200
        assert response.getheader("Content-Type") == "text/css; charset=utf-8"
        assert b":focus-visible" in css

        response, javascript = _get(connection, "/assets/app.js")
        assert response.status == 200
        assert response.getheader("Content-Type") == "text/javascript; charset=utf-8"
        assert b"/api/v1/session/run" in javascript

        for path in (
            "/assets/../web_api.py",
            "/assets/%2e%2e/web_api.py",
            "/.git/config",
            "/src/entity_resolution_workbench/web_server.py",
            "/unknown",
        ):
            response, payload = _get(connection, path)
            assert response.status == 404, (path, payload)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_static_responses_have_restrictive_headers_and_no_cors() -> None:
    server_module = module("web_server")
    server = server_module.create_server(port=0)
    thread = server_module.start_in_thread(server)
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        response, _ = _get(connection, "/")
        assert response.getheader("Cache-Control") == "no-store"
        assert response.getheader("X-Content-Type-Options") == "nosniff"
        assert response.getheader("Referrer-Policy") == "no-referrer"
        assert response.getheader("X-Frame-Options") == "DENY"
        policy = response.getheader("Content-Security-Policy")
        assert policy is not None
        assert "default-src 'self'" in policy
        assert response.getheader("Access-Control-Allow-Origin") is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_no_content_response_omits_content_length(monkeypatch: pytest.MonkeyPatch) -> None:
    server_module = module("web_server")
    server = server_module.create_server(port=0)
    thread = server_module.start_in_thread(server)
    origin = f"http://127.0.0.1:{server.server_port}"
    request_headers = {
        "Content-Type": "application/json",
        "Origin": origin,
        "X-ERW-Request": "local-workbench-v1",
    }
    real_handle = server.api.handle

    def no_content_for_delete(
        method: str, target: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, str], object]:
        if method == "DELETE":
            return 204, server.api._headers(), None
        return cast(tuple[int, dict[str, str], object], real_handle(method, target, headers, body))

    monkeypatch.setattr(server.api, "handle", no_content_for_delete)
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request(
            "POST",
            "/api/v1/session",
            body=json.dumps({"synthetic_only": True, "fixture_id": "catalogue-desk-v1"}).encode(
                "utf-8"
            ),
            headers=request_headers,
        )
        created_response = connection.getresponse()
        assert created_response.status == 201
        session_id = json.loads(created_response.read())["session_id"]

        connection.request(
            "DELETE",
            "/api/v1/session",
            headers={**request_headers, "X-ERW-Session": session_id},
        )
        reset_response = connection.getresponse()
        assert reset_response.status == 204
        assert reset_response.getheader("Content-Length") is None
        assert reset_response.read() == b""
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_reset_response_is_complete_json_acknowledgement() -> None:
    server_module = module("web_server")
    server = server_module.create_server(port=0)
    thread = server_module.start_in_thread(server)
    origin = f"http://127.0.0.1:{server.server_port}"
    request_headers = {
        "Content-Type": "application/json",
        "Origin": origin,
        "X-ERW-Request": "local-workbench-v1",
    }
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request(
            "POST",
            "/api/v1/session",
            body=json.dumps({"synthetic_only": True, "fixture_id": "catalogue-desk-v1"}).encode(
                "utf-8"
            ),
            headers=request_headers,
        )
        created_response = connection.getresponse()
        assert created_response.status == 201
        session_id = json.loads(created_response.read())["session_id"]

        connection.request(
            "DELETE",
            "/api/v1/session",
            headers={**request_headers, "X-ERW-Session": session_id},
        )
        reset_response = connection.getresponse()
        reset_payload = json.loads(reset_response.read())
        assert reset_response.status == 200
        assert reset_response.getheader("Content-Type") == "application/json; charset=utf-8"
        assert reset_payload == {"state": "RESET"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_incomplete_declared_body_times_out_with_bounded_safe_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server_module = module("web_server")
    monkeypatch.setattr(server_module, "_READ_TIMEOUT_SECONDS", 0.05)
    server = server_module.create_server(port=0)
    thread = server_module.start_in_thread(server)
    connection = socket.create_connection(("127.0.0.1", server.server_port), timeout=1)
    try:
        request = (
            f"POST /api/v1/session HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{server.server_port}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 10\r\n"
            "X-ERW-Request: 1\r\n"
            "Connection: close\r\n\r\n"
            "{"
        ).encode("ascii")
        connection.sendall(request)
        response = bytearray()
        while chunk := connection.recv(4096):
            response.extend(chunk)
        assert response.startswith(b"HTTP/1.1 408")
        assert b'"code":"REQUEST_TIMEOUT"' in response
        assert b"Traceback" not in response
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_slow_body_uses_one_absolute_framing_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server_module = module("web_server")
    monkeypatch.setattr(server_module, "_READ_TIMEOUT_SECONDS", 0.2)
    server = server_module.create_server(port=0)
    server_thread = server_module.start_in_thread(server)
    client = socket.create_connection(("127.0.0.1", server.server_port), timeout=2)
    stop = threading.Event()
    try:
        client.sendall(
            (
                "POST /api/v1/session HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{server.server_port}\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: 100\r\n"
                "X-ERW-Request: local-workbench-v1\r\n"
                "Connection: close\r\n\r\n"
                "{"
            ).encode("ascii")
        )

        def drip() -> None:
            for _ in range(4):
                if stop.wait(0.12):
                    return
                try:
                    client.sendall(b"x")
                except OSError:
                    return

        sender = threading.Thread(target=drip, daemon=True)
        started = time.monotonic()
        sender.start()
        response = client.recv(4_096)
        elapsed = time.monotonic() - started
        stop.set()
        sender.join(timeout=1)
        assert response.startswith(b"HTTP/1.1 408")
        assert elapsed < 0.45
    finally:
        stop.set()
        client.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def test_slow_request_line_is_closed_on_the_same_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server_module = module("web_server")
    monkeypatch.setattr(server_module, "_READ_TIMEOUT_SECONDS", 0.2)
    server = server_module.create_server(port=0)
    server_thread = server_module.start_in_thread(server)
    client = socket.create_connection(("127.0.0.1", server.server_port), timeout=2)
    stop = threading.Event()
    try:

        def drip() -> None:
            for byte in b"GET /":
                if stop.wait(0.12):
                    return
                try:
                    client.sendall(bytes([byte]))
                except OSError:
                    return

        sender = threading.Thread(target=drip, daemon=True)
        started = time.monotonic()
        sender.start()
        assert client.recv(4_096) == b""
        elapsed = time.monotonic() - started
        stop.set()
        sender.join(timeout=1)
        assert elapsed < 0.45
    finally:
        stop.set()
        client.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def test_client_resets_never_emit_network_tracebacks(
    capsys: pytest.CaptureFixture[str],
) -> None:
    server_module = module("web_server")
    server = server_module.create_server(port=0)
    server_thread = server_module.start_in_thread(server)
    try:
        for _ in range(12):
            client = socket.create_connection(("127.0.0.1", server.server_port), timeout=1)
            client.sendall(
                (
                    "GET / HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{server.server_port}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii")
            )
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            client.close()
        time.sleep(0.15)
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
    assert "Traceback" not in capsys.readouterr().err
