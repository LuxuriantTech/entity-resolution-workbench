from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from conftest import module


def test_each_cli_command_executes_the_frozen_vertical_slice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = module("cli")
    for command in ("generate", "match", "evaluate", "audit"):
        assert cli.main([command, "--workspace", str(tmp_path)]) == 0
        output = json.loads(capsys.readouterr().out)
        assert output["command"] == command and output["ok"] is True
    assert (tmp_path / "data" / "observed-manifest.json").is_file()
    assert (tmp_path / "predictions.json").is_file()
    assert (tmp_path / "report.json").is_file()
    assert (tmp_path / "report.html").is_file()


def test_cli_maps_integrity_failure_to_exit_three_without_success_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = module("cli")
    assert cli.main(["generate", "--workspace", str(tmp_path)]) == 0
    capsys.readouterr()
    observed = tmp_path / "data" / "observed" / "calibration" / "supplier_a.csv"
    with observed.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    rows[1][1] += " tampered"
    with observed.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle, lineterminator="\n").writerows(rows)
    assert cli.main(["match", "--workspace", str(tmp_path)]) == 3
    captured = capsys.readouterr()
    assert captured.out == "" and "error:" in captured.err


def test_cli_maps_live_writer_lock_to_exit_four(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = module("cli")
    assert cli.main(["generate", "--workspace", str(tmp_path)]) == 0
    capsys.readouterr()
    with module("database").exclusive_lock(tmp_path / "state.sqlite"):
        assert cli.main(["match", "--workspace", str(tmp_path)]) == 4
    captured = capsys.readouterr()
    assert captured.out == "" and "error:" in captured.err
