from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import pytest
from conftest import module


@pytest.mark.parametrize(
    "reader",
    [
        lambda path: module("common").read_json_object(path),
        lambda path: module("evaluator")._read_truth_csv(path),
    ],
)
def test_oversized_structured_input_is_bounded_during_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader: object
) -> None:
    path = tmp_path / "oversized.input"
    path.write_bytes(b"x" * 1_000_001)

    def forbid_unbounded_read(_path: Path) -> NoReturn:
        raise AssertionError("Path.read_bytes materializes input before enforcing its bound")

    monkeypatch.setattr(Path, "read_bytes", forbid_unbounded_read)
    with pytest.raises(module("common").ResourceBoundError):
        reader(path)  # type: ignore[operator]


def test_publication_rejects_size_mismatch_before_materializing_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "artifact.json"
    path.write_bytes(b"divergent and longer")

    def forbid_unbounded_read(_path: Path) -> NoReturn:
        raise AssertionError("publication must compare file size before reading content")

    monkeypatch.setattr(Path, "read_bytes", forbid_unbounded_read)
    with pytest.raises(module("common").IntegrityError):
        module("publication").publish_bytes(path, b"expected")
