"""Mission 015 — durable adaptive-replanning store (atomic, strict).

Persistence layout (all under ``<root>/goals/<goal_id>/replan/``)::

    current.json                pointer to the active generation
    events.jsonl                append-only replan event history
    generations/<id>.json       archived generation metadata + exact graph

The **active** graph document stays at ``<root>/goals/<goal_id>/graph.json``
so the M012 graph store and the M013 scheduler keep consuming the canonical
graph surface unchanged. Replanning archives the prior generation (metadata
plus its exact normalized graph) before atomically activating the new one.

Rules (ADR-013, matching the graph/scheduler/mission/reuse stores):

* every write is atomic (temp file + fsync + rename) or append-only JSONL;
* every read is strict: unknown fields, unsupported schema versions,
  malformed values and identity mismatches fail closed — data is never
  guessed, repaired or rewritten;
* reconstruction replays the whole generation chain and recomputes every
  generation identity from content, rejecting broken parent links, duplicate
  or out-of-order generations and an active graph that no longer matches the
  active generation;
* prior generations are never deleted or rewritten.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trajectory_os.graph import model as graph_model
from trajectory_os.graph.replan import model
from trajectory_os.runs.store import atomic_write_json


def replan_paths(root: str | Path, goal_id: str) -> dict[str, Path]:
    base = Path(root) / "goals" / goal_id / "replan"
    return {
        "root": base,
        "current": base / "current.json",
        "events": base / "events.jsonl",
        "generations": base / "generations",
    }


def generation_path(paths: Mapping[str, Path], generation_id: str) -> Path:
    return paths["generations"] / f"{generation_id}.json"


def replan_exists(root: str | Path, goal_id: str) -> bool:
    return (replan_paths(root, goal_id)["current"]).is_file()


# --- current pointer ----------------------------------------------------------


def load_current(root: str | Path, goal_id: str) -> model.Generation | None:
    path = replan_paths(root, goal_id)["current"]
    if not path.is_file():
        return None
    doc = _read_json(path)
    _known_keys(doc, ("generation",), str(path))
    if "generation" not in doc:
        raise model.ReplanValidationError(
            model.E_MALFORMED, str(path), "missing generation")
    return model.Generation.from_dict(doc["generation"], f"{path}[generation]")


def save_current(paths: Mapping[str, Path],
                 generation: model.Generation) -> None:
    atomic_write_json(paths["current"], {"generation": generation.to_dict()})


# --- generations --------------------------------------------------------------


def load_generation(paths: Mapping[str, Path],
                    generation_id: str) -> tuple[model.Generation,
                                                 graph_model.GoalGraph]:
    path = generation_path(paths, generation_id)
    if not path.is_file():
        raise model.ReplanValidationError(
            model.E_MALFORMED, str(path), "generation not archived")
    doc = _read_json(path)
    _known_keys(doc, ("generation", "graph"), str(path))
    if "generation" not in doc or "graph" not in doc:
        raise model.ReplanValidationError(
            model.E_MALFORMED, str(path), "generation/graph required")
    generation = model.Generation.from_dict(
        doc["generation"], f"{path}[generation]")
    if generation.generation_id != generation_id:
        raise model.ReplanValidationError(
            model.E_IDENTITY_MISMATCH, str(path), generation_id)
    graph = graph_model.GoalGraph.from_dict(doc["graph"], f"{path}[graph]")
    if (graph.graph_id != generation.graph_id
            or graph.spec_sha256 != generation.spec_sha256):
        raise model.ReplanValidationError(
            model.E_GRAPH_MISMATCH, str(path), generation_id)
    return generation, graph


def archive_generation(paths: Mapping[str, Path],
                       graph: graph_model.GoalGraph,
                       generation: model.Generation) -> None:
    """Atomically archive one exact generation (idempotent, never destructive)."""
    path = generation_path(paths, generation.generation_id)
    if path.is_file():
        existing, _ = load_generation(paths, generation.generation_id)
        if existing.identity_payload() != generation.identity_payload():
            raise model.ReplanValidationError(
                model.E_IDENTITY_MISMATCH, str(path),
                "existing generation content differs")
        return
    atomic_write_json(path, {
        "generation": generation.to_dict(),
        "graph": graph.to_dict(),
    })


def list_generation_ids(paths: Mapping[str, Path]) -> list[str]:
    base = paths["generations"]
    if not base.is_dir():
        return []
    return sorted(child.stem for child in base.iterdir()
                  if child.is_file() and child.suffix == ".json")


# --- events -------------------------------------------------------------------


def load_events(paths: Mapping[str, Path]) -> list[model.ReplanEvent]:
    path = paths["events"]
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise model.ReplanValidationError(
            model.E_EVENT_LOG, str(path), type(exc).__name__) from exc
    events: list[model.ReplanEvent] = []
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError as exc:
            raise model.ReplanValidationError(
                model.E_EVENT_LOG, str(path), f"line {index}") from exc
        events.append(model.ReplanEvent.from_dict(
            doc, f"{path}[line {index}]"))
    return events


def append_event(paths: Mapping[str, Path],
                 event: model.ReplanEvent) -> bool:
    """Append one replan event. Returns True when newly appended."""
    existing = load_events(paths)
    if len(existing) >= model.MAX_EVENTS:
        raise model.ReplanValidationError(
            model.E_OVERFLOW, str(paths["events"]),
            f"more than {model.MAX_EVENTS} events")
    for item in existing:
        if item.event_id == event.event_id:
            if item.identity_payload() != event.identity_payload():
                raise model.ReplanValidationError(
                    model.E_IDENTITY_MISMATCH, str(paths["events"]),
                    event.event_id)
            return False
    path = paths["events"]
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event.to_dict(), sort_keys=True,
                      separators=(",", ":"))
    with path.open("ab") as handle:
        handle.write(line.encode("utf-8") + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    return True


# --- reconstruction -----------------------------------------------------------


@dataclass
class ReconstructedReplan:
    """Read-only reconstruction of one goal's exact replan state."""

    generation: model.Generation
    generations: tuple[model.Generation, ...] = field(default_factory=tuple)
    events: tuple[model.ReplanEvent, ...] = field(default_factory=tuple)
    active_graph_id: str = ""

    @property
    def generation_number(self) -> int:
        return self.generation.generation_number

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "VALID",
            "goal_id": self.generation.goal_id,
            "generation_id": self.generation.generation_id,
            "generation_number": self.generation.generation_number,
            "parent_generation_id": self.generation.parent_generation_id,
            "graph_id": self.generation.graph_id,
            "active_graph_id": self.active_graph_id,
            "generations": [g.generation_id for g in self.generations],
            "events": len(self.events),
        }


def reconstruct(root: str | Path, goal_id: str,
                graph: graph_model.GoalGraph) -> ReconstructedReplan:
    """Strictly reconstruct and revalidate persisted replan state.

    Fails closed on: a broken generation chain, duplicate/out-of-order
    generations, a missing archived graph, an active graph that no longer
    matches the active generation, and an accepted event whose generation is
    not archived. When no replan has occurred the base generation is derived
    deterministically from the canonical graph (nothing is written).
    """
    paths = replan_paths(root, goal_id)
    current = load_current(root, goal_id)
    if current is None:
        if list_generation_ids(paths):
            raise model.ReplanValidationError(
                model.E_CONTRADICTORY, str(paths["current"]),
                "archived generations exist without an active generation")
        base = model.Generation.base(graph=graph)
        base = _with_times(base, activated_at=graph.provenance.created_at)
        return ReconstructedReplan(
            generation=base, generations=(base,), events=(),
            active_graph_id=graph.graph_id)

    generation_ids = list_generation_ids(paths)
    generations: list[model.Generation] = []
    for generation_id in generation_ids:
        generation, archived_graph = load_generation(paths, generation_id)
        if archived_graph.goal_id != goal_id:
            raise model.ReplanValidationError(
                model.E_GRAPH_MISMATCH, generation_id, "goal mismatch")
        generations.append(generation)
    generations.sort(key=lambda g: g.generation_number)
    _validate_chain(goal_id, generations)

    if current.generation_id not in {g.generation_id for g in generations}:
        raise model.ReplanValidationError(
            model.E_CONTRADICTORY, str(paths["current"]),
            "active generation is not archived")
    active = next(g for g in generations
                  if g.generation_id == current.generation_id)
    if active.identity_payload() != current.identity_payload():
        raise model.ReplanValidationError(
            model.E_IDENTITY_MISMATCH, str(paths["current"]),
            current.generation_id)
    if active.generation_number != generations[-1].generation_number:
        raise model.ReplanValidationError(
            model.E_CONTRADICTORY, str(paths["current"]),
            "active generation is not the newest")
    if (active.graph_id != graph.graph_id
            or active.spec_sha256 != graph.spec_sha256):
        raise model.ReplanValidationError(
            model.E_GRAPH_MISMATCH, str(paths["current"]),
            "active graph does not match active generation")

    events = tuple(load_events(paths))
    if len(events) > model.MAX_EVENTS:
        raise model.ReplanValidationError(
            model.E_OVERFLOW, str(paths["events"]), "too many events")
    known = {g.generation_id for g in generations}
    for event in events:
        if event.goal_id != goal_id:
            raise model.ReplanValidationError(
                model.E_CONTRADICTORY, str(paths["events"]),
                event.event_id)
        if (event.status == model.DS_ACCEPTED
                and event.generation_id not in known):
            raise model.ReplanValidationError(
                model.E_CONTRADICTORY, str(paths["events"]),
                f"accepted event references unknown generation: "
                f"{event.generation_id}")
    return ReconstructedReplan(
        generation=active, generations=tuple(generations), events=events,
        active_graph_id=graph.graph_id)


def _validate_chain(goal_id: str,
                    generations: list[model.Generation]) -> None:
    if not generations:
        return
    if len(generations) > model.MAX_GENERATIONS:
        raise model.ReplanValidationError(
            model.E_OVERFLOW, "generations", "too many generations")
    seen: set[str] = set()
    by_id: dict[str, model.Generation] = {}
    for generation in generations:
        if generation.goal_id != goal_id:
            raise model.ReplanValidationError(
                model.E_GRAPH_MISMATCH, "generations", "goal mismatch")
        if generation.generation_id in seen:
            raise model.ReplanValidationError(
                model.E_CONTRADICTORY, "generations",
                f"duplicate generation {generation.generation_id}")
        seen.add(generation.generation_id)
        by_id[generation.generation_id] = generation
    for index, generation in enumerate(generations):
        if generation.generation_number != index + 1:
            raise model.ReplanValidationError(
                model.E_CONTRADICTORY, "generations",
                f"non-contiguous generation number "
                f"{generation.generation_number}")
        if index == 0:
            if generation.parent_generation_id is not None:
                raise model.ReplanValidationError(
                    model.E_CONTRADICTORY, "generations",
                    "base generation has a parent")
            continue
        parent = by_id.get(generation.parent_generation_id or "")
        if parent is None:
            raise model.ReplanValidationError(
                model.E_CONTRADICTORY, "generations",
                f"missing parent {generation.parent_generation_id}")
        if parent.generation_number >= generation.generation_number:
            raise model.ReplanValidationError(
                model.E_CONTRADICTORY, "generations",
                "parent is not older than child")


def _with_times(generation: model.Generation,
                *, activated_at: str) -> model.Generation:
    from dataclasses import replace as _replace
    return _replace(generation, activated_at=activated_at)


# --- helpers ------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise model.ReplanValidationError(
            model.E_MALFORMED, str(path), type(exc).__name__) from exc
    if not isinstance(raw, dict):
        raise model.ReplanValidationError(
            model.E_MALFORMED, str(path), "top-level object expected")
    return raw


def _known_keys(doc: Mapping[str, Any], allowed: tuple[str, ...],
                path: str) -> None:
    unknown = set(doc) - set(allowed)
    if unknown:
        raise model.ReplanValidationError(
            model.E_MALFORMED, path, f"unknown field(s): {sorted(unknown)}")
