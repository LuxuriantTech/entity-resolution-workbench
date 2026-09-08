from __future__ import annotations

from .common import canonical_bytes, digest_bytes


def run_key(envelope: dict[str, object]) -> str:
    return digest_bytes(canonical_bytes(envelope))
