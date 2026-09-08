"""Contract-test helpers.  Imports are deliberately delayed for the RED phase."""

from __future__ import annotations

import importlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def module(name: str) -> Any:
    """Load a production module or fail with a useful RED-only assertion."""
    try:
        return importlib.import_module(f"entity_resolution_workbench.{name}")
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("entity_resolution_workbench"):
            pytest.fail("production package is not implemented", pytrace=False)
        raise


def write_csv(path: Path, rows: list[dict[str, str]]) -> Path:
    headers = ["source_id", "name", "brand", "sku", "category", "price"]
    lines = [",".join(headers)]
    for row in rows:
        lines.append(",".join(row.get(header, "") for header in headers))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def record(source_id: str, **fields: str) -> dict[str, str]:
    return {"source_id": source_id, **fields}


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def config_in(workspace_root: Path) -> Path:
    """Put the preregistered config beneath the explicit test workspace."""
    target = workspace_root / "config" / "matching-v1.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PROJECT_ROOT / "config" / "matching-v1.json", target)
    return target
