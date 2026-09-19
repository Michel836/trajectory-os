"""M021 — durable daemon store (atomic, strict, append-only).

Layout (all under ``<root>/daemon/``)::

    state.json     current daemon runtime state (strict schema 1)
    cycles.jsonl   append-only bounded daemon cycle log

The daemon never stores authoritative work state: only cycle accounting and
the exact portfolio decision identities that were already persisted.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from trajectory_os.daemon import model
from trajectory_os.runs.store import atomic_write_json


def daemon_paths(root: str | Path) -> dict[str, Path]:
    base = Path(root) / "daemon"
    return {
        "root": base,
        "state": base / "state.json",
        "cycles": base / "cycles.jsonl",
        "stop": base / "stop.json",
    }


def _read_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise model.DaemonError(
            model.E_MALFORMED, str(path), type(exc).__name__) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise model.DaemonError(
            model.E_MALFORMED, str(path), "invalid JSON") from exc


def load_state(root: str | Path) -> model.DaemonState | None:
    path = daemon_paths(root)["state"]
    if not path.is_file():
        return None
    return model.DaemonState.from_dict(_read_json(path), str(path))


def save_state(paths: Mapping[str, Path], state: model.DaemonState) -> None:
    atomic_write_json(paths["state"], state.to_dict())


def load_cycles(paths: Mapping[str, Path]) -> list[model.DaemonCycle]:
    path = paths["cycles"]
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise model.DaemonError(
            model.E_MALFORMED, str(path), type(exc).__name__) from exc
    cycles: list[model.DaemonCycle] = []
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError as exc:
            raise model.DaemonError(
                model.E_MALFORMED, str(path), f"line {index}") from exc
        cycles.append(model.DaemonCycle.from_dict(doc, f"{path}[{index}]"))
    return cycles


def append_cycle(paths: Mapping[str, Path], cycle: model.DaemonCycle) -> None:
    existing = load_cycles(paths)
    if len(existing) >= model.MAX_CYCLE_HISTORY:
        raise model.DaemonError(
            model.E_OVERFLOW, str(paths["cycles"]),
            f"more than {model.MAX_CYCLE_HISTORY} cycles")
    path = paths["cycles"]
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(cycle.to_dict(), sort_keys=True, separators=(",", ":"))
    with path.open("ab") as handle:
        handle.write(line.encode("utf-8") + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def reconstruct(root: str | Path) -> tuple[model.DaemonState,
                                           tuple[model.DaemonCycle, ...]]:
    """Strictly reconstruct durably persisted daemon state (fail closed)."""
    paths = daemon_paths(root)
    state = load_state(root)
    if state is None:
        raise model.DaemonError(
            model.E_MALFORMED, str(paths["state"]), "daemon state missing")
    cycles = load_cycles(paths)
    if len(cycles) != state.cycles_executed:
        raise model.DaemonError(
            model.E_IDENTITY_MISMATCH, str(paths["cycles"]),
            f"cycles={len(cycles)} != cycles_executed="
            f"{state.cycles_executed}")
    seen: set[str] = set()
    for cycle in cycles:
        if cycle.decision_id is not None:
            if cycle.decision_id in seen:
                raise model.DaemonError(
                    model.E_IDENTITY_MISMATCH, str(paths["cycles"]),
                    f"duplicate decision {cycle.decision_id}")
            seen.add(cycle.decision_id)
    for decision_id in state.decision_ids:
        if decision_id not in seen:
            raise model.DaemonError(
                model.E_IDENTITY_MISMATCH, str(paths["state"]),
                f"untracked decision {decision_id}")
    last = state.last_cycle
    if last is not None and cycles and cycles[-1] != last:
        raise model.DaemonError(
            model.E_IDENTITY_MISMATCH, str(paths["state"]),
            "last_cycle does not match the cycle log tail")
    return state, tuple(cycles)
