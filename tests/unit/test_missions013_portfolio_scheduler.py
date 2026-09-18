"""Mission 013 — portfolio scheduler unit tests (model, arbiter, store).

Deterministic coverage for Issue #217: eligibility, ordering, admission and
arbitration, resource accounting, fail-closed validation, decision identity,
persistence round-trip, append-only history and strict reconstruction. The
production dogfood lives in ``tests/integration``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trajectory_os.graph import model as graph_model
from trajectory_os.graph import readiness
from trajectory_os.graph import store as graph_store
from trajectory_os.graph.scheduler import arbiter, engine
from trajectory_os.graph.scheduler import evidence as sched_evidence
from trajectory_os.graph.scheduler import model as sched_model
from trajectory_os.graph.scheduler import store as sched_store
from trajectory_os.graph.scheduler import summary as sched_summary
from trajectory_os.missions import model as mission_model
from trajectory_os.missions import orchestrator
from trajectory_os.missions.runner import SubrunResult

POLICY = sched_model.SchedulerPolicy(
    cpu_slots=6, gpu_slots=1, gpu_mem_bytes=8 * (1 << 30),
    global_concurrency=4)


class OkRunner:
    def run(self, request: Any) -> SubrunResult:
        return SubrunResult(0, mission_model.CR_COMPLETED)


class SpyDispatcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def dispatch(self, *, root: str, goal_id: str,
                 node: graph_model.GraphNode,
                 decision: sched_model.ScheduleDecision
                 ) -> engine.DispatchOutcome:
        self.calls.append(node.node_id)
        mission_id = node.mission_ref.mission_id if node.mission_ref else ""
        report = orchestrator.run_mission(root, mission_id, OkRunner())
        return engine.DispatchOutcome(
            mission_id=mission_id,
            dispatch_ref=f"{goal_id}/{node.node_id}",
            stop=report.stop,
            mission_state=report.mission_state,
            mission_reason=report.mission_reason,
        )


def _criterion() -> dict[str, str]:
    return {"criterion_id": "ac-1", "statement": "done"}


def _node(node_id: str, *, priority: int = 50,
          deps: list[str] | None = None, mission: str | None = None,
          resources: dict[str, Any] | None = None,
          budgets: dict[str, Any] | None = None) -> dict[str, Any]:
    document: dict[str, Any] = {
        "node_id": node_id,
        "title": node_id,
        "priority": priority,
        "depends_on": list(deps or []),
        "acceptance_criteria": [_criterion()],
    }
    if mission is not None:
        document["mission_ref"] = {"mission_id": mission, "required": True}
    if resources is not None:
        document["resources"] = resources
    if budgets is not None:
        document["budgets"] = budgets
    return document


def _make_mission(root: str, mission_id: str) -> None:
    orchestrator.create_mission(root, orchestrator.MissionConfig(
        mission_id=mission_id,
        objective="scheduler unit mission",
        phase_specs=orchestrator.default_phase_specs(
            {kind: ("true",) for kind in mission_model.CANONICAL_SEQUENCE}),
        baseline_revision="base",
    ))


def _prepare(tmp_path: Path, nodes: list[dict[str, Any]],
             *, goal_id: str = "g-sched") -> str:
    root = str(tmp_path / "root")
    for node in nodes:
        ref = node.get("mission_ref")
        if ref is not None:
            _make_mission(root, ref["mission_id"])
    spec = {"schema_version": 1, "goal_id": goal_id, "objective": "sched",
            "nodes": nodes}
    graph_store.create_graph(root, spec, repo_root=str(tmp_path),
                             baseline_revision="base")
    return root


def _decision(root: str, goal_id: str = "g-sched",
              policy: sched_model.SchedulerPolicy = POLICY
              ) -> sched_model.ScheduleDecision:
    return engine.build_decision(root, goal_id, policy, created_at="t0")


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------


def test_policy_validation_bounds_and_roundtrip() -> None:
    assert POLICY.validate() is POLICY
    assert len(POLICY.policy_id) == 64
    assert sched_model.SchedulerPolicy.from_dict(POLICY.to_dict()) == POLICY
    with pytest.raises(sched_model.SchedulerValidationError) as excinfo:
        sched_model.SchedulerPolicy.from_dict({
            "cpu_slots": 1, "gpu_slots": 0, "gpu_mem_bytes": 0,
            "global_concurrency": 0})
    assert excinfo.value.code == sched_model.E_INVALID_POLICY
    with pytest.raises(sched_model.SchedulerValidationError):
        sched_model.SchedulerPolicy.from_dict({
            "cpu_slots": 1, "gpu_slots": 0, "gpu_mem_bytes": 0,
            "global_concurrency": 1, "extra": 1})
    with pytest.raises(sched_model.SchedulerValidationError) as excinfo:
        sched_model.SchedulerPolicy.from_dict({
            "schema_version": 9, "cpu_slots": 1, "gpu_slots": 0,
            "gpu_mem_bytes": 0, "global_concurrency": 1})
    assert excinfo.value.code == sched_model.E_UNSUPPORTED_VERSION
    assert sched_model.DEFAULT_POLICY.validate() is sched_model.DEFAULT_POLICY


def test_demand_remote_model_heavy_never_reserves_gpu() -> None:
    node = graph_model.GraphNode(
        node_id="n-remote", title="r", priority=50, depends_on=(),
        acceptance_criteria=(), mission_ref=None,
        resources=graph_model.NodeResources(cpu_slots=1, model_heavy=True),
        budgets=graph_model.NodeBudget())
    demand = sched_model.derive_demand(node)
    assert demand.execution == sched_model.X_REMOTE_MODEL
    assert demand.gpu_slots == 0
    assert demand.gpu_mem_bytes == 0
    assert demand.cpu_slots == 1


def test_demand_local_gpu_reserves_declared_capacity() -> None:
    node = graph_model.GraphNode(
        node_id="n-gpu", title="g", priority=50, depends_on=(),
        acceptance_criteria=(), mission_ref=None,
        resources=graph_model.NodeResources(
            gpu=True, gpu_mem_bytes=1234, model_heavy=True),
        budgets=graph_model.NodeBudget())
    demand = sched_model.derive_demand(node)
    assert demand.execution == sched_model.X_LOCAL_GPU
    assert demand.gpu_slots == 1
    assert demand.gpu_mem_bytes == 1234


def test_demand_rejects_gpu_memory_without_gpu() -> None:
    node = graph_model.GraphNode(
        node_id="n-bad", title="b", priority=50, depends_on=(),
        acceptance_criteria=(), mission_ref=None,
        resources=graph_model.NodeResources(gpu=False, gpu_mem_bytes=1024),
        budgets=graph_model.NodeBudget())
    with pytest.raises(sched_model.SchedulerValidationError) as excinfo:
        sched_model.derive_demand(node)
    assert excinfo.value.code == sched_model.E_INVALID_RESOURCE


def test_reason_codes_are_a_closed_stable_set() -> None:
    assert sched_model.R_ADMITTED in sched_model.REASON_CODES
    for code in ("DEPENDENCY_BLOCKED", "CPU_CAPACITY", "GPU_CAPACITY",
                 "GPU_MEMORY", "EXCLUSIVE_CONFLICT", "CONCURRENCY_LIMIT",
                 "BUDGET_EXHAUSTED", "INVALID_RESOURCE_SPEC",
                 "UNRESOLVED_EVIDENCE", "STALE_DECISION_INPUT"):
        assert code in sched_model.REASON_CODES
    assert frozenset(sched_model.REASON_CODES) == sched_model.REASON_CODES


def test_decision_from_dict_rejects_tampering(tmp_path: Path) -> None:
    root = _prepare(tmp_path, [_node("n-a", mission="m-a")])
    decision = _decision(root)
    raw = decision.to_dict()
    raw["admitted"][0]["reason"] = sched_model.R_CPU_CAPACITY
    with pytest.raises(sched_model.SchedulerValidationError) as excinfo:
        sched_model.ScheduleDecision.from_dict(raw, "tampered")
    assert excinfo.value.code == sched_model.E_IDENTITY_MISMATCH


def test_decision_from_dict_rejects_impossible_accounting() -> None:
    reservation = sched_model.Reservation(
        node_id="n-a", mission_id="m-a", execution=sched_model.X_LOCAL_GPU,
        cpu_slots=0, gpu_slots=1, gpu_mem_bytes=9 * (1 << 30),
        exclusive=False, owned=True)
    oversized = sched_model.ScheduleDecision.build(
        goal_id="g", graph_id="0" * 64, spec_sha256="1" * 64,
        input_projection_id="2" * 64, policy=POLICY, concurrency_limit=4,
        candidate_order=(), admitted=(), deferred=(), blocked=(), active=(),
        completed=(),
        reservations=sched_model.ReservationSnapshot(
            current=(reservation,), scheduler_owned=(reservation,)),
        dispatch=(), budget_counters={}, created_at="t")
    with pytest.raises(sched_model.SchedulerValidationError) as excinfo:
        sched_model.ScheduleDecision.from_dict(oversized.to_dict(), "x")
    assert excinfo.value.code == sched_model.E_IMPOSSIBLE_ACCOUNTING


# ---------------------------------------------------------------------------
# eligibility and ordering
# ---------------------------------------------------------------------------


def test_eligible_candidate_construction_and_dependency_blocked(
        tmp_path: Path) -> None:
    root = _prepare(tmp_path, [
        _node("n-a", priority=90, mission="m-a", resources={"cpu_slots": 1}),
        _node("n-b", priority=50, deps=["n-a"], mission="m-b",
              resources={"cpu_slots": 1}),
    ])
    decision = _decision(root)
    assert [n.node_id for n in decision.admitted] == ["n-a"]
    blocked = {n.node_id: n.reason for n in decision.blocked}
    assert blocked == {"n-b": sched_model.R_DEPENDENCY_BLOCKED}
    assert decision.candidate_order == ("n-a",)


def test_deterministic_priority_order_and_tie_break(tmp_path: Path) -> None:
    root = _prepare(tmp_path, [
        _node("n-b", priority=90, mission="m-b", resources={"cpu_slots": 1}),
        _node("n-a", priority=90, mission="m-a", resources={"cpu_slots": 1}),
        _node("n-c", priority=10, mission="m-c", resources={"cpu_slots": 1}),
    ])
    decision = _decision(root)
    assert decision.candidate_order == ("n-a", "n-b", "n-c")
    assert [n.node_id for n in decision.admitted] == ["n-a", "n-b", "n-c"]
    again = _decision(root)
    assert again.decision_id == decision.decision_id


# ---------------------------------------------------------------------------
# resource arbitration
# ---------------------------------------------------------------------------


def test_cpu_capacity_deferral(tmp_path: Path) -> None:
    root = _prepare(tmp_path, [
        _node("n-a", priority=90, mission="m-a", resources={"cpu_slots": 5}),
        _node("n-b", priority=80, mission="m-b", resources={"cpu_slots": 2}),
    ])
    decision = _decision(root)
    assert [n.node_id for n in decision.admitted] == ["n-a"]
    assert [(n.node_id, n.reason) for n in decision.deferred] == [
        ("n-b", sched_model.R_CPU_CAPACITY)]


def test_gpu_capacity_deferral(tmp_path: Path) -> None:
    root = _prepare(tmp_path, [
        _node("n-gpu", priority=90, mission="m-gpu",
              resources={"gpu": True, "gpu_mem_bytes": 1024}),
        _node("n-gpu2", priority=80, mission="m-gpu2",
              resources={"gpu": True, "gpu_mem_bytes": 1024}),
    ])
    decision = _decision(root)
    assert [n.node_id for n in decision.admitted] == ["n-gpu"]
    assert [(n.node_id, n.reason) for n in decision.deferred] == [
        ("n-gpu2", sched_model.R_GPU_CAPACITY)]


def test_gpu_memory_deferral(tmp_path: Path) -> None:
    policy = sched_model.SchedulerPolicy(
        cpu_slots=4, gpu_slots=2, gpu_mem_bytes=8 * (1 << 30),
        global_concurrency=4)
    root = _prepare(tmp_path, [
        _node("n-gpu", priority=90, mission="m-gpu",
              resources={"gpu": True, "gpu_mem_bytes": 6 * (1 << 30)}),
        _node("n-vram", priority=80, mission="m-vram",
              resources={"gpu": True, "gpu_mem_bytes": 4 * (1 << 30)}),
    ])
    decision = _decision(root, policy=policy)
    assert [n.node_id for n in decision.admitted] == ["n-gpu"]
    assert [(n.node_id, n.reason) for n in decision.deferred] == [
        ("n-vram", sched_model.R_GPU_MEMORY)]


def test_remote_model_heavy_does_not_reserve_local_gpu(
        tmp_path: Path) -> None:
    root = _prepare(tmp_path, [
        _node("n-remote", priority=90, mission="m-remote",
              resources={"cpu_slots": 1, "model_heavy": True}),
        _node("n-gpu", priority=80, mission="m-gpu",
              resources={"gpu": True, "gpu_mem_bytes": 2 * (1 << 30),
                         "model_heavy": True}),
    ])
    decision = _decision(root)
    assert decision.candidate_order == ("n-remote", "n-gpu")
    assert {n.node_id for n in decision.admitted} == {"n-remote", "n-gpu"}
    newly = {r.node_id: r for r in decision.reservations.newly_reserved}
    assert newly["n-remote"].gpu_slots == 0
    assert newly["n-remote"].gpu_mem_bytes == 0
    assert newly["n-remote"].execution == sched_model.X_REMOTE_MODEL
    assert newly["n-gpu"].gpu_slots == 1
    assert newly["n-gpu"].gpu_mem_bytes == 2 * (1 << 30)
    assert newly["n-gpu"].execution == sched_model.X_LOCAL_GPU


def test_local_gpu_consumes_declared_capacity(tmp_path: Path) -> None:
    root = _prepare(tmp_path, [
        _node("n-gpu", priority=90, mission="m-gpu",
              resources={"gpu": True, "gpu_mem_bytes": 3 * (1 << 30)}),
    ])
    dispatcher = engine.MissionPathDispatcher(
        runner_factory=OkRunner, session_subruns=1)
    result = engine.run_cycle(root, "g-sched", POLICY, dispatch=True,
                              dispatcher=dispatcher, created_at="t1")
    newly = {r.node_id: r for r in result.decision.reservations.newly_reserved}
    assert newly["n-gpu"].gpu_slots == 1
    assert newly["n-gpu"].gpu_mem_bytes == 3 * (1 << 30)
    read = engine.reconstruct(root, "g-sched")
    active = {r.node_id: r for r in read.reservations}
    assert active["n-gpu"].gpu_slots == 1
    assert active["n-gpu"].gpu_mem_bytes == 3 * (1 << 30)


def test_exclusive_conflict(tmp_path: Path) -> None:
    root = _prepare(tmp_path, [
        _node("n-cpu", priority=90, mission="m-cpu",
              resources={"cpu_slots": 1}),
        _node("n-excl", priority=80, mission="m-excl",
              resources={"gpu": True, "gpu_mem_bytes": 1024,
                         "exclusive": True}),
    ])
    decision = _decision(root)
    assert [n.node_id for n in decision.admitted] == ["n-cpu"]
    assert [(n.node_id, n.reason) for n in decision.deferred] == [
        ("n-excl", sched_model.R_EXCLUSIVE_CONFLICT)]


def test_global_concurrency_limit(tmp_path: Path) -> None:
    policy = sched_model.SchedulerPolicy(
        cpu_slots=8, gpu_slots=1, gpu_mem_bytes=1 << 30,
        global_concurrency=2)
    root = _prepare(tmp_path, [
        _node("n-a", priority=90, mission="m-a", resources={"cpu_slots": 1}),
        _node("n-b", priority=80, mission="m-b", resources={"cpu_slots": 1}),
        _node("n-c", priority=70, mission="m-c", resources={"cpu_slots": 1}),
    ])
    decision = _decision(root, policy=policy)
    assert [n.node_id for n in decision.admitted] == ["n-a", "n-b"]
    assert [(n.node_id, n.reason) for n in decision.deferred] == [
        ("n-c", sched_model.R_CONCURRENCY_LIMIT)]


def test_budget_exhaustion(tmp_path: Path) -> None:
    root = _prepare(tmp_path, [
        _node("n-a", priority=90, mission="m-a", resources={"cpu_slots": 1},
              budgets={"max_attempts": 1}),
    ])
    graph, _ = graph_store.load_graph(root, "g-sched")
    projection = readiness.project_with_store(root, graph)
    runtime = sched_evidence.collect_runtime(root, ["m-a"])
    decision = arbiter.plan(
        graph, projection, POLICY, runtime, (), {"n-a": 1}, created_at="t")
    assert decision.admitted == ()
    assert [(n.node_id, n.reason) for n in decision.deferred] == [
        ("n-a", sched_model.R_BUDGET_EXHAUSTED)]


def test_invalid_resource_metadata_defers_fail_closed(tmp_path: Path) -> None:
    criterion = graph_model.AcceptanceCriterion("ac-1", "s", None)
    node = graph_model.GraphNode(
        node_id="n-bad", title="bad", priority=10, depends_on=(),
        acceptance_criteria=(criterion,), mission_ref=None,
        resources=graph_model.NodeResources(gpu=False, gpu_mem_bytes=1024),
        budgets=graph_model.NodeBudget())
    provenance = graph_model.GraphProvenance(
        created_at="t", created_by="test", repo_root=None,
        baseline_revision=None)
    graph = graph_model.GoalGraph.build(
        goal_id="g-bad", objective="bad", nodes=(node,), provenance=provenance)
    projection = readiness.project(graph, {})
    decision = arbiter.plan(graph, projection, POLICY, {}, (), {},
                            created_at="t")
    assert decision.admitted == ()
    assert [(n.node_id, n.reason) for n in decision.deferred] == [
        ("n-bad", sched_model.R_INVALID_RESOURCE_SPEC)]


def test_missing_mission_reference_is_never_admitted(tmp_path: Path) -> None:
    root = _prepare(tmp_path, [_node("n-noref", priority=90)])
    decision = _decision(root)
    assert decision.admitted == ()
    assert [(n.node_id, n.reason) for n in decision.deferred] == [
        ("n-noref", sched_model.R_MISSING_MISSION_REFERENCE)]


# ---------------------------------------------------------------------------
# dispatch composition
# ---------------------------------------------------------------------------


def test_no_dependency_blocked_dispatch(tmp_path: Path) -> None:
    root = _prepare(tmp_path, [
        _node("n-a", priority=90, mission="m-a", resources={"cpu_slots": 1}),
        _node("n-b", priority=80, deps=["n-a"], mission="m-b",
              resources={"cpu_slots": 1}),
    ])
    spy = SpyDispatcher()
    result = engine.run_cycle(root, "g-sched", POLICY, dispatch=True,
                              dispatcher=spy, created_at="t1")
    assert spy.calls == ["n-a"]
    assert [r.node_id for r in result.dispatched] == ["n-a"]
    assert {n.node_id for n in result.decision.blocked} == {"n-b"}


def test_dispatch_composes_with_mission_path_and_unlocks_downstream(
        tmp_path: Path) -> None:
    root = _prepare(tmp_path, [
        _node("n-a", priority=90, mission="m-a", resources={"cpu_slots": 1}),
        _node("n-b", priority=80, deps=["n-a"], mission="m-b",
              resources={"cpu_slots": 1}),
    ])
    engine.run_cycle(root, "g-sched", POLICY, dispatch=True,
                     dispatcher=SpyDispatcher(), created_at="t1")
    after = _decision(root)
    assert [n.node_id for n in after.admitted] == ["n-b"]
    assert after.blocked == ()
    assert {n.node_id for n in after.completed} == {"n-a"}


def test_stale_decision_input_is_refused(tmp_path: Path) -> None:
    root = _prepare(tmp_path, [
        _node("n-a", priority=90, mission="m-a", resources={"cpu_slots": 1}),
    ])
    first = _decision(root)
    _make_mission(root, "unrelated")
    with pytest.raises(sched_model.SchedulerValidationError) as excinfo:
        engine.run_cycle(root, "g-sched", POLICY, dispatch=False,
                         created_at="t1",
                         expected_decision_id="3" * 64)
    assert sched_model.R_STALE_DECISION_INPUT in str(excinfo.value)
    assert sched_store.load_state(root, "g-sched") is None
    assert first.decision_id  # original decision was never persisted


# ---------------------------------------------------------------------------
# persistence / reconstruction
# ---------------------------------------------------------------------------


def test_persistence_round_trip_and_idempotency(tmp_path: Path) -> None:
    root = _prepare(tmp_path, [
        _node("n-a", priority=90, mission="m-a", resources={"cpu_slots": 1}),
    ])
    first = engine.run_cycle(root, "g-sched", POLICY, dispatch=False,
                             created_at="t1")
    assert first.persisted is True
    second = engine.run_cycle(root, "g-sched", POLICY, dispatch=False,
                              created_at="t2")
    assert second.persisted is False
    assert second.decision.decision_id == first.decision.decision_id
    paths = sched_store.scheduler_paths(root, "g-sched")
    stored = sched_store.load_decision(paths, first.decision.decision_id)
    assert stored.to_dict() == first.decision.to_dict()
    assert len(sched_store.list_decision_files(paths)) == 1
    assert len(sched_store.load_state(root, "g-sched").decision_ids) == 1


def test_append_only_history(tmp_path: Path) -> None:
    root = _prepare(tmp_path, [
        _node("n-a", priority=90, mission="m-a", resources={"cpu_slots": 1}),
        _node("n-b", priority=80, deps=["n-a"], mission="m-b",
              resources={"cpu_slots": 1}),
    ])
    engine.run_cycle(root, "g-sched", POLICY, dispatch=True,
                     dispatcher=SpyDispatcher(), created_at="t1")
    engine.run_cycle(root, "g-sched", POLICY, dispatch=False, created_at="t2")
    state = sched_store.load_state(root, "g-sched")
    assert state is not None
    assert len(state.decision_ids) == 2
    assert len(sched_store.list_decision_files(
        sched_store.scheduler_paths(root, "g-sched"))) == 2
    events = sched_store.load_events(
        sched_store.scheduler_paths(root, "g-sched"))
    assert [e["event"] for e in events].count("decision") == 2


def test_restart_reconstruction_equivalence(tmp_path: Path) -> None:
    root = _prepare(tmp_path, [
        _node("n-a", priority=90, mission="m-a", resources={"cpu_slots": 1}),
    ])
    result = engine.run_cycle(root, "g-sched", POLICY, dispatch=False,
                              created_at="t1")
    first = engine.reconstruct(root, "g-sched")
    second = engine.reconstruct(root, "g-sched")
    assert first.to_dict() == second.to_dict()
    assert first.latest_decision is not None
    assert (first.latest_decision.decision_id
            == result.decision.decision_id)
    assert first.latest_decision.to_dict() == result.decision.to_dict()


def test_graph_identity_mismatch_rejected(tmp_path: Path) -> None:
    root = _prepare(tmp_path, [
        _node("n-a", priority=90, mission="m-a", resources={"cpu_slots": 1}),
    ])
    engine.run_cycle(root, "g-sched", POLICY, dispatch=False, created_at="t1")
    paths = sched_store.scheduler_paths(root, "g-sched")
    state = sched_store.load_state(root, "g-sched")
    assert state is not None
    tampered = state.to_dict()
    tampered["graph_id"] = "0" * 64
    paths["state"].write_text(_json(tampered), encoding="utf-8")
    with pytest.raises(sched_model.SchedulerValidationError) as excinfo:
        engine.reconstruct(root, "g-sched")
    assert excinfo.value.code == sched_model.E_GRAPH_MISMATCH


def test_untracked_decision_rejected(tmp_path: Path) -> None:
    root = _prepare(tmp_path, [
        _node("n-a", priority=90, mission="m-a", resources={"cpu_slots": 1}),
    ])
    engine.run_cycle(root, "g-sched", POLICY, dispatch=False, created_at="t1")
    paths = sched_store.scheduler_paths(root, "g-sched")
    stray = paths["decisions"] / f"{'a' * 64}.json"
    stray.write_text("{}", encoding="utf-8")
    with pytest.raises(sched_model.SchedulerValidationError) as excinfo:
        engine.reconstruct(root, "g-sched")
    assert excinfo.value.code == sched_model.E_UNTRACKED_DECISION


def test_ambiguous_dispatch_evidence_rejected(tmp_path: Path) -> None:
    root = _prepare(tmp_path, [
        _node("n-a", priority=90, mission="m-a", resources={"cpu_slots": 1}),
    ])
    engine.run_cycle(root, "g-sched", POLICY, dispatch=True,
                     dispatcher=SpyDispatcher(), created_at="t1")
    paths = sched_store.scheduler_paths(root, "g-sched")
    state = sched_store.load_state(root, "g-sched")
    assert state is not None and state.dispatch_records
    tampered = state.to_dict()
    tampered["dispatch_records"][0]["decision_id"] = "9" * 64
    paths["state"].write_text(_json(tampered), encoding="utf-8")
    with pytest.raises(sched_model.SchedulerValidationError) as excinfo:
        engine.reconstruct(root, "g-sched")
    assert excinfo.value.code == sched_model.E_DISPATCH_EVIDENCE


def test_duplicate_dispatch_id_rejected(tmp_path: Path) -> None:
    root = _prepare(tmp_path, [
        _node("n-a", priority=90, mission="m-a", resources={"cpu_slots": 1}),
    ])
    engine.run_cycle(root, "g-sched", POLICY, dispatch=True,
                     dispatcher=SpyDispatcher(), created_at="t1")
    paths = sched_store.scheduler_paths(root, "g-sched")
    state = sched_store.load_state(root, "g-sched")
    assert state is not None and state.dispatch_records
    tampered = state.to_dict()
    tampered["dispatch_records"].append(
        dict(tampered["dispatch_records"][0]))
    paths["state"].write_text(_json(tampered), encoding="utf-8")
    with pytest.raises(sched_model.SchedulerValidationError) as excinfo:
        engine.reconstruct(root, "g-sched")
    assert excinfo.value.code == sched_model.E_DISPATCH_EVIDENCE


def test_unsupported_schema_rejected(tmp_path: Path) -> None:
    root = _prepare(tmp_path, [
        _node("n-a", priority=90, mission="m-a", resources={"cpu_slots": 1}),
    ])
    engine.run_cycle(root, "g-sched", POLICY, dispatch=False, created_at="t1")
    paths = sched_store.scheduler_paths(root, "g-sched")
    state = sched_store.load_state(root, "g-sched")
    assert state is not None
    tampered = state.to_dict()
    tampered["schema_version"] = 99
    paths["state"].write_text(_json(tampered), encoding="utf-8")
    with pytest.raises(sched_model.SchedulerValidationError) as excinfo:
        sched_store.load_state(root, "g-sched")
    assert excinfo.value.code == sched_model.E_UNSUPPORTED_VERSION


# ---------------------------------------------------------------------------
# projections
# ---------------------------------------------------------------------------


def test_machine_projection_and_human_summary(tmp_path: Path) -> None:
    root = _prepare(tmp_path, [
        _node("n-a", priority=90, mission="m-a", resources={"cpu_slots": 1}),
        _node("n-b", priority=80, deps=["n-a"], mission="m-b",
              resources={"cpu_slots": 1}),
    ])
    engine.run_cycle(root, "g-sched", POLICY, dispatch=False, created_at="t1")
    document = sched_summary.status_document(root, "g-sched")
    for key in ("policy", "policy_id", "decision_id", "input_projection_id",
                "admitted", "deferred", "blocked", "reservations", "history"):
        assert key in document
    assert document["admitted"][0]["node_id"] == "n-a"
    assert "portfolio" in sched_summary.render_portfolio(document)
    assert "configured" in sched_summary.render_resources(document)
    history = sched_summary.history_document(root, "g-sched")
    assert history["count"] == 1
    explain = sched_summary.explain_document(root, "g-sched", "n-a")
    assert explain["group"] == "admitted"
    assert "outcome" in sched_summary.render_explain(explain)


def _json(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"
