"""M040–M047 — shared deterministic helpers for the operator platform.

Pure, stdlib-only helpers: canonical JSON, domain-separated digests, UTC
timestamps and bounded, fail-closed JSON/JSONL IO. Nothing here owns state;
the canonical mission/release artifacts remain authoritative.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trajectory_os.observability import store as obs_store


def canonical_json(payload: object) -> str:
    """Deterministic JSON text: sorted keys, compact separators, ASCII."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        default=str)


def digest(payload: object, *, domain: str) -> str:
    """SHA-256 hex digest of ``domain || 0x00 || canonical_json``."""
    material = (domain.encode("utf-8") + b"\x00"
                + canonical_json(payload).encode("utf-8"))
    return hashlib.sha256(material).hexdigest()


def utc_now() -> str:
    """Canonical UTC timestamp (seconds precision, ``Z`` suffix)."""
    return (datetime.now(UTC).replace(microsecond=0).isoformat()
            .replace("+00:00", "Z"))


def optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def read_optional_json(path: Path) -> dict[str, Any] | None:
    """Read an optional JSON object; missing/unreadable/malformed -> None."""
    try:
        return obs_store.read_json(path)
    except obs_store.CanonicalStoreError:
        return None


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomic, fail-closed JSON write (reuses the canonical atomic writer)."""
    obs_store.write_json(path, payload)


def append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    """Append one canonical JSON line, fsynced, creating parents as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = canonical_json(dict(record))
    with path.open("ab") as handle:
        handle.write(line.encode("utf-8") + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read an append-only JSONL file; missing file -> empty list."""
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            document = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(document, dict):
            out.append(document)
    return out


__all__ = [
    "append_jsonl",
    "canonical_json",
    "digest",
    "optional_int",
    "optional_str",
    "read_jsonl",
    "read_optional_json",
    "utc_now",
    "write_json",
]
