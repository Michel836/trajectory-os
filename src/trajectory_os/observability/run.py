"""M030 — canonical live-run orchestrator (runtime adapter -> durable state).

This is the M030 runtime adapter. It runs one bounded implementation /
validation / review / repair loop for an isolated workspace and persists the
canonical durable artifacts after every meaningful phase transition::

    events.jsonl  ->  status.json  ->  CLI / TUI / Web

The orchestrator owns *collection and persistence*; aggregation lives in
:mod:`trajectory_os.observability.telemetry` and presentation lives in
:mod:`trajectory_os.observability.projection`.

Trust constraints:

* no Git trust-boundary write is performed;
* preflight runs first and a rejection stops the run before validation,
  review or repair;
* lifecycle completion and trust readiness are tracked independently;
* reviewer roles are explicit and an inactive reviewer is never displayed
  with a phantom/default model.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from trajectory_os.benchmark import metrics as bench_metrics
from trajectory_os.benchmark import model as bench_model
from trajectory_os.benchmark import patch as bench_patch
from trajectory_os.benchmark import review as bench_review
from trajectory_os.benchmark import validation as bench_validation
from trajectory_os.benchmark.executor import (
    ExecutionOutcome,
    ExecutionRequest,
    TrialExecutor,
)
from trajectory_os.missions import review_protocol
from trajectory_os.observability import model, preflight, store, telemetry

Clock = Callable[[], str]

#: Module-level defaults so the dataclass field named ``model`` cannot shadow
#: the imported ``model`` module inside the class body.
_DEFAULT_TELEMETRY_MODE = model.TELEMETRY_STANDARD
_DEFAULT_INLINE_REVIEWER_MODEL = model.INLINE_REVIEWER_MODEL
_DEFAULT_FINAL_REVIEWER_MODEL = model.FINAL_REVIEWER_MODEL


def utc_now() -> str:
    from datetime import UTC, datetime
    return (datetime.now(UTC).replace(microsecond=0).isoformat()
            .replace("+00:00", "Z"))


@dataclass(frozen=True)
class LiveRunConfig:
    """The bounded inputs of one observable run."""

    run_id: str
    root: str
    workspace: str
    backend: str
    provider: str | None
    model: str | None
    workload: bench_model.WorkloadSpec
    telemetry_mode: str = _DEFAULT_TELEMETRY_MODE
    max_repairs: int = 2
    require_review: bool = True
    inline_review_enabled: bool = False
    inline_reviewer_model: str | None = _DEFAULT_INLINE_REVIEWER_MODEL
    final_review_enabled: bool = True
    final_reviewer_model: str | None = _DEFAULT_FINAL_REVIEWER_MODEL
    timeout_s: int = 600
    mission_id: str | None = None
    machine: str | None = None
    locality: str | None = None
    mode: str = "LIVE"

    def validate(self) -> LiveRunConfig:
        if not self.run_id:
            raise model.ObservabilityError("MALFORMED_RUN", "run_id required")
        if self.telemetry_mode not in model.TELEMETRY_MODES:
            raise model.ObservabilityError(
                "MALFORMED_RUN", f"telemetry_mode {self.telemetry_mode!r}")
        if self.max_repairs < 0:
            raise model.ObservabilityError(
                "MALFORMED_RUN", "max_repairs must be >= 0")
        return self


@dataclass(frozen=True)
class RunResult:
    """The bounded result of one canonical run."""

    status: model.CanonicalStatus
    attempts: int
    events: int
    preflight: model.PreflightResult | None
    telemetry: Mapping[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.to_dict(),
            "attempts": self.attempts,
            "events": self.events,
            "preflight": (None if self.preflight is None
                          else self.preflight.to_dict()),
            "telemetry": (None if self.telemetry is None
                          else dict(self.telemetry)),
        }


@dataclass
class _Attempt:
    number: int
    outcome: ExecutionOutcome | None
    validation: bench_model.ValidationOutcome | None
    review: bench_model.ReviewOutcome | None
    patch: bench_model.PatchIdentity | None
    telemetry: bench_model.TrialTelemetry | None
    result: str
    gate: str
    phase_durations: tuple[tuple[str, int], ...] = ()
    patch_sha: str | None = None


def scripted_reviewer_factory(
    responses: Sequence[str],
) -> Callable[[], bench_review.ReviewerClient]:
    """A deterministic reviewer that emits a fixed response sequence."""
    state = {"index": 0}

    def factory() -> bench_review.ReviewerClient:
        index = state["index"]
        state["index"] = index + 1
        chosen = responses[min(index, len(responses) - 1)]
        return bench_review.FixtureReviewerClient(chosen)

    return factory


class RunCoordinator:
    """Canonical live-run coordinator with durable event/status/telemetry."""

    def __init__(
        self, config: LiveRunConfig, *,
        executor: TrialExecutor,
        reviewer_factory: Callable[[], bench_review.ReviewerClient] | None = None,
        clock: Clock = utc_now,
        resource_sampler: Callable[[str], bench_metrics.ResourceSample] | None = None,
    ) -> None:
        self._config = config.validate()
        self._executor = executor
        self._reviewer_factory = reviewer_factory
        self._clock = clock
        self._root = store.ensure_run_root(config.root, config.run_id)
        self._events = store.event_count(self._root)
        self._attempts: list[_Attempt] = []
        self._review_findings = ""
        self._phase_durations: list[tuple[str, int]] = []
        self._resource_sampler = resource_sampler

    # -- public ----------------------------------------------------------------

    @property
    def root(self) -> str:
        return str(self._root)

    def run(self) -> RunResult:
        result = self._preflight()
        if result is not None:
            return result
        readiness = self._loop()
        return self._finalize(readiness)

    # -- preflight -------------------------------------------------------------

    def _preflight(self) -> RunResult | None:
        self._status(state=model.LC_PREFLIGHT, stage=model.STAGE_PREFLIGHT,
                     phase="PREFLIGHT", attempt=0)
        request = preflight.PreflightRequest(
            run_id=self._config.run_id, backend=self._config.backend,
            provider=self._config.provider, model=self._config.model,
            workspace=self._config.workspace,
            reviewer_model=self._config.final_reviewer_model,
            review_enabled=self._config.require_review,
            telemetry_mode=self._config.telemetry_mode)
        outcome = preflight.preflight(request)
        self._emit(
            kind="PREFLIGHT_COMPLETED",
            stage=model.STAGE_PREFLIGHT, phase="PREFLIGHT", attempt=0,
            gate=model.GATE_PREFLIGHT,
            result=(model.RESULT_PASS if outcome.ok else model.RESULT_FAIL),
            detail={"reason": outcome.reason, "detail": outcome.detail,
                    "checks": [dict(c) for c in outcome.checks]})
        if outcome.ok:
            return None
        status = self._status(
            state=model.LC_COMPLETE, stage=model.STAGE_DONE,
            phase="DONE", attempt=0, readiness=model.RD_BLOCKED,
            previous_gate=model.GATE_PREFLIGHT,
            previous_result=model.RESULT_FAIL,
            terminal_reason=outcome.reason,
            terminal_reason_code=outcome.reason,
            next_action="operator: fix the invalid configuration and re-run")
        return RunResult(status=status, attempts=0,
                         events=self._events, preflight=outcome,
                         telemetry=None)

    # -- implementation / validation / review / repair -------------------------

    def _loop(self) -> str:
        baseline = bench_patch.capture(self._config.workspace)
        readiness = model.RD_INDETERMINATE
        for attempt in range(self._config.max_repairs + 1):
            self._attempts.append(self._run_attempt(attempt, baseline))
            last = self._attempts[-1]
            if last.result == model.RESULT_PASS:
                readiness = model.RD_READY_FOR_COMMIT
                break
            if last.gate == model.GATE_VALIDATION:
                readiness = model.RD_FAILED
                break
            if last.gate == model.GATE_REVIEW:
                if attempt < self._config.max_repairs:
                    self._status(
                        state=model.LC_REPAIRING, stage=model.STAGE_REPAIR,
                        phase="REPAIR", attempt=attempt + 1,
                        previous_gate=model.GATE_REVIEW,
                        previous_result=model.RESULT_REJECT,
                        reviewed_patch=last.patch_sha,
                        current_patch=last.patch_sha,
                        terminal_reason=None)
                    continue
                readiness = model.RD_BLOCKED
                break
            # A backend that failed without producing evidence is a failure.
            readiness = model.RD_FAILED
            break
        return readiness

    def _run_attempt(self, attempt: int,
                     baseline: bench_patch.WorkspaceSnapshot) -> _Attempt:
        workload = self._config.workload
        if attempt > 0 and self._review_findings:
            workload = replace(
                workload,
                objective=(f"{workload.objective}\n\nREPAIR THE FOLLOWING "
                           f"REVIEW FINDINGS:\n{self._review_findings}"))
        self._status(state=model.LC_IMPLEMENTING, stage=model.STAGE_EXECUTE,
                     phase="IMPLEMENT", attempt=attempt)
        started = time.monotonic()
        outcome = self._executor.execute(ExecutionRequest(
            workload=workload, workspace=self._config.workspace,
            backend=self._config.backend, provider=self._config.provider,
            model_name=self._config.model,
            locality=(self._config.locality
                      or bench_model.backend_locality(
                          self._config.backend, self._config.model)),
            mode=self._config.mode, attempt=attempt,
            timeout_s=self._config.timeout_s, thinking="medium"))
        implement_ms = int((time.monotonic() - started) * 1000)
        self._phase_durations.append(("IMPLEMENT", implement_ms))

        current = bench_patch.capture(self._config.workspace)
        patch_identity = bench_patch.build_identity(baseline, current)
        patch_sha = patch_identity.sha256
        self._emit(kind="PATCH_CAPTURED", stage=model.STAGE_EXECUTE,
                   phase="IMPLEMENT", attempt=attempt,
                   patch=patch_sha, result=model.RESULT_NA,
                   actor=model.ROLE_IMPLEMENTATION_AGENT,
                   detail={"available": patch_identity.available,
                           "files_changed": patch_identity.files_changed})

        if outcome.cancelled:
            return self._attempt(attempt, outcome, None, None, patch_identity,
                                 model.RESULT_BLOCKED, model.GATE_NONE,
                                 patch_sha)

        self._status(state=model.LC_VALIDATING, stage=model.STAGE_VALIDATE,
                     phase="VALIDATE", attempt=attempt,
                     current_patch=patch_sha)
        validation = bench_validation.run_validation(
            self._config.workload.validation_command,
            self._config.workspace,
            timeout_s=min(self._config.timeout_s, 600))
        self._phase_durations.append(("VALIDATE",
                                      validation.duration_ms or 0))
        self._emit(kind="VALIDATION_COMPLETED", stage=model.STAGE_VALIDATE,
                   phase="VALIDATE", attempt=attempt,
                   gate=model.GATE_VALIDATION,
                   result=(model.RESULT_PASS if validation.passed
                           else model.RESULT_FAIL),
                   patch=patch_sha,
                   detail={"reason": validation.reason,
                           "exit_code": validation.exit_code})
        if not validation.passed:
            return self._attempt(attempt, outcome, validation, None,
                                 patch_identity, model.RESULT_FAIL,
                                 model.GATE_VALIDATION, patch_sha)

        if not (self._config.require_review and self._config.final_review_enabled):
            # Review explicitly disabled: ready without any reviewer identity.
            return self._attempt(attempt, outcome, validation, None,
                                 patch_identity, model.RESULT_PASS,
                                 model.GATE_VALIDATION, patch_sha)

        assert self._reviewer_factory is not None
        self._status(state=model.LC_REVIEWING, stage=model.STAGE_REVIEW,
                     phase="REVIEW", attempt=attempt,
                     previous_gate=model.GATE_VALIDATION,
                     previous_result=model.RESULT_PASS,
                     reviewed_patch=patch_sha, current_patch=patch_sha)
        client = self._reviewer_factory()
        invocation = client.review(bench_review.ReviewRequest(
            objective=self._config.workload.objective,
            patch_text=self._patch_text(baseline, current),
            workspace=self._config.workspace,
            timeout_s=self._config.timeout_s))
        review = bench_review.assess_invocation(invocation)
        self._phase_durations.append(("REVIEW", invocation.duration_ms or 0))
        if review.active and review.outcome == review_protocol.OUTCOME_VALID_PASS:
            verdict = model.RESULT_PASS
        elif review.active:
            verdict = model.RESULT_REJECT
        else:
            verdict = model.RESULT_BLOCKED
        self._emit(kind="REVIEW_COMPLETED", stage=model.STAGE_REVIEW,
                   phase="REVIEW", attempt=attempt, gate=model.GATE_REVIEW,
                   result=verdict, patch=patch_sha,
                   actor=model.ROLE_FINAL_INDEPENDENT_REVIEWER,
                   detail={"outcome": review.outcome,
                           "reason": review.reason,
                           "reviewer": review.reviewer.model,
                           "active": review.active})
        if verdict == model.RESULT_PASS:
            return self._attempt(attempt, outcome, validation, review,
                                 patch_identity, model.RESULT_PASS,
                                 model.GATE_REVIEW, patch_sha)
        self._review_findings = self._findings_text(review)
        return self._attempt(attempt, outcome, validation, review,
                             patch_identity, verdict,
                             model.GATE_REVIEW, patch_sha)

    def _patch_text(self, baseline: bench_patch.WorkspaceSnapshot,
                    current: bench_patch.WorkspaceSnapshot) -> str:
        return bench_patch.compute_patch(baseline, current).text

    def _findings_text(self, review: bench_model.ReviewOutcome) -> str:
        assessment = review.assessment
        if not isinstance(assessment, Mapping):
            return review.reason
        parts: list[str] = []
        for label in ("blockers", "majors"):
            items = assessment.get(label)
            if isinstance(items, Sequence) and not isinstance(items, str):
                parts.extend(str(item) for item in items)
        return "\n".join(parts) or review.reason

    def _attempt(self, attempt: int, outcome: ExecutionOutcome,
                 validation: bench_model.ValidationOutcome | None,
                 review: bench_model.ReviewOutcome | None,
                 patch_identity: bench_model.PatchIdentity,
                 result: str, gate: str,
                 patch_sha: str | None) -> _Attempt:
        telemetry_record = self._attempt_telemetry(
            outcome, patch_identity, review, validation)
        return _Attempt(
            number=attempt, outcome=outcome, validation=validation,
            review=review, patch=patch_identity, telemetry=telemetry_record,
            result=result, gate=gate,
            phase_durations=tuple(self._phase_durations[-2:]),
            patch_sha=patch_sha)

    def _attempt_telemetry(
        self, outcome: ExecutionOutcome,
        patch_identity: bench_model.PatchIdentity,
        review: bench_model.ReviewOutcome | None,
        validation: bench_model.ValidationOutcome | None,
    ) -> bench_model.TrialTelemetry:
        result = outcome.agent_result
        if result is None:
            from trajectory_os.agents import model as agent_model

            result = agent_model.AgentResult.build(
                backend=outcome.backend,
                status=agent_model.RS_FAILED,
                reason=agent_model.R_LAUNCH_FAILED)
        resources = None
        if (self._resource_sampler is not None
                and self._config.telemetry_mode == model.TELEMETRY_BENCHMARK):
            resources = self._resource_sampler(self._config.backend)
        return bench_metrics.from_agent_result(
            result, provider=outcome.provider, model_name=outcome.model_name,
            wall_ms=outcome.wall_ms,
            first_useful_patch_ms=outcome.first_useful_patch_ms,
            phase_durations=outcome.phase_durations,
            review_rejects=(1 if review is not None and review.active
                            and review.outcome
                            != review_protocol.OUTCOME_VALID_PASS
                            else 0),
            protocol_errors=(1 if review is not None
                             and review.outcome == "REVIEW_PROTOCOL_INVALID"
                             else 0),
            validation_failures=(0 if validation is None
                                 or validation.passed else 1),
            resources=resources)

    # -- finalize --------------------------------------------------------------

    def _finalize(self, readiness: str) -> RunResult:
        records = [self._trial_record(attempt)
                   for attempt in self._attempts]
        successful = sum(1 for attempt in self._attempts
                         if attempt.result == model.RESULT_PASS)
        telemetry_document: dict[str, Any] | None = None
        if self._config.telemetry_mode != model.TELEMETRY_OFF:
            identity = self._identity_document()
            telemetry_document = telemetry.aggregate_telemetry(
                run_id=self._config.run_id,
                mode=self._config.telemetry_mode, trials=records,
                identity=identity,
                phase_durations=tuple(self._phase_durations),
                resources=None, successful_tasks=successful,
                reviewed_trials=sum(
                    1 for a in self._attempts
                    if a.review is not None and a.review.active))
            telemetry_document["waste"] = telemetry.wasted_in_failed_cycles(
                records)
            store.write_telemetry(self._root, telemetry_document)
        terminal_reason, reason_code = self._terminal_reason(readiness)
        self._emit(kind="RUN_COMPLETED", stage=model.STAGE_DONE, phase="DONE",
                   attempt=max(0, len(self._attempts) - 1),
                   result=(model.RESULT_PASS
                           if readiness == model.RD_READY_FOR_COMMIT
                           else model.RESULT_FAIL),
                   detail={"readiness": readiness,
                           "terminal_reason": terminal_reason})
        status = self._status(
            state=model.LC_COMPLETE, stage=model.STAGE_DONE, phase="DONE",
            attempt=max(0, len(self._attempts) - 1), readiness=readiness,
            terminal_reason=terminal_reason, terminal_reason_code=reason_code,
            telemetry=telemetry_document)
        summary = self._summary_document(readiness, records,
                                         telemetry_document)
        store.write_summary(self._root, summary)
        return RunResult(status=status, attempts=len(self._attempts),
                         events=self._events, preflight=None,
                         telemetry=telemetry_document)

    def _identity_document(self) -> dict[str, Any]:
        import platform

        return {
            "run_id": self._config.run_id,
            "mission_id": self._config.mission_id,
            "backend": self._config.backend,
            "provider": self._config.provider,
            "model": self._config.model,
            "mode": self._config.telemetry_mode,
            "machine": self._config.machine or platform.node(),
            "locality": (self._config.locality
                         or bench_model.backend_locality(
                             self._config.backend, self._config.model)),
        }

    def _summary_document(
        self, readiness: str, records: Sequence[bench_model.TrialRecord],
        telemetry_document: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        statuses = [attempt.result for attempt in self._attempts]
        return {
            "schema_version": model.CANONICAL_SCHEMA_VERSION,
            "observability_version": model.OBSERVABILITY_VERSION,
            "run_id": self._config.run_id,
            "lifecycle": model.LC_COMPLETE,
            "readiness": readiness,
            "success": readiness == model.RD_READY_FOR_COMMIT,
            "attempts": len(self._attempts),
            "results": {result: statuses.count(result)
                        for result in sorted(set(statuses))},
            "reviewed_patch": self._reviewed_patch(),
            "current_patch": self._current_patch(),
            "telemetry_mode": self._config.telemetry_mode,
            "telemetry": (None if telemetry_document is None
                          else dict(telemetry_document)),
            "trial_count": len(records),
            "updated_at": self._clock(),
        }

    def _terminal_reason(self, readiness: str) -> tuple[str, str]:
        if readiness == model.RD_READY_FOR_COMMIT:
            return ("all gates passed", model.R_COMPLETE)
        if readiness == model.RD_BLOCKED:
            return ("blocking review findings or inactive reviewer",
                    model.R_BLOCKED)
        if readiness == model.RD_CANCELLED:
            return ("run cancelled", model.R_CANCELLED)
        return ("run failed a trust gate", model.R_FAILED)

    # -- durable state ---------------------------------------------------------

    def _status(self, *, state: str, stage: str, phase: str, attempt: int,
                readiness: str | None = None,
                previous_gate: str | None = None,
                previous_result: str | None = None,
                reviewed_patch: str | None = None,
                current_patch: str | None = None,
                terminal_reason: str | None = None,
                terminal_reason_code: str | None = None,
                next_action: str | None = None,
                telemetry: Mapping[str, Any] | None = None,
                ) -> model.CanonicalStatus:
        now = self._clock()
        readiness_value = readiness or self._current_readiness()
        last = self._attempts[-1] if self._attempts else None
        current_patch_value = (current_patch if current_patch is not None
                               else self._current_patch())
        reviewed_patch_value = (reviewed_patch if reviewed_patch is not None
                                else self._reviewed_patch())
        inline = self._inline_reviewer_status()
        final = self._final_reviewer_status(state, phase)
        status = model.CanonicalStatus.build(
            run_id=self._config.run_id, state=state, stage=stage,
            phase=phase, attempt=attempt,
            current_backend=self._config.backend,
            current_provider=self._config.provider,
            current_model=self._config.model,
            inline_review_enabled=self._config.inline_review_enabled,
            inline_reviewer=inline,
            final_review_enabled=self._config.final_review_enabled,
            final_reviewer=final,
            previous_gate=(previous_gate if previous_gate is not None
                           else self._last_gate()),
            previous_result=(previous_result if previous_result is not None
                             else self._last_result()),
            reviewed_patch=reviewed_patch_value,
            current_patch=current_patch_value,
            next_action=(next_action
                         or model.next_action_for(state, readiness_value)),
            last_meaningful_event_at=(self._last_event_at() or now),
            heartbeat_at=now,
            terminal_reason=terminal_reason,
            terminal_reason_code=terminal_reason_code,
            readiness=readiness_value,
            current=self._current_phase_label(last),
            next=self._next_phase_label(state),
            telemetry_mode=self._config.telemetry_mode,
            telemetry=telemetry,
            updated_at=now,
        )
        store.write_status(self._root, status)
        return status

    def _current_readiness(self) -> str:
        if any(a.result == model.RESULT_BLOCKED for a in self._attempts):
            return model.RD_BLOCKED
        if any(a.result == model.RESULT_REJECT for a in self._attempts):
            return model.RD_BLOCKED
        if any(a.result == model.RESULT_FAIL for a in self._attempts):
            return model.RD_FAILED
        if any(a.result == model.RESULT_PASS for a in self._attempts):
            return model.RD_READY_FOR_COMMIT
        return model.RD_INDETERMINATE

    def _inline_reviewer_status(self) -> model.ReviewerStatus:
        if not self._config.inline_review_enabled:
            return model.ReviewerStatus.disabled(
                model.ROLE_INLINE_REVIEWER,
                reason="inline review disabled by operator")
        # Enabled but this adapter does not invoke an inline reviewer, so it is
        # rendered inactive with no display model (never a phantom).
        return model.ReviewerStatus(
            role=model.ROLE_INLINE_REVIEWER, enabled=True, active=False,
            backend="ollama", provider="ollama",
            model=self._config.inline_reviewer_model,
            reason="inline review configured but not invoked"
            ).validate()

    def _final_reviewer_status(self, state: str,
                               phase: str) -> model.ReviewerStatus:
        if not (self._config.require_review
                and self._config.final_review_enabled):
            return model.ReviewerStatus.disabled(
                model.ROLE_FINAL_INDEPENDENT_REVIEWER,
                reason="final independent review disabled by operator")
        invoked = any(attempt.review is not None
                      and attempt.review.active
                      for attempt in self._attempts)
        if not invoked:
            return model.ReviewerStatus(
                role=model.ROLE_FINAL_INDEPENDENT_REVIEWER, enabled=True,
                active=False, backend="ollama", provider="ollama",
                model=self._config.final_reviewer_model,
                reason="final review not invoked").validate()
        return model.ReviewerStatus(
            role=model.ROLE_FINAL_INDEPENDENT_REVIEWER, enabled=True,
            active=True, backend="ollama", provider="ollama",
            model=self._config.final_reviewer_model,
            reason=model.R_OK).validate()

    def _trial_record(self, attempt: _Attempt) -> bench_model.TrialRecord:
        review = attempt.review or bench_review.assess_invocation(
            bench_review.ReviewerInvocation(
                role=model.ROLE_FINAL_INDEPENDENT_REVIEWER, backend="ollama",
                provider="ollama",
                model=self._config.final_reviewer_model or "",
                active=False, reason=model.R_REVIEW_INACTIVE))
        patch_identity = attempt.patch or bench_model.PatchIdentity.build(
            available=False, sha256=None, files_changed=None,
            insertions=None, deletions=None, reason=model.R_UNKNOWN)
        validation = attempt.validation or bench_model.ValidationOutcome(
            command=self._config.workload.validation_command, passed=False,
            exit_code=None, duration_ms=None, timed_out=False,
            output_sha256=None, reason=model.R_NOT_STARTED)
        telemetry_record = (attempt.telemetry
                            or bench_model.TrialTelemetry.empty())
        status = {
            model.RESULT_PASS: bench_model.TS_PASS,
            model.RESULT_REJECT: bench_model.TS_FAILED,
            model.RESULT_FAIL: bench_model.TS_FAILED,
            model.RESULT_BLOCKED: bench_model.TS_BLOCKED,
        }.get(attempt.result, bench_model.TS_FAILED)
        return bench_model.TrialRecord.build(
            benchmark_run_id=self._config.run_id,
            trial_id=f"{self._config.run_id}-attempt-{attempt.number}",
            workload_id=self._config.workload.workload_id,
            workload_class=self._config.workload.workload_class,
            repetition=attempt.number, backend=self._config.backend,
            provider=self._config.provider, model=self._config.model,
            locality=(self._config.locality
                      or bench_model.backend_locality(
                          self._config.backend, self._config.model)),
            runtime_version=None, sdk_version=None, mode="LIVE",
            status=status, reason=attempt.result,
            fail_closed_case=self._config.workload.fail_closed_case,
            interrupted=False, resumed=attempt.number > 0,
            agent={"evidence": "M030_LIVE_RUN"},
            validation=validation, review=review, patch=patch_identity,
            telemetry=telemetry_record, created_at=self._clock())

    def _last_gate(self) -> str | None:
        return self._attempts[-1].gate if self._attempts else None

    def _last_result(self) -> str | None:
        return self._attempts[-1].result if self._attempts else None

    def _current_patch(self) -> str | None:
        for attempt in reversed(self._attempts):
            if attempt.patch_sha:
                return attempt.patch_sha
        return None

    def _reviewed_patch(self) -> str | None:
        for attempt in reversed(self._attempts):
            if attempt.review is not None and attempt.review.active:
                return attempt.patch_sha
        return None

    def _current_phase_label(self, last: _Attempt | None) -> str | None:
        if last is None:
            return None
        return f"attempt {last.number} ({last.result})"

    def _next_phase_label(self, state: str) -> str | None:
        if state == model.LC_COMPLETE:
            return None
        return model.next_action_for(state, model.RD_INDETERMINATE)

    def _last_event_at(self) -> str | None:
        events = store.load_events(self._root)
        return str(events[-1].get("at")) if events else None

    def _emit(self, *, kind: str, stage: str, phase: str, attempt: int,
              result: str = model.RESULT_NA, gate: str = model.GATE_NONE,
              patch: str | None = None, actor: str | None = None,
              detail: Mapping[str, Any] | None = None) -> None:
        self._events += 1
        event = model.CanonicalEvent.build(
            run_id=self._config.run_id, sequence=self._events, kind=kind,
            at=self._clock(), stage=stage, phase=phase, attempt=attempt,
            gate=gate, result=result, patch=patch, actor=actor,
            detail=dict(detail or {}))
        store.append_event(self._root, event)
