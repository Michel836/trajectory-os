"""Mission 012 — deterministic dependency readiness projection tests.

Covers the M013-facing readiness contract from Issue #215: eligible
(``READY``) vs blocked nodes, complete/proven upstream dependencies,
explicit block reasons, unresolved/invalid evidence fail-closed behavior,
readiness transitions when authoritative mission evidence permits, and the
invariant that a dependency-blocked node is never eligible.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from trajectory_os.graph import evidence, model, readiness, store
from trajectory_os.missions import model as mission_model
from trajectory_os.missions import orchestrator
from trajectory_os.missions.runner import SubrunResult


class _OkRunner:
    def run(self, request: Any) -> SubrunResult:
        return SubrunResult(0, mission_model.CR_COMPLETED)


class _FailRunner:
    def run(self, request: Any) -> SubrunResult:
        if request.phase_id == "validate":
            return SubrunResult(1, mission_model.CR_FAILED)
        return SubrunResult(0, mission_model.CR_COMPLETED)


def _criterion() -> dict[str, Any]:
    return {"criterion_id": "ac-1", "statement": "done"}


def _node(node_id: str, *, depends_on: list[str] | None = None,
          mission_ref: dict[str, Any] | None = None,
          priority: int = 50) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "title": f"node {node_id}",
        "priority": priority,
        "depends_on": list(depends_on or []),
        "acceptance_criteria": [_criterion()],
        "mission_ref": mission_ref,
    }


def _graph(nodes: list[dict[str, Any]], goal_id: str = "g-ready") -> model.GoalGraph:
    return store.build_graph(
        spec_doc={
            "schema_version": 1, "goal_id": goal_id, "objective": "r",
            "nodes": nodes,
        },
        created_at="2026-09-18T00:00:00Z")


def _record(mission_id: str, *, proven: bool = False,
            state: str | None = None, reason: str | None = None,
            error: str | None = None,
            resolved: bool = True) -> evidence.MissionEvidenceRecord:
    return evidence.MissionEvidenceRecord(
        mission_id=mission_id,
        resolved=resolved,
        error=error,
        state=state,
        reason=reason,
        phases_passed=5 if proven else 0,
        phases_total=5,
        proven_complete=proven,
    )


def _evidence(graph: model.GoalGraph,
              records: dict[str, evidence.MissionEvidenceRecord],
              ) -> dict[str, evidence.MissionEvidenceRecord]:
    out: dict[str, evidence.MissionEvidenceRecord] = {}
    for node in graph.nodes:
        if node.mission_ref is not None:
            out[node.mission_ref.mission_id] = records[node.mission_ref.mission_id]
    return out


def _create_mission(root: str, mission_id: str,
                    repair_budget: int = 0) -> None:
    orchestrator.create_mission(root, orchestrator.MissionConfig(
        mission_id=mission_id,
        objective="referenced",
        phase_specs=orchestrator.default_phase_specs(
            {kind: ("true",) for kind in mission_model.CANONICAL_SEQUENCE},
            repair_budget=repair_budget),
        repair_budget=repair_budget,
    ))


# --- semantic distinctions ----------------------------------------------------


def test_dependency_free_node_is_ready() -> None:
    graph = _graph([_node("n-a")])
    projection = readiness.project(graph, {})
    status = projection.by_id()["n-a"]
    assert status.state == readiness.RS_READY
    assert status.eligible is True
    assert status.reason == readiness.REASON_NO_DEPENDENCIES
    assert projection.ready() == ("n-a",)


def test_node_with_proven_mission_is_complete() -> None:
    graph = _graph([
        _node("n-a", mission_ref={"mission_id": "m-a", "required": True}),
    ])
    projection = readiness.project(graph, _evidence(graph, {
        "m-a": _record("m-a", proven=True,
                       state=mission_model.MS_COMPLETE,
                       reason=mission_model.R_COMPLETE),
    }))
    status = projection.by_id()["n-a"]
    assert status.state == readiness.RS_COMPLETE
    assert status.eligible is False


def test_dependency_blocked_node_is_never_eligible() -> None:
    graph = _graph([_node("n-a"), _node("n-b", depends_on=["n-a"])])
    projection = readiness.project(graph, {})
    status = projection.by_id()["n-b"]
    assert status.state == readiness.RS_BLOCKED
    assert status.eligible is False
    assert status.reason == readiness.REASON_UPSTREAM_NOT_PROVEN
    assert status.dependencies[0].node_id == "n-a"
    assert status.dependencies[0].state == readiness.RS_READY
    assert status.dependencies[0].proven is False


def test_complete_upstream_makes_dependent_ready() -> None:
    graph = _graph([
        _node("n-a", mission_ref={"mission_id": "m-a", "required": True}),
        _node("n-b", depends_on=["n-a"]),
    ])
    projection = readiness.project(graph, _evidence(graph, {
        "m-a": _record("m-a", proven=True,
                       state=mission_model.MS_COMPLETE,
                       reason=mission_model.R_COMPLETE),
    }))
    assert projection.by_id()["n-a"].state == readiness.RS_COMPLETE
    assert projection.by_id()["n-b"].state == readiness.RS_READY
    assert projection.by_id()["n-b"].reason \
        == readiness.REASON_DEPENDENCIES_COMPLETE


def test_own_mission_in_progress_blocks_dependents() -> None:
    graph = _graph([
        _node("n-a", mission_ref={"mission_id": "m-a", "required": True}),
        _node("n-b", depends_on=["n-a"]),
    ])
    projection = readiness.project(graph, _evidence(graph, {
        "m-a": _record("m-a", state=mission_model.MS_RUNNING,
                       reason=mission_model.R_OK),
    }))
    assert projection.by_id()["n-a"].state == readiness.RS_IN_PROGRESS
    assert projection.by_id()["n-b"].state == readiness.RS_BLOCKED


def test_failed_own_mission_blocks_node_and_dependents() -> None:
    graph = _graph([
        _node("n-a", mission_ref={"mission_id": "m-a", "required": True}),
        _node("n-b", depends_on=["n-a"]),
    ])
    projection = readiness.project(graph, _evidence(graph, {
        "m-a": _record("m-a", state=mission_model.MS_BLOCKED,
                       reason=mission_model.R_CONTRADICTION),
    }))
    status = projection.by_id()["n-a"]
    assert status.own_status == readiness.OS_FAILED
    assert status.state == readiness.RS_BLOCKED
    assert status.reason == readiness.REASON_OWN_MISSION_FAILED
    assert projection.by_id()["n-b"].state == readiness.RS_BLOCKED


def test_unresolved_required_reference_is_unresolved() -> None:
    graph = _graph([
        _node("n-a", mission_ref={"mission_id": "m-a", "required": True}),
        _node("n-b", depends_on=["n-a"]),
    ])
    projection = readiness.project(graph, _evidence(graph, {
        "m-a": _record("m-a", resolved=False, error=evidence.ERR_NOT_FOUND),
    }))
    assert projection.by_id()["n-a"].state == readiness.RS_UNRESOLVED
    assert projection.by_id()["n-a"].reason \
        == readiness.REASON_REFERENCE_UNRESOLVED
    assert projection.by_id()["n-b"].state == readiness.RS_UNRESOLVED
    assert projection.by_id()["n-b"].reason \
        == readiness.REASON_UPSTREAM_UNRESOLVED


def test_non_required_missing_reference_is_pending_not_unresolved() -> None:
    graph = _graph([
        _node("n-a", mission_ref={"mission_id": "m-a", "required": False}),
    ])
    projection = readiness.project(graph, _evidence(graph, {
        "m-a": _record("m-a", resolved=False, error=evidence.ERR_NOT_FOUND),
    }))
    status = projection.by_id()["n-a"]
    assert status.own_status == readiness.OS_PENDING
    assert status.state == readiness.RS_READY


def test_malformed_reference_evidence_is_invalid() -> None:
    graph = _graph([
        _node("n-a", mission_ref={"mission_id": "m-a", "required": True}),
    ])
    projection = readiness.project(graph, _evidence(graph, {
        "m-a": _record("m-a", error=evidence.ERR_MALFORMED),
    }))
    status = projection.by_id()["n-a"]
    assert status.state == readiness.RS_INVALID
    assert status.reason == readiness.REASON_REFERENCE_INVALID


def test_contradiction_is_invalid() -> None:
    graph = _graph([
        _node("n-a", mission_ref={"mission_id": "m-a", "required": True}),
    ])
    projection = readiness.project(graph, _evidence(graph, {
        "m-a": _record("m-a", error=evidence.ERR_CONTRADICTION,
                       state=mission_model.MS_COMPLETE),
    }))
    assert projection.by_id()["n-a"].state == readiness.RS_INVALID


def test_completed_with_unproven_upstream_is_invalid() -> None:
    graph = _graph([
        _node("n-a", mission_ref={"mission_id": "m-a", "required": True}),
        _node("n-b", depends_on=["n-a"],
              mission_ref={"mission_id": "m-b", "required": True}),
    ])
    projection = readiness.project(graph, _evidence(graph, {
        "m-a": _record("m-a", state=mission_model.MS_RUNNING,
                       reason=mission_model.R_OK),
        "m-b": _record("m-b", proven=True,
                       state=mission_model.MS_COMPLETE,
                       reason=mission_model.R_COMPLETE),
    }))
    status = projection.by_id()["n-b"]
    assert status.state == readiness.RS_INVALID
    assert status.reason == readiness.REASON_COMPLETED_WITH_UNPROVEN_UPSTREAM


# --- projection shape ---------------------------------------------------------


def test_projection_is_in_topological_order() -> None:
    graph = _graph([
        _node("n-b", depends_on=["n-a"], priority=90),
        _node("n-a", priority=10),
    ])
    projection = readiness.project(graph, {})
    assert projection.topological_order == ("n-a", "n-b")
    assert [n.node_id for n in projection.nodes] == ["n-a", "n-b"]


def test_counts_cover_every_state() -> None:
    graph = _graph([_node("n-a")])
    projection = readiness.project(graph, {})
    counts = projection.counts()
    assert set(counts) == set(readiness.NODE_STATES)
    assert counts[readiness.RS_READY] == 1


def test_scheduler_projection_exposes_trusted_structure() -> None:
    graph = _graph([
        _node("n-a"),
        _node("n-b", depends_on=["n-a"]),
    ])
    projection = readiness.project(graph, {})
    scheduler = readiness.scheduler_projection(graph, projection)
    assert scheduler["goal_id"] == "g-ready"
    assert scheduler["graph_id"] == graph.graph_id
    assert scheduler["topological_order"] == ["n-a", "n-b"]
    assert scheduler["ready"] == ["n-a"]
    assert scheduler["blocked"] == ["n-b"]
    node = scheduler["nodes"][0]
    for key in ("node_id", "state", "eligible", "priority", "depends_on",
                "acceptance_criteria", "mission_ref", "resources", "budgets",
                "dependencies", "reason"):
        assert key in node, key


# --- authoritative mission evidence transitions -------------------------------


def test_readiness_transitions_with_authoritative_mission_evidence(
        tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    _create_mission(root, "m-a")
    _create_mission(root, "m-b")
    graph = store.create_graph(root, {
        "schema_version": 1, "goal_id": "g-live", "objective": "live",
        "nodes": [
            _node("n-a", mission_ref={"mission_id": "m-a", "required": True}),
            _node("n-b", depends_on=["n-a"],
                  mission_ref={"mission_id": "m-b", "required": True}),
            _node("n-c", depends_on=["n-b"]),
        ],
    })
    before = readiness.project_with_store(root, graph)
    assert before.by_id()["n-a"].state == readiness.RS_IN_PROGRESS
    assert before.by_id()["n-b"].state == readiness.RS_IN_PROGRESS
    assert before.by_id()["n-c"].state == readiness.RS_BLOCKED

    orchestrator.run_mission(root, "m-a", _OkRunner())
    after = readiness.project_with_store(root, graph)
    assert after.by_id()["n-a"].state == readiness.RS_COMPLETE
    # n-b is still its own in-progress mission; n-c cannot proceed until
    # n-b is proven, so a dependency-blocked node is never eligible.
    assert after.by_id()["n-b"].state == readiness.RS_IN_PROGRESS
    assert after.by_id()["n-c"].state == readiness.RS_BLOCKED

    orchestrator.run_mission(root, "m-b", _OkRunner())
    final = readiness.project_with_store(root, graph)
    assert final.by_id()["n-b"].state == readiness.RS_COMPLETE
    assert final.by_id()["n-c"].state == readiness.RS_READY
    assert final.ready() == ("n-c",)


def test_failed_mission_evidence_blocks_fail_closed(tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    _create_mission(root, "m-fail")
    graph = store.create_graph(root, {
        "schema_version": 1, "goal_id": "g-fail", "objective": "fail",
        "nodes": [
            _node("n-fail",
                  mission_ref={"mission_id": "m-fail", "required": True}),
            _node("n-next", depends_on=["n-fail"]),
        ],
    })
    orchestrator.run_mission(root, "m-fail", _FailRunner())
    projection = readiness.project_with_store(root, graph)
    assert projection.by_id()["n-fail"].state == readiness.RS_BLOCKED
    assert projection.by_id()["n-next"].state == readiness.RS_BLOCKED
    assert projection.ready() == ()


def test_resolve_missing_mission_is_read_only(tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    record = evidence.resolve_mission_evidence(root, "m-absent")
    assert record.resolved is False
    assert record.error == evidence.ERR_NOT_FOUND


def test_evidence_module_has_no_write_surface() -> None:
    source = Path(evidence.__file__).read_text(encoding="utf-8")
    assert "save_mission" not in source
    assert "save_subrun" not in source
