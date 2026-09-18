"""Mission 014 — durable cross-mission reuse store (atomic, strict).

Persistence layout (all under ``<root>/goals/<goal_id>/reuse/``)::

    projection.json      the exact versioned resolved reuse projection
    consumptions.jsonl   append-only producer -> consumer consumption log

Rules (ADR-012, matching the graph/scheduler/mission stores):

* every write is atomic (temp file + fsync + rename) or append-only JSONL;
* every read is strict: unknown fields, unsupported schema versions,
  malformed values and identity mismatches fail closed — data is never
  guessed, repaired or rewritten;
* reconstruction recomputes the projection identity from content **and**
  re-resolves the live projection from canonical mission evidence, failing
  closed when the persisted projection no longer reproduces (stale evidence);
* producer records are strictly read-only: consumption never mutates them.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trajectory_os.graph import model as graph_model
from trajectory_os.graph.reuse import model, resolver
from trajectory_os.runs.store import atomic_write_json


def reuse_paths(root: str | Path, goal_id: str) -> dict[str, Path]:
    base = Path(root) / "goals" / goal_id / "reuse"
    return {
        "root": base,
        "projection": base / "projection.json",
        "consumptions": base / "consumptions.jsonl",
    }


# --- projection ---------------------------------------------------------------


def load_projection(root: str | Path, goal_id: str,
                    ) -> model.ReuseProjection | None:
    """Strictly load the persisted reuse projection (None when absent)."""
    path = reuse_paths(root, goal_id)["projection"]
    if not path.is_file():
        return None
    return model.ReuseProjection.from_dict(_read_json(path), str(path))


def save_projection(paths: Mapping[str, Path],
                    projection: model.ReuseProjection) -> None:
    atomic_write_json(paths["projection"], projection.to_dict())


def projection_exists(root: str | Path, goal_id: str) -> bool:
    return reuse_paths(root, goal_id)["projection"].is_file()


# --- consumptions -------------------------------------------------------------


def load_consumptions(paths: Mapping[str, Path]
                      ) -> list[model.ConsumptionRecord]:
    path = paths["consumptions"]
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise model.ReuseValidationError(
            model.E_MALFORMED, str(path), type(exc).__name__) from exc
    records: list[model.ConsumptionRecord] = []
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError as exc:
            raise model.ReuseValidationError(
                model.E_MALFORMED, str(path), f"line {index}") from exc
        records.append(model.ConsumptionRecord.from_dict(
            doc, f"{path}[line {index}]"))
    return records


def append_consumption(paths: Mapping[str, Path],
                       record: model.ConsumptionRecord) -> bool:
    """Append one consumption event. Returns True when newly appended.

    Re-appending byte-identical consumption (same ``consumption_id``) is an
    idempotent no-op that preserves the original evidence timestamp. Any
    mismatch for an already-known identity is impossible by construction
    (identity is content-addressed) and is therefore never silently merged.
    """
    existing = load_consumptions(paths)
    if len(existing) >= model.MAX_CONSUMPTIONS:
        raise model.ReuseValidationError(
            model.E_OVERFLOW, str(paths["consumptions"]),
            f"more than {model.MAX_CONSUMPTIONS} consumptions")
    for item in existing:
        if item.consumption_id == record.consumption_id:
            if item.identity_payload() != record.identity_payload():
                raise model.ReuseValidationError(
                    model.E_IDENTITY_MISMATCH, str(paths["consumptions"]),
                    record.consumption_id)
            return False
    path = paths["consumptions"]
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record.to_dict(), sort_keys=True,
                      separators=(",", ":"))
    with path.open("ab") as handle:
        handle.write(line.encode("utf-8") + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    return True


# --- reconstruction -----------------------------------------------------------


@dataclass
class ReconstructedReuse:
    """Read-only reconstruction of one goal's exact persisted reuse state."""

    projection: model.ReuseProjection
    consumptions: tuple[model.ConsumptionRecord, ...] = field(
        default_factory=tuple)
    live_projection_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "VALID",
            "goal_id": self.projection.goal_id,
            "graph_id": self.projection.graph_id,
            "projection_id": self.projection.projection_id,
            "counts": self.projection.counts(),
            "blocked_nodes": list(self.projection.blocked_nodes()),
            "inputs": [item.to_dict() for item in self.projection.inputs],
            "consumptions": [c.to_dict() for c in self.consumptions],
            "live_projection_id": self.live_projection_id,
        }


def reconstruct(root: str | Path, goal_id: str,
                graph: graph_model.GoalGraph) -> ReconstructedReuse:
    """Strictly reconstruct and revalidate persisted reuse state.

    Fails closed on: missing projection, schema/identity mismatch, graph
    identity mismatch, stale evidence (the persisted projection no longer
    reproduces from canonical evidence), untracked/contradictory consumption
    records and history overflow.
    """
    paths = reuse_paths(root, goal_id)
    projection = load_projection(root, goal_id)
    if projection is None:
        raise model.ReuseValidationError(
            model.E_MALFORMED, str(paths["projection"]),
            "reuse projection missing")
    if projection.goal_id != goal_id:
        raise model.ReuseValidationError(
            model.E_GRAPH_MISMATCH, str(paths["projection"]),
            f"goal {projection.goal_id} != {goal_id}")
    if projection.graph_id != graph.graph_id:
        raise model.ReuseValidationError(
            model.E_GRAPH_MISMATCH, str(paths["projection"]),
            f"graph {projection.graph_id} != {graph.graph_id}")
    if projection.spec_sha256 != graph.spec_sha256:
        raise model.ReuseValidationError(
            model.E_GRAPH_MISMATCH, str(paths["projection"]),
            "spec identity mismatch")

    live = resolver.build_projection(str(root), graph)
    if live.projection_id != projection.projection_id:
        raise model.ReuseValidationError(
            model.E_STALE, str(paths["projection"]),
            _diff_detail(projection, live))

    consumptions = tuple(load_consumptions(paths))
    if len(consumptions) > model.MAX_CONSUMPTIONS:
        raise model.ReuseValidationError(
            model.E_OVERFLOW, str(paths["consumptions"]),
            "too many consumptions")
    _validate_consumptions(projection, consumptions)
    return ReconstructedReuse(
        projection=projection, consumptions=consumptions,
        live_projection_id=live.projection_id)


def _validate_consumptions(
    projection: model.ReuseProjection,
    consumptions: tuple[model.ConsumptionRecord, ...],
) -> None:
    known = {
        (item.consumer_node_id, item.input_id): item
        for item in projection.inputs if item.status == model.ST_RESOLVED
    }
    seen: set[str] = set()
    for record in consumptions:
        if record.consumption_id in seen:
            raise model.ReuseValidationError(
                model.E_DUPLICATE_CONSUMPTION, "consumptions",
                record.consumption_id)
        seen.add(record.consumption_id)
        if (record.goal_id != projection.goal_id
                or record.graph_id != projection.graph_id
                or record.projection_id != projection.projection_id):
            raise model.ReuseValidationError(
                model.E_CONTRADICTORY, "consumptions",
                record.consumption_id)
        item = known.get((record.consumer_node_id, record.input_id))
        if item is None:
            raise model.ReuseValidationError(
                model.E_UNTRACKED_CONSUMPTION, "consumptions",
                record.consumption_id)
        if (record.artifact_content_sha256
                != item.artifact_content_sha256
                or record.producer_node_id != item.producer_node_id
                or record.trust != item.trust):
            raise model.ReuseValidationError(
                model.E_IDENTITY_MISMATCH, "consumptions",
                record.consumption_id)


def _diff_detail(persisted: model.ReuseProjection,
                 live: model.ReuseProjection) -> str:
    p_by = {(i.consumer_node_id, i.input_id): i for i in persisted.inputs}
    l_by = {(i.consumer_node_id, i.input_id): i for i in live.inputs}
    if set(p_by) != set(l_by):
        return f"{model.E_AMBIGUOUS}: declared input set changed"
    for key in sorted(p_by):
        p_item, l_item = p_by[key], l_by[key]
        if p_item.artifact_content_sha256 != l_item.artifact_content_sha256:
            return (f"{key[0]}/{key[1]} artifact "
                    f"{p_item.artifact_content_sha256} != "
                    f"{l_item.artifact_content_sha256}")
        if p_item.status != l_item.status or p_item.reason != l_item.reason:
            return (f"{key[0]}/{key[1]} {p_item.status}/{p_item.reason} != "
                    f"{l_item.status}/{l_item.reason}")
    return "projection identity changed"


# --- helpers ------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise model.ReuseValidationError(
            model.E_MALFORMED, str(path), type(exc).__name__) from exc
    if not isinstance(raw, dict):
        raise model.ReuseValidationError(
            model.E_MALFORMED, str(path), "top-level object expected")
    return raw
