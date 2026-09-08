from __future__ import annotations

import base64
import contextlib
import http.server
import json
import os
import select
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from conftest import canonical_json, module


class ReportDOM(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.body_attributes: dict[str, str | None] = {}
        self.heading_ids: set[str] = set()
        self.meta_viewports: list[str | None] = []
        self.review_cards: list[dict[str, object]] = []
        self.scroll_regions: list[dict[str, str | None]] = []
        self.tables: list[dict[str, object]] = []
        self._active_card: dict[str, object] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "body":
            self.body_attributes = attributes
        if tag in {"h1", "h2", "h3", "h4"} and isinstance(attributes.get("id"), str):
            self.heading_ids.add(str(attributes["id"]))
        if tag == "meta" and attributes.get("name") == "viewport":
            self.meta_viewports.append(attributes.get("content"))
        classes = set((attributes.get("class") or "").split())
        if tag == "div" and "table-scroll" in classes:
            self.scroll_regions.append(attributes)
        if tag == "table":
            self.tables.append({"caption": False, "header_scopes": []})
        elif tag == "caption" and self.tables:
            self.tables[-1]["caption"] = True
        elif tag == "th" and self.tables:
            scopes = self.tables[-1]["header_scopes"]
            assert isinstance(scopes, list)
            scopes.append(attributes.get("scope"))
        if tag == "article" and "review-card" in classes:
            self._active_card = {"attributes": attributes, "text": []}
            self.review_cards.append(self._active_card)

    def handle_endtag(self, tag: str) -> None:
        if tag == "article":
            self._active_card = None

    def handle_data(self, data: str) -> None:
        if self._active_card is not None:
            text = self._active_card["text"]
            assert isinstance(text, list)
            text.append(data)


def _score(display: str, fraction: str) -> dict[str, str]:
    return {"display": display, "fraction": fraction}


def _review_payload() -> dict[str, object]:
    field_name = "_".join(("evaluation", "key"))
    return {
        "schema": "erw-report-v1",
        "run_key": "run-opaque-123",
        field_name: "demo-evaluation",
        "metrics_by_split": {
            "calibration": {
                "tp": 4,
                "fp": 0,
                "tn": 157,
                "fn": 8,
                "precision": 1.0,
                "recall": 1 / 3,
                "f1": 0.5,
            }
        },
        "review_rows": [
            {
                "split": "calibration",
                "left_id": "left-opaque-001",
                "right_id": "right-opaque-001",
                "decision": "REVIEW",
                "admitted": True,
                "block_reasons": ["brand_exact", "name_token:marteau"],
                "scores": {
                    "name": _score("0.875000", "7/8"),
                    "brand": _score("1.000000", "1/1"),
                    "sku": _score("0.000000", "0/1"),
                    "price": None,
                    "total": _score("0.723214", "81/112"),
                },
                "evidence_count": 3,
                "contradictions": ["sku_disagreement"],
                "explanation": {
                    "failed_conditions": ["contradiction", "below_match_threshold"],
                    "left_margin": "9/50",
                    "left_rank": 1,
                    "missing_components": ["price"],
                    "normalization": {
                        "left": {
                            "name": "marteau alpha",
                            "brand": "acme",
                            "sku": "sku01",
                            "category": "outillage",
                            "price": None,
                        },
                        "right": {
                            "name": "marteau alfa",
                            "brand": "acme",
                            "sku": "other01",
                            "category": "outillage",
                            "price": None,
                        },
                    },
                    "reasons": [],
                    "right_margin": "7/50",
                    "right_rank": 1,
                },
            },
            {
                "split": "validation",
                "left_id": "left-opaque-002",
                "right_id": "right-opaque-002",
                "decision": "REVIEW",
                "admitted": True,
                "block_reasons": ["category_exact"],
                "scores": {
                    "name": _score("0.800000", "4/5"),
                    "brand": None,
                    "sku": _score("1.000000", "1/1"),
                    "price": _score("0.750000", "3/4"),
                    "total": _score("0.814286", "57/70"),
                },
                "evidence_count": 3,
                "contradictions": ["price_delta"],
                "explanation": {
                    "failed_conditions": ["contradiction"],
                    "left_margin": "1/10",
                    "left_rank": 2,
                    "missing_components": ["brand"],
                    "normalization": {
                        "left": {
                            "name": "cafe filtre compact",
                            "brand": None,
                            "sku": "sku02",
                            "category": "cuisine",
                            "price": "20.00",
                        },
                        "right": {
                            "name": "cafe filtre kompact <test>",
                            "brand": None,
                            "sku": "sku02",
                            "category": "cuisine",
                            "price": "27.00",
                        },
                    },
                    "reasons": ["manual_price_check"],
                    "right_margin": "3/40",
                    "right_rank": 2,
                },
            },
        ],
    }


def _parse(document: bytes | str) -> ReportDOM:
    parser = ReportDOM()
    parser.feed(document.decode("utf-8") if isinstance(document, bytes) else document)
    return parser


def _chrome() -> str:
    executable = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    assert executable is not None, "the narrow-render gate requires local Google Chrome"
    return executable


def _close_file_descriptors(*file_descriptors: int) -> None:
    for file_descriptor in file_descriptors:
        if file_descriptor < 0:
            continue
        with contextlib.suppress(OSError):
            os.close(file_descriptor)


class CDPPipeClient:
    def __init__(self, read_fd: int = -1, write_fd: int = -1) -> None:
        self._read_fd = read_fd
        self._write_fd = write_fd
        self._buffer = bytearray()
        self._request_id = 0
        self._session_id: str | None = None

    def close(self) -> None:
        read_fd, write_fd = self._read_fd, self._write_fd
        self._read_fd = -1
        self._write_fd = -1
        _close_file_descriptors(write_fd, read_fd)

    def attach(self, session_id: str) -> None:
        assert self._session_id is None
        self._session_id = session_id

    def _send_message(self, message: dict[str, object]) -> None:
        remaining = memoryview(json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\0")
        while remaining:
            written = os.write(self._write_fd, remaining)
            assert written > 0, "Chrome closed its DevTools input pipe"
            remaining = remaining[written:]

    def _receive_message(self) -> dict[str, Any]:
        deadline = time.monotonic() + 5
        while True:
            delimiter = self._buffer.find(0)
            if delimiter >= 0:
                payload = bytes(self._buffer[:delimiter])
                del self._buffer[: delimiter + 1]
                if not payload:
                    continue
                message = json.loads(payload)
                assert isinstance(message, dict)
                return message
            remaining = deadline - time.monotonic()
            assert remaining > 0, "Chrome did not answer on its DevTools pipe"
            readable, _, _ = select.select([self._read_fd], [], [], remaining)
            assert readable, "Chrome did not answer on its DevTools pipe"
            chunk = os.read(self._read_fd, 65536)
            assert chunk, "Chrome closed its DevTools output pipe"
            self._buffer.extend(chunk)

    def command(self, method: str, params: dict[str, object] | None = None) -> dict[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        request: dict[str, object] = {
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        if self._session_id is not None:
            request["sessionId"] = self._session_id
        self._send_message(request)
        while True:
            response = self._receive_message()
            if response.get("id") != request_id:
                continue
            assert "error" not in response, response
            result = response.get("result")
            assert isinstance(result, dict)
            return result


def _transfer_parent_pipe_ownership(client: CDPPipeClient, read_fd: int, write_fd: int) -> None:
    object.__setattr__(client, "_read_fd", read_fd)
    object.__setattr__(client, "_write_fd", write_fd)


def _start_chrome(chrome: str, profile: Path) -> tuple[subprocess.Popen[bytes], CDPPipeClient]:
    chrome_read_fd, parent_write_fd = os.pipe()
    try:
        parent_read_fd, chrome_write_fd = os.pipe()
    except BaseException:
        _close_file_descriptors(chrome_read_fd, parent_write_fd)
        raise
    chrome_arguments = [
        chrome,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-sync",
        "--metrics-recording-only",
        "--no-first-run",
        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
        "--remote-debugging-pipe",
        f"--user-data-dir={profile}",
        "about:blank",
    ]
    pipe_wrapper = [
        sys.executable,
        "-c",
        "import os,sys; "
        "read_fd=int(sys.argv[1]); write_fd=int(sys.argv[2]); "
        "os.dup2(read_fd,3); os.dup2(write_fd,4); "
        "os.set_inheritable(3,True); os.set_inheritable(4,True); "
        "os.execv(sys.argv[3],sys.argv[3:])",
        str(chrome_read_fd),
        str(chrome_write_fd),
        *chrome_arguments,
    ]
    try:
        process = subprocess.Popen(
            pipe_wrapper,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(chrome_read_fd, chrome_write_fd),
        )
    except BaseException:
        _close_file_descriptors(
            chrome_read_fd,
            parent_write_fd,
            parent_read_fd,
            chrome_write_fd,
        )
        raise
    client: CDPPipeClient | None = None
    ownership_transferred = False
    try:
        file_descriptor, chrome_read_fd = chrome_read_fd, -1
        os.close(file_descriptor)
        file_descriptor, chrome_write_fd = chrome_write_fd, -1
        os.close(file_descriptor)
        client = CDPPipeClient()
        _transfer_parent_pipe_ownership(client, parent_read_fd, parent_write_fd)
        ownership_transferred = True
        targets = client.command("Target.getTargets").get("targetInfos")
        assert isinstance(targets, list) and targets, "Chrome DevTools pipe did not start"
        page = next(
            (
                item
                for item in targets
                if isinstance(item, dict)
                and item.get("type") == "page"
                and item.get("url") == "about:blank"
            ),
            None,
        )
        assert isinstance(page, dict), targets
        target_id = page.get("targetId")
        assert isinstance(target_id, str)
        attachment = client.command(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )
        session_id = attachment.get("sessionId")
        assert isinstance(session_id, str)
        client.attach(session_id)
        client.command("Network.enable")
        client.command(
            "Network.setBlockedURLs",
            {"urls": ["http://*/*", "https://*/*", "ws://*/*", "wss://*/*"]},
        )
    except BaseException:
        _close_file_descriptors(
            chrome_read_fd,
            chrome_write_fd,
        )
        if ownership_transferred:
            assert client is not None
            client.close()
        else:
            if client is not None:
                object.__setattr__(client, "_read_fd", -1)
                object.__setattr__(client, "_write_fd", -1)
            _close_file_descriptors(parent_read_fd, parent_write_fd)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        raise
    assert client is not None
    return process, client


def test_chrome_gate_closes_first_pipe_when_second_allocation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_pipe = os.pipe
    captured_file_descriptors: list[int] = []
    calls = 0

    def fail_second_pipe() -> tuple[int, int]:
        nonlocal calls
        calls += 1
        if calls == 1:
            pipe = real_pipe()
            captured_file_descriptors.extend(pipe)
            return pipe
        raise OSError("forced second pipe allocation failure")

    monkeypatch.setattr(os, "pipe", fail_second_pipe)

    try:
        with pytest.raises(OSError, match="forced second pipe allocation failure"):
            _start_chrome("chrome-must-not-start", tmp_path / "chrome-pipe-allocation-failure")

        assert calls == 2
        assert len(captured_file_descriptors) == 2
        for file_descriptor in captured_file_descriptors:
            with pytest.raises(OSError):
                os.fstat(file_descriptor)
    finally:
        for file_descriptor in captured_file_descriptors:
            with contextlib.suppress(OSError):
                os.close(file_descriptor)


def test_chrome_gate_cleans_up_when_pipe_client_construction_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processes: list[subprocess.Popen[bytes]] = []
    pipe_descriptors: list[int] = []
    real_pipe = os.pipe
    real_popen = subprocess.Popen

    def recording_pipe() -> tuple[int, int]:
        descriptors = real_pipe()
        if len(pipe_descriptors) < 4:
            pipe_descriptors.extend(descriptors)
        return descriptors

    def recording_popen(
        args: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        pass_fds: tuple[int, int],
    ) -> subprocess.Popen[bytes]:
        process = real_popen(
            args,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            pass_fds=pass_fds,
        )
        processes.append(process)
        return process

    def fail_client_construction(
        self: CDPPipeClient, read_fd: int = -1, write_fd: int = -1
    ) -> None:
        assert (read_fd, write_fd) == (-1, -1)
        raise MemoryError("forced pipe client construction failure")

    monkeypatch.setattr(os, "pipe", recording_pipe)
    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    monkeypatch.setattr(CDPPipeClient, "__init__", fail_client_construction)

    try:
        with pytest.raises(MemoryError, match="forced pipe client construction failure"):
            _start_chrome(_chrome(), tmp_path / "chrome-client-construction-failure")

        assert len(processes) == 1
        assert len(pipe_descriptors) == 4
        assert processes[0].poll() is not None
        for file_descriptor in pipe_descriptors:
            with pytest.raises(OSError):
                os.fstat(file_descriptor)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        for file_descriptor in pipe_descriptors:
            with contextlib.suppress(OSError):
                os.close(file_descriptor)


def test_chrome_gate_cleans_up_partial_parent_pipe_ownership_transfer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processes: list[subprocess.Popen[bytes]] = []
    captured_clients: list[CDPPipeClient] = []
    client_descriptors: list[int] = []
    pipe_descriptors: list[int] = []
    replacement_descriptors: list[int] = []
    real_pipe = os.pipe
    real_popen = subprocess.Popen
    injected_failure = False

    def recording_pipe() -> tuple[int, int]:
        descriptors = real_pipe()
        if len(pipe_descriptors) < 4:
            pipe_descriptors.extend(descriptors)
        return descriptors

    def recording_popen(
        args: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        pass_fds: tuple[int, int],
    ) -> subprocess.Popen[bytes]:
        process = real_popen(
            args,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            pass_fds=pass_fds,
        )
        processes.append(process)
        return process

    def fail_after_mutating_both_client_descriptors(
        client: CDPPipeClient, read_fd: int, write_fd: int
    ) -> None:
        nonlocal injected_failure
        captured_clients.append(client)
        client_descriptors.extend((read_fd, write_fd))
        object.__setattr__(client, "_read_fd", read_fd)
        object.__setattr__(client, "_write_fd", write_fd)
        injected_failure = True
        raise MemoryError("forced partial parent pipe ownership transfer")

    monkeypatch.setattr(os, "pipe", recording_pipe)
    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    monkeypatch.setitem(
        globals(),
        "_transfer_parent_pipe_ownership",
        fail_after_mutating_both_client_descriptors,
    )

    try:
        with pytest.raises(MemoryError, match="forced partial parent pipe ownership transfer"):
            _start_chrome(_chrome(), tmp_path / "chrome-partial-parent-transfer")

        assert injected_failure is True
        assert len(processes) == 1
        assert len(captured_clients) == 1
        assert len(pipe_descriptors) == 4
        assert processes[0].poll() is not None
        for file_descriptor in pipe_descriptors:
            with pytest.raises(OSError):
                os.fstat(file_descriptor)
        while not set(client_descriptors).issubset(replacement_descriptors):
            replacement_descriptors.extend(os.pipe())
            assert len(replacement_descriptors) <= 8

        captured_clients[0].close()

        for file_descriptor in replacement_descriptors:
            os.fstat(file_descriptor)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        _close_file_descriptors(*replacement_descriptors, *pipe_descriptors)


def test_pipe_client_close_is_idempotent_after_file_descriptor_reuse() -> None:
    client_descriptors = os.pipe()
    client = CDPPipeClient(*client_descriptors)
    replacement_descriptors: tuple[int, int] | None = None
    try:
        client.close()
        replacement_descriptors = os.pipe()
        assert replacement_descriptors == client_descriptors

        client.close()

        for file_descriptor in replacement_descriptors:
            os.fstat(file_descriptor)
    finally:
        if replacement_descriptors is not None:
            _close_file_descriptors(*replacement_descriptors)
        _close_file_descriptors(*client_descriptors)


def test_pipe_client_close_never_retries_an_ambiguous_numeric_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_descriptors = os.pipe()
    client = CDPPipeClient(*client_descriptors)
    real_close = os.close
    injected_failure = False

    def close_reuse_same_pipe_then_fail(file_descriptor: int) -> None:
        nonlocal injected_failure
        if file_descriptor == client_descriptors[1] and not injected_failure:
            injected_failure = True
            real_close(file_descriptor)
            assert os.dup2(client_descriptors[0], file_descriptor) == file_descriptor
            raise OSError("forced ambiguous client close failure")
        real_close(file_descriptor)

    monkeypatch.setattr(os, "close", close_reuse_same_pipe_then_fail)
    try:
        client.close()

        assert injected_failure is True
        assert (client._read_fd, client._write_fd) == (-1, -1)
        os.fstat(client_descriptors[1])
        with pytest.raises(OSError):
            os.fstat(client_descriptors[0])
    finally:
        for file_descriptor in client_descriptors:
            with contextlib.suppress(OSError):
                real_close(file_descriptor)


def test_chrome_gate_does_not_double_close_after_ambiguous_child_pipe_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processes: list[subprocess.Popen[bytes]] = []
    pipe_descriptors: list[int] = []
    replacement_descriptors: list[int] = []
    real_pipe = os.pipe
    real_close = os.close
    real_popen = subprocess.Popen
    popen_returned = False
    injected_failure = False

    def recording_pipe() -> tuple[int, int]:
        descriptors = real_pipe()
        if len(pipe_descriptors) < 4:
            pipe_descriptors.extend(descriptors)
        return descriptors

    def recording_popen(
        args: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        pass_fds: tuple[int, int],
    ) -> subprocess.Popen[bytes]:
        nonlocal popen_returned
        process = real_popen(
            args,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            pass_fds=pass_fds,
        )
        processes.append(process)
        popen_returned = True
        return process

    def close_then_fail_first_child_once(file_descriptor: int) -> None:
        nonlocal injected_failure
        if popen_returned and file_descriptor == pipe_descriptors[0] and not injected_failure:
            injected_failure = True
            real_close(file_descriptor)
            replacements = real_pipe()
            replacement_descriptors.extend(replacements)
            assert replacements[0] == file_descriptor
            raise OSError("forced ambiguous child pipe close failure")
        real_close(file_descriptor)

    monkeypatch.setattr(os, "pipe", recording_pipe)
    monkeypatch.setattr(os, "close", close_then_fail_first_child_once)
    monkeypatch.setattr(subprocess, "Popen", recording_popen)

    try:
        with pytest.raises(OSError, match="forced ambiguous child pipe close failure"):
            _start_chrome(_chrome(), tmp_path / "chrome-child-close-failure")

        assert injected_failure is True
        assert len(processes) == 1
        assert len(replacement_descriptors) == 2
        assert processes[0].poll() is not None
        for file_descriptor in replacement_descriptors:
            os.fstat(file_descriptor)
        for file_descriptor in pipe_descriptors[1:]:
            with pytest.raises(OSError):
                os.fstat(file_descriptor)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        for file_descriptor in (*replacement_descriptors, *pipe_descriptors):
            with contextlib.suppress(OSError):
                real_close(file_descriptor)


def test_chrome_gate_never_retries_ambiguous_child_descriptor_for_same_pipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processes: list[subprocess.Popen[bytes]] = []
    pipe_descriptors: list[int] = []
    real_pipe = os.pipe
    real_close = os.close
    real_popen = subprocess.Popen
    popen_returned = False
    injected_failure = False

    def recording_pipe() -> tuple[int, int]:
        descriptors = real_pipe()
        if len(pipe_descriptors) < 4:
            pipe_descriptors.extend(descriptors)
        return descriptors

    def recording_popen(
        args: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        pass_fds: tuple[int, int],
    ) -> subprocess.Popen[bytes]:
        nonlocal popen_returned
        process = real_popen(
            args,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            pass_fds=pass_fds,
        )
        processes.append(process)
        popen_returned = True
        return process

    def close_reuse_same_pipe_then_fail(file_descriptor: int) -> None:
        nonlocal injected_failure
        if popen_returned and file_descriptor == pipe_descriptors[0] and not injected_failure:
            injected_failure = True
            real_close(file_descriptor)
            assert os.dup2(pipe_descriptors[1], file_descriptor) == file_descriptor
            raise OSError("forced ambiguous same-pipe child close failure")
        real_close(file_descriptor)

    monkeypatch.setattr(os, "pipe", recording_pipe)
    monkeypatch.setattr(os, "close", close_reuse_same_pipe_then_fail)
    monkeypatch.setattr(subprocess, "Popen", recording_popen)

    try:
        with pytest.raises(OSError, match="forced ambiguous same-pipe child close failure"):
            _start_chrome(_chrome(), tmp_path / "chrome-child-same-pipe-reuse")

        assert injected_failure is True
        assert len(processes) == 1
        assert processes[0].poll() is not None
        os.fstat(pipe_descriptors[0])
        for file_descriptor in pipe_descriptors[1:]:
            with pytest.raises(OSError):
                os.fstat(file_descriptor)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        for file_descriptor in pipe_descriptors:
            with contextlib.suppress(OSError):
                real_close(file_descriptor)


def test_chrome_gate_uses_anonymous_pipe_without_tcp_debug_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_socket(*args: object, **kwargs: object) -> object:
        raise AssertionError("the Chrome gate attempted to create a Python socket")

    monkeypatch.setattr(socket, "socket", forbidden_socket)

    process, client = _start_chrome(_chrome(), tmp_path / "chrome-pipe-profile")
    try:
        assert isinstance(process.args, list)
        assert "--remote-debugging-pipe" in process.args
        assert not any(
            argument.startswith("--remote-debugging-port=")
            or argument.startswith("--remote-debugging-address=")
            for argument in process.args
        )
        client.command("Runtime.enable")
        result = client.command(
            "Runtime.evaluate",
            {"expression": "6 * 7", "returnByValue": True},
        )
        assert result["result"]["value"] == 42
    finally:
        client.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_chrome_gate_cleans_up_process_and_pipes_when_bootstrap_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processes: list[subprocess.Popen[bytes]] = []
    clients: list[CDPPipeClient] = []
    client_descriptors: list[int] = []
    replacement_descriptors: list[int] = []
    real_popen = subprocess.Popen
    real_client_init = CDPPipeClient.__init__

    def recording_popen(
        args: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        pass_fds: tuple[int, int],
    ) -> subprocess.Popen[bytes]:
        process = real_popen(
            args,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            pass_fds=pass_fds,
        )
        processes.append(process)
        return process

    def recording_client_init(self: CDPPipeClient, read_fd: int = -1, write_fd: int = -1) -> None:
        real_client_init(self, read_fd, write_fd)
        clients.append(self)

    def fail_bootstrap(
        self: CDPPipeClient,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        if not client_descriptors:
            client_descriptors.extend((self._read_fd, self._write_fd))
        raise RuntimeError("forced CDP bootstrap failure")

    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    monkeypatch.setattr(CDPPipeClient, "__init__", recording_client_init)
    monkeypatch.setattr(CDPPipeClient, "command", fail_bootstrap)

    with pytest.raises(RuntimeError, match="forced CDP bootstrap failure"):
        _start_chrome(_chrome(), tmp_path / "chrome-bootstrap-failure-profile")

    assert len(processes) == len(clients) == 1
    process = processes[0]
    client = clients[0]
    try:
        assert process.poll() is not None
        for file_descriptor in client_descriptors:
            with pytest.raises(OSError):
                os.fstat(file_descriptor)
        while not set(client_descriptors).issubset(replacement_descriptors):
            replacement_descriptors.extend(os.pipe())
            assert len(replacement_descriptors) <= 8

        client.close()

        assert (client._read_fd, client._write_fd) == (-1, -1)
        for file_descriptor in replacement_descriptors:
            os.fstat(file_descriptor)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        _close_file_descriptors(*replacement_descriptors)
        _close_file_descriptors(*client_descriptors)


def test_chrome_gate_blocks_page_web_requests(tmp_path: Path) -> None:
    requests: list[str] = []

    class ProbeHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            body = b"<!doctype html><title>local network control</title>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ProbeHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    process, client = _start_chrome(_chrome(), tmp_path / "chrome-network-profile")
    try:
        probe_url = f"http://127.0.0.1:{server.server_port}/page-web-request-probe"
        client.command("Runtime.enable")
        blocked_fetch = client.command(
            "Runtime.evaluate",
            {
                "expression": (
                    f"fetch({json.dumps(probe_url)},{{mode:'no-cors'}})"
                    ".then(()=> 'resolved',()=> 'rejected')"
                ),
                "awaitPromise": True,
                "returnByValue": True,
            },
        )
        assert blocked_fetch["result"]["value"] == "rejected"
        assert requests == []

        client.command("Network.setBlockedURLs", {"urls": []})
        allowed_url = f"{probe_url}?control=allowed"
        allowed_fetch = client.command(
            "Runtime.evaluate",
            {
                "expression": (
                    f"fetch({json.dumps(allowed_url)},{{mode:'no-cors'}})"
                    ".then(()=> 'resolved',()=> 'rejected')"
                ),
                "awaitPromise": True,
                "returnByValue": True,
            },
        )
        assert allowed_fetch["result"]["value"] == "resolved"
        deadline = time.monotonic() + 2
        while "/page-web-request-probe?control=allowed" not in requests:
            assert time.monotonic() < deadline, requests
            time.sleep(0.02)
    finally:
        client.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


def test_report_source_is_deterministic_static_and_declares_a_narrow_strategy() -> None:
    reporting = module("reporting")
    payload = _review_payload()

    first = reporting.render_report(payload)
    second = reporting.render_report(payload)

    assert first.json_bytes == second.json_bytes == canonical_json(payload)
    assert first.html_bytes == second.html_bytes
    source = first.html_bytes.decode("utf-8")
    assert '<meta name="viewport" content="width=device-width,initial-scale=1">' in source
    assert "overflow-x:auto" in source
    assert "overflow-wrap:anywhere" in source
    assert "@media(max-width:480px)" in source
    assert '<article class="review-card"' in source
    assert "<script" not in source and "<link" not in source and " src=" not in source
    assert "http://" not in source and "https://" not in source


def test_report_dom_exposes_every_review_decision_with_accessible_table_headers() -> None:
    rendered = module("reporting").render_report(_review_payload())
    dom = _parse(rendered.html_bytes)

    assert dom.meta_viewports == ["width=device-width,initial-scale=1"]
    assert len(dom.review_cards) == 2
    expected_text = (
        {
            "left-opaque-001",
            "right-opaque-001",
            "marteau alpha",
            "marteau alfa",
            "0.875000",
            "7/8",
            "sku_disagreement",
            "9/50",
            "7/50",
            "Not available",
        },
        {
            "left-opaque-002",
            "right-opaque-002",
            "cafe filtre compact",
            "cafe filtre kompact <test>",
            "0.814286",
            "57/70",
            "price_delta",
            "1/10",
            "3/40",
            "manual_price_check",
        },
    )
    for card, required in zip(dom.review_cards, expected_text, strict=True):
        attributes = card["attributes"]
        text_fragments = card["text"]
        assert isinstance(attributes, dict) and isinstance(text_fragments, list)
        assert attributes.get("data-decision") == "REVIEW"
        labelled_by = attributes.get("aria-labelledby")
        assert isinstance(labelled_by, str) and labelled_by in dom.heading_ids
        card_text = " ".join(str(fragment) for fragment in text_fragments)
        assert all(value in card_text for value in required)

    assert len(dom.tables) == 7
    assert all(table["caption"] is True for table in dom.tables)
    for table in dom.tables:
        scopes = table["header_scopes"]
        assert isinstance(scopes, list) and scopes
        assert set(scopes) <= {"col", "row"}
    assert len(dom.scroll_regions) == 1
    region = dom.scroll_regions[0]
    assert region.get("role") == "region"
    assert region.get("tabindex") == "0"
    assert region.get("aria-labelledby") in dom.heading_ids
    assert isinstance(region.get("aria-describedby"), str)


def test_report_states_when_no_pairs_require_human_review() -> None:
    payload = _review_payload()
    payload["review_rows"] = []

    rendered = module("reporting").render_report(payload)
    source = rendered.html_bytes.decode("utf-8")
    dom = _parse(rendered.html_bytes)

    assert dom.review_cards == []
    assert '<li class="review-empty">No pairs require human review.</li>' in source


def test_demo_report_renders_all_review_cards_at_320px_without_clipping(tmp_path: Path) -> None:
    demo_workspace = tmp_path / "demo"
    assert (
        module("cli").main(["demo", "--workspace", str(demo_workspace), "--seed", "20260902"]) == 0
    )
    payload = json.loads((demo_workspace / "report.json").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    review_rows = payload.get("review_rows")
    assert isinstance(review_rows, list) and len(review_rows) == 12
    report = demo_workspace / "report.html"
    chrome = _chrome()
    process, client = _start_chrome(chrome, tmp_path / "chrome-profile")
    try:
        client.command("Page.enable")
        client.command("Runtime.enable")
        client.command(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": 320,
                "height": 1200,
                "deviceScaleFactor": 1,
                "mobile": True,
                "screenWidth": 320,
                "screenHeight": 1200,
            },
        )
        report_uri = report.as_uri()
        navigation = client.command("Page.navigate", {"url": report_uri})
        last_readiness: object = None
        for _ in range(100):
            readiness = client.command(
                "Runtime.evaluate",
                {
                    "expression": "[location.href, document.readyState]",
                    "returnByValue": True,
                },
            )
            result = readiness.get("result")
            last_readiness = result.get("value") if isinstance(result, dict) else result
            if isinstance(result, dict) and result.get("value") == [report_uri, "complete"]:
                break
            time.sleep(0.05)
        else:
            raise AssertionError(
                "the local report did not finish loading in Chrome: "
                f"navigate={navigation!r}, last={last_readiness!r}"
            )

        layout = client.command(
            "Runtime.evaluate",
            {
                "returnByValue": True,
                "expression": """(() => {
  const root = document.documentElement;
  const metrics = document.querySelector('.table-scroll');
  const cards = [...document.querySelectorAll('.review-card')];
  const withinViewport = (element) => {
    const box = element.getBoundingClientRect();
    return box.left >= -0.5 && box.right <= root.clientWidth + 0.5;
  };
  const result = {
    viewportWidth: root.clientWidth,
    pageFits: root.scrollWidth <= root.clientWidth + 1,
    reviewCount: cards.length,
    cardsFit: cards.length === 12 && cards.every(withinViewport),
    cardsUnclipped: cards.length === 12 &&
      cards.every((card) => card.scrollWidth <= card.clientWidth + 1),
    metricsExplicit: false,
    metricsScrollable: false,
    finalMetricReachable: false,
  };
  if (metrics) {
    const style = getComputedStyle(metrics);
    result.metricsExplicit = ['auto', 'scroll'].includes(style.overflowX) &&
      metrics.tabIndex === 0 && Boolean(metrics.getAttribute('aria-describedby'));
    result.metricsScrollable = metrics.scrollWidth > metrics.clientWidth;
    metrics.scrollLeft = metrics.scrollWidth;
    const regionBox = metrics.getBoundingClientRect();
    const finalHeaderBox = metrics.querySelector('thead th:last-child').getBoundingClientRect();
    result.finalMetricReachable = finalHeaderBox.left >= regionBox.left - 1 &&
      finalHeaderBox.right <= regionBox.right + 1;
    metrics.scrollLeft = 0;
  }
  return result;
})()""",
            },
        )
        remote_result = layout.get("result")
        assert isinstance(remote_result, dict)
        values = remote_result.get("value")
        assert values == {
            "viewportWidth": 320,
            "pageFits": True,
            "reviewCount": 12,
            "cardsFit": True,
            "cardsUnclipped": True,
            "metricsExplicit": True,
            "metricsScrollable": True,
            "finalMetricReachable": True,
        }

        screenshot = client.command(
            "Page.captureScreenshot",
            {"format": "png", "captureBeyondViewport": False},
        )
        encoded_png = screenshot.get("data")
        assert isinstance(encoded_png, str)
        png = base64.b64decode(encoded_png, validate=True)
        (tmp_path / "report-320x1200.png").write_bytes(png)
    finally:
        client.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", png[16:24]) == (320, 1200)
