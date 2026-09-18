"""Mission 014 — cross-mission reuse resolution cycle (composition, no engine).

This module composes the pure resolver with the durable reuse store. It
introduces no second mission engine and no second graph: it consumes the
canonical M012 graph and the canonical mission evidence read-only, persists
the exact resolved projection and appends the producer -> consumer
consumption history.

Resolving or recording reuse **never** advances, completes or promotes a
mission. It records input provenance only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trajectory_os.graph import model as graph_model
from trajectory_os.graph import store as graph_store
from trajectory_os.graph.reuse import model, resolver, store


@dataclass
class ReuseResolveResult:
    """One deterministic reuse resolution/persistence cycle outcome."""

    projection: model.ReuseProjection
    appended: tuple[model.ConsumptionRecord, ...]
    persisted: bool

    @property
    def blocked(self) -> bool:
        return self.projection.blocked

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "RESOLVED" if not self.blocked else "BLOCKED",
            "persisted": self.persisted,
            "goal_id": self.projection.goal_id,
            "graph_id": self.projection.graph_id,
            "projection_id": self.projection.projection_id,
            "counts": self.projection.counts(),
            "blocked_nodes": list(self.projection.blocked_nodes()),
            "inputs": [item.to_dict() for item in self.projection.inputs],
            "appended": [record.consumption_id for record in self.appended],
        }


def resolve_and_record(
    root: str,
    goal_id: str,
    *,
    consumed_at: str,
) -> ReuseResolveResult:
    """Resolve the live projection, persist it and append consumption events.

    Consumption events are appended only for **resolved** inputs; an
    unresolved or rejected input is never recorded as consumed. Re-running
    with identical content is an idempotent no-op (content-addressed
    consumption identity), preserving the original evidence timestamp.
    """
    graph, _ = graph_store.load_graph(root, goal_id)
    projection = resolver.build_projection(root, graph)
    paths = store.reuse_paths(root, goal_id)
    store.save_projection(paths, projection)
    appended: list[model.ConsumptionRecord] = []
    for item in projection.inputs:
        if item.status != model.ST_RESOLVED:
            continue
        record = model.ConsumptionRecord.build(
            goal_id=projection.goal_id,
            graph_id=projection.graph_id,
            projection_id=projection.projection_id,
            input_status=item,
            consumed_at=consumed_at,
        )
        if store.append_consumption(paths, record):
            appended.append(record)
    return ReuseResolveResult(
        projection=projection, appended=tuple(appended), persisted=True)


def load_projection(root: str, goal_id: str) -> model.ReuseProjection | None:
    return store.load_projection(root, goal_id)


def reconstruct(root: str, goal_id: str,
                ) -> store.ReconstructedReuse:
    graph, _ = graph_store.load_graph(root, goal_id)
    return store.reconstruct(root, goal_id, graph)


def blocking_reason(projection: model.ReuseProjection | None,
                    node_id: str) -> str | None:
    """Scheduler-facing: stable blocking reason for one consumer node."""
    if projection is None:
        return None
    return projection.blocking_reason(node_id)


def declared_consumer_nodes(graph: graph_model.GoalGraph) -> tuple[str, ...]:
    return tuple(sorted(node.node_id for node in graph.nodes
                        if node.reuse_inputs))
