"""Mission 013 — durable portfolio-scheduler store (atomic, strict).

Persistence layout (all under ``<root>/goals/<goal_id>/scheduler/``)::

    state.json                 current scheduler state (strict schema 1)
    decisions/<decision_id>.json   immutable scheduling decisions
    events.jsonl               append-only bounded scheduler event log

Rules (ADR-011, matching the graph/mission stores):

* every write is atomic (temp file + fsync + rename) or append-only JSONL;
* every read is strict: unknown fields, unsupported schema versions,
  malformed values and identity mismatches fail closed — data is never
  guessed, repaired or rewritten;
* decisions are immutable and idempotent: re-persisting a byte-identical
  decision is a no-op, and any mismatch is a hard failure;
* reconstruction recomputes every decision identity from its content and
  rejects missing/untracked decision files, contradictory reservations and
  impossible resource accounting.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from trajectory_os.graph.scheduler import model
from trajectory_os.runs.store import atomic_write_json


def scheduler_paths(root: str | Path, goal_id: str) -> dict[str, Path]:
    base = Path(root) / "goals" / goal_id / "scheduler"
    return {
        "root": base,
        "state": base / "state.json",
        "decisions": base / "decisions",
        "events": base / "events.jsonl",
    }


def decision_path(paths: Mapping[str, Path], decision_id: str) -> Path:
    return paths["decisions"] / f"{decision_id}.json"


# --- state --------------------------------------------------------------------


def load_state(root: str | Path, goal_id: str) -> model.SchedulerState | None:
    """Strictly load the current scheduler state (None when absent)."""
    paths = scheduler_paths(root, goal_id)
    path = paths["state"]
    if not path.is_file():
        return None
    raw = _read_json(path)
    return model.SchedulerState.from_dict(raw, str(path))


def save_state(paths: Mapping[str, Path], state: model.SchedulerState) -> None:
    atomic_write_json(paths["state"], state.to_dict())


# --- decisions -----------------------------------------------------------------


def save_decision(paths: Mapping[str, Path], decision: model.ScheduleDecision,
                  ) -> bool:
    """Persist one decision. Returns True when newly written (False = no-op).

    A decision that already exists must be byte-semantically identical;
    any mismatch fails closed (``SCHEDULER_IDENTITY_MISMATCH``).
    """
    path = decision_path(paths, decision.decision_id)
    if path.is_file():
        existing = load_decision(paths, decision.decision_id)
        # Identity is timestamp-free: an identical decision is a no-op and
        # the original evidence timestamp is preserved (append-only history).
        if existing.identity_payload() != decision.identity_payload():
            raise model.SchedulerValidationError(
                model.E_IDENTITY_MISMATCH, str(path),
                "existing decision content differs")
        return False
    if len(list_decision_files(paths)) >= model.MAX_DECISIONS:
        raise model.SchedulerValidationError(
            model.E_OVERFLOW, str(paths["decisions"]),
            f"more than {model.MAX_DECISIONS} decisions")
    atomic_write_json(path, decision.to_dict())
    return True


def load_decision(paths: Mapping[str, Path],
                  decision_id: str) -> model.ScheduleDecision:
    path = decision_path(paths, decision_id)
    if not path.is_file():
        raise model.SchedulerValidationError(
            model.E_MISSING_DECISION, str(path), decision_id)
    raw = _read_json(path)
    decision = model.ScheduleDecision.from_dict(raw, str(path))
    if decision.decision_id != decision_id:
        raise model.SchedulerValidationError(
            model.E_IDENTITY_MISMATCH, str(path),
            f"file {decision_id} != decision {decision.decision_id}")
    return decision


def list_decision_files(paths: Mapping[str, Path]) -> list[str]:
    base = paths["decisions"]
    if not base.is_dir():
        return []
    return sorted(
        child.stem for child in base.iterdir()
        if child.is_file() and child.suffix == ".json"
    )


# --- events --------------------------------------------------------------------


def load_events(paths: Mapping[str, Path]) -> list[dict[str, Any]]:
    path = paths["events"]
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise model.SchedulerValidationError(
            model.E_EVENT_LOG, str(path), type(exc).__name__) from exc
    events: list[dict[str, Any]] = []
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError as exc:
            raise model.SchedulerValidationError(
                model.E_EVENT_LOG, str(path), f"line {index}") from exc
        if not isinstance(doc, dict):
            raise model.SchedulerValidationError(
                model.E_EVENT_LOG, str(path), f"line {index}")
        events.append(doc)
    return events


def append_event(paths: Mapping[str, Path], event: Mapping[str, Any]) -> None:
    existing = load_events(paths)
    if len(existing) >= model.MAX_SCHEDULER_EVENTS:
        raise model.SchedulerValidationError(
            model.E_OVERFLOW, str(paths["events"]),
            f"more than {model.MAX_SCHEDULER_EVENTS} events")
    path = paths["events"]
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dict(event), sort_keys=True, separators=(",", ":"))
    with path.open("ab") as handle:
        handle.write(line.encode("utf-8") + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


# --- reconstruction ------------------------------------------------------------


def reconstruct(root: str | Path, goal_id: str,
                graph: Any) -> model.ReconstructedScheduler:
    """Strictly reconstruct persisted scheduler state for one goal graph.

    Fails closed on: missing/untracked decision files, schema/version
    mismatch, graph identity mismatch, duplicate selected identities,
    contradictory reservations and impossible CPU/GPU accounting.
    """
    paths = scheduler_paths(root, goal_id)
    state = load_state(root, goal_id)
    if state is None:
        raise model.SchedulerValidationError(
            model.E_MALFORMED, str(paths["state"]), "scheduler state missing")
    if state.goal_id != goal_id:
        raise model.SchedulerValidationError(
            model.E_GRAPH_MISMATCH, str(paths["state"]),
            f"goal {state.goal_id} != {goal_id}")
    graph_id = getattr(graph, "graph_id", None)
    if graph_id is not None and state.graph_id != graph_id:
        raise model.SchedulerValidationError(
            model.E_GRAPH_MISMATCH, str(paths["state"]),
            f"graph {state.graph_id} != {graph_id}")

    tracked = set(state.decision_ids)
    on_disk = set(list_decision_files(paths))
    untracked = sorted(on_disk - tracked)
    if untracked:
        raise model.SchedulerValidationError(
            model.E_UNTRACKED_DECISION, str(paths["decisions"]),
            f"untracked decision file(s): {untracked}")
    missing = sorted(tracked - on_disk)
    if missing:
        raise model.SchedulerValidationError(
            model.E_MISSING_DECISION, str(paths["decisions"]),
            f"missing decision file(s): {missing}")

    decisions: list[model.ScheduleDecision] = []
    for decision_id in state.decision_ids:
        decision = load_decision(paths, decision_id)
        if decision.goal_id != state.goal_id:
            raise model.SchedulerValidationError(
                model.E_GRAPH_MISMATCH, str(paths["decisions"]),
                f"decision goal {decision.goal_id}")
        if decision.graph_id != state.graph_id:
            raise model.SchedulerValidationError(
                model.E_GRAPH_MISMATCH, str(paths["decisions"]),
                f"decision graph {decision.graph_id}")
        decisions.append(decision)

    known_decisions = set(state.decision_ids)
    for record in state.dispatch_records:
        if record.decision_id not in known_decisions:
            raise model.SchedulerValidationError(
                model.E_DISPATCH_EVIDENCE, str(paths["state"]),
                f"dispatch references unknown decision {record.decision_id}")

    latest = decisions[-1] if decisions else None
    if state.last_decision_id is not None and (
            latest is None or latest.decision_id != state.last_decision_id):
        raise model.SchedulerValidationError(
            model.E_CONTRADICTORY_RESERVATION, str(paths["state"]),
            "last_decision_id is not the newest decision")
    events = load_events(paths)
    return model.ReconstructedScheduler(
        state=state,
        latest_decision=latest,
        decisions=tuple(decisions),
        events=tuple(events),
        graph_identity=(None if graph_id is None else str(graph_id)),
        spec_identity=(None if graph is None
                       else str(getattr(graph, "spec_sha256", ""))),
    )


# --- helpers -------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise model.SchedulerValidationError(
            model.E_MALFORMED, str(path), type(exc).__name__) from exc
    if not isinstance(raw, dict):
        raise model.SchedulerValidationError(
            model.E_MALFORMED, str(path), "top-level object expected")
    return raw


def state_exists(root: str | Path, goal_id: str) -> bool:
    return scheduler_paths(root, goal_id)["state"].is_file()
