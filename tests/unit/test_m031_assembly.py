"""M031 — end-to-end mission assembly unit tests (fail-closed contract).

Covers the mission-level model only (the runtime flow is covered by the
integration suite):

* trust policy rejects any Git trust-boundary write and a review without a
  reviewer model;
* the durable mission definition validates its bounded intent;
* the plan is bounded, ordered and reuses the canonical workload gate;
* closure can never claim readiness without a COMPLETE lifecycle;
* the mission store round-trips and fails closed on missing documents;
* the human gate blocks a stale reviewed patch and an inactive reviewer.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from trajectory_os.agents import model as agent_model
from trajectory_os.assembly import closure as assembly_closure
from trajectory_os.assembly import model, store
from trajectory_os.assembly import orchestrator as assembly_run
from trajectory_os.benchmark import model as bench_model
from trajectory_os.benchmark.executor import ExecutionOutcome, ExecutionRequest
from trajectory_os.observability import model as obs_model
from trajectory_os.observability import store as obs_store

#: Modules that must never contain a Git trust-boundary write.
_ASSEMBLY_MODULES = (
    "model", "store", "baseline", "closure", "orchestrator", "cli",
)
_TRUST_WRITE_VERBS = (
    "commit", "push", "merge", "reset", "restore", "clean", "stash",
    "rebase", "switch", "checkout", "pull", "fetch", "cherry-pick",
)


def _mission(mission_id: str = "m031-unit", **overrides: object,
             ) -> model.MissionDefinition:
    data: dict[str, object] = {
        "mission_id": mission_id,
        "objective": "fix the add function",
        "constraints": ("no git writes",),
        "definition_of_done": ("validation passes", "review passes"),
        "backend": agent_model.BACKEND_PI,
        "provider": "deepseek",
        "model": "deepseek-flash",
        "trust_policy": model.TrustPolicy(),
        "baseline": model.MissionBaseline(
            revision="abc123", workspace_digest="digest",
            captured_at="2026-01-01T00:00:00Z"),
        "workspace": "/tmp/workspace",
        "workload_id": "small-targeted-repair",
        "mode": bench_model.MODE_FIXTURE,
        "telemetry_mode": obs_model.TELEMETRY_STANDARD,
        "created_at": "2026-01-01T00:00:00Z",
    }
    data.update(overrides)
    return model.MissionDefinition.build(**data)


def test_trust_policy_rejects_git_trust_write() -> None:
    with pytest.raises(model.AssemblyError) as excinfo:
        model.TrustPolicy(allow_git_trust_writes=True).validate()
    assert excinfo.value.code == model.R_INVALID_POLICY


def test_trust_policy_requires_reviewer_model() -> None:
    with pytest.raises(model.AssemblyError) as excinfo:
        model.TrustPolicy(
            require_review=True, final_reviewer_model="").validate()
    assert excinfo.value.code == model.R_INVALID_POLICY


def test_trust_policy_human_gate_is_ready_for_commit() -> None:
    with pytest.raises(model.AssemblyError):
        model.TrustPolicy(stop_at=obs_model.RD_BLOCKED).validate()
    policy = model.TrustPolicy()
    assert policy.stop_at == obs_model.RD_READY_FOR_COMMIT
    assert policy.allow_git_trust_writes is False


def test_mission_definition_requires_definition_of_done() -> None:
    with pytest.raises(model.AssemblyError):
        _mission(definition_of_done=())


def test_mission_definition_unknown_workload_fails_closed() -> None:
    with pytest.raises(bench_model.BenchmarkError):
        _mission(workload_id="does-not-exist")


def test_mission_definition_rejects_unknown_backend() -> None:
    with pytest.raises(model.AssemblyError):
        _mission(backend="not-a-backend")


def test_mission_definition_round_trip() -> None:
    mission = _mission()
    assert model.MissionDefinition.from_dict(mission.to_dict()).to_dict() == (
        mission.to_dict())


def test_build_plan_is_bounded_and_ordered() -> None:
    mission = _mission()
    plan = model.build_plan(mission)
    step_ids = [step.step_id for step in plan.steps]
    assert step_ids == [
        "intake", "preflight", "plan", "implement", "validate", "review",
        "repair", "human_gate", "closure",
    ]
    assert plan.retry_budget == mission.trust_policy.max_repairs
    assert plan.reviewer_role == obs_model.ROLE_FINAL_INDEPENDENT_REVIEWER
    assert plan.expected_validation_gates
    assert plan.expected_terminal_conditions == (
        obs_model.RD_READY_FOR_COMMIT, obs_model.RD_BLOCKED,
        obs_model.RD_FAILED, obs_model.RD_CANCELLED)
    assert model.MissionPlan.from_dict(plan.to_dict()).to_dict() == (
        plan.to_dict())


def test_closure_rejects_ready_without_complete_lifecycle() -> None:
    with pytest.raises(model.AssemblyError):
        model.MissionClosure(
            mission_id="m", objective="o", definition_of_done=("d",),
            constraints=(), baseline={}, plan=None, steps_executed=(),
            attempts=0, repairs=0, validation_results=(), review_results=(),
            current_patch=None, reviewed_patch=None, telemetry_summary=None,
            lifecycle=obs_model.LC_RUNNING,
            readiness=obs_model.RD_READY_FOR_COMMIT,
            terminal_reason=None, terminal_reason_code=None,
            next_action="", artifacts={}, created_at="t").validate()


def test_generate_mission_id_is_deterministic_and_safe() -> None:
    first = model.generate_mission_id("objective", "2026-01-01T00:00:00Z")
    second = model.generate_mission_id("objective", "2026-01-01T00:00:00Z")
    assert first == second
    assert re.fullmatch(model.MISSION_ID_RE, first)


def test_store_round_trip(tmp_path: Path) -> None:
    mission = _mission()
    plan = model.build_plan(mission)
    store.write_mission(tmp_path, mission)
    store.write_plan(tmp_path, plan)
    assert store.mission_exists(tmp_path, mission.mission_id)
    assert store.plan_exists(tmp_path, mission.mission_id)
    assert store.load_mission(tmp_path, mission.mission_id).to_dict() == (
        mission.to_dict())
    assert store.load_plan(tmp_path, mission.mission_id).to_dict() == (
        plan.to_dict())


def test_store_missing_mission_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(model.AssemblyError) as excinfo:
        store.load_mission(tmp_path, "absent")
    assert excinfo.value.code == model.R_MISSION_MISSING


def test_reconstruct_missing_mission_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(model.AssemblyError):
        assembly_closure.reconstruct_mission(tmp_path, "absent")


def _stale_status() -> obs_model.CanonicalStatus:
    return obs_model.CanonicalStatus.build(
        run_id="m031-unit", state=obs_model.LC_COMPLETE,
        stage=obs_model.STAGE_DONE, phase="DONE", attempt=1,
        current_backend=agent_model.BACKEND_PI, current_provider="deepseek",
        current_model="deepseek-flash", inline_review_enabled=False,
        inline_reviewer=obs_model.ReviewerStatus.disabled(
            obs_model.ROLE_INLINE_REVIEWER),
        final_review_enabled=True,
        final_reviewer=obs_model.ReviewerStatus(
            role=obs_model.ROLE_FINAL_INDEPENDENT_REVIEWER, enabled=True,
            active=True, backend="ollama", provider="ollama",
            model=obs_model.FINAL_REVIEWER_MODEL, reason=obs_model.R_OK),
        previous_gate=obs_model.GATE_REVIEW,
        previous_result=obs_model.RESULT_PASS,
        reviewed_patch="a" * 64, current_patch="b" * 64,
        next_action="operator: commit the reviewed patch",
        last_meaningful_event_at=None, heartbeat_at=None,
        terminal_reason="all gates passed",
        readiness=obs_model.RD_READY_FOR_COMMIT)


def test_human_gate_blocks_stale_review(tmp_path: Path) -> None:
    mission = _mission()
    plan = model.build_plan(mission)
    orchestrator = assembly_run.MissionOrchestrator(
        tmp_path, executor=_NoopExecutor())
    status, result = orchestrator._human_gate(  # noqa: SLF001 - unit contract
        store.ensure_mission_root(tmp_path, mission.mission_id), mission,
        plan, _stale_status())
    assert result == "blocked"
    assert status.readiness == obs_model.RD_BLOCKED
    assert status.terminal_reason_code == model.R_HUMAN_GATE_VIOLATION
    persisted = obs_store.load_status(
        store.mission_root(tmp_path, mission.mission_id))
    assert persisted["readiness"] == obs_model.RD_BLOCKED
    assert persisted["success"] is False


def test_no_git_trust_boundary_write_in_assembly_sources() -> None:
    import importlib

    offenders: list[str] = []
    for name in _ASSEMBLY_MODULES:
        module = importlib.import_module(f"trajectory_os.assembly.{name}")
        source = Path(module.__file__).read_text(encoding="utf-8")
        for verb in _TRUST_WRITE_VERBS:
            if f'"git", "{verb}"' in source or f"'git', '{verb}'" in source:
                offenders.append(f"{module.__name__}:{verb}")
    assert offenders == []


class _NoopExecutor:
    """A trial executor that must never be invoked in unit tests."""

    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:  # pragma: no cover
        raise AssertionError("executor must not run")
