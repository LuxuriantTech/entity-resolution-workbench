#!/usr/bin/env python3
"""Start, inspect, or stop the local workbench without a broad process match."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import select
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from typing import Any, BinaryIO

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = PROJECT_ROOT / ".erw.lock"
CONTROL_LOCK_PATH = PROJECT_ROOT / ".ci-artifacts" / "workbench" / "control.lock"
ARTIFACT_ROOT = PROJECT_ROOT / ".ci-artifacts" / "workbench"
LOG_PATH = ARTIFACT_ROOT / "server.log"
WORKSPACE_PATH = ARTIFACT_ROOT / "session"
ERW = PROJECT_ROOT / ".venv" / "bin" / "erw"
READINESS_TIMEOUT_SECONDS = 5.0
TERMINATION_TIMEOUT_SECONDS = 2.0


def _port(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= result <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return result


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate state key")
        value[key] = item
    return value


def _process_identity(pid: int) -> tuple[str, str, tuple[bytes, ...]] | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        command = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return None
    closing = stat.rfind(")")
    fields = stat[closing + 2 :].split()
    if closing < 0 or len(fields) < 20 or fields[0] == "Z" or not command:
        return None
    start_ticks = fields[19]
    digest = hashlib.sha256(command).hexdigest()
    arguments = tuple(part for part in command.split(b"\0") if part)
    return start_ticks, digest, arguments


def _process_fingerprint(pid: int) -> tuple[str, str] | None:
    identity = _process_identity(pid)
    if identity is None:
        return None
    return identity[0], identity[1]


def _state_identifies_workbench(state: dict[str, Any]) -> bool:
    identity = _process_identity(state["pid"])
    if identity is None or identity[:2] != (state["start_ticks"], state["cmdline_sha256"]):
        return False
    expected = (
        os.fsencode(ERW.parent / "python"),
        os.fsencode(ERW),
        b"workbench",
        b"--workspace",
        os.fsencode(WORKSPACE_PATH),
        b"--host",
        b"127.0.0.1",
        b"--port",
        str(state["port"]).encode("ascii"),
        b"--ready-fd",
    )
    arguments = identity[2]
    return (
        len(arguments) == len(expected) + 1
        and arguments[:-1] == expected
        and arguments[-1].isdigit()
        and 0 <= int(arguments[-1]) <= 1_048_576
    )


def _load_state() -> dict[str, Any] | None:
    try:
        raw = STATE_PATH.read_bytes()
    except FileNotFoundError:
        return None
    if len(raw) > 2_048:
        raise ValueError("workbench state is invalid")
    value = json.loads(raw, object_pairs_hook=_strict_object, parse_constant=lambda _: None)
    if not isinstance(value, dict) or set(value) != {
        "cmdline_sha256",
        "pid",
        "port",
        "schema",
        "start_ticks",
    }:
        raise ValueError("workbench state is invalid")
    if (
        value["schema"] != "erw-workbench-process-v1"
        or isinstance(value["pid"], bool)
        or not isinstance(value["pid"], int)
        or value["pid"] <= 0
        or isinstance(value["port"], bool)
        or not isinstance(value["port"], int)
        or not 1 <= value["port"] <= 65_535
        or not isinstance(value["start_ticks"], str)
        or not value["start_ticks"].isdecimal()
        or not isinstance(value["cmdline_sha256"], str)
        or len(value["cmdline_sha256"]) != 64
    ):
        raise ValueError("workbench state is invalid")
    return value


def _live_state() -> dict[str, Any] | None:
    state = _load_state()
    if state is None:
        return None
    if not _state_identifies_workbench(state):
        return None
    return state


def _write_state(state: dict[str, Any]) -> None:
    data = (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = STATE_PATH.with_suffix(".lock.new")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, STATE_PATH)
    except BaseException:
        with suppress(FileNotFoundError):
            temporary.unlink()
        raise


def _state_for_started_child(process: subprocess.Popen[bytes], port: int) -> dict[str, Any] | None:
    fingerprint = _process_fingerprint(process.pid)
    if fingerprint is None:
        return None
    return {
        "cmdline_sha256": fingerprint[1],
        "pid": process.pid,
        "port": port,
        "schema": "erw-workbench-process-v1",
        "start_ticks": fingerprint[0],
    }


def _terminate_started_child(process: subprocess.Popen[bytes], port: int) -> bool:
    """Stop only the captured child, retaining recoverable state if it stays alive."""
    if process.poll() is not None:
        STATE_PATH.unlink(missing_ok=True)
        return True
    state = _state_for_started_child(process, port)
    if state is None:
        return process.poll() is not None
    try:
        _write_state(state)
    except OSError:
        return False
    if not _state_identifies_workbench(state):
        return False
    try:
        os.kill(state["pid"], signal.SIGTERM)
    except ProcessLookupError:
        STATE_PATH.unlink(missing_ok=True)
        return True
    try:
        process.wait(timeout=TERMINATION_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            return False
    STATE_PATH.unlink(missing_ok=True)
    return True


def _open_directory(path: Path) -> int:
    absolute = Path(os.path.abspath(path))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(absolute.anchor, flags)
    try:
        for component in absolute.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _ensure_directory(path: Path) -> None:
    descriptor = _open_directory(path)
    os.close(descriptor)


def _open_regular(path: Path, flags: int) -> int:
    parent = _open_directory(path.parent)
    try:
        descriptor = os.open(
            path.name,
            flags | os.O_CLOEXEC | os.O_NOFOLLOW,
            mode=0o600,
            dir_fd=parent,
        )
    finally:
        os.close(parent)
    status = os.fstat(descriptor)
    if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
        os.close(descriptor)
        raise OSError("local control file must be a single regular file")
    return descriptor


def _open_append_log(path: Path) -> BinaryIO:
    descriptor = _open_regular(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    return os.fdopen(descriptor, "ab", buffering=0)


def _health(port: int) -> bool:
    connection = HTTPConnection("127.0.0.1", port, timeout=0.25)
    try:
        connection.request("GET", "/api/v1/fixtures", headers={"Host": f"127.0.0.1:{port}"})
        response = connection.getresponse()
        response.read(1)
        return response.status == 200
    except (OSError, HTTPException):
        return False
    finally:
        connection.close()


def _await_child_readiness(process: subprocess.Popen[bytes], descriptor: int, port: int) -> bool:
    expected = f"ERW_READY {port}\n".encode("ascii")
    received = bytearray()
    deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        readable, _, _ = select.select(
            [descriptor], [], [], min(0.05, max(0.0, deadline - time.monotonic()))
        )
        if not readable:
            continue
        chunk = os.read(descriptor, len(expected) + 1)
        if not chunk:
            return False
        received.extend(chunk)
        if len(received) > len(expected):
            return False
        if received.endswith(b"\n"):
            return bytes(received) == expected
    return False


@contextmanager
def _control_lock() -> Iterator[None]:
    """Serialize local lifecycle changes without treating a path as process identity."""
    descriptor = _open_regular(CONTROL_LOCK_PATH, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def start(port: int) -> int:
    with _control_lock():
        return _start_locked(port)


def _start_locked(port: int) -> int:
    if _live_state() is not None:
        print("Workbench is already running.", file=sys.stderr)
        return 2
    if STATE_PATH.exists():
        STATE_PATH.unlink()
    if not ERW.is_file():
        print(
            "Install the editable project in .venv before starting the workbench.", file=sys.stderr
        )
        return 2
    _ensure_directory(ARTIFACT_ROOT)
    _ensure_directory(WORKSPACE_PATH)
    ready_read, ready_write = os.pipe()
    command = [
        str(ERW),
        "workbench",
        "--workspace",
        str(WORKSPACE_PATH),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--ready-fd",
        str(ready_write),
    ]
    try:
        with _open_append_log(LOG_PATH) as log:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                close_fds=True,
                pass_fds=(ready_write,),
                start_new_session=True,
                env={
                    "LC_ALL": "C.UTF-8",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONHASHSEED": "0",
                },
            )
    finally:
        os.close(ready_write)
    try:
        ready = _await_child_readiness(process, ready_read, port)
    finally:
        os.close(ready_read)
    if not ready or not _health(port):
        stopped = _terminate_started_child(process, port)
        suffix = "" if stopped else " Trusted process state was retained for a later stop."
        print(f"Workbench did not become ready.{suffix}", file=sys.stderr)
        return 3
    state = _state_for_started_child(process, port)
    if state is None:
        _terminate_started_child(process, port)
        print("Workbench process identity could not be recorded.", file=sys.stderr)
        return 3
    try:
        _write_state(state)
    except OSError:
        _terminate_started_child(process, port)
        print("Workbench state could not be recorded.", file=sys.stderr)
        return 3
    print(f"Workbench ready at http://127.0.0.1:{port}")
    return 0


def status() -> int:
    state = _live_state()
    if state is None:
        print("Workbench is not running.")
        return 1
    print(f"Workbench is running at http://127.0.0.1:{state['port']}")
    return 0


def stop() -> int:
    with _control_lock():
        return _stop_locked()


def _stop_locked() -> int:
    try:
        state = _load_state()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if state is None:
        print("Workbench is already stopped.")
        return 0
    if not _state_identifies_workbench(state):
        print(
            "Workbench state does not identify a live process; refusing to signal it.",
            file=sys.stderr,
        )
        return 2
    os.kill(state["pid"], signal.SIGTERM)
    for _ in range(100):
        if _process_fingerprint(state["pid"]) is None:
            STATE_PATH.unlink(missing_ok=True)
            print("Workbench stopped.")
            return 0
        time.sleep(0.05)
    print("Workbench did not stop after SIGTERM; no stronger signal was sent.", file=sys.stderr)
    return 3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--port", type=_port, default=8765)
    subparsers.add_parser("status")
    subparsers.add_parser("stop")
    args = parser.parse_args()
    if args.command == "start":
        return start(int(args.port))
    if args.command == "status":
        return status()
    return stop()


if __name__ == "__main__":
    raise SystemExit(main())
