"""Program block B (M020-M022) — portfolio + daemon + backend dogfood.

Deterministic end-to-end proof that:

* M020 — two isolated strategic goals coexist under one portfolio scheduler
  without state/evidence/scheduler-decision leakage, and portfolio resources
  are arbitrated deterministically;
* M021 — a bounded, restart-safe daemon reconstructs exact state after a
  process interruption, respects a safe stop and resumes to completion;
* M022 — provider-neutral backend capability discovery, bounded retry,
  lifecycle normalization and exact provider/model route identity are
  machine-readable and fail closed.

The deterministic runner replaces the model subprocess only (exactly as the
M016-M019 harnesses do), so no network/GPU/Git write is involved.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trajectory_os.agents import capabilities, lifecycle, retry, route
from trajectory_os.agents import model as agent_model
from trajectory_os.daemon import engine as daemon_engine
from trajectory_os.daemon import model as daemon_model
from trajectory_os.daemon import summary as daemon_summary
from trajectory_os.goals import launch
from trajectory_os.graph import store as graph_store
from trajectory_os.graph.proof import engine as proof_engine
from trajectory_os.missions import model as mission_model
from trajectory_os.missions import semantic
from trajectory_os.missions.runner import SubrunResult
from trajectory_os.portfolio import engine as portfolio_engine
from trajectory_os.portfolio import model as portfolio_model
from trajectory_os.portfolio import summary as portfolio_summary

GOAL_A = "g-portfolio-a"
GOAL_B = "g-portfolio-b"
MISSION_A = "m-portfolio-a"
MISSION_B = "m-portfolio-b"


class AttestedRunner:
    """Deterministic exact-attestation success for every phase."""

    def run(self, request: Any) -> SubrunResult:
        return SubrunResult(
            0, mission_model.CR_COMPLETED,
            semantic_status=semantic.STATUS_SUCCESS,
            attestation=semantic.ATTESTATION_VERIFIED)


def _spec(goal_id: str, mission_id: str, priority: int) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "goal_id": goal_id,
        "objective": f"Prove portfolio isolation for {goal_id}.",
        "nodes": [{
            "node_id": f"n-{goal_id}",
            "title": f"Mission for {goal_id}",
            "priority": priority,
            "depends_on": [],
            "acceptance_criteria": [{
                "criterion_id": f"ac-{goal_id}",
                "statement": f"{goal_id} work is proven"}],
            "mission_ref": {"mission_id": mission_id, "required": True},
            "resources": {"cpu_slots": 1},
            "budgets": {"repair_budget": 0, "max_attempts": 1},
        }],
    }


def _defaults() -> launch.MissionLaunchDefaults:
    return launch.MissionLaunchDefaults(
        repo_root=None, baseline_revision="base", repair_budget=0)


def _setup(root: str, goal_id: str, mission_id: str, priority: int) -> None:
    defaults = _defaults()
    spec = _spec(goal_id, mission_id, priority)
    launch.provision_from_spec(root, spec, defaults)
    graph_store.create_graph(root, spec, repo_root=defaults.repo_root,
                             baseline_revision=defaults.baseline_revision)


# ---------------------------------------------------------------------------
# M020 — multi-goal portfolio
# ---------------------------------------------------------------------------


def test_portfolio_arbitrates_and_keeps_goals_isolated(
        tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    _setup(root, GOAL_A, MISSION_A, priority=90)
    _setup(root, GOAL_B, MISSION_B, priority=10)

    policy = portfolio_model.PortfolioPolicy(max_active_goals=1,
                                             global_concurrency=2)

    # Cycle 1: only the higher-priority goal A is selected; goal B is excluded
    # by the deterministic portfolio concurrency budget.
    first = portfolio_engine.run_cycle(
        root, [GOAL_A, GOAL_B], policy,
        runner_factory=AttestedRunner, session_subruns=32,
        created_at="2026-01-01T00:00:00Z")
    assert first.status == portfolio_engine.PS_DISPATCHED
    selected = [e.goal_id for e in first.decision.selected()]
    assert selected == [GOAL_A]
    excluded = {e.goal_id: e.reason for e in first.decision.entries
                if e.outcome == portfolio_model.O_EXCLUDED}
    assert excluded[GOAL_B] == portfolio_model.R_CONCURRENCY_LIMIT
    assert first.steps[0].goal_id == GOAL_A
    assert first.steps[0].complete is True
    assert first.steps[0].scheduler_decision_id

    # Cycle 2: goal A is terminal; goal B advances independently.
    second = portfolio_engine.run_cycle(
        root, [GOAL_A, GOAL_B], policy,
        runner_factory=AttestedRunner, session_subruns=32,
        created_at="2026-01-01T00:00:01Z")
    assert [e.goal_id for e in second.decision.selected()] == [GOAL_B]
    assert second.steps[0].goal_id == GOAL_B
    assert second.steps[0].complete is True
    # Scheduler decisions are distinct per goal (never shared across goals).
    assert first.steps[0].scheduler_decision_id != \
        second.steps[0].scheduler_decision_id

    # Each goal has its own independent completion proof over its own mission.
    proof_a = proof_engine.build_proof(root, GOAL_A)
    proof_b = proof_engine.build_proof(root, GOAL_B)
    assert proof_a.complete is True
    assert proof_b.complete is True
    assert proof_a.proof_id != proof_b.proof_id
    assert proof_a.graph_id != proof_b.graph_id
    assert {n.node_id for n in proof_a.nodes} == {f"n-{GOAL_A}"}
    assert {n.node_id for n in proof_b.nodes} == {f"n-{GOAL_B}"}

    state, latest = portfolio_engine.reconstruct(root)
    assert state.cycle_count == 2
    assert latest is not None
    assert latest.decision_id == state.last_decision_id
    assert {e.goal_id for e in latest.entries} == {GOAL_A, GOAL_B}


def test_portfolio_cross_goal_dependency_blocks_dependent(
        tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    _setup(root, GOAL_A, MISSION_A, priority=90)
    _setup(root, GOAL_B, MISSION_B, priority=10)
    dependencies = portfolio_model.PortfolioDependencies.normalize(
        {GOAL_B: [GOAL_A]})
    policy = portfolio_model.PortfolioPolicy(max_active_goals=2,
                                             global_concurrency=4)

    # Goal B depends on A: it is never selected while A is incomplete.
    decision = portfolio_engine.plan_portfolio(
        root, [GOAL_A, GOAL_B], policy, dependencies,
        created_at="2026-01-01T00:00:00Z")
    entry_b = decision.entry(GOAL_B)
    assert entry_b is not None
    assert entry_b.outcome == portfolio_model.O_EXCLUDED
    assert entry_b.reason == portfolio_model.R_DEPENDENCY_PENDING
    assert decision.entry(GOAL_A) is not None
    assert decision.entry(GOAL_A).outcome == portfolio_model.O_SELECTED

    # Complete A, then B becomes selectable in dependency order.
    portfolio_engine.run_cycle(
        root, [GOAL_A], policy, runner_factory=AttestedRunner,
        session_subruns=32, created_at="2026-01-01T00:00:01Z")
    after = portfolio_engine.plan_portfolio(
        root, [GOAL_A, GOAL_B], policy, dependencies,
        created_at="2026-01-01T00:00:02Z")
    assert after.entry(GOAL_B).reason == portfolio_model.R_SELECTED


def test_portfolio_rejects_cyclic_dependencies(tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    _setup(root, GOAL_A, MISSION_A, priority=90)
    _setup(root, GOAL_B, MISSION_B, priority=10)
    deps = portfolio_model.PortfolioDependencies.normalize(
        {GOAL_A: [GOAL_B], GOAL_B: [GOAL_A]})
    with pytest.raises(portfolio_model.PortfolioError):
        portfolio_engine.plan_portfolio(
            root, [GOAL_A, GOAL_B], portfolio_model.DEFAULT_POLICY, deps,
            created_at="2026-01-01T00:00:00Z")


# ---------------------------------------------------------------------------
# M021 — persistent daemon
# ---------------------------------------------------------------------------


def test_daemon_restart_reconstruction_and_resume(
        tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    _setup(root, GOAL_A, MISSION_A, priority=90)
    _setup(root, GOAL_B, MISSION_B, priority=10)
    config = daemon_model.DaemonConfig(
        max_cycles=1, session_subruns=32,
        policy=portfolio_model.PortfolioPolicy(max_active_goals=1,
                                               global_concurrency=2))

    # First bounded session completes goal A, then stops at the session
    # cycle bound before goal B can run (one goal slot per cycle).
    first = daemon_engine.run_daemon(
        root, [GOAL_A, GOAL_B], config=config,
        runner_factory=AttestedRunner, clock=lambda: "2026-01-01T00:00:00Z")
    assert first.status == daemon_model.DS_CYCLE_BOUND
    assert first.state.cycles_executed == 1

    # Interruption: a fresh process reconstructs the exact durable state.
    state, cycles = daemon_engine.reconstruct(root)
    assert state.cycles_executed == first.state.cycles_executed
    assert len(cycles) == state.cycles_executed
    assert state.last_cycle == cycles[-1]
    assert state.decision_ids == first.state.decision_ids

    # Resume continues from the persisted cycle count without replaying.
    resumed = daemon_engine.run_daemon(
        root, [GOAL_A, GOAL_B], config=config,
        runner_factory=AttestedRunner, clock=lambda: "2026-01-01T00:00:01Z")
    assert resumed.status == daemon_model.DS_COMPLETE
    assert resumed.reason == daemon_engine.DR_ALL_GOALS_COMPLETE
    assert resumed.state.cycles_executed == 2

    # Both goals are independently complete; the daemon never merged them.
    assert proof_engine.build_proof(root, GOAL_A).complete
    assert proof_engine.build_proof(root, GOAL_B).complete

    document = daemon_summary.status_document(root)
    assert document["present"] is True
    assert document["daemon_status"] == daemon_model.DS_COMPLETE
    goal_doc = daemon_summary.goal_daemon_document(root, GOAL_B)
    assert goal_doc["completed_in_cycles"] >= 1


def test_daemon_safe_stop_blocks_new_work_and_resume_completes(
        tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    _setup(root, GOAL_A, MISSION_A, priority=90)
    config = daemon_model.DaemonConfig(
        max_cycles=4, session_subruns=32,
        policy=portfolio_model.PortfolioPolicy(max_active_goals=1,
                                               global_concurrency=2))

    daemon_engine.request_stop(root, reason="operator safe stop",
                               requested_at="2026-01-01T00:00:00Z")
    stopped = daemon_engine.run_daemon(
        root, [GOAL_A], config=config, runner_factory=AttestedRunner,
        clock=lambda: "2026-01-01T00:00:00Z")
    assert stopped.status == daemon_model.DS_STOPPED
    assert stopped.reason == daemon_engine.DR_SAFE_STOP
    assert stopped.session_cycles == 0
    assert not proof_engine.build_proof(root, GOAL_A).complete

    resumed = daemon_engine.resume_daemon(
        root, [GOAL_A], config=config, runner_factory=AttestedRunner,
        clock=lambda: "2026-01-01T00:00:01Z")
    assert resumed.status == daemon_model.DS_COMPLETE
    assert daemon_engine.is_stop_requested(root) is False


def test_daemon_snapshot_is_observable_in_goal_snapshot(
        tmp_path: Path) -> None:
    from trajectory_os.goals import snapshot as goal_snapshot

    root = str(tmp_path / "root")
    _setup(root, GOAL_A, MISSION_A, priority=90)
    config = daemon_model.DaemonConfig(
        max_cycles=4, session_subruns=32,
        policy=portfolio_model.PortfolioPolicy(max_active_goals=1,
                                               global_concurrency=2))
    daemon_engine.run_daemon(
        root, [GOAL_A], config=config, runner_factory=AttestedRunner,
        clock=lambda: "2026-01-01T00:00:00Z")
    portfolio_engine.run_cycle(
        root, [GOAL_A], config.policy, runner_factory=AttestedRunner,
        session_subruns=32, created_at="2026-01-01T00:00:00Z")
    snap = goal_snapshot.build_snapshot(root, GOAL_A)
    assert snap["portfolio"]["present"] is True
    assert snap["portfolio"]["member"] is True
    assert snap["daemon"]["present"] is True
    assert snap["daemon"]["goal_id"] == GOAL_A


# ---------------------------------------------------------------------------
# M022 — production agent backends
# ---------------------------------------------------------------------------


class _FakeBackend:
    name = agent_model.BACKEND_PI

    def __init__(self, results: list[agent_model.AgentResult]) -> None:
        self._results = results
        self.calls = 0

    def probe(self) -> agent_model.BackendProbe:
        return agent_model.BackendProbe(
            backend=self.name, available=True, reason=agent_model.R_OK,
            transport=agent_model.TRANSPORT_SUBPROCESS)

    def run(self, request: agent_model.AgentRequest, *,
            cancel: object | None = None) -> agent_model.AgentResult:
        result = self._results[min(self.calls, len(self._results) - 1)]
        self.calls += 1
        return result


def _result(status: str, reason: str,
            source: str = agent_model.CS_NONE,
            reliable: bool = False) -> agent_model.AgentResult:
    return agent_model.AgentResult.build(
        backend=agent_model.BACKEND_PI, status=status, reason=reason,
        completion=agent_model.CompletionEvidence.build(
            source=source, reliable=reliable, detail=reason),
        events=[agent_model.AgentEvent.build(
            sequence=0, kind=agent_model.LK_STARTED, method="fake")])


def test_capability_discovery_is_machine_readable() -> None:
    backend = _FakeBackend([_result(agent_model.RS_COMPLETED,
                                    agent_model.R_OK,
                                    agent_model.CS_EXIT_CODE_MARKER, True)])
    report = capabilities.discover(backend)
    assert report.available is True
    assert capabilities.CAP_TIMEOUT in report.capabilities
    assert capabilities.CAP_CANCELLATION in report.capabilities
    assert report.report_id == report.compute_report_id()
    assert capabilities.CAP_STRUCTURED_LIFECYCLE not in report.capabilities


def test_bounded_retry_fails_closed_when_exhausted() -> None:
    backend = _FakeBackend([
        _result(agent_model.RS_TIMEOUT, agent_model.R_TIMEOUT),
        _result(agent_model.RS_TIMEOUT, agent_model.R_TIMEOUT),
    ])
    outcome = retry.run_with_retries(
        backend, agent_model.AgentRequest(task="x", workspace="/tmp"),
        retry.RetryPolicy(max_attempts=2))
    assert outcome.exhausted is True
    assert outcome.attempt_count == 2
    assert outcome.result.status == agent_model.RS_TIMEOUT
    assert backend.calls == 2


def test_bounded_retry_succeeds_without_overwriting_provenance() -> None:
    backend = _FakeBackend([
        _result(agent_model.RS_FAILED, agent_model.R_LAUNCH_FAILED),
        _result(agent_model.RS_COMPLETED, agent_model.R_OK,
                agent_model.CS_EXIT_CODE_MARKER, True),
    ])
    outcome = retry.run_with_retries(
        backend, agent_model.AgentRequest(task="x", workspace="/tmp"),
        retry.RetryPolicy(max_attempts=2))
    assert outcome.exhausted is False
    assert outcome.result.status == agent_model.RS_COMPLETED
    assert [a.status for a in outcome.attempts] == [
        agent_model.RS_FAILED, agent_model.RS_COMPLETED]


def test_retry_never_retries_unavailable_backend() -> None:
    backend = _FakeBackend([
        _result(agent_model.RS_UNAVAILABLE, agent_model.R_CREDENTIALS_MISSING)])
    outcome = retry.run_with_retries(
        backend, agent_model.AgentRequest(task="x", workspace="/tmp"),
        retry.RetryPolicy(max_attempts=3))
    assert outcome.attempt_count == 1
    assert backend.calls == 1


def test_lifecycle_normalization_and_route_identity() -> None:
    result = agent_model.AgentResult.build(
        backend=agent_model.BACKEND_DEEPSEEK_HARNESS,
        status=agent_model.RS_COMPLETED, reason=agent_model.R_OK,
        events=[
            agent_model.AgentEvent.build(
                sequence=0, kind=agent_model.LK_STARTED, method="spawn"),
            agent_model.AgentEvent.build(
                sequence=1, kind=agent_model.LK_INITIALIZED,
                method="initialize"),
            agent_model.AgentEvent.build(
                sequence=2, kind=agent_model.LK_IDLE,
                method="session.status"),
        ],
        completion=agent_model.CompletionEvidence.build(
            source=agent_model.CS_LIFECYCLE_IDLE, reliable=True, detail="idle"),
    )
    summary = lifecycle.LifecycleSummary.of(result)
    assert summary.has(lifecycle.FLAG_STARTED)
    assert summary.has(lifecycle.FLAG_INITIALIZED)
    assert summary.has(lifecycle.FLAG_IDLE)
    assert summary.has(lifecycle.FLAG_COMPLETED)
    assert summary.events_total == 3
    assert summary.summary_id == summary.compute_summary_id()

    request = agent_model.AgentRequest(
        task="x", workspace="/tmp", provider="deepseek-official",
        model="deepseek-reasoner")
    exact = route.resolve_route(
        agent_model.BACKEND_DEEPSEEK_HARNESS, request,
        transport=agent_model.TRANSPORT_RUNTIME)
    assert exact.provider == "deepseek-official"
    assert exact.model == "deepseek-reasoner"
    other = route.default_route(agent_model.BACKEND_DEEPSEEK_HARNESS)
    assert exact.route_id != other.route_id  # explicit vs default route


def test_portfolio_status_document_roundtrip(tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    _setup(root, GOAL_A, MISSION_A, priority=90)
    _setup(root, GOAL_B, MISSION_B, priority=10)
    portfolio_engine.run_cycle(
        root, [GOAL_A, GOAL_B],
        portfolio_model.PortfolioPolicy(max_active_goals=1,
                                        global_concurrency=2),
        runner_factory=AttestedRunner, session_subruns=32,
        created_at="2026-01-01T00:00:00Z")
    document = portfolio_summary.status_document(root)
    assert document["present"] is True
    assert document["counts"]["goals_total"] == 2
    text = portfolio_summary.render_status(document)
    assert "portfolio :" in text
    # JSON round-trip of the persisted decision is stable.
    decision_path = (Path(root) / "portfolio" / "decisions" /
                     f"{document['decision_id']}.json")
    raw = json.loads(decision_path.read_text(encoding="utf-8"))
    assert raw["decision_id"] == document["decision_id"]
