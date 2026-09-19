"""M020 — durable multi-goal portfolio store (atomic, strict).

Persistence layout (all under ``<root>/portfolio/``)::

    state.json                     current portfolio state (strict schema 1)
    decisions/<decision_id>.json   immutable portfolio decisions
    events.jsonl                   append-only bounded portfolio event log

Rules match the graph/mission/scheduler stores:

* every write is atomic (temp file + fsync + rename) or append-only JSONL;
* every read is strict: unknown fields, unsupported schema versions and
  identity mismatches fail closed;
* decisions are immutable and idempotent.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from trajectory_os.portfolio import model
from trajectory_os.runs.store import atomic_write_json


def portfolio_paths(root: str | Path) -> dict[str, Path]:
    base = Path(root) / "portfolio"
    return {
        "root": base,
        "state": base / "state.json",
        "decisions": base / "decisions",
        "events": base / "events.jsonl",
    }


def decision_path(paths: Mapping[str, Path], decision_id: str) -> Path:
    return paths["decisions"] / f"{decision_id}.json"


def _read_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise model.PortfolioError(
            model.E_MALFORMED, str(path), type(exc).__name__) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise model.PortfolioError(
            model.E_MALFORMED, str(path), "invalid JSON") from exc


# --- state --------------------------------------------------------------------


def load_state(root: str | Path) -> model.PortfolioState | None:
    path = portfolio_paths(root)["state"]
    if not path.is_file():
        return None
    return model.PortfolioState.from_dict(_read_json(path), str(path))


def save_state(paths: Mapping[str, Path], state: model.PortfolioState) -> None:
    atomic_write_json(paths["state"], state.to_dict())


# --- decisions -----------------------------------------------------------------


def list_decision_files(paths: Mapping[str, Path]) -> list[str]:
    base = paths["decisions"]
    if not base.is_dir():
        return []
    return sorted(
        child.stem for child in base.iterdir()
        if child.is_file() and child.suffix == ".json"
    )


def save_decision(paths: Mapping[str, Path], decision: model.PortfolioDecision,
                  ) -> bool:
    """Persist one decision. Returns True when newly written (False = no-op)."""
    path = decision_path(paths, decision.decision_id)
    if path.is_file():
        existing = load_decision(paths, decision.decision_id)
        if existing.identity_payload() != decision.identity_payload():
            raise model.PortfolioError(
                model.E_IDENTITY_MISMATCH, str(path),
                "existing decision content differs")
        return False
    if len(list_decision_files(paths)) >= model.MAX_DECISIONS:
        raise model.PortfolioError(
            model.E_OVERFLOW, str(paths["decisions"]),
            f"more than {model.MAX_DECISIONS} decisions")
    atomic_write_json(path, decision.to_dict())
    return True


def load_decision(paths: Mapping[str, Path],
                  decision_id: str) -> model.PortfolioDecision:
    path = decision_path(paths, decision_id)
    if not path.is_file():
        raise model.PortfolioError(
            model.E_MALFORMED, str(path), f"missing decision {decision_id}")
    decision = model.PortfolioDecision.from_dict(_read_json(path), str(path))
    if decision.decision_id != decision_id:
        raise model.PortfolioError(
            model.E_IDENTITY_MISMATCH, str(path),
            f"file {decision_id} != decision {decision.decision_id}")
    return decision


# --- events --------------------------------------------------------------------


def load_events(paths: Mapping[str, Path]) -> list[dict[str, Any]]:
    path = paths["events"]
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise model.PortfolioError(
            model.E_MALFORMED, str(path), type(exc).__name__) from exc
    events: list[dict[str, Any]] = []
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError as exc:
            raise model.PortfolioError(
                model.E_MALFORMED, str(path), f"line {index}") from exc
        if not isinstance(doc, dict):
            raise model.PortfolioError(
                model.E_MALFORMED, str(path), f"line {index}")
        events.append(doc)
    return events


def append_event(paths: Mapping[str, Path],
                 event: Mapping[str, Any]) -> None:
    existing = load_events(paths)
    if len(existing) >= model.MAX_EVENTS:
        raise model.PortfolioError(
            model.E_OVERFLOW, str(paths["events"]),
            f"more than {model.MAX_EVENTS} events")
    path = paths["events"]
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dict(event), sort_keys=True, separators=(",", ":"))
    with path.open("ab") as handle:
        handle.write(line.encode("utf-8") + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


# --- reconstruction -----------------------------------------------------------


def reconstruct(root: str | Path) -> model.PortfolioState:
    """Strictly reconstruct the durable portfolio state (fail closed)."""
    paths = portfolio_paths(root)
    state = load_state(root)
    if state is None:
        raise model.PortfolioError(
            model.E_MALFORMED, str(paths["state"]), "portfolio state missing")
    tracked = set(state.decision_ids)
    on_disk = set(list_decision_files(paths))
    untracked = sorted(on_disk - tracked)
    if untracked:
        raise model.PortfolioError(
            model.E_MALFORMED, str(paths["decisions"]),
            f"untracked decision file(s): {untracked}")
    missing = sorted(tracked - on_disk)
    if missing:
        raise model.PortfolioError(
            model.E_MALFORMED, str(paths["decisions"]),
            f"missing decision file(s): {missing}")
    for decision_id in state.decision_ids:
        decision = load_decision(paths, decision_id)
        if decision.portfolio_id != state.portfolio_id:
            raise model.PortfolioError(
                model.E_ISOLATION_VIOLATION, str(paths["decisions"]),
                f"decision portfolio {decision.portfolio_id}")
        model.assert_isolation(decision.entries)
    return state
