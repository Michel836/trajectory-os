"""Mission 012 — deterministic dependency readiness projection (M013 interface).

This module turns a normalized :class:`~trajectory_os.graph.model.GoalGraph`
plus resolved read-only mission evidence into a deterministic readiness
projection. It is pure given its inputs: no I/O, no clocks, no mutation.

Readiness vocabulary (semantically distinct):

* ``READY``       — dependency ready / eligible: every upstream dependency is
  ``COMPLETE`` and the node is not itself complete/running/failed;
* ``COMPLETE``    — complete/proven: the node's own referenced mission is
  proven green *and* every upstream dependency is ``COMPLETE``;
* ``IN_PROGRESS`` — the node's own referenced mission exists but is not yet
  proven complete;
* ``BLOCKED``     — at least one upstream dependency is not proven complete
  (or the node's own referenced mission failed); never eligible;
* ``UNRESOLVED``  — unresolved required reference evidence (missing mission);
* ``INVALID``     — malformed or contradictory evidence (fail closed).

Precedence is fixed and explicit: ``INVALID`` > ``UNRESOLVED`` > own
``COMPLETE``/``IN_PROGRESS``/``FAILED`` > dependency satisfaction. A node
with a proven-complete mission but an unproven upstream dependency is a
contradiction and is reported ``INVALID`` — a dependency-blocked node is
never presented as ``READY`` or eligible.

M012 intentionally stops at this interface: it does not schedule, launch,
queue or arbitrate. M013 consumes the machine projection.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from trajectory_os.graph import evidence as graph_evidence
from trajectory_os.graph import model
from trajectory_os.missions import model as mission_model

# --- node readiness states ----------------------------------------------------

RS_COMPLETE = "COMPLETE"
RS_READY = "READY"
RS_IN_PROGRESS = "IN_PROGRESS"
RS_BLOCKED = "BLOCKED"
RS_UNRESOLVED = "UNRESOLVED"
RS_INVALID = "INVALID"

NODE_STATES = frozenset({
    RS_COMPLETE, RS_READY, RS_IN_PROGRESS, RS_BLOCKED, RS_UNRESOLVED,
    RS_INVALID,
})

# --- own-mission statuses (internal classification) ---------------------------

OS_UNBOUND = "UNBOUND"
OS_PENDING = "PENDING"
OS_COMPLETE = "COMPLETE"
OS_IN_PROGRESS = "IN_PROGRESS"
OS_FAILED = "FAILED"
OS_UNRESOLVED = "UNRESOLVED"
OS_INVALID = "INVALID"

OWN_STATUSES = frozenset({
    OS_UNBOUND, OS_PENDING, OS_COMPLETE, OS_IN_PROGRESS, OS_FAILED,
    OS_UNRESOLVED, OS_INVALID,
})

# --- stable reason codes ------------------------------------------------------

REASON_NO_DEPENDENCIES = "NO_DEPENDENCIES"
REASON_DEPENDENCIES_COMPLETE = "DEPENDENCIES_COMPLETE"
REASON_UPSTREAM_NOT_PROVEN = "UPSTREAM_NOT_PROVEN"
REASON_UPSTREAM_UNRESOLVED = "UPSTREAM_UNRESOLVED"
REASON_UPSTREAM_INVALID = "UPSTREAM_INVALID"
REASON_OWN_MISSION_COMPLETE = "OWN_MISSION_COMPLETE"
REASON_OWN_MISSION_IN_PROGRESS = "OWN_MISSION_IN_PROGRESS"
REASON_OWN_MISSION_FAILED = "OWN_MISSION_FAILED"
REASON_REFERENCE_UNRESOLVED = "REFERENCE_UNRESOLVED"
REASON_REFERENCE_INVALID = "REFERENCE_INVALID"
REASON_REFERENCE_PENDING = "REFERENCE_PENDING"
REASON_COMPLETED_WITH_UNPROVEN_UPSTREAM = "COMPLETED_WITH_UNPROVEN_UPSTREAM"

REASON_CODES = frozenset({
    REASON_NO_DEPENDENCIES, REASON_DEPENDENCIES_COMPLETE,
    REASON_UPSTREAM_NOT_PROVEN, REASON_UPSTREAM_UNRESOLVED,
    REASON_UPSTREAM_INVALID, REASON_OWN_MISSION_COMPLETE,
    REASON_OWN_MISSION_IN_PROGRESS, REASON_OWN_MISSION_FAILED,
    REASON_REFERENCE_UNRESOLVED, REASON_REFERENCE_INVALID,
    REASON_REFERENCE_PENDING, REASON_COMPLETED_WITH_UNPROVEN_UPSTREAM,
})

#: Dependency-edge reason for each resolved upstream node state.
_DEPENDENCY_REASON = {
    RS_COMPLETE: "UPSTREAM_COMPLETE",
    RS_READY: "UPSTREAM_READY",
    RS_IN_PROGRESS: "UPSTREAM_IN_PROGRESS",
    RS_BLOCKED: "UPSTREAM_BLOCKED",
    RS_UNRESOLVED: "UPSTREAM_UNRESOLVED",
    RS_INVALID: "UPSTREAM_INVALID",
}


@dataclass(frozen=True)
class DependencyStatus:
    """Resolved status of one explicit dependency edge (deterministic)."""

    node_id: str
    state: str
    proven: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "state": self.state,
            "proven": self.proven,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class NodeReadiness:
    """Deterministic readiness of one graph node."""

    node_id: str
    state: str
    eligible: bool
    reason: str
    own_status: str
    mission: graph_evidence.MissionEvidenceRecord | None
    dependencies: tuple[DependencyStatus, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "state": self.state,
            "eligible": self.eligible,
            "reason": self.reason,
            "own_status": self.own_status,
            "mission": (None if self.mission is None
                        else self.mission.to_dict()),
            "dependencies": [dep.to_dict() for dep in self.dependencies],
        }


@dataclass(frozen=True)
class ReadinessProjection:
    """Full deterministic readiness projection for one goal graph."""

    goal_id: str
    graph_id: str
    topological_order: tuple[str, ...]
    nodes: tuple[NodeReadiness, ...]

    def by_id(self) -> dict[str, NodeReadiness]:
        return {node.node_id: node for node in self.nodes}

    def ready(self) -> tuple[str, ...]:
        return tuple(node.node_id for node in self.nodes
                     if node.state == RS_READY)

    def blocked(self) -> tuple[str, ...]:
        return tuple(node.node_id for node in self.nodes
                     if node.state == RS_BLOCKED)

    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(sorted(NODE_STATES), 0)
        for node in self.nodes:
            counts[node.state] += 1
        return counts

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "goal_id": self.goal_id,
            "graph_id": self.graph_id,
            "topological_order": list(self.topological_order),
            "nodes": [node.to_dict() for node in self.nodes],
            "ready": list(self.ready()),
            "blocked": list(self.blocked()),
            "counts": self.counts(),
        }


def own_status(
    node: model.GraphNode,
    evidence: Mapping[str, graph_evidence.MissionEvidenceRecord],
) -> str:
    """Classify a node's own mission reference (pure, fail closed)."""
    ref = node.mission_ref
    if ref is None:
        return OS_UNBOUND
    record = evidence.get(ref.mission_id)
    if record is None:
        return OS_UNRESOLVED if ref.required else OS_PENDING
    if record.error is not None:
        if record.error in graph_evidence.INVALID_ERRORS:
            return OS_INVALID
        if record.error == graph_evidence.ERR_NOT_FOUND:
            return OS_UNRESOLVED if ref.required else OS_PENDING
        return OS_INVALID
    if record.proven_complete:
        return OS_COMPLETE
    if record.state in (mission_model.MS_BLOCKED, mission_model.MS_FAILED):
        return OS_FAILED
    return OS_IN_PROGRESS


def _classify(
    own: str,
    dependencies: Sequence[DependencyStatus],
) -> tuple[str, str]:
    if own == OS_INVALID:
        return RS_INVALID, REASON_REFERENCE_INVALID
    if any(dep.state == RS_INVALID for dep in dependencies):
        return RS_INVALID, REASON_UPSTREAM_INVALID
    if own == OS_UNRESOLVED:
        return RS_UNRESOLVED, REASON_REFERENCE_UNRESOLVED
    if any(dep.state == RS_UNRESOLVED for dep in dependencies):
        return RS_UNRESOLVED, REASON_UPSTREAM_UNRESOLVED
    if own == OS_COMPLETE:
        if all(dep.proven for dep in dependencies):
            return RS_COMPLETE, REASON_OWN_MISSION_COMPLETE
        return RS_INVALID, REASON_COMPLETED_WITH_UNPROVEN_UPSTREAM
    if own == OS_FAILED:
        return RS_BLOCKED, REASON_OWN_MISSION_FAILED
    if own == OS_IN_PROGRESS:
        return RS_IN_PROGRESS, REASON_OWN_MISSION_IN_PROGRESS
    # OS_UNBOUND / OS_PENDING: eligibility is decided by dependencies.
    if all(dep.proven for dep in dependencies):
        if not dependencies:
            return RS_READY, REASON_NO_DEPENDENCIES
        return RS_READY, REASON_DEPENDENCIES_COMPLETE
    return RS_BLOCKED, REASON_UPSTREAM_NOT_PROVEN


def project(
    graph: model.GoalGraph,
    evidence: Mapping[str, graph_evidence.MissionEvidenceRecord],
) -> ReadinessProjection:
    """Deterministic readiness projection in topological order (pure)."""
    node_map = graph.node_map()
    order = graph.topological_order()
    states: dict[str, str] = {}
    records: dict[str, NodeReadiness] = {}
    for node_id in order:
        node = node_map[node_id]
        own = own_status(node, evidence)
        dependencies = tuple(
            DependencyStatus(
                node_id=dep,
                state=states[dep],
                proven=states[dep] == RS_COMPLETE,
                reason=_DEPENDENCY_REASON[states[dep]],
            )
            for dep in node.depends_on
        )
        state, reason = _classify(own, dependencies)
        states[node_id] = state
        mission = None
        if node.mission_ref is not None:
            mission = evidence.get(node.mission_ref.mission_id)
        records[node_id] = NodeReadiness(
            node_id=node_id,
            state=state,
            eligible=state == RS_READY,
            reason=reason,
            own_status=own,
            mission=mission,
            dependencies=dependencies,
        )
    return ReadinessProjection(
        goal_id=graph.goal_id,
        graph_id=graph.graph_id,
        topological_order=order,
        nodes=tuple(records[node_id] for node_id in order),
    )


def project_with_store(root: str, graph: model.GoalGraph) -> ReadinessProjection:
    """Resolve references from the canonical mission store, then project."""
    mission_ids = {
        node.mission_ref.mission_id
        for node in graph.nodes if node.mission_ref is not None
    }
    return project(graph, graph_evidence.collect_evidence(root, mission_ids))


def scheduler_projection(graph: model.GoalGraph,
                         projection: ReadinessProjection) -> dict[str, object]:
    """Machine-readable projection for M013 (no scheduling is performed).

    Exposes the deterministic topological order, the trusted bounded node
    structure (acceptance criteria, resources, budgets) and the readiness
    state of every node so the future scheduler can consume it without
    re-deriving graph structure.
    """
    readiness = projection.by_id()
    node_map = graph.node_map()
    nodes: list[dict[str, object]] = []
    for node_id in projection.topological_order:
        node = node_map[node_id]
        status = readiness[node_id]
        nodes.append({
            "node_id": node.node_id,
            "title": node.title,
            "priority": node.priority,
            "state": status.state,
            "eligible": status.eligible,
            "reason": status.reason,
            "depends_on": list(node.depends_on),
            "acceptance_criteria": [c.to_dict()
                                    for c in node.acceptance_criteria],
            "mission_ref": (node.mission_ref.to_dict()
                            if node.mission_ref is not None else None),
            "resources": node.resources.to_dict(),
            "budgets": node.budgets.to_dict(),
            "dependencies": [dep.to_dict() for dep in status.dependencies],
            "mission": (None if status.mission is None
                        else status.mission.to_dict()),
        })
    return {
        "schema_version": model.SCHEMA_VERSION,
        "goal_id": graph.goal_id,
        "graph_id": graph.graph_id,
        "spec_sha256": graph.spec_sha256,
        "topological_order": list(projection.topological_order),
        "nodes": nodes,
        "ready": list(projection.ready()),
        "blocked": list(projection.blocked()),
        "counts": projection.counts(),
    }
