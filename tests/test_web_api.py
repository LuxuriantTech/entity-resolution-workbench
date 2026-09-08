from __future__ import annotations

import base64
import json
import socket
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from typing import Any, cast
from urllib.parse import quote

import pytest
from conftest import module


def _csv(rows: str) -> str:
    return base64.b64encode(rows.encode("utf-8")).decode("ascii")


def _request(
    api: Any,
    method: str,
    path: str,
    payload: object | None = None,
    session: str | None = None,
) -> tuple[int, dict[str, str], Any]:
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Content-Length": str(len(body)),
        "Host": "127.0.0.1:8765",
    }
    if method in {"POST", "PUT", "DELETE"}:
        headers["Origin"] = "http://127.0.0.1:8765"
        headers["X-ERW-Request"] = "local-workbench-v1"
    if session:
        headers["X-ERW-Session"] = session
    return cast(tuple[int, dict[str, str], Any], api.handle(method, path, headers, body))


def test_api_upload_mapping_run_review_and_reset_visible_real_result() -> None:
    api = module("web_api").WorkbenchApi(port=8765)
    left = "id,title,make,code,amount\nL1,Cafetiere Eclair,Acme,X-1,19.00\n"
    right = "id,title,make,code,amount\nR1,Cafetiere Éclair,Acme,X-1,19.00\n"
    status, _, created = _request(
        api,
        "POST",
        "/api/v1/session",
        {
            "synthetic_only": True,
            "uploads": {
                "left": {"filename": "left.csv", "content_base64": _csv(left)},
                "right": {"filename": "right.csv", "content_base64": _csv(right)},
            },
        },
    )
    assert status == 201
    session = created["session_id"]
    side_mapping = {
        "source_id": "id",
        "name": "title",
        "brand": "make",
        "sku": "code",
        "category": None,
        "price": "amount",
    }
    mapping = {"left": side_mapping, "right": side_mapping}
    assert _request(api, "PUT", "/api/v1/session/mapping", {"mapping": mapping}, session)[0] == 200
    status, _, results = _request(api, "POST", "/api/v1/session/run", {}, session)
    assert set(created) >= {"previews", "suggested_mapping"}
    assert status == 200 and results["pairs"][0]["decision"] == "MATCH"
    reset_status, _, reset_payload = _request(api, "DELETE", "/api/v1/session", None, session)
    assert reset_status == 200
    assert reset_payload == {"state": "RESET"}
    assert _request(api, "GET", "/api/v1/session/results", None, session)[0] == 404


def test_api_fails_closed_for_missing_length_cross_origin_and_nonreview_annotation() -> None:
    api = module("web_api").WorkbenchApi(port=8765)
    body = b"{}"
    assert (
        api.handle(
            "POST",
            "/api/v1/session",
            {
                "Host": "127.0.0.1:8765",
                "Content-Type": "application/json",
                "Origin": "http://127.0.0.1:8765",
                "X-ERW-Request": "local-workbench-v1",
            },
            body,
        )[0]
        == 411
    )
    assert api.handle("GET", "/api/v1/fixtures", {"Host": "example.test:8765"}, b"")[0] == 403


def test_api_reviews_only_engine_review_pairs_and_returns_complete_pair() -> None:
    api = module("web_api").WorkbenchApi(port=8765)
    left = "id,title,make,code,amount\nL1,Cafetiere Eclair,Acme,X-1,19.00\n"
    right = "id,title,make,code,amount\nR1,Cafetiere Eclair,Acme,X-1,19.00\n"
    payload = {
        "synthetic_only": True,
        "uploads": {
            "left": {"filename": "left.csv", "content_base64": _csv(left)},
            "right": {"filename": "right.csv", "content_base64": _csv(right)},
        },
    }
    _, _, created = _request(api, "POST", "/api/v1/session", payload)
    session = created["session_id"]
    mapping = {
        key: {
            "source_id": "id",
            "name": "title",
            "brand": "make",
            "sku": "code",
            "category": None,
            "price": "amount",
        }
        for key in ("left", "right")
    }
    _request(api, "PUT", "/api/v1/session/mapping", {"mapping": mapping}, session)
    _, _, result = _request(api, "POST", "/api/v1/session/run", {}, session)
    pair = result["pairs"][0]
    encoded_pair = quote(pair["pair_id"], safe="")
    assert _request(api, "GET", f"/api/v1/session/pairs/{encoded_pair}", None, session)[0] == 200
    assert (
        _request(
            api,
            "PUT",
            f"/api/v1/session/reviews/{pair['pair_id']}",
            {"review": "SAME_ENTITY"},
            session,
        )[0]
        == 422
    )


def test_loopback_server_exposes_api_with_security_headers() -> None:
    server_module = module("web_server")
    server = server_module.create_server(port=0)
    try:
        port = server.server_port
        thread = server_module.start_in_thread(server)
        connection = HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request("GET", "/api/v1/fixtures")
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("X-Content-Type-Options") == "nosniff"
        assert response.getheader("Cross-Origin-Opener-Policy") == "same-origin"
        assert response.getheader("Cross-Origin-Resource-Policy") == "same-origin"
        assert "Python" not in (response.getheader("Server") or "")
        response.read()
        thread.join(0)
    finally:
        server.shutdown()
        server.server_close()


def test_http_server_rejects_hostile_host_for_static_assets() -> None:
    server_module = module("web_server")
    server = server_module.create_server(port=0)
    thread = server_module.start_in_thread(server)
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request("GET", "/", headers={"Host": "attacker.example"})
        response = connection.getresponse()
        payload = response.read()
        assert response.status == 403
        assert b"Entity Resolution Workbench" not in payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_server_rejects_duplicate_security_headers() -> None:
    server_module = module("web_server")
    server = server_module.create_server(port=0)
    thread = server_module.start_in_thread(server)
    try:
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=2) as client:
            request = (
                f"GET /api/v1/fixtures HTTP/1.1\r\n"
                f"hOsT: 127.0.0.1:{server.server_port}\r\n"
                f"Host: 127.0.0.1:{server.server_port}\r\n"
                "Connection: close\r\n\r\n"
            )
            client.sendall(request.encode("ascii"))
            response = b""
            while chunk := client.recv(4096):
                response += chunk
        assert response.startswith(b"HTTP/1.1 400")
        assert b"DUPLICATE_HEADER" in response
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_server_rejects_hostile_framing_before_reading_a_body() -> None:
    server_module = module("web_server")
    server = server_module.create_server(port=0)
    thread = server_module.start_in_thread(server)
    try:
        requests = (
            (
                "Content-Length: 2700001\r\n",
                b"HTTP/1.1 413",
            ),
            (
                f"Content-Length: {'9' * 5_000}\r\n",
                b"HTTP/1.1 413",
            ),
            (
                "Content-Length: 1000000\r\nTransfer-Encoding: chunked\r\n",
                b"HTTP/1.1 400",
            ),
        )
        for framing, expected in requests:
            with socket.create_connection(("127.0.0.1", server.server_port), timeout=1) as client:
                client.settimeout(0.5)
                request = (
                    "POST /api/v1/session HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{server.server_port}\r\n"
                    "Content-Type: application/json\r\n"
                    "X-ERW-Request: local-workbench-v1\r\n"
                    f"{framing}"
                    "Connection: close\r\n\r\n"
                )
                client.sendall(request.encode("ascii"))
                assert client.recv(4_096).startswith(expected)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_unsupported_http_method_keeps_security_headers() -> None:
    server_module = module("web_server")
    server = server_module.create_server(port=0)
    thread = server_module.start_in_thread(server)
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request("OPTIONS", "/api/v1/fixtures")
        response = connection.getresponse()
        response.read()
        assert response.status == 405
        assert response.getheader("X-Content-Type-Options") == "nosniff"
        assert response.getheader("Content-Security-Policy")
        assert response.getheader("Connection") == "close"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_concurrent_session_creation_is_bounded_and_reset_releases_capacity() -> None:
    api_module = module("web_api")
    api = api_module.WorkbenchApi(port=8765)
    payload = {"synthetic_only": True, "fixture_id": "catalogue-desk-v1"}

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(
            pool.map(
                lambda _: _request(api, "POST", "/api/v1/session", payload),
                range(12),
            )
        )

    accepted = [result for result in results if result[0] == 201]
    rejected = [result for result in results if result[0] == 429]
    assert len(accepted) == api_module.MAX_ACTIVE_SESSIONS
    assert len(rejected) == 12 - api_module.MAX_ACTIVE_SESSIONS
    assert all(
        "existing workbench tab" in result[2]["error"]["message"].lower()
        and "restart" in result[2]["error"]["message"].lower()
        for result in rejected
    )
    session = accepted[0][2]["session_id"]
    assert _request(api, "DELETE", "/api/v1/session", None, session) == (
        200,
        api._headers(),
        {"state": "RESET"},
    )
    assert _request(api, "POST", "/api/v1/session", payload)[0] == 201


def test_api_versioned_fixture_runs_without_truth_or_client_paths() -> None:
    api = module("web_api").WorkbenchApi(port=8765)
    _, _, created = _request(
        api,
        "POST",
        "/api/v1/session",
        {"synthetic_only": True, "fixture_id": "catalogue-desk-v1"},
    )
    session = created["session_id"]
    mapping = {field: field for field in ("source_id", "name", "brand", "sku", "category", "price")}
    assert (
        _request(
            api,
            "PUT",
            "/api/v1/session/mapping",
            {"mapping": {"left": mapping, "right": mapping}},
            session,
        )[0]
        == 200
    )
    status, _, result = _request(api, "POST", "/api/v1/session/run", {}, session)
    assert status == 200 and {pair["decision"] for pair in result["pairs"]} >= {
        "MATCH",
        "REVIEW",
        "NO_MATCH",
    }


def test_manual_review_revision_never_changes_engine_output_or_fresh_sessions() -> None:
    api = module("web_api").WorkbenchApi(port=8765)
    mapping = {field: field for field in ("source_id", "name", "brand", "sku", "category", "price")}

    def run_fixture() -> tuple[str, dict[str, Any]]:
        _, _, created = _request(
            api,
            "POST",
            "/api/v1/session",
            {"synthetic_only": True, "fixture_id": "catalogue-desk-v1"},
        )
        session_id = created["session_id"]
        assert (
            _request(
                api,
                "PUT",
                "/api/v1/session/mapping",
                {"mapping": {"left": mapping, "right": mapping}},
                session_id,
            )[0]
            == 200
        )
        status, _, result = _request(api, "POST", "/api/v1/session/run", {}, session_id)
        assert status == 200
        return session_id, cast(dict[str, Any], result)

    def engine_projection(pair: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in pair.items()
            if key not in {"human_review", "review_revision"}
        }

    session, initial = run_fixture()
    pair = next(pair for pair in initial["pairs"] if pair["decision"] == "REVIEW")
    expected_engine = engine_projection(pair)
    route = f"/api/v1/session/reviews/{quote(pair['pair_id'], safe='')}"

    first = _request(api, "PUT", route, {"review": "SAME_ENTITY"}, session)[2]
    second = _request(api, "PUT", route, {"review": "DIFFERENT_ENTITY"}, session)[2]
    assert first["decision"] == second["decision"] == "REVIEW"
    assert first["review_revision"] == 1
    assert second["review_revision"] == 2
    assert engine_projection(first) == engine_projection(second) == expected_engine

    _, _, rerun = _request(api, "POST", "/api/v1/session/run", {}, session)
    rerun_pair = next(item for item in rerun["pairs"] if item["pair_id"] == pair["pair_id"])
    assert engine_projection(rerun_pair) == expected_engine
    assert rerun_pair["human_review"] == "UNREVIEWED"
    assert rerun_pair["review_revision"] == 0

    _, fresh = run_fixture()
    fresh_pair = next(item for item in fresh["pairs"] if item["pair_id"] == pair["pair_id"])
    assert engine_projection(fresh_pair) == expected_engine
    assert fresh_pair["human_review"] == "UNREVIEWED"


def test_mutations_need_exact_origin_and_reject_transfer_encoding() -> None:
    api = module("web_api").WorkbenchApi(port=8765)
    body = b"{}"
    headers = {"Host": "127.0.0.1:8765", "Content-Type": "application/json", "Content-Length": "2"}
    assert api.handle("POST", "/api/v1/session", headers, body)[0] == 403
    headers["X-ERW-Request"] = "local-workbench-v1"
    assert api.handle("POST", "/api/v1/session", headers, body)[0] == 422
    headers["Origin"] = "http://localhost:8765"
    assert api.handle("POST", "/api/v1/session", headers, body)[0] == 403
    headers["Origin"] = "http://127.0.0.1:8765"
    headers["Transfer-Encoding"] = "chunked"
    assert api.handle("POST", "/api/v1/session", headers, body)[0] == 400


def test_request_header_names_are_case_insensitive_like_http() -> None:
    api = module("web_api").WorkbenchApi(port=8765)
    body = json.dumps({"synthetic_only": True, "fixture_id": "catalogue-desk-v1"}).encode()
    headers = {
        "host": "127.0.0.1:8765",
        "content-type": "application/json",
        "content-length": str(len(body)),
        "x-erw-request": "local-workbench-v1",
    }
    assert api.handle("POST", "/api/v1/session", headers, body)[0] == 201


def _large_valid_csv(prefix: str) -> str:
    field = "x" * 2_050
    rows = ["id,name,brand,sku,category,price"]
    rows.extend(f"{prefix}{index},{field},{field},{field},{field},1" for index in range(100))
    return "\n".join(rows) + "\n"


def test_global_body_bound_accepts_two_individually_bounded_uploads() -> None:
    api = module("web_api").WorkbenchApi(port=8765)
    request = {
        "synthetic_only": True,
        "uploads": {
            "left": {
                "filename": "l.csv",
                "content_base64": _csv(_large_valid_csv("L")),
            },
            "right": {
                "filename": "r.csv",
                "content_base64": _csv(_large_valid_csv("R")),
            },
        },
    }
    body = json.dumps(request).encode("utf-8")
    assert 2_100_000 < len(body) < 2_700_000
    assert _request(api, "POST", "/api/v1/session", request)[0] == 201


def test_http_server_reads_the_complete_bounded_two_upload_request() -> None:
    server_module = module("web_server")
    server = server_module.create_server(port=0)
    thread = server_module.start_in_thread(server)
    try:
        request = {
            "synthetic_only": True,
            "uploads": {
                "left": {
                    "filename": "l.csv",
                    "content_base64": _csv(_large_valid_csv("L")),
                },
                "right": {
                    "filename": "r.csv",
                    "content_base64": _csv(_large_valid_csv("R")),
                },
            },
        }
        body = json.dumps(request).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=4)
        connection.request(
            "POST",
            "/api/v1/session",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{server.server_port}",
                "X-ERW-Request": "local-workbench-v1",
            },
        )
        response = connection.getresponse()
        response.read()
        assert response.status == 201
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_results_filter_normalized_values_and_paginate() -> None:
    api = module("web_api").WorkbenchApi(port=8765)
    left = "id,title,make,code,amount\nL1,Café Aurora,Acme,X-1,19.00\nL2,Other,Acme,X-2,20.00\n"
    right = (
        "id,title,make,code,amount\nR1,Cafe Aurora,Acme,X-1,19.00\nR2,Other two,Acme,X-3,20.00\n"
    )
    request = {
        "synthetic_only": True,
        "uploads": {
            "left": {"filename": "l.csv", "content_base64": _csv(left)},
            "right": {"filename": "r.csv", "content_base64": _csv(right)},
        },
    }
    _, _, created = _request(api, "POST", "/api/v1/session", request)
    session = created["session_id"]
    side = {
        "source_id": "id",
        "name": "title",
        "brand": "make",
        "sku": "code",
        "category": None,
        "price": "amount",
    }
    _request(
        api, "PUT", "/api/v1/session/mapping", {"mapping": {"left": side, "right": side}}, session
    )
    _request(api, "POST", "/api/v1/session/run", {}, session)
    status, _, page = _request(
        api, "GET", "/api/v1/session/results?query=cafe&page=1&page_size=1", None, session
    )
    assert status == 200 and page["total"] == 3 and len(page["pairs"]) == 1
    assert page["page"] == 1 and page["page_size"] == 1
    assert "fingerprint" not in page["pairs"][0]["left"]["normalized"]
    assert _request(api, "GET", "/api/v1/session/results?page_size=101", None, session)[0] == 422


def _run_fixture(api: Any) -> tuple[str, dict[str, str], dict[str, Any]]:
    _, _, created = _request(
        api,
        "POST",
        "/api/v1/session",
        {"synthetic_only": True, "fixture_id": "catalogue-desk-v1"},
    )
    session = created["session_id"]
    mapping = {field: field for field in ("source_id", "name", "brand", "sku", "category", "price")}
    _request(
        api,
        "PUT",
        "/api/v1/session/mapping",
        {"mapping": {"left": mapping, "right": mapping}},
        session,
    )
    _, _, result = _request(api, "POST", "/api/v1/session/run", {}, session)
    return session, mapping, result


def test_prior_result_response_is_an_immutable_snapshot_after_review() -> None:
    api = module("web_api").WorkbenchApi(port=8765)
    session, _, result = _run_fixture(api)
    review_pair = next(pair for pair in result["pairs"] if pair["decision"] == "REVIEW")

    status, _, updated = _request(
        api,
        "PUT",
        f"/api/v1/session/reviews/{review_pair['pair_id']}",
        {"review": "SAME_ENTITY"},
        session,
    )

    assert status == 200 and updated["human_review"] == "SAME_ENTITY"
    assert review_pair["human_review"] == "UNREVIEWED"
    assert review_pair["review_revision"] == 0


def test_remapping_invalidates_prior_results_and_export_until_rerun() -> None:
    api = module("web_api").WorkbenchApi(port=8765)
    session, mapping, _ = _run_fixture(api)

    assert (
        _request(
            api,
            "PUT",
            "/api/v1/session/mapping",
            {"mapping": {"left": mapping, "right": mapping}},
            session,
        )[0]
        == 200
    )
    assert _request(api, "GET", "/api/v1/session/results", None, session)[0] == 409
    assert _request(api, "GET", "/api/v1/session/export.csv", None, session)[0] == 409


def test_expired_orphan_sessions_release_the_bounded_capacity() -> None:
    api_module = module("web_api")
    now = [10.0]
    api = api_module.WorkbenchApi(port=8765, clock=lambda: now[0])
    payload = {"synthetic_only": True, "fixture_id": "catalogue-desk-v1"}
    sessions = [
        _request(api, "POST", "/api/v1/session", payload)[2]["session_id"]
        for _ in range(api_module.MAX_ACTIVE_SESSIONS)
    ]
    assert _request(api, "POST", "/api/v1/session", payload)[0] == 429

    now[0] += api_module.SESSION_TTL_SECONDS + 1

    assert _request(api, "POST", "/api/v1/session", payload)[0] == 201
    assert _request(api, "GET", "/api/v1/session/results", None, sessions[0])[0] == 404


@pytest.mark.parametrize(
    "session_payload",
    [
        {"synthetic_only": False, "fixture_id": "catalogue-desk-v1"},
        {
            "synthetic_only": True,
            "fixture_id": "catalogue-desk-v1",
            "contains_personal_data": True,
        },
        {
            "synthetic_only": True,
            "uploads": {
                "left": {"path": "/tmp/left.csv"},
                "right": {"url": "file:///tmp/right.csv"},
            },
        },
    ],
)
def test_session_rejects_personal_data_claims_paths_urls_and_unknown_keys(
    session_payload: dict[str, object],
) -> None:
    api = module("web_api").WorkbenchApi(port=8765)
    status, _, response = _request(api, "POST", "/api/v1/session", session_payload)
    assert status == 422
    assert "/tmp" not in json.dumps(response)


@pytest.mark.parametrize(
    ("left_rows", "right_rows"),
    [
        (
            "id,name,price\nDUP,Lamp,10\nDUP,Desk,20\n",
            "id,name,price\nR1,Lamp,10\n",
        ),
        (
            "id,name,price\nL1,Lamp,not-a-price\n",
            "id,name,price\nR1,Lamp,10\n",
        ),
    ],
)
def test_web_run_reuses_canonical_type_and_source_id_validation(
    left_rows: str, right_rows: str
) -> None:
    api = module("web_api").WorkbenchApi(port=8765)
    request = {
        "synthetic_only": True,
        "uploads": {
            "left": {"filename": "left.csv", "content_base64": _csv(left_rows)},
            "right": {"filename": "right.csv", "content_base64": _csv(right_rows)},
        },
    }
    _, _, created = _request(api, "POST", "/api/v1/session", request)
    session = created["session_id"]
    mapping = {
        "source_id": "id",
        "name": "name",
        "brand": None,
        "sku": None,
        "category": None,
        "price": "price",
    }
    assert (
        _request(
            api,
            "PUT",
            "/api/v1/session/mapping",
            {"mapping": {"left": mapping, "right": mapping}},
            session,
        )[0]
        == 200
    )
    status, _, response = _request(api, "POST", "/api/v1/session/run", {}, session)
    assert status == 422
    assert response["error"]["code"] == "INVALID_INPUT"
