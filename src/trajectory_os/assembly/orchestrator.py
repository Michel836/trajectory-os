"""M031 — end-to-end mission assembly orchestrator (trust-gated, no Git write).

The orchestrator assembles the existing M023–M030 components into the one
canonical mission flow::

    Mission -> Preflight -> Plan -> Execution -> Validation
            -> Review -> Repair -> Human Gate -> Closure

It owns *mission-level* composition only:

* ``mission.json`` / ``plan.json`` / ``closure.json`` are the durable mission
  documents (see :mod:`trajectory_os.assembly.store`);
* execution/validation/review/repair is delegated unchanged to the M030
  :class:`~trajectory_os.observability.run.RunCoordinator`, which owns
  ``events.jsonl`` / ``status.json`` / ``telemetry.json`` / ``summary.json``;
* ``status.json`` remains the single canonical status truth — this module
  never creates a parallel state model.

Trust constraints:

* preflight runs first; a rejection stops before planning or any execution
  (no downstream expensive gate is invoked);
* automation stops at ``READY_FOR_COMMIT`` and never commits/pushes/merges;
* the human gate is defended in depth: even if a lower layer claimed
  readiness, the mission blocks a stale review or an inactive reviewer;
* interruption leaves the durable evidence in place and keeps the identical
  ``mission_id`` for a later resume.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from trajectory_os.agents import model as agent_model
from trajectory_os.assembly import closure as assembly_closure
from trajectory_os.assembly import control as assembly_control
from trajectory_os.assembly import model, recovery, store
from trajectory_os.assembly.baseline import capture_baseline
from trajectory_os.benchmark import model as bench_model
from trajectory_os.benchmark.executor import TrialExecutor
from trajectory_os.benchmark.review import ReviewerClient
from trajectory_os.observability import model as obs_model
from trajectory_os.observability import preflight as obs_preflight
from trajectory_os.observability import run as obs_run
from trajectory_os.observability import store as obs_store

Clock = Callable[[], str]

DEFAULT_DEFINITION_OF_DONE = (
    "the deterministic validation gate passes on the exact current patch",
    "an independent final review PASS is recorded on the exact current patch",
    "automation stops at READY_FOR_COMMIT without a Git trust-boundary write",
)


@dataclass(frozen=True)
class MissionRequest:
    """One operator objective and its bounded mission intent."""

    objective: str
    workspace: str
    constraints: tuple[str, ...] = ()
    definition_of_done: tuple[str, ...] = DEFAULT_DEFINITION_OF_DONE
    trust_policy: model.TrustPolicy = field(default_factory=model.TrustPolicy)
    backend: str = agent_model.BACKEND_PI
    provider: str | None = "deepseek"
    model: str | None = "deepseek-flash"
    workload_id: str = "small-targeted-repair"
    workload: bench_model.WorkloadSpec | None = None
    mission_id: str | None = None
    mode: str = bench_model.MODE_FIXTURE
    telemetry_mode: str = obs_model.TELEMETRY_STANDARD
    timeout_s: int = 600


@dataclass(frozen=True)
class MissionResult:
    """The bounded result of one mission phase run."""

    mission: model.MissionDefinition
    plan: model.MissionPlan | None
    closure: model.MissionClosure | None
    status: Mapping[str, Any]
    resumed: bool
    interrupted: bool = False
    interrupt_phase: str | None = None
    recovery: recovery.ResumeDecision | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission": self.mission.to_dict(),
            "plan": (None if self.plan is None else self.plan.to_dict()),
            "closure": (None if self.closure is None
                        else self.closure.to_dict()),
            "status": dict(self.status),
            "resumed": self.resumed,
            "interrupted": self.interrupted,
            "interrupt_phase": self.interrupt_phase,
            "recovery": (None if self.recovery is None
                         else self.recovery.to_dict()),
        }


class MissionOrchestrator:
    """Assemble and run one trust-gated mission (mission_id == run_id)."""

    def __init__(
        self,
        root: str | Path,
        *,
        executor: TrialExecutor,
        reviewer_factory: Callable[[], ReviewerClient] | None = None,
        clock: Clock = model.utc_now,
        resource_sampler: Callable[[str], Any] | None = None,
    ) -> None:
        self._root = Path(root)
        self._executor = executor
        self._reviewer_factory = reviewer_factory
        self._clock = clock
        self._resource_sampler = resource_sampler

    @property
    def root(self) -> str:
        return str(self._root)

    # -- public ----------------------------------------------------------------

    def start(self, request: MissionRequest, *,
              interrupt_after_phase: str | None = None) -> MissionResult:
        """Create a mission from one objective and drive it to a gate."""
        created_at = self._clock()
        mission_id = request.mission_id or model.generate_mission_id(
            request.objective, created_at)
        if store.mission_exists(self._root, mission_id):
            raise model.AssemblyError(model.R_MISSION_EXISTS, mission_id)
        mission_root = store.ensure_mission_root(self._root, mission_id)
        baseline = capture_baseline(request.workspace, clock=self._clock)
        workload_id = (request.workload.workload_id
                       if request.workload is not None
                       else request.workload_id)
        workload_document = (request.workload.validate().to_dict()
                             if request.workload is not None else None)
        mission = model.MissionDefinition.build(
            mission_id=mission_id,
            objective=request.objective,
            constraints=tuple(request.constraints),
            definition_of_done=tuple(request.definition_of_done),
            backend=request.backend,
            provider=request.provider,
            model=request.model,
            trust_policy=request.trust_policy.validate(),
            baseline=baseline,
            workspace=request.workspace,
            workload_id=workload_id,
            workload=workload_document,
            mode=request.mode,
            telemetry_mode=request.telemetry_mode,
            created_at=created_at,
            timeout_s=request.timeout_s,
        )
        store.write_mission(self._root, mission)
        self._emit(
            mission_root, mission_id, kind="MISSION_CREATED",
            stage=obs_model.STAGE_PREFLIGHT, phase=model.MP_INTAKE,
            detail={"objective": mission.objective,
                    "backend": mission.backend,
                    "provider": mission.provider,
                    "model": mission.model,
                    "baseline_revision": mission.baseline.revision})
        self._write_phase_status(
            mission_root, mission, current="MISSION_CREATED",
            next_label="PREFLIGHT", state=obs_model.LC_PENDING,
            stage=obs_model.STAGE_PREFLIGHT, phase=model.MP_INTAKE,
            readiness=obs_model.RD_INDETERMINATE)
        return self._run_phases(
            mission_root, mission, resumed=False,
            interrupt_after_phase=interrupt_after_phase,
            resume_point=model.MP_INTAKE)

    def resume(self, mission_id: str, *,
               interrupt_after_phase: str | None = None) -> MissionResult:
        """Resume an existing mission, preserving identity and evidence."""
        if not store.mission_exists(self._root, mission_id):
            raise model.AssemblyError(model.R_MISSION_MISSING, mission_id)
        mission = store.load_mission(self._root, mission_id)
        decision = recovery.detect_resume(self._root, mission_id)
        recovery.write_recovery_record(self._root, decision)
        mission_root = store.mission_root(self._root, mission_id)

        # Consume any pending graceful stop request exactly once, before any
        # resume path proceeds (a later resume must not be immediately
        # re-interrupted by a stale latch).
        if assembly_control.stop_requested(self._root, mission_id):
            assembly_control.clear_stop_request(self._root, mission_id,
                                                clock=self._clock)

        # Idempotent terminal resume: an existing durable closure is the
        # finalized trust decision and is never re-executed or re-closed.
        if decision.kind == recovery.RC_TERMINAL_COMPLETE:
            closure = store.load_closure(self._root, mission_id)
            plan = (store.load_plan(self._root, mission_id)
                    if store.plan_exists(self._root, mission_id) else None)
            status_document = recovery.load_identity_status(mission_root,
                                                            mission_id)
            return MissionResult(
                mission=mission, plan=plan, closure=closure,
                status=(status_document if status_document is not None
                        else obs_store.load_status(mission_root)),
                resumed=True, recovery=decision)

        # A finalized execution is reused as-is; only the human gate and
        # closure remain (never a second execution cycle).
        if decision.kind == recovery.RC_EXECUTION_FINALIZED:
            plan = (store.load_plan(self._root, mission_id)
                    if store.plan_exists(self._root, mission_id)
                    else model.build_plan(mission, created_at=self._clock()))
            status = _status_from_document(
                obs_store.load_status(mission_root))
            return self._close(mission_root, mission, plan, status, resumed=True,
                               decision=decision)

        self._emit(
            mission_root, mission_id, kind="MISSION_RESUMED",
            stage=obs_model.STAGE_PREFLIGHT, phase=model.MP_INTAKE,
            detail={"reason": "operator resume",
                    "recovery_kind": decision.kind,
                    "resume_point": decision.resume_point})
        return self._run_phases(
            mission_root, mission, resumed=True,
            interrupt_after_phase=interrupt_after_phase,
            resume_point=decision.resume_point, decision=decision)

    # -- phases ----------------------------------------------------------------

    def _run_phases(self, mission_root: Path, mission: model.MissionDefinition,
                    *, resumed: bool,
                    interrupt_after_phase: str | None,
                    resume_point: str = model.MP_INTAKE,
                    decision: recovery.ResumeDecision | None = None,
                    ) -> MissionResult:
        mission_id = mission.mission_id
        # Resume after a finalized execution: reuse the authoritative status so
        # a resume never rewrites a terminal M030 decision with a mission-phase
        # placeholder status. Only the human gate + closure remain.
        if self._execution_finalized(obs_store.load_events(mission_root)):
            plan = (store.load_plan(self._root, mission_id)
                    if store.plan_exists(self._root, mission_id)
                    else model.build_plan(mission, created_at=self._clock()))
            status = _status_from_document(
                obs_store.load_status(mission_root))
            return self._close(mission_root, mission, plan, status, resumed,
                               decision=decision)

        # 1. Preflight — knowable errors stop before planning/execution.
        preflight_required = (
            not resumed
            or resume_point in (model.MP_INTAKE, model.MP_PREFLIGHT))
        if not preflight_required:
            # A safe mid-mission resume requires a durable, identity-consistent
            # status; otherwise fall back to re-running preflight.
            existing = recovery.load_identity_status(mission_root, mission_id)
            preflight_required = existing is None
        if preflight_required:
            self._write_phase_status(
                mission_root, mission, current="PREFLIGHT",
                next_label="PLAN", state=obs_model.LC_PREFLIGHT,
                stage=obs_model.STAGE_PREFLIGHT, phase=model.MP_PREFLIGHT,
                readiness=obs_model.RD_INDETERMINATE)
            outcome = obs_preflight.preflight(obs_preflight.PreflightRequest(
                run_id=mission_id, backend=mission.backend,
                provider=mission.provider, model=mission.model,
                workspace=mission.workspace,
                reviewer_model=mission.trust_policy.final_reviewer_model,
                review_enabled=mission.trust_policy.require_review,
                telemetry_mode=mission.telemetry_mode))
            self._emit(
                mission_root, mission_id, kind="PREFLIGHT_COMPLETED",
                stage=obs_model.STAGE_PREFLIGHT, phase=model.MP_PREFLIGHT,
                gate=obs_model.GATE_PREFLIGHT,
                result=(obs_model.RESULT_PASS if outcome.ok
                        else obs_model.RESULT_FAIL),
                detail={"reason": outcome.reason, "detail": outcome.detail,
                        "checks": [dict(check) for check in outcome.checks]})
            if not outcome.ok:
                return self._preflight_rejected(
                    mission_root, mission, resumed, outcome, decision)
            if self._stop_requested(mission_id):
                return self._interrupt(mission_root, mission, plan=None,
                                       resumed=resumed, phase=model.MP_PREFLIGHT,
                                       mission_id=mission_id, decision=decision)

        # 2. Plan — durable and bounded.
        plan_written = False
        if store.plan_exists(self._root, mission_id):
            plan = store.load_plan(self._root, mission_id)
        else:
            plan = model.build_plan(mission, created_at=self._clock())
            store.write_plan(self._root, plan)
            plan_written = True
        if plan_written:
            self._emit(
                mission_root, mission_id, kind="PLAN_PERSISTED",
                stage=obs_model.STAGE_PREFLIGHT, phase=model.MP_PLAN,
                detail={"steps": [step.step_id for step in plan.steps],
                        "retry_budget": plan.retry_budget,
                        "reviewer_role": plan.reviewer_role})
        self._write_phase_status(
            mission_root, mission, current="PLAN_PERSISTED",
            next_label="EXECUTE", state=obs_model.LC_PENDING,
            stage=obs_model.STAGE_PREFLIGHT, phase=model.MP_PLAN,
            readiness=obs_model.RD_INDETERMINATE)
        self._checkpoint(mission_root, mission_id)
        if self._stop_requested(mission_id):
            return self._interrupt(mission_root, mission, plan, resumed,
                                   model.MP_PLAN, mission_id=mission_id,
                                   decision=decision)
        if interrupt_after_phase == model.MP_PLAN:
            return self._interrupt(mission_root, mission, plan, resumed,
                                   model.MP_PLAN, decision=decision)

        # 3. Execution / validation / review / repair (M030, unchanged).
        run_result = self._execute(mission_root, mission, plan)
        status = run_result.status
        self._checkpoint(mission_root, mission_id)
        if self._stop_requested(mission_id):
            return self._interrupt(mission_root, mission, plan, resumed,
                                   model.MP_EXECUTION,
                                   status_document=status.to_dict(),
                                   mission_id=mission_id, decision=decision)
        if interrupt_after_phase == model.MP_EXECUTION:
            return self._interrupt(mission_root, mission, plan, resumed,
                                   model.MP_EXECUTION,
                                   status_document=status.to_dict(),
                                   decision=decision)

        # 4. Human gate — defended in depth, then closure evidence.
        return self._close(mission_root, mission, plan, status, resumed,
                           decision=decision)

    def _preflight_rejected(
        self, mission_root: Path, mission: model.MissionDefinition,
        resumed: bool, outcome: obs_model.PreflightResult,
        decision: recovery.ResumeDecision | None,
    ) -> MissionResult:
        status = self._write_phase_status(
            mission_root, mission, current="PREFLIGHT_REJECTED",
            next_label=None, state=obs_model.LC_COMPLETE,
            stage=obs_model.STAGE_DONE, phase=model.MP_PREFLIGHT,
            readiness=obs_model.RD_BLOCKED,
            previous_gate=obs_model.GATE_PREFLIGHT,
            previous_result=obs_model.RESULT_FAIL,
            terminal_reason=outcome.detail,
            terminal_reason_code=outcome.reason,
            next_action="operator: fix the invalid configuration and re-run")
        status_document = status.to_dict()
        self._emit(
            mission_root, mission.mission_id, kind="MISSION_CLOSED",
            stage=obs_model.STAGE_DONE, phase=model.MP_CLOSURE,
            result=obs_model.RESULT_FAIL,
            detail={"readiness": obs_model.RD_BLOCKED,
                    "reason": outcome.reason})
        closure = assembly_closure.build_closure(
            self._root, mission, plan=None, status=status_document,
            created_at=self._clock())
        store.write_closure(self._root, closure)
        self._checkpoint(mission_root, mission.mission_id)
        return MissionResult(
            mission=mission, plan=None, closure=closure,
            status=status_document, resumed=resumed, recovery=decision)

    def _close(self, mission_root: Path, mission: model.MissionDefinition,
               plan: model.MissionPlan, status: obs_model.CanonicalStatus,
               resumed: bool,
               decision: recovery.ResumeDecision | None = None,
               ) -> MissionResult:
        status, gate_result = self._human_gate(mission_root, mission, plan,
                                               status)
        self._emit(
            mission_root, mission.mission_id, kind="HUMAN_GATE_REACHED",
            stage=obs_model.STAGE_DONE, phase=model.MP_HUMAN_GATE,
            gate=obs_model.GATE_REVIEW,
            result=(obs_model.RESULT_PASS
                    if status.readiness == obs_model.RD_READY_FOR_COMMIT
                    else obs_model.RESULT_BLOCKED),
            detail={"readiness": status.readiness,
                    "gate": gate_result})
        status_document = status.to_dict()
        self._emit(
            mission_root, mission.mission_id, kind="MISSION_CLOSED",
            stage=obs_model.STAGE_DONE, phase=model.MP_CLOSURE,
            result=(obs_model.RESULT_PASS
                    if status.readiness == obs_model.RD_READY_FOR_COMMIT
                    else obs_model.RESULT_BLOCKED),
            detail={"readiness": status.readiness,
                    "terminal_reason": status.terminal_reason})
        closure = assembly_closure.build_closure(
            self._root, mission, plan=plan, status=status_document,
            created_at=self._clock())
        store.write_closure(self._root, closure)
        self._checkpoint(mission_root, mission.mission_id)
        return MissionResult(
            mission=mission, plan=plan, closure=closure,
            status=status_document, resumed=resumed, recovery=decision)

    # -- execution -------------------------------------------------------------

    def _execute(self, mission_root: Path,
                 mission: model.MissionDefinition,
                 plan: model.MissionPlan) -> obs_run.RunResult:
        # Reuse a finalized execution on resume (prior evidence stays durable).
        events = obs_store.load_events(mission_root)
        if self._execution_finalized(events):
            document = obs_store.load_status(mission_root)
            return obs_run.RunResult(
                status=_status_from_document(document), attempts=0,
                events=len(events), preflight=None,
                telemetry=closure_telemetry(mission_root))
        workload = model.mission_workload(mission)
        config = obs_run.LiveRunConfig(
            run_id=mission.mission_id, root=str(self._root),
            workspace=mission.workspace, backend=mission.backend,
            provider=mission.provider, model=mission.model,
            workload=workload, telemetry_mode=mission.telemetry_mode,
            max_repairs=mission.trust_policy.max_repairs,
            require_review=mission.trust_policy.require_review,
            inline_review_enabled=mission.trust_policy.inline_review_enabled,
            final_review_enabled=mission.trust_policy.require_review,
            final_reviewer_model=mission.trust_policy.final_reviewer_model,
            timeout_s=mission.timeout_s,
            mission_id=mission.mission_id, mode=mission.mode)
        coordinator = obs_run.RunCoordinator(
            config, executor=self._executor,
            reviewer_factory=self._reviewer_factory, clock=self._clock,
            resource_sampler=self._resource_sampler)
        return coordinator.run()

    @staticmethod
    def _execution_finalized(events: list[dict[str, Any]]) -> bool:
        return any(event.get("kind") == "RUN_COMPLETED" for event in events)

    # -- human gate ------------------------------------------------------------

    def _human_gate(self, mission_root: Path,
                    mission: model.MissionDefinition, plan: model.MissionPlan,
                    status: obs_model.CanonicalStatus,
                    ) -> tuple[obs_model.CanonicalStatus, str]:
        if status.readiness != obs_model.RD_READY_FOR_COMMIT:
            return status, "not_ready"
        violation = ""
        if status.state != obs_model.LC_COMPLETE:
            violation = "READY_FOR_COMMIT without a COMPLETE lifecycle"
        elif mission.trust_policy.require_review:
            if not status.final_reviewer.active:
                violation = "READY_FOR_COMMIT without an active final reviewer"
            elif status.reviewed_patch != status.current_patch:
                violation = "READY_FOR_COMMIT with a stale reviewed patch"
        if not violation:
            return status, "pass"
        blocked = replace(
            status, readiness=obs_model.RD_BLOCKED,
            terminal_reason=violation,
            terminal_reason_code=model.R_HUMAN_GATE_VIOLATION,
            next_action="operator: resolve the blocking trust gate")
        blocked = blocked.validate()
        obs_store.write_status(mission_root, blocked)
        return blocked, "blocked"

    # -- interruption ----------------------------------------------------------

    def _stop_requested(self, mission_id: str) -> bool:
        """Read the durable mission-scoped graceful stop latch (fail closed)."""
        return assembly_control.stop_requested(self._root, mission_id)

    def _checkpoint(self, mission_root: Path, mission_id: str) -> None:
        """Persist the explicit safe resume point (best effort, read-only)."""
        try:
            decision = recovery.detect_resume(self._root, mission_id)
        except (model.AssemblyError, obs_store.CanonicalStoreError):
            return
        recovery.write_recovery_record(self._root, decision)

    def _interrupt(self, mission_root: Path,
                   mission: model.MissionDefinition,
                   plan: model.MissionPlan | None, resumed: bool,
                   phase: str,
                   status_document: Mapping[str, Any] | None = None,
                   mission_id: str | None = None,
                   decision: recovery.ResumeDecision | None = None,
                   ) -> MissionResult:
        reason = ("operator graceful stop requested"
                  if mission_id is not None
                  and self._stop_requested(mission_id)
                  else "phase-boundary interruption")
        self._emit(
            mission_root, mission.mission_id, kind="MISSION_INTERRUPTED",
            stage=obs_model.STAGE_PREFLIGHT, phase=phase,
            detail={"phase": phase, "reason": reason,
                    "note": "durable evidence retained for resume"})
        if status_document is None:
            status_document = self._write_phase_status(
                mission_root, mission, current=f"INTERRUPTED_AFTER_{phase}",
                next_label="resume", state=obs_model.LC_PENDING,
                stage=obs_model.STAGE_PREFLIGHT, phase=phase,
                readiness=obs_model.RD_INDETERMINATE,
                next_action="operator: resume the mission").to_dict()
        self._checkpoint(mission_root, mission.mission_id)
        return MissionResult(
            mission=mission, plan=plan, closure=None,
            status=status_document, resumed=resumed, interrupted=True,
            interrupt_phase=phase, recovery=decision)

    # -- durable canonical status (M030 contract) ------------------------------

    def _write_phase_status(self, mission_root: Path,
                            mission: model.MissionDefinition, *,
                            current: str, next_label: str | None,
                            state: str, stage: str, phase: str,
                            readiness: str,
                            previous_gate: str | None = None,
                            previous_result: str | None = None,
                            reviewed_patch: str | None = None,
                            current_patch: str | None = None,
                            terminal_reason: str | None = None,
                            terminal_reason_code: str | None = None,
                            next_action: str | None = None,
                            attempt: int = 0,
                            ) -> obs_model.CanonicalStatus:
        now = self._clock()
        policy = mission.trust_policy
        inline = self._inline_reviewer_status(policy)
        final = self._final_reviewer_status(policy)
        status = obs_model.CanonicalStatus.build(
            run_id=mission.mission_id, state=state, stage=stage, phase=phase,
            attempt=attempt, current_backend=mission.backend,
            current_provider=mission.provider, current_model=mission.model,
            inline_review_enabled=policy.inline_review_enabled,
            inline_reviewer=inline, final_review_enabled=policy.require_review,
            final_reviewer=final, previous_gate=previous_gate,
            previous_result=previous_result, reviewed_patch=reviewed_patch,
            current_patch=current_patch,
            next_action=(next_action
                         or obs_model.next_action_for(state, readiness)),
            last_meaningful_event_at=now, heartbeat_at=now,
            terminal_reason=terminal_reason,
            terminal_reason_code=terminal_reason_code, readiness=readiness,
            current=current, next=next_label,
            telemetry_mode=mission.telemetry_mode, telemetry=None,
            updated_at=now)
        obs_store.write_status(mission_root, status)
        return status

    @staticmethod
    def _inline_reviewer_status(policy: model.TrustPolicy,
                                ) -> obs_model.ReviewerStatus:
        if not policy.inline_review_enabled:
            return obs_model.ReviewerStatus.disabled(
                obs_model.ROLE_INLINE_REVIEWER,
                reason="inline review disabled by operator")
        return obs_model.ReviewerStatus(
            role=obs_model.ROLE_INLINE_REVIEWER, enabled=True, active=False,
            backend="ollama", provider="ollama",
            model=obs_model.INLINE_REVIEWER_MODEL,
            reason="inline review configured but not invoked").validate()

    @staticmethod
    def _final_reviewer_status(policy: model.TrustPolicy,
                               ) -> obs_model.ReviewerStatus:
        if not policy.require_review:
            return obs_model.ReviewerStatus.disabled(
                obs_model.ROLE_FINAL_INDEPENDENT_REVIEWER,
                reason="final independent review disabled by operator")
        return obs_model.ReviewerStatus(
            role=obs_model.ROLE_FINAL_INDEPENDENT_REVIEWER, enabled=True,
            active=False, backend="ollama", provider="ollama",
            model=policy.final_reviewer_model,
            reason="final independent review not yet invoked").validate()

    # -- canonical events ------------------------------------------------------

    def _emit(self, mission_root: Path, mission_id: str, *, kind: str,
              stage: str, phase: str,
              result: str = obs_model.RESULT_NA,
              gate: str = obs_model.GATE_NONE,
              patch: str | None = None, actor: str | None = None,
              detail: Mapping[str, Any] | None = None) -> None:
        sequence = obs_store.event_count(mission_root) + 1
        event = obs_model.CanonicalEvent.build(
            run_id=mission_id, sequence=sequence, kind=kind, at=self._clock(),
            stage=stage, phase=phase, attempt=0, gate=gate, result=result,
            patch=patch, actor=actor, detail=dict(detail or {}))
        obs_store.append_event(mission_root, event)


def closure_telemetry(mission_root: Path) -> Mapping[str, Any] | None:
    try:
        return obs_store.load_telemetry(mission_root)
    except obs_store.CanonicalStoreError:
        return None


def _status_from_document(
    document: Mapping[str, Any],
) -> obs_model.CanonicalStatus:
    """Rebuild a validated canonical status from its durable document."""
    inline = _reviewer_from_document(document.get("inline_reviewer"),
                                     obs_model.ROLE_INLINE_REVIEWER)
    final = _reviewer_from_document(
        document.get("final_reviewer"),
        obs_model.ROLE_FINAL_INDEPENDENT_REVIEWER)
    telemetry = document.get("telemetry")
    return obs_model.CanonicalStatus.build(
        run_id=str(document["run_id"]), state=str(document["state"]),
        stage=str(document["stage"]), phase=str(document["phase"]),
        attempt=int(document.get("attempt", 0)),
        current_backend=_opt_str(document.get("current_backend")),
        current_provider=_opt_str(document.get("current_provider")),
        current_model=_opt_str(document.get("current_model")),
        inline_review_enabled=bool(document.get("inline_review_enabled",
                                                 False)),
        inline_reviewer=inline,
        final_review_enabled=bool(document.get("final_review_enabled",
                                               False)),
        final_reviewer=final,
        previous_gate=_opt_str(document.get("previous_gate")),
        previous_result=_opt_str(document.get("previous_result")),
        reviewed_patch=_opt_str(document.get("reviewed_patch")),
        current_patch=_opt_str(document.get("current_patch")),
        next_action=str(document.get("next_action", "")),
        last_meaningful_event_at=_opt_str(
            document.get("last_meaningful_event_at")),
        heartbeat_at=_opt_str(document.get("heartbeat_at")),
        terminal_reason=_opt_str(document.get("terminal_reason")),
        terminal_reason_code=_opt_str(document.get("terminal_reason_code")),
        readiness=str(document["readiness"]),
        current=_opt_str(document.get("current")),
        next=_opt_str(document.get("next")),
        telemetry_mode=str(document.get("telemetry_mode",
                                        obs_model.TELEMETRY_STANDARD)),
        telemetry=(telemetry if isinstance(telemetry, Mapping) else None),
        updated_at=_opt_str(document.get("updated_at")))


def _reviewer_from_document(value: object, role: str,
                            ) -> obs_model.ReviewerStatus:
    if not isinstance(value, Mapping) or not value.get("enabled"):
        return obs_model.ReviewerStatus.disabled(role)
    return obs_model.ReviewerStatus(
        role=role, enabled=True, active=bool(value.get("active", False)),
        backend=_opt_str(value.get("backend")),
        provider=_opt_str(value.get("provider")),
        model=_opt_str(value.get("model")),
        reason=str(value.get("reason", ""))).validate()


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


__all__ = [
    "DEFAULT_DEFINITION_OF_DONE",
    "MissionOrchestrator",
    "MissionRequest",
    "MissionResult",
    "closure_telemetry",
]
