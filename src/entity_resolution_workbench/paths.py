from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path


class PathSafetyError(ValueError):
    pass


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def validate_paths(
    root: Path,
    *,
    inputs: Iterable[Path] = (),
    outputs: Iterable[Path] = (),
    locks: Iterable[Path] = (),
    temporaries: Iterable[Path] = (),
) -> None:
    """Reject lexical escapes, symlink components, collisions, and existing inode aliases."""
    root_absolute = _lexical_absolute(root)
    if os.path.lexists(root_absolute) and root_absolute.is_symlink():
        raise PathSafetyError("workspace root is a symlink")
    if not root_absolute.is_dir():
        raise PathSafetyError("workspace root must be an existing directory")
    values = [*inputs, *outputs, *locks, *temporaries]
    normalized: list[Path] = []
    for value in values:
        absolute = _lexical_absolute(value)
        try:
            relative = absolute.relative_to(root_absolute)
        except ValueError as exc:
            raise PathSafetyError("artifact escapes workspace") from exc
        cursor = root_absolute
        for part in relative.parts:
            cursor /= part
            if os.path.lexists(cursor) and cursor.is_symlink():
                raise PathSafetyError("artifact has a symlink component")
        normalized.append(absolute)
    if len(set(normalized)) != len(normalized):
        raise PathSafetyError("artifact paths collide after normalization")
    identities: set[tuple[int, int]] = set()
    for path in normalized:
        if os.path.lexists(path):
            stat_result = os.stat(path)
            identity = (stat_result.st_dev, stat_result.st_ino)
            if identity in identities:
                raise PathSafetyError("artifacts alias the same inode")
            identities.add(identity)


def validate_artifact_paths(
    root: Path, *, database: Path, predictions: Path, report_json: Path, report_html: Path
) -> None:
    validate_paths(
        root,
        outputs=(database, predictions, report_json, report_html),
        locks=(database.with_suffix(database.suffix + ".lock"),),
    )
