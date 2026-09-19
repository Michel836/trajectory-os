"""M035 — deterministic production acceptance matrix.

This module runs the assembled mission operator system through a
machine-checkable acceptance matrix and returns one canonical report. It is
deterministic by construction: every case drives the real M031 orchestrator /
M033 recovery / M034 control code with bounded fixture executors, scripted
reviewers and an explicit clock, so the report is a pure function of those
inputs.

The matrix proves the release-hardening claims:

1. happy-path mission -> ``READY_FOR_COMMIT``;
2. validation reject;
3. review reject -> bounded repair -> fresh ``PASS``;
4. repair-budget exhaustion -> ``BLOCKED``;
5. invalid provider/model -> preflight ``BLOCKED`` before expensive phases;
6. process interruption -> resume -> same mission identity;
7. cancel -> canonical ``CANCELLED``;
8. stale review -> ``BLOCKED``;
9. ``COMPLETE`` + non-ready readiness never renders success;
10. closure/reconstruct works for terminal states;
11. status/follow/reconstruct remain read-only;
12. exact semantic patch identity remains authoritative;
13. reviewer roles remain correct; no phantom reviewer;
14. repeated resume after completion is idempotent.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trajectory_os.agents import model as agent_model
from trajectory_os.assembly import closure as assembly_closure
from trajectory_os.assembly import control as assembly_control
from trajectory_os.assembly import model, store
from trajectory_os.assembly import orchestrator as assembly_run
from trajectory_os.benchmark import patch as bench_patch
from trajectory_os.benchmark.executor import (
    ExecutionOutcome,
    ExecutionRequest,
    FixtureExecutor,
    TrialExecutor,
)
from trajectory_os.observability import follow as obs_follow
from trajectory_os.observability import model as obs_model
from trajectory_os.observability import projection
from trajectory_os.observability import run as obs_run
from trajectory_os.observability import store as obs_store

#: Acceptance schema/report version.
ACCEPTANCE_VERSION = "m035.1"

PASS_REVIEW = (
    "VERDICT: PASS\nBLOCKERS:\n- none\nMAJORS:\n- none\nMINORS:\n- none\n"
    "FINAL RECOMMENDATION: GO COMMIT\n")
REJECT_REVIEW = (
    "VERDICT: REJECT\nBLOCKERS:\n- missing edge-case handling\n"
    "MAJORS:\n- none\nMINORS:\n- none\nFINAL RECOMMENDATION: REPAIR\n")


# --- deterministic executors (test doubles, never live measurements) ---------


class _CountingExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        self.calls += 1
        raise AssertionError("downstream execution must not run")


class _WrongSolutionExecutor:
    def __init__(self, inner: FixtureExecutor) -> None:
        self._inner = inner

    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        outcome = self._inner.execute(request)
        (Path(request.workspace) / "calc.py").write_text(
            "def add(a, b):\n    return a * b\n", encoding="utf-8")
        return outcome


class _ScriptedClock:
    """A monotonic, deterministic clock for reproducible reports."""

    def __init__(self, start: int = 0) -> None:
        self._tick = start

    def __call__(self) -> str:
        self._tick += 1
        return f"2026-09-20T00:00:{self._tick:02d}Z"


# --- report model -------------------------------------------------------------


@dataclass(frozen=True)
class AcceptanceCheck:
    case: str
    description: str
    ok: bool
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"case": self.case, "description": self.description,
                "ok": self.ok, "detail": dict(self.detail)}


@dataclass(frozen=True)
class AcceptanceCase:
    case: str
    title: str
    ok: bool
    checks: tuple[AcceptanceCheck, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "case": self.case,
            "title": self.title,
            "ok": self.ok,
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass(frozen=True)
class AcceptanceReport:
    version: str
    root: str
    generated_at: str
    cases: tuple[AcceptanceCase, ...]

    @property
    def status(self) -> str:
        return "PASS" if all(case.ok for case in self.cases) else "FAIL"

    @property
    def checks(self) -> tuple[AcceptanceCheck, ...]:
        return tuple(check for case in self.cases for check in case.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": f"trajectory-acceptance/{self.version}",
            "version": self.version,
            "generated_at": self.generated_at,
            "root": self.root,
            "status": self.status,
            "cases": [case.to_dict() for case in self.cases],
            "summary": {
                "cases": len(self.cases),
                "passed_cases": sum(1 for c in self.cases if c.ok),
                "checks": len(self.checks),
                "passed_checks": sum(1 for c in self.checks if c.ok),
            },
        }

    def render(self) -> str:
        lines = [
            "=" * 72,
            f"TRAJECTORY-OS PRODUCTION ACCEPTANCE ({self.version})",
            f"status : {self.status}",
            f"root   : {self.root}",
            f"at     : {self.generated_at}",
            "-" * 72,
        ]
        for case in self.cases:
            marker = "PASS" if case.ok else "FAIL"
            lines.append(f"[{marker}] {case.case}: {case.title}")
            for check in case.checks:
                lines.append(
                    f"    {'ok  ' if check.ok else 'FAIL'} {check.description}")
        lines.append("-" * 72)
        lines.append(
            f"{sum(1 for c in self.cases if c.ok)}/{len(self.cases)} cases, "
            f"{sum(1 for c in self.checks if c.ok)}/{len(self.checks)} checks")
        lines.append("=" * 72)
        return "\n".join(lines)


# --- helpers ------------------------------------------------------------------


def _workspace(root: str, mission_id: str) -> str:
    workspace = Path(root) / mission_id / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return str(workspace)


def _request(root: str, mission_id: str, **overrides: Any,
             ) -> assembly_run.MissionRequest:
    base: dict[str, Any] = {
        "objective": f"acceptance case {mission_id}: fix add(a, b)",
        "workspace": _workspace(root, mission_id),
        "mission_id": mission_id,
        "workload_id": "small-targeted-repair",
        "trust_policy": model.TrustPolicy(max_repairs=2),
    }
    base.update(overrides)
    return assembly_run.MissionRequest(**base)


def _orchestrator(root: str, executor: TrialExecutor,
                  reviews: Sequence[str],
                  clock: _ScriptedClock) -> assembly_run.MissionOrchestrator:
    return assembly_run.MissionOrchestrator(
        root, executor=executor,
        reviewer_factory=obs_run.scripted_reviewer_factory(reviews),
        clock=clock)


def _digest_root(mission_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(mission_root.rglob("*")):
        if not path.is_file():
            continue
        digest.update(str(path.relative_to(mission_root)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _check(checks: list[AcceptanceCheck], case: str, description: str,
           ok: bool, **detail: Any) -> None:
    checks.append(AcceptanceCheck(case=case, description=description,
                                  ok=bool(ok), detail=detail))


# --- cases --------------------------------------------------------------------


def _case_happy_path(root: str, checks: list[AcceptanceCheck]) -> str:
    clock = _ScriptedClock()
    mission_id = "m035-happy"
    orchestrator = _orchestrator(
        root, FixtureExecutor(interrupt_once=False), [PASS_REVIEW], clock)
    result = orchestrator.start(_request(root, mission_id))
    mission_root = store.mission_root(root, mission_id)
    artifacts = all((mission_root / name).is_file() for name in (
        store.MISSION_NAME, store.PLAN_NAME, obs_store.EVENTS_NAME,
        obs_store.STATUS_NAME, obs_store.TELEMETRY_NAME,
        obs_store.SUMMARY_NAME, store.CLOSURE_NAME))
    _check(checks, "1", "mission root persists all canonical artifacts",
           artifacts)
    _check(checks, "1", "happy path reaches READY_FOR_COMMIT",
           result.status["readiness"] == obs_model.RD_READY_FOR_COMMIT
           and result.status["state"] == obs_model.LC_COMPLETE)
    _check(checks, "1",
           "reviewed_patch == current_patch and final reviewer active",
           result.status["reviewed_patch"]
           and result.status["reviewed_patch"] == result.status["current_patch"]
           and result.status["final_reviewer"]["active"] is True)
    return mission_id


def _case_validation_reject(root: str, checks: list[AcceptanceCheck]) -> None:
    clock = _ScriptedClock()
    executor = _WrongSolutionExecutor(FixtureExecutor(interrupt_once=False))
    orchestrator = _orchestrator(root, executor, [PASS_REVIEW], clock)
    result = orchestrator.start(_request(root, "m035-valfail"))
    _check(checks, "2", "validation reject cannot become READY_FOR_COMMIT",
           result.status["readiness"] == obs_model.RD_FAILED
           and result.status["previous_gate"] == obs_model.GATE_VALIDATION)
    _check(checks, "2", "no independent review runs after a validation reject",
           result.closure is not None
           and result.closure.review_results == ())


def _case_review_repair(root: str, checks: list[AcceptanceCheck]) -> None:
    clock = _ScriptedClock()
    orchestrator = _orchestrator(
        root, FixtureExecutor(interrupt_once=False),
        [REJECT_REVIEW, PASS_REVIEW], clock)
    result = orchestrator.start(_request(root, "m035-repair"))
    _check(checks, "3", "review reject triggers a bounded repair",
           result.closure is not None and result.closure.attempts == 2
           and result.closure.repairs == 1)
    _check(checks, "3", "fresh PASS on the exact current patch",
           result.status["readiness"] == obs_model.RD_READY_FOR_COMMIT
           and result.status["reviewed_patch"] == result.status["current_patch"]
           and [entry["result"]
                for entry in (result.closure.review_results
                              if result.closure else ())]
           == [obs_model.RESULT_REJECT, obs_model.RESULT_PASS])


def _case_repair_budget(root: str, checks: list[AcceptanceCheck]) -> None:
    clock = _ScriptedClock()
    orchestrator = _orchestrator(
        root, FixtureExecutor(interrupt_once=False), [REJECT_REVIEW], clock)
    result = orchestrator.start(_request(
        root, "m035-budget",
        trust_policy=model.TrustPolicy(max_repairs=0)))
    _check(checks, "4", "repair-budget exhaustion yields BLOCKED",
           result.status["readiness"] == obs_model.RD_BLOCKED
           and result.status["state"] == obs_model.LC_COMPLETE
           and result.closure is not None
           and result.closure.repairs == 0)


def _case_preflight(root: str, checks: list[AcceptanceCheck]) -> None:
    clock = _ScriptedClock()
    executor = _CountingExecutor()
    orchestrator = assembly_run.MissionOrchestrator(
        root, executor=executor, clock=clock)
    result = orchestrator.start(_request(
        root, "m035-preflight", provider="ollama", model="deepseek-flash"))
    mission_root = store.mission_root(root, "m035-preflight")
    events = [event["kind"]
              for event in obs_store.load_events(mission_root)]
    _check(checks, "5", "invalid provider/model preflight BLOCKED",
           result.status["readiness"] == obs_model.RD_BLOCKED
           and result.status["terminal_reason_code"]
           == obs_model.R_INVALID_MODEL_PROVIDER)
    _check(checks, "5", "no expensive phase runs after preflight reject",
           executor.calls == 0
           and not store.plan_exists(root, "m035-preflight")
           and "PLAN_PERSISTED" not in events)


def _case_interruption_resume(root: str,
                              checks: list[AcceptanceCheck]) -> None:
    clock = _ScriptedClock()
    orchestrator = _orchestrator(
        root, FixtureExecutor(interrupt_once=False), [PASS_REVIEW], clock)
    mission_id = "m035-resume"
    first = orchestrator.start(
        _request(root, mission_id),
        interrupt_after_phase=model.MP_EXECUTION)
    mission_root = store.mission_root(root, mission_id)
    events_before = obs_store.load_events(mission_root)
    resumed = orchestrator.resume(mission_id)
    events_after = obs_store.load_events(mission_root)
    _check(checks, "6", "interruption is non-terminal and preserves identity",
           first.interrupted is True and first.closure is None
           and first.mission.mission_id == mission_id)
    _check(checks, "6",
           "resume keeps mission_id and append-only prior evidence",
           resumed.mission.mission_id == mission_id
           and events_after[:len(events_before)] == events_before
           and resumed.status["readiness"] == obs_model.RD_READY_FOR_COMMIT)
    _check(checks, "6",
           "interruption is recorded with a durable non-terminal closure",
           any(event["kind"] == "MISSION_INTERRUPTED"
               for event in events_before)
           and first.closure is None)


def _case_cancel(root: str, checks: list[AcceptanceCheck]) -> None:
    clock = _ScriptedClock()
    orchestrator = _orchestrator(
        root, FixtureExecutor(interrupt_once=False), [PASS_REVIEW], clock)
    mission_id = "m035-cancel"
    orchestrator.start(_request(root, mission_id),
                       interrupt_after_phase=model.MP_PLAN)
    outcome = assembly_control.cancel(root, mission_id, clock=clock)
    status = obs_store.load_status(store.mission_root(root, mission_id))
    closure = store.load_closure(root, mission_id)
    _check(checks, "7", "cancel produces canonical CANCELLED",
           outcome.result == assembly_control.RESULT_ACCEPTED
           and status["readiness"] == obs_model.RD_CANCELLED
           and status["state"] == obs_model.LC_COMPLETE)
    _check(checks, "7", "cancel preserves evidence and closure",
           closure.readiness == obs_model.RD_CANCELLED
           and (store.mission_root(root, mission_id)
                / obs_store.EVENTS_NAME).is_file())
    again = assembly_control.cancel(root, mission_id, clock=clock)
    _check(checks, "7", "repeated cancel is idempotent",
           again.result == assembly_control.RESULT_ALREADY_CANCELLED)


def _case_stale_review(root: str, checks: list[AcceptanceCheck]) -> None:
    clock = _ScriptedClock()
    orchestrator = _orchestrator(
        root, FixtureExecutor(interrupt_once=False), [PASS_REVIEW], clock)
    mission_id = "m035-stale"
    orchestrator.start(_request(root, mission_id))
    mission_root = store.mission_root(root, mission_id)
    mission = store.load_mission(root, mission_id)
    plan = store.load_plan(root, mission_id)
    stale = obs_model.CanonicalStatus.build(
        run_id=mission_id, state=obs_model.LC_COMPLETE,
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
    blocked, gate = orchestrator._human_gate(  # noqa: SLF001 - proof
        mission_root, mission, plan, stale)
    _check(checks, "8", "a stale reviewed patch is blocked",
           gate == "blocked"
           and blocked.readiness == obs_model.RD_BLOCKED
           and blocked.terminal_reason_code
           == model.R_HUMAN_GATE_VIOLATION)


def _case_complete_not_ready(root: str,
                             checks: list[AcceptanceCheck]) -> None:
    blocked = obs_model.CanonicalStatus.build(
        run_id="m035-blocked", state=obs_model.LC_COMPLETE,
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
    web = projection.render_projection(blocked,
                                       target=projection.PROJECTION_WEB)
    cli = projection.render_projection(blocked,
                                       target=projection.PROJECTION_CLI)
    _check(checks, "9", "COMPLETE + non-ready never renders as success",
           blocked["state"] == obs_model.LC_COMPLETE
           and blocked["success"] is False
           and 'data-ready-for-commit="false"' in web
           and "READY_FOR_COMMIT —" not in cli)


def _case_reconstruct(root: str, checks: list[AcceptanceCheck]) -> None:
    document = assembly_closure.reconstruct_mission(root, "m035-happy")
    closure = document["closure"]
    _check(checks, "10", "closure reconstructs the READY mission",
           document["mission"]["mission_id"] == "m035-happy"
           and closure["readiness"] == obs_model.RD_READY_FOR_COMMIT
           and closure["artifacts"][store.CLOSURE_NAME]
           and closure["steps_executed"][0] == "intake"
           and closure["steps_executed"][-1] == "closure")
    cancelled = assembly_closure.reconstruct_mission(root, "m035-cancel")
    _check(checks, "10", "closure reconstructs a CANCELLED terminal state",
           cancelled["closure"]["readiness"] == obs_model.RD_CANCELLED
           and cancelled["status"]["readiness"] == obs_model.RD_CANCELLED)


def _case_read_only(root: str, checks: list[AcceptanceCheck]) -> None:
    mission_id = "m035-happy"
    mission_root = store.mission_root(root, mission_id)
    before = _digest_root(mission_root)
    # status
    status = obs_store.load_status(mission_root)
    # follow (terminal => a single read)
    outcome = obs_follow.follow(
        lambda: obs_store.load_status(mission_root),
        interval_s=0.0, max_polls=1, notifier=None)
    # reconstruct
    document = assembly_closure.reconstruct_mission(root, mission_id)
    after = _digest_root(mission_root)
    _check(checks, "11", "status/follow/reconstruct are read-only",
           before == after)
    _check(checks, "11", "follow exits at the terminal outcome",
           outcome.exited is True
           and status["readiness"] == obs_model.RD_READY_FOR_COMMIT
           and document["closure"]["mission_id"] == mission_id)


def _case_patch_identity(root: str, checks: list[AcceptanceCheck]) -> None:
    mission_id = "m035-happy"
    mission = store.load_mission(root, mission_id)
    closure = store.load_closure(root, mission_id)
    # The mission workspace starts empty and the fixture executor adds the
    # corrected calc.py, so the exact semantic patch identity is reproducible
    # from the durable workspace alone (no Git, no guessed fact).
    baseline = bench_patch.WorkspaceSnapshot(files={})
    current = bench_patch.capture(mission.workspace)
    identity = bench_patch.build_identity(baseline, current)
    status = obs_store.load_status(store.mission_root(root, mission_id))
    _check(checks, "12", "current patch is an exact 64-hex semantic identity",
           isinstance(status["current_patch"], str)
           and len(status["current_patch"]) == 64
           and all(ch in "0123456789abcdef"
                   for ch in status["current_patch"]))
    _check(checks, "12", "reviewed_patch equals the authoritative current patch",
           status["reviewed_patch"] == status["current_patch"]
           and closure.current_patch == status["current_patch"])
    _check(checks, "12", "recomputed semantic patch matches the durable identity",
           identity.available
           and identity.sha256 == status["current_patch"])
    _check(checks, "12", "closure records validation on the same patch",
           all(entry["patch"] == status["current_patch"]
               for entry in closure.validation_results))


def _case_reviewer_roles(root: str, checks: list[AcceptanceCheck]) -> None:
    status = obs_store.load_status(store.mission_root(root, "m035-happy"))
    final = status["final_reviewer"]
    inline = status["inline_reviewer"]
    rendered = json.dumps(status, sort_keys=True)
    _check(checks, "13", "final independent reviewer is active and explicit",
           final["active"] is True and final["enabled"] is True
           and final["display_model"] == obs_model.FINAL_REVIEWER_MODEL)
    _check(checks, "13", "no phantom/default reviewer leaks into the status",
           inline["active"] is False and inline["display_model"] is None
           and obs_model.LEGACY_PHANTOM_REVIEWER not in rendered)


def _case_repeated_resume(root: str, checks: list[AcceptanceCheck]) -> None:
    clock = _ScriptedClock()
    orchestrator = _orchestrator(
        root, FixtureExecutor(interrupt_once=False), [PASS_REVIEW], clock)
    mission_id = "m035-resume"
    mission_root = store.mission_root(root, mission_id)
    events_before = obs_store.load_events(mission_root)
    digests_before = _digest_root(mission_root)
    again = orchestrator.resume(mission_id)
    events_after = obs_store.load_events(mission_root)
    _check(checks, "14", "repeated resume after completion is idempotent",
           again.closure is not None
           and events_after == events_before
           and _digest_root(mission_root) == digests_before)


# --- runner -------------------------------------------------------------------


_CASES = (
    ("1", "Real happy-path mission -> READY_FOR_COMMIT"),
    ("2", "Validation reject"),
    ("3", "Review reject -> bounded repair -> fresh PASS"),
    ("4", "Repair-budget exhaustion -> BLOCKED"),
    ("5", "Invalid provider/model -> preflight BLOCKED"),
    ("6", "Process interruption -> resume -> same identity"),
    ("7", "Cancel -> CANCELLED"),
    ("8", "Stale review -> BLOCKED"),
    ("9", "COMPLETE + non-ready never renders success"),
    ("10", "Closure/reconstruct for terminal states"),
    ("11", "Status/follow/reconstruct remain read-only"),
    ("12", "Semantic patch identity remains authoritative"),
    ("13", "Reviewer roles correct; no phantom reviewer"),
    ("14", "Repeated resume after completion is idempotent"),
)


def run_acceptance(root: str | Path, *,
                   generated_at: str | None = None) -> AcceptanceReport:
    """Run the deterministic acceptance matrix under ``root``.

    Cases are intentionally ordered: cases 10–14 inspect missions created by
    earlier cases (``m035-happy``, ``m035-cancel``, ``m035-resume``), so the
    order is part of the acceptance contract and must not be reordered.
    """
    root_str = str(root)
    Path(root_str).mkdir(parents=True, exist_ok=True)
    checks: list[AcceptanceCheck] = []
    _case_happy_path(root_str, checks)
    _case_validation_reject(root_str, checks)
    _case_review_repair(root_str, checks)
    _case_repair_budget(root_str, checks)
    _case_preflight(root_str, checks)
    _case_interruption_resume(root_str, checks)
    _case_cancel(root_str, checks)
    _case_stale_review(root_str, checks)
    _case_complete_not_ready(root_str, checks)
    _case_reconstruct(root_str, checks)
    _case_read_only(root_str, checks)
    _case_patch_identity(root_str, checks)
    _case_reviewer_roles(root_str, checks)
    _case_repeated_resume(root_str, checks)
    cases = tuple(
        AcceptanceCase(
            case=code, title=title,
            ok=all(check.ok for check in checks if check.case == code),
            checks=tuple(check for check in checks if check.case == code))
        for code, title in _CASES)
    return AcceptanceReport(
        version=ACCEPTANCE_VERSION, root=root_str,
        generated_at=generated_at or model.utc_now(), cases=cases)


__all__ = [
    "ACCEPTANCE_VERSION",
    "AcceptanceCase",
    "AcceptanceCheck",
    "AcceptanceReport",
    "run_acceptance",
]
