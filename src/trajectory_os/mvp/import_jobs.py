"""MVP — background import jobs with real progress, cancel and a report.

The initial factual import runs outside the HTTP request thread so the cockpit
can show real backend progress (stage, chunk, percent, elapsed, ETA, engine,
cost, fallback count) instead of a fake animation. Everything stays in memory,
bounded and dependency-light: no queue service, no database, no background
framework.

The job layer never chooses an engine silently. It resolves the caller's
explicit selection through :mod:`trajectory_os.mvp.import_engine`, and a
failed selected engine is surfaced as a clear failure so the UI can offer the
user a fallback *choice*.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from trajectory_os.mvp import import_engine, importer

#: Job lifecycle statuses.
JOB_QUEUED = "QUEUED"
JOB_RUNNING = "RUNNING"
JOB_COMPLETED = "COMPLETED"
JOB_FAILED = "FAILED"
JOB_CANCELLED = "CANCELLED"

_MAX_JOBS = 20


class ImportJobError(Exception):
    """A job cannot be created or found (fail closed)."""


@dataclass
class ImportJob:
    """A live import job snapshot (mutated only under the manager lock)."""

    job_id: str
    filename: str
    engine: str
    model: str
    mode: str
    pricing_state: str
    status: str = JOB_QUEUED
    stage: str = importer.STAGE_QUEUED
    message: str = "Queued"
    chunks_total: int = 0
    chunks_done: int = 0
    percent: int = 0
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    elapsed_ms: int = 0
    eta_ms: int | None = None
    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    cost_so_far_usd: float = 0.0
    fallback_count: int = 0
    last_activity: str = "Queued"
    error: str | None = None
    cancel_requested: bool = False
    draft_id: str | None = None
    analysis: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        report = None
        if self.analysis is not None:
            report = self.analysis.get("completion_report")
        return {
            "job_id": self.job_id,
            "filename": self.filename,
            "engine": self.engine,
            "model": self.model,
            "mode": self.mode,
            "pricing_state": self.pricing_state,
            "status": self.status,
            "stage": self.stage,
            "message": self.message,
            "chunks_total": self.chunks_total,
            "chunks_done": self.chunks_done,
            "percent": self.percent,
            "elapsed_ms": self.elapsed_ms,
            "eta_ms": self.eta_ms,
            "api_calls": self.api_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "cost_so_far_usd": self.cost_so_far_usd,
            "fallback_count": self.fallback_count,
            "last_activity": self.last_activity,
            "error": self.error,
            "cancel_requested": self.cancel_requested,
            "draft_id": self.draft_id,
            "analysis": self.analysis,
            "completion_report": report,
        }


class ImportJobManager:
    """Bounded, in-memory registry of import jobs (thread-safe)."""

    def __init__(
        self,
        root: str,
        *,
        llm: Callable[[str, str], Mapping[str, Any]] | None = None,
        now: Callable[[], datetime] | None = None,
        max_jobs: int = _MAX_JOBS,
    ) -> None:
        self.root = root
        self._llm_override = llm
        self._now = now or (lambda: datetime.now(tz=UTC))
        self._max_jobs = max(1, max_jobs)
        self._jobs: dict[str, ImportJob] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    # --- public API ----------------------------------------------------------

    def start(
        self,
        filename: str,
        data: bytes,
        *,
        engine: str | None = None,
        mode: str = importer.MODE_FACTUAL,
    ) -> ImportJob:
        """Resolve the explicit engine and start a background job."""
        if mode not in importer.MODES:
            raise ImportJobError(f"unknown import mode {mode!r}")
        now = self._now()
        api_key = bool(os.environ.get("DEEPSEEK_API_KEY"))
        try:
            selection = import_engine.resolve_selection(
                engine, now=now, api_key_configured=api_key,
                chars=_safe_len(data))
        except import_engine.EngineError as exc:
            raise ImportJobError(str(exc)) from exc
        try:
            llm = self._resolve_llm(selection.engine)
        except importer.PortfolioImportError as exc:
            raise ImportJobError(str(exc)) from exc

        job = ImportJob(
            job_id=uuid.uuid4().hex[:16],
            filename=filename,
            engine=selection.engine,
            model=selection.model,
            mode=mode,
            pricing_state=selection.pricing_state,
            estimated_cost_usd=selection.estimated_cost_usd,
        )
        with self._lock:
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)
            self._prune_locked()
        thread = threading.Thread(
            target=self._run, args=(job.job_id, filename, data, llm, selection,
                                    mode),
            name=f"trajectory-import-{job.job_id}", daemon=True)
        thread.start()
        return self._snapshot(job.job_id)

    def get(self, job_id: str) -> ImportJob:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise ImportJobError(f"unknown import job {job_id!r}")
            return _clone(job)

    def cancel(self, job_id: str) -> ImportJob:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise ImportJobError(f"unknown import job {job_id!r}")
            job.cancel_requested = True
            if job.status == JOB_QUEUED:
                job.status = JOB_CANCELLED
                job.stage = importer.STAGE_CANCELLED
                job.message = "Cancelled before start"
                job.last_activity = job.message
                job.finished_at = time.monotonic()
                job.elapsed_ms = int(
                    (job.finished_at - job.started_at) * 1000)
                job.percent = 100
            elif job.status == JOB_RUNNING:
                job.message = "Cancellation requested"
                job.last_activity = job.message
            return _clone(job)

    # --- internals -----------------------------------------------------------

    def _resolve_llm(
        self, engine: str) -> Callable[[str, str], Mapping[str, Any]] | None:
        if self._llm_override is not None:
            return self._llm_override
        llm, _engine_id, _model = importer.make_engine_llm(engine)
        return llm

    def _run(
        self,
        job_id: str,
        filename: str,
        data: bytes,
        llm: Callable[[str, str], Mapping[str, Any]] | None,
        selection: import_engine.EngineSelection,
        mode: str,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.cancel_requested:
                return
            job.status = JOB_RUNNING
            job.stage = importer.STAGE_EXTRACTING
            job.message = "Starting"
            job.last_activity = job.message
            job.started_at = time.monotonic()

        def on_progress(progress: importer.ImportProgress) -> None:
            self._on_progress(job_id, progress)

        def cancel() -> bool:
            with self._lock:
                job = self._jobs.get(job_id)
                return job is None or job.cancel_requested

        try:
            analysis = importer.analyze_document(
                self.root, filename, data, llm=llm, mode=mode,
                engine=selection.engine, model=selection.model,
                pricing_state=selection.pricing_state,
                progress=on_progress, cancel=cancel)
        except importer.ImportCancelled:
            self._finish(job_id, JOB_CANCELLED, importer.STAGE_CANCELLED,
                         message="Cancelled by user")
            return
        except importer.PortfolioImportError as exc:
            self._finish(job_id, JOB_FAILED, importer.STAGE_FAILED,
                         message=f"Import failed: {exc}", error=str(exc))
            return
        except Exception as exc:  # pragma: no cover - defensive
            self._finish(job_id, JOB_FAILED, importer.STAGE_FAILED,
                         message=f"Import failed: {exc}", error=str(exc))
            return

        try:
            draft_id = importer.save_draft(self.root, analysis)
        except Exception as exc:  # pragma: no cover - defensive
            self._finish(job_id, JOB_FAILED, importer.STAGE_FAILED,
                         message=f"Could not save draft: {exc}",
                         error=str(exc))
            return

        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = JOB_COMPLETED
            job.stage = importer.STAGE_COMPLETED
            job.message = "Completed"
            job.last_activity = job.message
            job.finished_at = time.monotonic()
            job.elapsed_ms = int((job.finished_at - job.started_at) * 1000)
            job.percent = 100
            job.eta_ms = 0
            job.draft_id = draft_id
            job.api_calls = analysis.api_calls
            job.input_tokens = analysis.input_tokens
            job.output_tokens = analysis.output_tokens
            job.fallback_count = analysis.fallback_count
            job.cost_so_far_usd = analysis.estimated_cost_usd
            job.analysis = analysis.to_dict()

    def _on_progress(self, job_id: str,
                     progress: importer.ImportProgress) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.stage = progress.stage
            job.message = progress.message
            job.last_activity = progress.message
            job.chunks_total = progress.chunks_total
            job.chunks_done = progress.chunks_done
            job.percent = progress.percent
            job.api_calls = progress.api_calls
            job.fallback_count = progress.fallback_count
            job.elapsed_ms = int((time.monotonic() - job.started_at) * 1000)
            if (progress.stage == importer.STAGE_CLASSIFYING
                    and progress.chunks_done > 0
                    and progress.chunks_total > progress.chunks_done):
                per_chunk = job.elapsed_ms / progress.chunks_done
                remaining = progress.chunks_total - progress.chunks_done
                job.eta_ms = int(per_chunk * remaining)
            if job.estimated_cost_usd > 0:
                job.cost_so_far_usd = round(
                    job.estimated_cost_usd * progress.percent / 100.0, 6)

    def _finish(self, job_id: str, status: str, stage: str, *,
                message: str, error: str | None = None) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = status
            job.stage = stage
            job.message = message
            job.last_activity = message
            job.error = error
            job.finished_at = time.monotonic()
            job.elapsed_ms = int((job.finished_at - job.started_at) * 1000)
            job.percent = 100

    def _snapshot(self, job_id: str) -> ImportJob:
        with self._lock:
            return _clone(self._jobs[job_id])

    def _prune_locked(self) -> None:
        while len(self._order) > self._max_jobs:
            oldest = self._order.pop(0)
            self._jobs.pop(oldest, None)


def _clone(job: ImportJob) -> ImportJob:
    return ImportJob(**{**job.__dict__})


def _safe_len(data: bytes) -> int:
    return len(data) if isinstance(data, bytes) else 0


__all__ = [
    "JOB_QUEUED", "JOB_RUNNING", "JOB_COMPLETED", "JOB_FAILED",
    "JOB_CANCELLED", "ImportJob", "ImportJobManager", "ImportJobError",
]
