"""M029 — canonical benchmark event stream (append-only, restart-safe).

Events are derived from the exact persisted trial/manifest state and carry a
monotonic sequence. The stream is the operator-visible timeline; it is a
projection of durable state, never a second source of truth.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from trajectory_os.benchmark import identity, model

#: Event kinds (closed set).
KIND_RUN_STARTED = "RUN_STARTED"
KIND_TRIAL_STARTED = "TRIAL_STARTED"
KIND_TRIAL_COMPLETED = "TRIAL_COMPLETED"
KIND_RUN_RESUMED = "RUN_RESUMED"
KIND_RUN_COMPLETED = "RUN_COMPLETED"
KIND_RUN_CANCELLED = "RUN_CANCELLED"

EVENT_KINDS = frozenset({
    KIND_RUN_STARTED, KIND_TRIAL_STARTED, KIND_TRIAL_COMPLETED,
    KIND_RUN_RESUMED, KIND_RUN_COMPLETED, KIND_RUN_CANCELLED,
})

#: Phases (closed set) shown by the status document.
PHASE_QUALIFY = "QUALIFY"
PHASE_EXECUTE = "EXECUTE"
PHASE_VALIDATE = "VALIDATE"
PHASE_REVIEW = "REVIEW"
PHASE_AGGREGATE = "AGGREGATE"
PHASE_DONE = "DONE"

PHASES = frozenset({
    PHASE_QUALIFY, PHASE_EXECUTE, PHASE_VALIDATE, PHASE_REVIEW,
    PHASE_AGGREGATE, PHASE_DONE,
})


@dataclass(frozen=True)
class BenchmarkEvent:
    sequence: int
    kind: str
    at: str
    phase: str
    trial_id: str | None = None
    workload_id: str | None = None
    backend: str | None = None
    repetition: int | None = None
    detail: Mapping[str, Any] | None = None
    event_id: str = ""

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "sequence": self.sequence,
            "kind": self.kind,
            "at": self.at,
            "phase": self.phase,
            "trial_id": self.trial_id,
            "workload_id": self.workload_id,
            "backend": self.backend,
            "repetition": self.repetition,
            "detail": (None if self.detail is None else dict(self.detail)),
        }

    def compute_event_id(self) -> str:
        return identity.event_id(self.identity_payload())

    @staticmethod
    def build(**kwargs: Any) -> BenchmarkEvent:
        base = BenchmarkEvent(event_id="", **kwargs)
        if base.kind not in EVENT_KINDS:
            raise model.BenchmarkError("MALFORMED_EVENT", base.kind)
        if base.phase not in PHASES:
            raise model.BenchmarkError("MALFORMED_EVENT", base.phase)
        return replace(base, event_id=base.compute_event_id())

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "event_id": self.event_id}
