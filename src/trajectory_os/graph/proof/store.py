"""Mission 016 — durable goal-proof store (atomic, strict, append-only).

Persistence layout (all under ``<root>/goals/<goal_id>/proof/``)::

    projection.json   the exact versioned derived goal-proof projection
    events.jsonl      append-only proof/operator decision history

Rules (ADR-015, matching the mission/graph/scheduler/reuse/replan stores):

* the projection is **derived evidence**: it is written atomically for later
  reconstruction and is never a second source of truth;
* every read is strict: unknown fields, unsupported schema versions,
  malformed values, identity mismatches and impossible states fail closed
  with :class:`~trajectory_os.graph.proof.model.GoalProofError` — data is
  never guessed, repaired or rewritten;
* reconstruction recomputes the proof identity from canonical live evidence
  and fails closed on any mismatch;
* proof/operator events are append-only and content-addressed; re-appending
  byte-identical content is an idempotent no-op.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, NoReturn

from trajectory_os.graph.proof import identity as proof_identity
from trajectory_os.graph.proof import model
from trajectory_os.runs.store import atomic_write_json

#: Hard bounded number of retainable proof events for one goal.
MAX_EVENTS = 512


@dataclass(frozen=True)
class ProofEvent:
    """One append-only goal-proof operator/proof event (content-addressed)."""

    event: str
    status: str
    reason: str
    goal_id: str
    graph_id: str
    generation_id: str | None
    proof_id: str
    final_state: str
    computed_at: str
    event_id: str = ""

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "proof_version": model.PROOF_VERSION,
            "event": self.event,
            "status": self.status,
            "reason": self.reason,
            "goal_id": self.goal_id,
            "graph_id": self.graph_id,
            "generation_id": self.generation_id,
            "proof_id": self.proof_id,
            "final_state": self.final_state,
        }

    def compute_event_id(self) -> str:
        return proof_identity.event_id(self.identity_payload())

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "event_id": self.event_id,
                "computed_at": self.computed_at}

    @staticmethod
    def build(*, event: str, status: str, reason: str, goal_id: str,
              graph_id: str, generation_id: str | None, proof_id: str,
              final_state: str, computed_at: str) -> ProofEvent:
        record = ProofEvent(
            event=event, status=status, reason=reason, goal_id=goal_id,
            graph_id=graph_id, generation_id=generation_id, proof_id=proof_id,
            final_state=final_state, computed_at=computed_at)
        return replace(record, event_id=record.compute_event_id())

    @staticmethod
    def from_dict(doc: object, path: str = "event") -> ProofEvent:
        mapping = _mapping(doc, path, "proof event required")
        keys = tuple(ProofEvent.__dataclass_fields__)
        _known_keys(mapping, (*keys, "schema_version", "proof_version"),
                    path)
        _require_keys(mapping, keys, path)
        state = mapping["final_state"]
        if state not in model.GOAL_STATES:
            _fail(model.E_MALFORMED, path, f"final_state={state!r}")
        if mapping.get("schema_version") != model.SCHEMA_VERSION:
            _fail(model.E_UNSUPPORTED_VERSION, path,
                  f"schema_version={mapping.get('schema_version')!r}")
        record = ProofEvent(
            event=_str(mapping["event"], path, "event", maximum=64),
            status=_str(mapping["status"], path, "status", maximum=64),
            reason=_reason(mapping["reason"], path),
            goal_id=_str(mapping["goal_id"], path, "goal_id", maximum=64),
            graph_id=_digest(mapping["graph_id"], path, "graph_id"),
            generation_id=_optional_digest(mapping["generation_id"], path,
                                           "generation_id"),
            proof_id=_digest(mapping["proof_id"], path, "proof_id"),
            final_state=state,
            computed_at=_str(mapping["computed_at"], path, "computed_at",
                             maximum=model.MAX_TIMESTAMP_LEN),
            event_id=_digest(mapping["event_id"], path, "event_id"),
        )
        if record.compute_event_id() != record.event_id:
            _fail(model.E_IDENTITY_MISMATCH, path, record.event_id)
        return record


def _fail(code: str, path: str, detail: str = "") -> NoReturn:
    raise model.GoalProofError(code, path, detail)


def _mapping(value: object, path: str, detail: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        _fail(model.E_MALFORMED, path, detail)
    return value


def _require_keys(doc: Mapping[str, Any], required: tuple[str, ...],
                  path: str) -> None:
    missing = [key for key in required if key not in doc]
    if missing:
        _fail(model.E_MALFORMED, path, f"missing field(s): {missing}")


def _known_keys(doc: Mapping[str, Any], allowed: tuple[str, ...],
                path: str) -> None:
    unknown = set(doc) - set(allowed)
    if unknown:
        _fail(model.E_MALFORMED, path, f"unknown field(s): {sorted(unknown)}")


def _str(value: object, path: str, detail: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value:
        _fail(model.E_MALFORMED, path, detail)
    if len(value) > maximum:
        _fail(model.E_MALFORMED, path, f"{detail} exceeds {maximum} chars")
    return value


def _digest(value: object, path: str, detail: str) -> str:
    if not proof_identity.is_valid_digest(value):
        _fail(model.E_MALFORMED, path, detail)
    assert isinstance(value, str)
    return value


def _optional_digest(value: object, path: str, detail: str) -> str | None:
    if value is None:
        return None
    return _digest(value, path, detail)


def _reason(value: object, path: str) -> str:
    if not isinstance(value, str) or value not in model.REASON_CODES:
        _fail(model.E_MALFORMED, path, f"unknown reason code: {value!r}")
    return value


def proof_paths(root: str | Path, goal_id: str) -> dict[str, Path]:
    base = Path(root) / "goals" / goal_id / "proof"
    return {
        "root": base,
        "projection": base / "projection.json",
        "events": base / "events.jsonl",
    }


def projection_exists(root: str | Path, goal_id: str) -> bool:
    return proof_paths(root, goal_id)["projection"].is_file()


def load_projection(root: str | Path, goal_id: str) -> model.GoalProof | None:
    """Strictly load the persisted goal-proof projection (None when absent)."""
    path = proof_paths(root, goal_id)["projection"]
    if not path.is_file():
        return None
    return model.GoalProof.from_dict(_read_json(path), str(path))


def save_projection(root: str | Path, proof: model.GoalProof) -> None:
    paths = proof_paths(root, proof.goal_id)
    paths["root"].mkdir(parents=True, exist_ok=True)
    atomic_write_json(paths["projection"], proof.to_dict())


def load_events(root: str | Path, goal_id: str) -> list[ProofEvent]:
    path = proof_paths(root, goal_id)["events"]
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise model.GoalProofError(
            model.E_MALFORMED, str(path), type(exc).__name__) from exc
    events: list[ProofEvent] = []
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError as exc:
            raise model.GoalProofError(
                model.E_MALFORMED, str(path), f"line {index}") from exc
        events.append(ProofEvent.from_dict(doc, f"{path}[line {index}]"))
    return events


def append_event(root: str | Path, event: ProofEvent) -> bool:
    """Append one event; byte-identical re-append is an idempotent no-op."""
    path = proof_paths(root, event.goal_id)["events"]
    existing = load_events(root, event.goal_id)
    if len(existing) >= MAX_EVENTS:
        raise model.GoalProofError(
            model.E_OVERFLOW, str(path), f"more than {MAX_EVENTS} events")
    for item in existing:
        if item.event_id == event.event_id:
            if item.identity_payload() != event.identity_payload():
                raise model.GoalProofError(
                    model.E_IDENTITY_MISMATCH, str(path), event.event_id)
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event.to_dict(), sort_keys=True,
                      separators=(",", ":"))
    with path.open("ab") as handle:
        handle.write(line.encode("utf-8") + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    return True


def record_proof(root: str, goal_id: str, *,
                 computed_at: str) -> tuple[model.GoalProof, ProofEvent, bool]:
    """Build, persist and record one derived goal-proof snapshot.

    The projection is written atomically and one content-addressed event is
    appended. Re-recording an unchanged proof is idempotent.
    """
    from trajectory_os.graph.proof import engine

    proof = engine.build_proof(root, goal_id, computed_at=computed_at)
    save_projection(root, proof)
    event = ProofEvent.build(
        event="goal_proof_recorded",
        status="COMPLETE" if proof.complete else "INCOMPLETE",
        reason=proof.final_reason,
        goal_id=proof.goal_id,
        graph_id=proof.graph_id,
        generation_id=(None if proof.generation is None
                       else proof.generation.generation_id),
        proof_id=proof.proof_id,
        final_state=proof.final_state,
        computed_at=computed_at,
    )
    appended = append_event(root, event)
    return proof, event, appended


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise model.GoalProofError(
            model.E_MALFORMED, str(path), type(exc).__name__) from exc
    if not isinstance(raw, dict):
        raise model.GoalProofError(
            model.E_MALFORMED, str(path), "top-level object expected")
    return raw
