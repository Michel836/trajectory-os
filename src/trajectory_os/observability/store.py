"""M030 — durable canonical artifacts (one source of truth per run).

Layout under ``<root>/<run_id>/``::

    events.jsonl     append-only canonical operator timeline
    status.json      authoritative current run status
    telemetry.json   authoritative run telemetry document
    summary.json     deterministic aggregate

All JSON writes are atomic (temp file + ``os.replace``); every read fails
closed on malformed content. Observation surfaces read these artifacts; they
never reconstruct current truth by grepping historical logs.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from trajectory_os.observability import model

EVENTS_NAME = "events.jsonl"
STATUS_NAME = "status.json"
TELEMETRY_NAME = "telemetry.json"
SUMMARY_NAME = "summary.json"


class CanonicalStoreError(Exception):
    """Malformed or untrusted persisted observability state (fail closed)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def run_root(root: str | Path, run_id: str) -> Path:
    return Path(root) / run_id


def ensure_run_root(root: str | Path, run_id: str) -> Path:
    path = run_root(root, run_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=f".tmp.{os.getpid()}",
        dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def read_json(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CanonicalStoreError("MISSING", str(path)) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CanonicalStoreError(
            "MALFORMED", f"{path}: {type(exc).__name__}") from exc
    if not isinstance(document, dict):
        raise CanonicalStoreError("MALFORMED", f"{path}: object required")
    return document


def write_status(root: Path, status: model.CanonicalStatus) -> None:
    write_json(root / STATUS_NAME, status.to_dict())


def load_status(root: Path) -> dict[str, Any]:
    return read_json(root / STATUS_NAME)


def write_telemetry(root: Path, document: Mapping[str, Any]) -> None:
    write_json(root / TELEMETRY_NAME, dict(document))


def load_telemetry(root: Path) -> dict[str, Any]:
    return read_json(root / TELEMETRY_NAME)


def write_summary(root: Path, document: Mapping[str, Any]) -> None:
    write_json(root / SUMMARY_NAME, dict(document))


def load_summary(root: Path) -> dict[str, Any]:
    return read_json(root / SUMMARY_NAME)


def append_event(root: Path, event: model.CanonicalEvent) -> None:
    path = root / EVENTS_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event.to_dict(), sort_keys=True,
                      separators=(",", ":"), default=str)
    with path.open("ab") as handle:
        handle.write(line.encode("utf-8") + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_events(root: Path) -> list[dict[str, Any]]:
    path = root / EVENTS_NAME
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CanonicalStoreError(
            "MALFORMED_EVENTS", type(exc).__name__) from exc
    out: list[dict[str, Any]] = []
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        try:
            document = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CanonicalStoreError(
                "MALFORMED_EVENTS", f"line {index}") from exc
        if not isinstance(document, dict):
            raise CanonicalStoreError("MALFORMED_EVENTS", f"line {index}")
        out.append(document)
    return out


def event_count(root: Path) -> int:
    return len(load_events(root))
