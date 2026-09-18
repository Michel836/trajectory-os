"""Mission 012 — durable goal decomposition graph store (atomic, strict).

Persistence layout (all under ``<root>/goals/<goal_id>/``)::

    graph.json     canonical normalized graph (strict schema 1)
    events.jsonl   bounded creation provenance log (append-only)

Rules (ADR-010, matching the mission store):

* every write is atomic (temp file + fsync + rename) or append-only JSONL;
* every read is strict: unknown fields, unknown states, unsupported schema
  versions, malformed values and identity mismatches fail closed with
  :class:`~trajectory_os.graph.model.GraphValidationError` — data is never
  guessed, repaired or rewritten;
* reconstruction recomputes the canonical spec and graph digests from the
  normalized content and rejects any mismatch, so a graph reloaded after a
  restart yields the same identity, nodes, edges, order and provenance;
* required mission references are validated read-only at creation; the
  graph never copies mission trust evidence into its own state.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from trajectory_os.graph import evidence as graph_evidence
from trajectory_os.graph import model
from trajectory_os.missions import orchestrator as mission_orchestrator
from trajectory_os.missions import store as mission_store
from trajectory_os.runs.store import atomic_write_json

CREATED_BY = "trajectory-pi-goals"


class GraphNotFound(Exception):
    """The requested goal graph does not exist in the store."""

    def __init__(self, goal_id: str, root: str) -> None:
        super().__init__(f"no goal graph {goal_id!r} under {root!r}")
        self.goal_id = goal_id
        self.root = root


class GraphExists(Exception):
    """A goal graph with this identity already exists in the store."""

    def __init__(self, goal_id: str, root: str) -> None:
        super().__init__(f"goal graph {goal_id!r} already exists under {root!r}")
        self.goal_id = goal_id
        self.root = root


def graph_paths(root: str | Path, goal_id: str) -> dict[str, Path]:
    base = Path(root) / "goals" / goal_id
    return {
        "root": base,
        "graph": base / "graph.json",
        "events": base / "events.jsonl",
    }


def _validate_required_references(
    root: str, nodes: tuple[model.GraphNode, ...],
) -> None:
    for node in nodes:
        ref = node.mission_ref
        if ref is None or not ref.required:
            continue
        record = graph_evidence.resolve_mission_evidence(root, ref.mission_id)
        if record.error is None:
            continue
        if record.error == graph_evidence.ERR_NOT_FOUND:
            raise model.GraphValidationError(
                model.E_UNRESOLVED_REFERENCE, "spec",
                f"node {node.node_id!r} references missing mission "
                f"{ref.mission_id!r}")
        if record.error == graph_evidence.ERR_CONTRADICTION:
            raise model.GraphValidationError(
                model.E_CONTRADICTORY_REFERENCE, "spec",
                f"node {node.node_id!r} mission {ref.mission_id!r} "
                "contradicts its persisted evidence")
        raise model.GraphValidationError(
            model.E_REFERENCE_INVALID, "spec",
            f"node {node.node_id!r} mission {ref.mission_id!r} is malformed")


def _assemble(
    normalized: model.NormalizedSpec,
    *,
    repo_root: str | None,
    baseline_revision: str | None,
    created_at: str | None,
    created_by: str,
) -> model.GoalGraph:
    if baseline_revision is not None and not (
            1 <= len(baseline_revision) <= model.MAX_REVISION_LEN):
        raise model.GraphValidationError(
            model.E_MALFORMED, "spec", "baseline_revision out of bounds")
    provenance = model.GraphProvenance(
        created_at=created_at or mission_orchestrator.utc_now_iso(),
        created_by=created_by,
        repo_root=repo_root,
        baseline_revision=baseline_revision,
    )
    return model.GoalGraph.build(
        goal_id=normalized.goal_id,
        objective=normalized.objective,
        nodes=normalized.nodes,
        provenance=provenance,
    )


def build_graph(
    *,
    spec_doc: object,
    repo_root: str | None = None,
    baseline_revision: str | None = None,
    created_at: str | None = None,
    created_by: str = CREATED_BY,
    path: str = "spec",
) -> model.GoalGraph:
    """Normalize a declarative spec into a graph (pure; no store access)."""
    normalized = model.normalize_spec(spec_doc, path=path)
    return _assemble(
        normalized,
        repo_root=repo_root,
        baseline_revision=baseline_revision,
        created_at=created_at,
        created_by=created_by,
    )


def create_graph(
    root: str,
    spec_doc: object,
    *,
    repo_root: str | None = None,
    baseline_revision: str | None = None,
    created_at: str | None = None,
    created_by: str = CREATED_BY,
    path: str = "spec",
) -> model.GoalGraph:
    """Create and atomically persist one goal graph (fail closed).

    Required mission references must already resolve in the canonical
    mission store under the same root; a missing/malformed/contradictory
    required reference rejects the graph before anything is written.
    """
    normalized = model.normalize_spec(spec_doc, path=path)
    if graph_paths(root, normalized.goal_id)["graph"].is_file():
        raise GraphExists(normalized.goal_id, root)
    _validate_required_references(root, normalized.nodes)
    graph = _assemble(
        normalized,
        repo_root=repo_root,
        baseline_revision=baseline_revision,
        created_at=created_at,
        created_by=created_by,
    )
    save_graph(root, graph)
    return graph


def save_graph(root: str | Path, graph: model.GoalGraph) -> None:
    """Atomic durable write plus bounded creation provenance event."""
    paths = graph_paths(root, graph.goal_id)
    paths["root"].mkdir(parents=True, exist_ok=True)
    atomic_write_json(paths["graph"], graph.to_dict())
    with_event_log = {"events": paths["events"]}
    mission_store.append_event(with_event_log, {
        "ts": graph.provenance.created_at,
        "event": "created",
        "goal_id": graph.goal_id,
        "graph_id": graph.graph_id,
        "spec_sha256": graph.spec_sha256,
        "created_by": graph.provenance.created_by,
    })


def load_graph(root: str, goal_id: str) -> tuple[model.GoalGraph, dict[str, Path]]:
    """Strict, fail-closed reconstruction of one persisted goal graph."""
    paths = graph_paths(root, goal_id)
    path = paths["graph"]
    if not path.is_file():
        raise GraphNotFound(goal_id, str(root))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise model.GraphValidationError(
            model.E_MALFORMED, str(path), type(exc).__name__) from exc
    if not isinstance(raw, dict):
        raise model.GraphValidationError(
            model.E_MALFORMED, str(path), "top-level object expected")
    return model.GoalGraph.from_dict(raw, str(path)), paths


def load_events(root: str, goal_id: str) -> list[dict[str, Any]]:
    """Read the bounded creation provenance log (strict JSONL)."""
    paths = graph_paths(root, goal_id)
    path = paths["events"]
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise model.GraphValidationError(
            model.E_MALFORMED, str(path), type(exc).__name__) from exc
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError as exc:
            raise model.GraphValidationError(
                model.E_MALFORMED, str(path), f"line {index}") from exc
        if not isinstance(doc, dict):
            raise model.GraphValidationError(
                model.E_MALFORMED, str(path), f"line {index}")
        events.append(doc)
    return events


def list_goal_ids(root: str | Path) -> list[str]:
    """Sorted goal ids present under the root (filesystem order normalized)."""
    base = Path(root) / "goals"
    if not base.is_dir():
        return []
    return sorted(
        child.name for child in base.iterdir()
        if child.is_dir() and (child / "graph.json").is_file()
    )


def graph_exists(root: str | Path, goal_id: str) -> bool:
    return graph_paths(root, goal_id)["graph"].is_file()


def provenance_document(graph: model.GoalGraph) -> Mapping[str, object]:
    """Canonical, read-only creation provenance for operator output."""
    return {
        "goal_id": graph.goal_id,
        "graph_id": graph.graph_id,
        "spec_sha256": graph.spec_sha256,
        **graph.provenance.to_dict(),
    }
