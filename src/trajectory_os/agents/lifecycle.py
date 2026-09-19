"""M022 — provider-neutral lifecycle normalization.

Every backend reports a bounded sequence of :class:`AgentEvent` values. This
module normalizes them into one machine-readable lifecycle summary, so the
same lifecycle vocabulary (started/initialized/session/idle/completed/
errored/cancelled/timed-out/shutdown) is consumed identically for Pi and
DeepSeek Harness. Normalization never invents an event: a backend that does
not emit structured lifecycle evidence simply reports fewer flags.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from trajectory_os.agents import identity as agent_identity
from trajectory_os.agents import model

#: Lifecycle flags exposed by a normalized summary (closed set).
FLAG_STARTED = "started"
FLAG_INITIALIZED = "initialized"
FLAG_SESSION_OPENED = "session_opened"
FLAG_RUNNING = "running"
FLAG_IDLE = "idle"
FLAG_RESULT = "result"
FLAG_COMPLETED = "completed"
FLAG_ERRORED = "errored"
FLAG_CANCELLED = "cancelled"
FLAG_TIMED_OUT = "timed_out"
FLAG_SHUTDOWN = "shutdown"

FLAGS = frozenset({
    FLAG_STARTED, FLAG_INITIALIZED, FLAG_SESSION_OPENED, FLAG_RUNNING,
    FLAG_IDLE, FLAG_RESULT, FLAG_COMPLETED, FLAG_ERRORED, FLAG_CANCELLED,
    FLAG_TIMED_OUT, FLAG_SHUTDOWN,
})

_KIND_TO_FLAG = {
    model.LK_STARTED: FLAG_STARTED,
    model.LK_INITIALIZED: FLAG_INITIALIZED,
    model.LK_SESSION_OPENED: FLAG_SESSION_OPENED,
    model.LK_RUNNING: FLAG_RUNNING,
    model.LK_IDLE: FLAG_IDLE,
    model.LK_RESULT: FLAG_RESULT,
    model.LK_COMPLETED: FLAG_COMPLETED,
    model.LK_ERROR: FLAG_ERRORED,
    model.LK_CANCELLED: FLAG_CANCELLED,
    model.LK_TIMEOUT: FLAG_TIMED_OUT,
    model.LK_SHUTDOWN: FLAG_SHUTDOWN,
}


@dataclass(frozen=True)
class LifecycleSummary:
    """One normalized, provider-neutral lifecycle summary."""

    backend: str
    status: str
    events_total: int
    kinds: tuple[tuple[str, int], ...]
    flags: tuple[str, ...]
    summary_id: str = ""

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "backend": self.backend,
            "status": self.status,
            "events_total": self.events_total,
            "kinds": [[kind, count] for kind, count in self.kinds],
            "flags": list(self.flags),
        }

    def compute_summary_id(self) -> str:
        return agent_identity.lifecycle_id(self.identity_payload())

    def has(self, flag: str) -> bool:
        return flag in self.flags

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "summary_id": self.summary_id}

    @staticmethod
    def normalize(backend: str, status: str,
                  events: Sequence[model.AgentEvent]) -> LifecycleSummary:
        counts: dict[str, int] = {}
        flags: set[str] = set()
        for event in events:
            counts[event.kind] = counts.get(event.kind, 0) + 1
            flag = _KIND_TO_FLAG.get(event.kind)
            if flag is not None:
                flags.add(flag)
        if status == model.RS_COMPLETED:
            flags.add(FLAG_COMPLETED)
        elif status == model.RS_FAILED:
            flags.add(FLAG_ERRORED)
        elif status == model.RS_CANCELLED:
            flags.add(FLAG_CANCELLED)
        elif status == model.RS_TIMEOUT:
            flags.add(FLAG_TIMED_OUT)
        summary = LifecycleSummary(
            backend=backend, status=status, events_total=len(events),
            kinds=tuple(sorted(counts.items())), flags=tuple(sorted(flags)))
        return replace(summary, summary_id=summary.compute_summary_id())

    @staticmethod
    def of(result: model.AgentResult) -> LifecycleSummary:
        return LifecycleSummary.normalize(
            result.backend, result.status, result.events)

    @staticmethod
    def from_dict(doc: object, path: str = "lifecycle") -> LifecycleSummary:
        if not isinstance(doc, Mapping):
            raise model.AgentBackendError(
                "MALFORMED_AGENT", path, "lifecycle object required")
        backend = doc.get("backend")
        status = doc.get("status")
        if not isinstance(backend, str) or not isinstance(status, str):
            raise model.AgentBackendError(
                "MALFORMED_AGENT", path, "backend/status required")
        raw_kinds = doc.get("kinds", [])
        raw_flags = doc.get("flags", [])
        if not isinstance(raw_kinds, list) or not isinstance(raw_flags, list):
            raise model.AgentBackendError(
                "MALFORMED_AGENT", path, "kinds/flags must be lists")
        kinds = tuple(
            (str(item[0]), int(item[1]))
            for item in raw_kinds
            if isinstance(item, list) and len(item) == 2)
        flags = tuple(str(flag) for flag in raw_flags)
        for flag in flags:
            if flag not in FLAGS:
                raise model.AgentBackendError(
                    "MALFORMED_AGENT", path, f"unknown flag {flag!r}")
        events_total = doc.get("events_total", 0)
        if isinstance(events_total, bool) or not isinstance(events_total, int):
            raise model.AgentBackendError(
                "MALFORMED_AGENT", path, "events_total must be an integer")
        summary = LifecycleSummary(
            backend=backend, status=status,
            events_total=events_total, kinds=kinds,
            flags=flags)
        expected = summary.compute_summary_id()
        stored = doc.get("summary_id", expected)
        if stored != expected:
            raise model.AgentBackendError(
                model.R_ROUTE_INVALID, path,
                f"summary_id {stored!r} != {expected!r}")
        return replace(summary, summary_id=expected)
