from __future__ import annotations

from pathlib import Path

import pytest
from conftest import module


def test_workbench_cli_starts_only_loopback_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, int, int | None]] = []
    monkeypatch.setattr(
        module("web_server"),
        "serve",
        lambda *, host, port, ready_fd: calls.append((host, port, ready_fd)),
    )

    status = module("cli").main(
        [
            "workbench",
            "--workspace",
            str(tmp_path),
            "--host",
            "127.0.0.1",
            "--port",
            "43127",
        ]
    )

    assert status == 0
    assert calls == [("127.0.0.1", 43127, None)]


@pytest.mark.parametrize("host", ["0.0.0.0", "localhost", "::1", "example.test"])
def test_workbench_cli_rejects_every_nonliteral_ipv4_loopback(tmp_path: Path, host: str) -> None:
    with pytest.raises(SystemExit) as caught:
        module("cli").main(
            ["workbench", "--workspace", str(tmp_path), "--host", host, "--port", "8765"]
        )
    assert caught.value.code == 2


@pytest.mark.parametrize("port", ["0", "65536", "not-a-port"])
def test_workbench_cli_rejects_invalid_ports(tmp_path: Path, port: str) -> None:
    with pytest.raises(SystemExit) as caught:
        module("cli").main(
            [
                "workbench",
                "--workspace",
                str(tmp_path),
                "--host",
                "127.0.0.1",
                "--port",
                port,
            ]
        )
    assert caught.value.code == 2
