"""M031 integration — end-to-end mission assembly and operator flow.

Drives the production assembly orchestrator and CLI with deterministic
fixtures (no network, no Git trust-boundary write) and verifies the canonical
mission root, the human gate, interruption/resume identity preservation and
every negative scenario failing closed.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from trajectory_os.agents import model as agent_model
from trajectory_os.assembly import cli as assembly_cli
from trajectory_os.assembly import closure as assembly_closure
from trajectory_os.assembly import model, store
from trajectory_os.assembly import orchestrator as assembly_run
from trajectory_os.benchmark.executor import (
    ExecutionOutcome,
    ExecutionRequest,
    FixtureExecutor,
    TrialExecutor,
)
from trajectory_os.observability import model as obs_model
from trajectory_os.observability import projection
from trajectory_os.observability import run as obs_run
from trajectory_os.observability import store as obs_store

PASS_REVIEW = (
    "VERDICT: PASS\nBLOCKERS:\n- none\nMAJORS:\n- none\nMINORS:\n- none\n"
    "FINAL RECOMMENDATION: GO COMMIT\n")
REJECT_REVIEW = (
    "VERDICT: REJECT\nBLOCKERS:\n- missing edge-case handling\n"
    "MAJORS:\n- none\nMINORS:\n- none\nFINAL RECOMMENDATION: REPAIR\n")

_ARTIFACTS = (
    store.MISSION_NAME, store.PLAN_NAME, obs_store.EVENTS_NAME,
    obs_store.STATUS_NAME, obs_store.TELEMETRY_NAME, obs_store.SUMMARY_NAME,
    store.CLOSURE_NAME,
)


class _CountingExecutor:
    """A trial executor that must never be called (preflight fail-closed)."""

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        self.calls += 1
        raise AssertionError("downstream execution must not run")


class _WrongSolutionExecutor:
    """Wraps the fixture executor but corrupts the produced patch."""

    def __init__(self, inner: FixtureExecutor) -> None:
        self._inner = inner

    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        outcome = self._inner.execute(request)
        (Path(request.workspace) / "calc.py").write_text(
            "def add(a, b):\n    return a * b\n", encoding="utf-8")
        return outcome


def _workspace(root: Path, mission_id: str) -> str:
    workspace = root / mission_id / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return str(workspace)


def _request(root: Path, mission_id: str, **overrides: object,
             ) -> assembly_run.MissionRequest:
    data: dict[str, object] = {
        "objective": "fix add(a, b) so it adds",
        "workspace": _workspace(root, mission_id),
        "mission_id": mission_id,
        "workload_id": "small-targeted-repair",
        "trust_policy": model.TrustPolicy(max_repairs=2),
    }
    data.update(overrides)
    return assembly_run.MissionRequest(**data)


def _orchestrator(root: Path, executor: TrialExecutor,
                  reviews: list[str]) -> assembly_run.MissionOrchestrator:
    return assembly_run.MissionOrchestrator(
        root, executor=executor,
        reviewer_factory=obs_run.scripted_reviewer_factory(reviews))


def test_happy_path_reaches_ready_for_commit_with_repair(
    tmp_path: Path,
) -> None:
    orchestrator = _orchestrator(
        tmp_path, FixtureExecutor(interrupt_once=False),
        [REJECT_REVIEW, PASS_REVIEW])
    result = orchestrator.start(_request(tmp_path, "m031-happy"))
    mission_root = store.mission_root(tmp_path, "m031-happy")
    for name in _ARTIFACTS:
        assert (mission_root / name).is_file(), name
    assert result.status["state"] == obs_model.LC_COMPLETE
    assert result.status["readiness"] == obs_model.RD_READY_FOR_COMMIT
    assert result.status["reviewed_patch"] == result.status["current_patch"]
    assert result.status["final_reviewer"]["active"] is True
    assert result.closure is not None
    assert result.closure.attempts == 2
    assert result.closure.repairs == 1
    assert [entry["result"] for entry in result.closure.review_results] == [
        obs_model.RESULT_REJECT, obs_model.RESULT_PASS]
    assert [entry["result"] for entry in result.closure.validation_results] == [
        obs_model.RESULT_PASS, obs_model.RESULT_PASS]
    assert all(entry["command"] for entry in
               result.closure.validation_results)
    assert all(entry["patch"] for entry in
               result.closure.validation_results)
    assert result.closure.steps_executed == (
        "intake", "preflight", "plan", "implement", "validate", "review",
        "repair", "human_gate", "closure")
    assert result.closure.artifacts[store.MISSION_NAME] == str(
        mission_root / store.MISSION_NAME)


def test_closure_reconstructs_mission_without_prose_logs(
    tmp_path: Path,
) -> None:
    orchestrator = _orchestrator(
        tmp_path, FixtureExecutor(interrupt_once=False), [PASS_REVIEW])
    orchestrator.start(_request(tmp_path, "m031-recon"))
    document = assembly_closure.reconstruct_mission(tmp_path, "m031-recon")
    assert document["mission"]["mission_id"] == "m031-recon"
    assert document["mission"]["objective"]
    assert document["mission"]["definition_of_done"]
    assert document["mission"]["baseline"]["workspace_digest"]
    assert document["plan"]["steps"]
    assert document["closure"]["readiness"] == obs_model.RD_READY_FOR_COMMIT
    assert document["status"]["readiness"] == obs_model.RD_READY_FOR_COMMIT
    assert document["counts"]["events"] > 0
    assert document["closure"]["next_action"]


def test_preflight_rejection_stops_before_expensive_gates(
    tmp_path: Path,
) -> None:
    executor = _CountingExecutor()
    orchestrator = assembly_run.MissionOrchestrator(
        tmp_path, executor=executor)
    result = orchestrator.start(_request(
        tmp_path, "m031-pf", provider="ollama", model="deepseek-flash"))
    assert executor.calls == 0
    assert result.status["readiness"] == obs_model.RD_BLOCKED
    assert result.status["terminal_reason_code"] == (
        obs_model.R_INVALID_MODEL_PROVIDER)
    assert not store.plan_exists(tmp_path, "m031-pf")
    mission_root = store.mission_root(tmp_path, "m031-pf")
    events = obs_store.load_events(mission_root)
    assert [event["kind"] for event in events] == [
        "MISSION_CREATED", "PREFLIGHT_COMPLETED", "MISSION_CLOSED"]
    assert result.closure is not None
    assert result.closure.readiness == obs_model.RD_BLOCKED


def test_validation_failure_cannot_become_ready_for_commit(
    tmp_path: Path,
) -> None:
    executor = _WrongSolutionExecutor(FixtureExecutor(interrupt_once=False))
    orchestrator = _orchestrator(
        tmp_path, executor, [PASS_REVIEW])
    result = orchestrator.start(_request(tmp_path, "m031-valfail"))
    assert result.status["readiness"] == obs_model.RD_FAILED
    assert result.status["readiness"] != obs_model.RD_READY_FOR_COMMIT
    assert result.status["previous_gate"] == obs_model.GATE_VALIDATION
    assert result.closure is not None
    assert result.closure.review_results == ()


def test_repair_budget_exhaustion_yields_blocked(tmp_path: Path) -> None:
    orchestrator = _orchestrator(
        tmp_path, FixtureExecutor(interrupt_once=False), [REJECT_REVIEW])
    result = orchestrator.start(_request(
        tmp_path, "m031-budget",
        trust_policy=model.TrustPolicy(max_repairs=0)))
    assert result.status["readiness"] == obs_model.RD_BLOCKED
    assert result.status["state"] == obs_model.LC_COMPLETE
    assert result.closure is not None
    assert result.closure.attempts == 1
    assert result.closure.repairs == 0


def test_stale_review_cannot_become_ready_for_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = obs_model.CanonicalStatus.build(
        run_id="m031-stale", state=obs_model.LC_COMPLETE,
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
        readiness=obs_model.RD_READY_FOR_COMMIT).to_dict()

    def fake_execute(self: object, mission_root: Path, mission: object,
                     plan: object) -> obs_run.RunResult:
        obs_store.write_json(mission_root / obs_store.STATUS_NAME, stale)
        return obs_run.RunResult(
            status=assembly_run._status_from_document(stale), attempts=1,
            events=0, preflight=None, telemetry=None)

    monkeypatch.setattr(
        assembly_run.MissionOrchestrator, "_execute", fake_execute)
    orchestrator = _orchestrator(
        tmp_path, FixtureExecutor(interrupt_once=False), [PASS_REVIEW])
    result = orchestrator.start(_request(tmp_path, "m031-stale"))
    assert result.status["readiness"] == obs_model.RD_BLOCKED
    assert result.status["terminal_reason_code"] == (
        model.R_HUMAN_GATE_VIOLATION)


def test_complete_non_ready_never_renders_as_success(tmp_path: Path) -> None:
    blocked = obs_model.CanonicalStatus.build(
        run_id="m031-blocked", state=obs_model.LC_COMPLETE,
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
        previous_result=obs_model.RESULT_REJECT,
        reviewed_patch="a" * 64, current_patch="a" * 64,
        next_action="operator: resolve blocking findings",
        last_meaningful_event_at=None, heartbeat_at=None,
        terminal_reason="blocking review findings",
        readiness=obs_model.RD_BLOCKED).to_dict()
    web = projection.render_projection(
        blocked, target=projection.PROJECTION_WEB)
    assert blocked["state"] == obs_model.LC_COMPLETE
    assert blocked["success"] is False
    assert 'data-ready-for-commit="false"' in web
    assert "READY_FOR_COMMIT —" not in web


def test_interruption_resume_preserves_identity_and_evidence(
    tmp_path: Path,
) -> None:
    orchestrator = _orchestrator(
        tmp_path, FixtureExecutor(interrupt_once=False), [PASS_REVIEW])
    first = orchestrator.start(
        _request(tmp_path, "m031-resume"),
        interrupt_after_phase=model.MP_EXECUTION)
    mission_root = store.mission_root(tmp_path, "m031-resume")
    assert first.interrupted is True
    assert first.closure is None
    events_before = obs_store.load_events(mission_root)
    resumed = orchestrator.resume("m031-resume")
    events_after = obs_store.load_events(mission_root)
    assert resumed.mission.mission_id == first.mission.mission_id == (
        "m031-resume")
    assert events_after[:len(events_before)] == events_before
    assert resumed.closure is not None
    assert resumed.status["readiness"] == obs_model.RD_READY_FOR_COMMIT
    # A second resume is idempotent and keeps the same closure.
    again = orchestrator.resume("m031-resume")
    assert again.closure is not None
    assert again.closure.mission_id == "m031-resume"


def _capture(argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = assembly_cli.main(argv)
    return code, buffer.getvalue()


def test_operator_cli_start_status_reconstruct_closure(
    tmp_path: Path,
) -> None:
    root = str(tmp_path / "state")
    code, output = _capture([
        "start", "--root", root, "--mission-id", "m031-cli",
        "--objective", "fix add", "--workspace",
        str(tmp_path / "ws"), "--reviewer", "reject-then-pass", "--json"])
    assert code == assembly_cli.EXIT_OK
    document = json.loads(output)
    assert document["status"]["readiness"] == obs_model.RD_READY_FOR_COMMIT
    code, status_output = _capture([
        "status", "--root", root, "--mission-id", "m031-cli", "--json"])
    assert code == assembly_cli.EXIT_OK
    assert json.loads(status_output)["ready_for_commit"] is True
    code, reconstruct = _capture([
        "reconstruct", "--root", root, "--mission-id", "m031-cli"])
    assert code == assembly_cli.EXIT_OK
    assert json.loads(reconstruct)["closure"]["mission_id"] == "m031-cli"
    code, closure = _capture([
        "closure", "--root", root, "--mission-id", "m031-cli"])
    assert code == assembly_cli.EXIT_OK
    assert json.loads(closure)["mission_id"] == "m031-cli"
    code, follow = _capture([
        "follow", "--root", root, "--mission-id", "m031-cli",
        "--interval", "0.0", "--max-polls", "1"])
    assert code == assembly_cli.EXIT_OK
    assert "MISSION TERMINÉE" in follow


def test_operator_cli_blocked_returns_rejected(tmp_path: Path) -> None:
    root = str(tmp_path / "state")
    code, output = _capture([
        "start", "--root", root, "--mission-id", "m031-cli-blocked",
        "--objective", "fix add", "--workspace", str(tmp_path / "ws2"),
        "--provider", "ollama", "--model", "deepseek-flash", "--json"])
    assert code == assembly_cli.EXIT_REJECTED
    document = json.loads(output)
    assert document["status"]["readiness"] == obs_model.RD_BLOCKED
