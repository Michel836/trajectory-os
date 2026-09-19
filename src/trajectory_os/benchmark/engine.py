"""M029 — benchmark orchestration (the repeatable command's core).

One benchmark run:

1. persists its exact inputs (manifest) once;
2. executes the canonical workload set for every backend / repetition in a
   fresh isolated workspace;
3. applies the deterministic validation gate, the strict review protocol and
   the exact semantic patch identity;
4. persists one authoritative trial record per execution;
5. aggregates and renders the summary + decision report;
6. derives the canonical run state.

Failures and blocked trials are persisted as evidence. A run is resumable:
already-terminal trials are skipped, and an interrupted trial is retried on
the next ``resume`` invocation.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trajectory_os.agents import model as agent_model
from trajectory_os.benchmark import aggregate, identity, metrics, model, patch, store, validation
from trajectory_os.benchmark import events as bench_events
from trajectory_os.benchmark import review as bench_review
from trajectory_os.benchmark.executor import (
    ExecutionOutcome,
    ExecutionRequest,
    TrialExecutor,
    prepare_workspace,
)

Clock = Callable[[], str]

#: Terminal trial statuses that are not retried on resume.
_SETTLED = frozenset({model.TS_PASS, model.TS_FAILED, model.TS_BLOCKED,
                      model.TS_UNAVAILABLE})


def utc_now() -> str:
    from datetime import UTC, datetime
    return (datetime.now(UTC).replace(microsecond=0).isoformat()
            .replace("+00:00", "Z"))


def make_run_id(created_at: str, mode: str) -> str:
    """Deterministic, readable run id (stable for a given creation second)."""
    compact = created_at.replace("-", "").replace(":", "").replace("Z", "")
    return f"m029-{compact}-{mode.lower()}"


def deterministic_trial_id(benchmark_run_id: str, workload_id: str,
                           backend: str, repetition: int) -> str:
    return identity.trial_id({
        "benchmark_run_id": benchmark_run_id,
        "workload_id": workload_id,
        "backend": backend,
        "repetition": repetition,
    })[:32]


@dataclass(frozen=True)
class PlannedTrial:
    workload: model.WorkloadSpec
    backend: str
    repetition: int
    trial_id: str


@dataclass(frozen=True)
class RunConfig:
    root: str
    benchmark_run_id: str
    mode: str
    workloads: tuple[model.WorkloadSpec, ...]
    backends: tuple[str, ...]
    repetitions: int
    target_provider: str
    target_model: str
    final_reviewer_model: str = model.FINAL_REVIEWER_MODEL
    timeout_s: int = 600
    thinking: str = "medium"
    baseline_revision: str | None = None
    baseline_patch: str | None = None
    environment: dict[str, Any] = field(default_factory=dict)
    require_review: bool = True


def plan_trials(config: RunConfig) -> tuple[PlannedTrial, ...]:
    planned: list[PlannedTrial] = []
    for workload in config.workloads:
        for backend in config.backends:
            for repetition in range(config.repetitions):
                planned.append(PlannedTrial(
                    workload=workload, backend=backend, repetition=repetition,
                    trial_id=deterministic_trial_id(
                        config.benchmark_run_id, workload.workload_id,
                        backend, repetition)))
    return tuple(planned)


# --- reviewer wiring ----------------------------------------------------------


def default_reviewer_factory(
    *, live: bool, model_name: str, base_url: str = "http://127.0.0.1:11434",
    timeout_s: int = 300,
) -> Callable[[model.WorkloadSpec, str], bench_review.ReviewerClient]:
    if not live:
        def inactive(workload: model.WorkloadSpec,
                     backend: str) -> bench_review.ReviewerClient:
            return bench_review.InactiveReviewerClient(
                model_name=model_name,
                reason="reviewer not invoked in pipeline-validation mode")
        return inactive

    def live_factory(workload: model.WorkloadSpec,
                     backend: str) -> bench_review.ReviewerClient:
        return bench_review.OllamaReviewerClient(
            model_name=model_name, base_url=base_url, timeout_s=timeout_s)

    return live_factory


def fixture_reviewer_factory(
    response: str,
) -> Callable[[model.WorkloadSpec, str], bench_review.ReviewerClient]:
    def factory(workload: model.WorkloadSpec,
                backend: str) -> bench_review.ReviewerClient:
        return bench_review.FixtureReviewerClient(response)
    return factory


def default_passing_review() -> str:
    return (
        "VERDICT: PASS\n"
        "BLOCKERS:\n- none\n"
        "MAJORS:\n- none\n"
        "MINORS:\n- none\n"
        "FINAL RECOMMENDATION: GO COMMIT\n"
    )


# --- run result ---------------------------------------------------------------


@dataclass(frozen=True)
class RunResult:
    benchmark_run_id: str
    state: str
    root: str
    ran: int
    skipped: int
    cancelled: bool
    trials: tuple[model.TrialRecord, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "benchmark_run_id": self.benchmark_run_id,
            "state": self.state,
            "root": self.root,
            "ran": self.ran,
            "skipped": self.skipped,
            "cancelled": self.cancelled,
            "trial_ids": [record.trial_id for record in self.trials],
            "trial_statuses": {
                record.trial_id: record.status for record in self.trials},
        }


# --- engine -------------------------------------------------------------------


class BenchmarkEngine:
    def __init__(
        self,
        config: RunConfig,
        *,
        executor: TrialExecutor,
        reviewer_factory: Callable[
            [model.WorkloadSpec, str], bench_review.ReviewerClient],
        resource_sampler: Callable[[str], metrics.ResourceSample] | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self._config = config
        self._executor = executor
        self._reviewer_factory = reviewer_factory
        self._resource_sampler = resource_sampler or (
            lambda backend: metrics.empty_resources())
        self._clock = clock
        self._root = store.ensure_run_root(config.root, config.benchmark_run_id)
        self._event_sequence = 0

    # -- public ----------------------------------------------------------------

    def run(self, *, resume: bool = False,
            cancel: object | None = None) -> RunResult:
        manifest = self._ensure_manifest(resume=resume)
        self._event_sequence = len(store.load_events(self._root))
        existing = {record.trial_id: record
                    for record in store.load_trials(self._root)}
        self._emit(
            bench_events.KIND_RUN_RESUMED if resume
            else bench_events.KIND_RUN_STARTED,
            bench_events.PHASE_QUALIFY,
            detail={"mode": self._config.mode,
                    "backends": list(self._config.backends),
                    "repetitions": self._config.repetitions})
        ran = 0
        skipped = 0
        cancelled = False
        planned = plan_trials(self._config)
        for plan in planned:
            if cancel is not None and getattr(cancel, "is_set",
                                              lambda: False)():
                cancelled = True
                break
            previous = existing.get(plan.trial_id)
            if resume and previous is not None and previous.status in _SETTLED:
                skipped += 1
                continue
            record = self._run_trial(plan, attempt=(1 if previous else 0),
                                     cancel=cancel)
            store.save_trial(self._root, record)
            existing[plan.trial_id] = record
            ran += 1
            if record.status == model.TS_CANCELLED:
                cancelled = True
                # A trial-level interruption does not discard the remaining
                # planned evidence. This is what lets the interruption/resume
                # workload cover *every* backend symmetrically in fixture
                # mode (each backend is interrupted once, then resumed) while
                # the run stays CANCELLED until a resume clears it. A real
                # operator cancellation is caught by the ``cancel`` check at
                # the top of the loop and still stops the run immediately.
                if self._config.mode != model.MODE_FIXTURE:
                    break
        records = tuple(
            existing[trial.trial_id] for trial in planned
            if trial.trial_id in existing)
        summary = self._finalize(records, manifest)
        state = summary["state"]
        self._emit(
            bench_events.KIND_RUN_CANCELLED if cancelled
            else bench_events.KIND_RUN_COMPLETED,
            bench_events.PHASE_DONE,
            detail={"state": state, "ran": ran, "skipped": skipped})
        return RunResult(
            benchmark_run_id=self._config.benchmark_run_id, state=state,
            root=str(self._root), ran=ran, skipped=skipped,
            cancelled=cancelled, trials=records)

    # -- manifest --------------------------------------------------------------

    def _ensure_manifest(self, *, resume: bool) -> model.RunManifest:
        path = self._root / store.MANIFEST_NAME
        if path.is_file():
            if not resume:
                raise store.StoreError(
                    "RUN_EXISTS",
                    f"{self._config.benchmark_run_id} already exists; "
                    "use resume")
            manifest = store.load_manifest(self._root)
            expected = _manifest_payload(self._config, manifest.created_at)
            if manifest.identity_payload() != expected.identity_payload():
                raise store.StoreError(
                    "MANIFEST_MISMATCH",
                    "resume configuration differs from the persisted manifest")
            return manifest
        manifest = _manifest_payload(self._config, self._clock())
        store.save_manifest(self._root, manifest)
        return manifest

    # -- one trial -------------------------------------------------------------

    def _run_trial(self, plan: PlannedTrial, *, attempt: int,
                   cancel: object | None) -> model.TrialRecord:
        workload = plan.workload
        workspace = str(self._root / "workspaces" / plan.trial_id)
        baseline = prepare_workspace(workload, workspace)
        self._emit(
            bench_events.KIND_TRIAL_STARTED, bench_events.PHASE_EXECUTE,
            trial=plan, detail={"attempt": attempt})
        outcome = self._executor.execute(ExecutionRequest(
            workload=workload, workspace=workspace, backend=plan.backend,
            provider=self._config.target_provider,
            model_name=self._config.target_model,
            locality=model.backend_locality(plan.backend,
                                            self._config.target_model),
            mode=self._config.mode, attempt=attempt,
            timeout_s=self._config.timeout_s, thinking=self._config.thinking,
            cancel=cancel))
        current = patch.capture(workspace)
        patch_result = patch.compute_patch(baseline, current)
        patch_identity = patch.build_identity(baseline, current)
        validation_outcome = validation.run_validation(
            workload.validation_command, workspace,
            timeout_s=min(self._config.timeout_s, 600))
        review_outcome = self._review(
            workload, plan.backend, outcome, patch_result.text)
        first_patch_ms = outcome.first_useful_patch_ms
        telemetry = self._telemetry(outcome, patch_identity, review_outcome,
                                    validation_outcome, first_patch_ms)
        status, reason = self._status(
            workload, outcome, validation_outcome, review_outcome,
            patch_identity)
        record = model.TrialRecord.build(
            benchmark_run_id=self._config.benchmark_run_id,
            trial_id=plan.trial_id, workload_id=workload.workload_id,
            workload_class=workload.workload_class,
            repetition=plan.repetition, backend=plan.backend,
            provider=outcome.provider, model=outcome.model_name,
            locality=outcome.locality, runtime_version=outcome.runtime_version,
            sdk_version=outcome.sdk_version, mode=self._config.mode,
            status=status, reason=reason,
            fail_closed_case=workload.fail_closed_case,
            interrupted=outcome.cancelled, resumed=attempt > 0,
            agent=_agent_document(outcome, workload),
            validation=validation_outcome, review=review_outcome,
            patch=patch_identity, telemetry=telemetry,
            created_at=self._clock())
        self._emit(
            bench_events.KIND_TRIAL_COMPLETED, bench_events.PHASE_VALIDATE,
            trial=plan,
            detail={"status": status, "reason": reason,
                    "patch": patch_identity.sha256,
                    "wall_ms": outcome.wall_ms})
        return record

    def _review(
        self, workload: model.WorkloadSpec, backend: str,
        outcome: ExecutionOutcome, patch_text: str,
    ) -> model.ReviewOutcome:
        if outcome.cancelled or outcome.backend_unavailable:
            client: bench_review.ReviewerClient = (
                bench_review.InactiveReviewerClient(
                    model_name=self._config.final_reviewer_model,
                    reason="trial did not reach the review gate"))
        elif workload.fail_closed_case:
            client = bench_review.InactiveReviewerClient(
                model_name=self._config.final_reviewer_model,
                reason="fail-closed workload commits no promotable patch")
        else:
            client = self._reviewer_factory(workload, backend)
        invocation = client.review(bench_review.ReviewRequest(
            objective=workload.objective, patch_text=patch_text,
            workspace="", timeout_s=self._config.timeout_s))
        return bench_review.assess_invocation(invocation)

    def _telemetry(
        self, outcome: ExecutionOutcome,
        patch_identity: model.PatchIdentity,
        review_outcome: model.ReviewOutcome,
        validation_outcome: model.ValidationOutcome,
        first_patch_ms: int | None,
    ) -> model.TrialTelemetry:
        result = outcome.agent_result
        if result is None:
            return metrics.from_agent_result(
                agent_model.AgentResult.build(
                    backend=outcome.backend,
                    status=(agent_model.RS_CANCELLED if outcome.cancelled
                            else agent_model.RS_UNAVAILABLE),
                    reason=(agent_model.R_CANCELLED if outcome.cancelled
                            else agent_model.R_BACKEND_UNAVAILABLE)),
                provider=outcome.provider, model_name=outcome.model_name,
                wall_ms=outcome.wall_ms, first_useful_patch_ms=first_patch_ms,
                phase_durations=outcome.phase_durations,
                review_rejects=(1 if review_outcome.active
                                and model.reviewer_is_reject(
                                    review_outcome.outcome) else 0),
                protocol_errors=(1 if review_outcome.outcome
                                 == "REVIEW_PROTOCOL_INVALID" else 0),
                validation_failures=(0 if validation_outcome.passed else 1),
                resources=self._resource_sampler(outcome.backend))
        return metrics.from_agent_result(
            result, provider=outcome.provider, model_name=outcome.model_name,
            wall_ms=outcome.wall_ms, first_useful_patch_ms=first_patch_ms,
            phase_durations=outcome.phase_durations,
            review_rejects=(1 if review_outcome.active
                            and model.reviewer_is_reject(
                                review_outcome.outcome) else 0),
            protocol_errors=(1 if review_outcome.outcome
                             == "REVIEW_PROTOCOL_INVALID" else 0),
            validation_failures=(0 if validation_outcome.passed else 1),
            resources=self._resource_sampler(outcome.backend))

    def _status(
        self, workload: model.WorkloadSpec, outcome: ExecutionOutcome,
        validation_outcome: model.ValidationOutcome,
        review_outcome: model.ReviewOutcome,
        patch_identity: model.PatchIdentity,
    ) -> tuple[str, str]:
        """Delegate the trial decision to the one canonical pure path."""
        touched = model.protected_paths_touched(
            workload.protected_paths, patch_identity.untracked)
        fail_closed_violated = workload.fail_closed_case and (
            not validation_outcome.passed or bool(touched))
        return model.trial_decision(
            backend_unavailable=outcome.backend_unavailable,
            unavailable_reason=outcome.unavailable_reason,
            validation_passed=validation_outcome.passed,
            fail_closed_case=workload.fail_closed_case,
            fail_closed_violated=fail_closed_violated,
            review_active=review_outcome.active,
            review_outcome=review_outcome.outcome,
            cancelled=outcome.cancelled,
            require_review=self._config.require_review)

    # -- finalize --------------------------------------------------------------

    def _finalize(self, records: Sequence[model.TrialRecord],
                  manifest: model.RunManifest) -> dict[str, Any]:
        summary = aggregate.build_summary(
            records, benchmark_run_id=self._config.benchmark_run_id,
            mode=self._config.mode)
        statuses = [record.status for record in records]
        run_state = model.aggregate_status(statuses)
        summary_document = {
            **summary,
            "state": run_state,
            "complete": all(s in model.SUCCESS_STATUSES for s in statuses)
            if statuses else False,
            "final_reviewer_model": manifest.final_reviewer_model,
            "manifest_id": manifest.manifest_id,
        }
        store.save_summary(self._root, summary_document)
        state = {
            "schema_version": model.SCHEMA_VERSION,
            "benchmark_run_id": self._config.benchmark_run_id,
            "state": run_state,
            "trials": len(records),
            "statuses": {
                status: statuses.count(status)
                for status in sorted(set(statuses))},
            "updated_at": self._clock(),
        }
        store.save_state(self._root, state)
        from trajectory_os.benchmark import report as bench_report
        store.write_report(
            self._root,
            bench_report.render(self._root, manifest, records,
                                summary_document))
        return summary_document

    def _emit(self, kind: str, phase: str, *, trial: PlannedTrial | None = None,
              detail: dict[str, Any] | None = None) -> None:
        self._event_sequence += 1
        event = bench_events.BenchmarkEvent.build(
            sequence=self._event_sequence, kind=kind, at=self._clock(),
            phase=phase,
            trial_id=(trial.trial_id if trial else None),
            workload_id=(trial.workload.workload_id if trial else None),
            backend=(trial.backend if trial else None),
            repetition=(trial.repetition if trial else None),
            detail=detail)
        store.append_event(self._root, event)


# --- helpers ------------------------------------------------------------------


def _manifest_payload(config: RunConfig,
                      created_at: str) -> model.RunManifest:
    return model.RunManifest.build(
        benchmark_run_id=config.benchmark_run_id,
        created_at=created_at,
        baseline_revision=config.baseline_revision,
        baseline_patch=config.baseline_patch,
        mode=config.mode,
        repetitions=config.repetitions,
        backends=config.backends,
        target_provider=config.target_provider,
        target_model=config.target_model,
        final_reviewer_model=config.final_reviewer_model,
        workload_ids=tuple(w.workload_id for w in config.workloads),
        workloads=tuple(w.to_dict() for w in config.workloads),
        environment=dict(config.environment))


def _agent_document(outcome: ExecutionOutcome,
                    workload: model.WorkloadSpec) -> dict[str, Any]:
    result = outcome.agent_result
    document: dict[str, Any] = {
        "evidence": outcome.evidence,
        "workload_class": workload.workload_class,
        "wall_ms": outcome.wall_ms,
        "unavailable_reason": outcome.unavailable_reason,
        "error": outcome.error,
    }
    if result is not None:
        document.update({
            "run_id": result.run_id,
            "status": result.status,
            "reason": result.reason,
            "transport": result.transport,
            "completion_source": (None if result.completion is None
                                  else result.completion.source),
            "completion_reliable": (False if result.completion is None
                                    else result.completion.reliable),
            "lifecycle_evidence_count": len(result.events),
        })
    return document


def detect_repository_revision(repo_root: str | None = None) -> str | None:
    """Read-only repository baseline identity (never a Git write)."""
    root = repo_root or os.getcwd()
    try:
        proc = subprocess.run(  # noqa: S603 - fixed read-only argv
            ["git", "rev-parse", "HEAD"], cwd=root,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def environment_snapshot() -> dict[str, Any]:
    return {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "executable": sys.executable,
    }


def read_trial_records(root: str | Path,
                       benchmark_run_id: str) -> list[model.TrialRecord]:
    return store.load_trials(store.run_root(root, benchmark_run_id))
