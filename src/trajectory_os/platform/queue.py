"""M050 — multi-mission portfolio queue and deterministic scheduler.

The queue is the durable admission surface that sits *above* the existing
mission runtime. It never re-implements mission execution: a scheduling
decision only selects which queued missions may run and persists the exact
deterministic reason for every entry. Dispatch is delegated to an explicit
callback (the existing operator control plane) so the queue orchestrates the
existing runtime/control paths.

Design invariants:

* durable, restart-safe queue with a monotonic enqueue sequence;
* explicit priority, dependencies and bounded resource requests;
* admission control over bounded concurrency, local GPU/VRAM and remote
  provider availability;
* every decision carries a deterministic reason and is persisted;
* starvation resistance via deterministic waiting-time aging;
* a mission already completed/ready in canonical state is never re-executed
  after a restart;
* pause / resume / cancel / requeue are explicit and idempotent;
* no Git trust-boundary write.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from trajectory_os.assembly import store as assembly_store
from trajectory_os.observability import model as obs_model
from trajectory_os.operator._util import (
    append_jsonl,
    digest,
    optional_str,
    read_jsonl,
    read_optional_json,
    utc_now,
    write_json,
)
from trajectory_os.platform import model
from trajectory_os.platform import projects as project_registry

#: Domain id for a scheduling decision.
DECISION_DOMAIN = "trajectory-os.platform-schedule-decision.v1"

# --- queue entry states (closed set) ------------------------------------------

Q_QUEUED = "QUEUED"
Q_ACTIVE = "ACTIVE"
Q_PAUSED = "PAUSED"
Q_CANCELLED = "CANCELLED"
Q_DONE = "DONE"
Q_FAILED = "FAILED"
Q_BLOCKED = "BLOCKED"

STATES = frozenset({
    Q_QUEUED, Q_ACTIVE, Q_PAUSED, Q_CANCELLED, Q_DONE, Q_FAILED, Q_BLOCKED,
})

#: Non-terminal states that may still be scheduled.
LIVE_STATES = frozenset({Q_QUEUED, Q_ACTIVE, Q_BLOCKED, Q_PAUSED})

# --- admission reason codes (closed set) --------------------------------------

R_SELECTED = "SELECTED"
R_DEPENDENCY_PENDING = "DEPENDENCY_PENDING"
R_DEPENDENCY_FAILED = "DEPENDENCY_FAILED"
R_CONCURRENCY_LIMIT = "CONCURRENCY_LIMIT"
R_CPU_LIMIT = "CPU_LIMIT"
R_GPU_LIMIT = "GPU_LIMIT"
R_VRAM_LIMIT = "VRAM_LIMIT"
R_BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
R_REMOTE_PROVIDER_UNAVAILABLE = "REMOTE_PROVIDER_UNAVAILABLE"
R_ALREADY_TERMINAL = "MISSION_ALREADY_TERMINAL"
R_PAUSED = "PAUSED_BY_OPERATOR"
R_STARVATION_AGED = "STARVATION_AGED"
R_LOWER_PRIORITY = "LOWER_PRIORITY"
R_NO_ACTIVE_WORK = "NO_ACTIVE_WORK"

REASONS = frozenset({
    R_SELECTED, R_DEPENDENCY_PENDING, R_DEPENDENCY_FAILED,
    R_CONCURRENCY_LIMIT, R_CPU_LIMIT, R_GPU_LIMIT, R_VRAM_LIMIT,
    R_BACKEND_UNAVAILABLE, R_REMOTE_PROVIDER_UNAVAILABLE,
    R_ALREADY_TERMINAL, R_PAUSED, R_STARVATION_AGED, R_LOWER_PRIORITY,
    R_NO_ACTIVE_WORK,
})

#: Locality (closed set).
LOCAL = "local"
REMOTE = "remote"
LOCALITIES = frozenset({LOCAL, REMOTE})

#: Canonical completions that mean "do not execute again".
_TERMINAL_READINESS = frozenset({
    obs_model.RD_READY_FOR_COMMIT, obs_model.RD_BLOCKED,
    obs_model.RD_FAILED, obs_model.RD_CANCELLED,
})

#: Aging cap applied to the starvation-resistant effective priority.
MAX_AGE_BONUS = 64


def queue_dir(root: str | Path) -> Path:
    return Path(root) / model.PLATFORM_DIR / model.QUEUE_DIR


def _state_path(root: str | Path) -> Path:
    return queue_dir(root) / model.QUEUE_STATE_NAME


def _decisions_path(root: str | Path) -> Path:
    return queue_dir(root) / model.QUEUE_DECISIONS_NAME


@dataclass(frozen=True)
class ResourceRequest:
    """Explicit bounded resource demand for one mission."""

    cpu_slots: int = 1
    gpu_slots: int = 0
    gpu_mem_bytes: int = 0
    locality: str = REMOTE
    provider: str | None = None

    def validate(self) -> ResourceRequest:
        if self.cpu_slots < 0 or self.cpu_slots > 1024:
            model.fail(model.E_QUEUE_INVALID, "cpu_slots out of bounds")
        if self.gpu_slots < 0 or self.gpu_slots > 16:
            model.fail(model.E_QUEUE_INVALID, "gpu_slots out of bounds")
        if self.gpu_mem_bytes < 0 or self.gpu_mem_bytes > (1 << 50):
            model.fail(model.E_QUEUE_INVALID, "gpu_mem_bytes out of bounds")
        if self.locality not in LOCALITIES:
            model.fail(model.E_QUEUE_INVALID, "locality")
        return self

    def consumes_gpu(self) -> bool:
        return self.locality == LOCAL and (self.gpu_slots > 0
                                           or self.gpu_mem_bytes > 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cpu_slots": self.cpu_slots,
            "gpu_slots": self.gpu_slots,
            "gpu_mem_bytes": self.gpu_mem_bytes,
            "locality": self.locality,
            "provider": self.provider,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> ResourceRequest:
        return ResourceRequest(
            cpu_slots=int(data.get("cpu_slots", 1)),
            gpu_slots=int(data.get("gpu_slots", 0)),
            gpu_mem_bytes=int(data.get("gpu_mem_bytes", 0)),
            locality=str(data.get("locality", REMOTE)),
            provider=optional_str(data.get("provider")),
        ).validate()


@dataclass(frozen=True)
class QueueEntry:
    """One durable queued mission across one project."""

    mission_id: str
    project_id: str
    priority: int = 0
    dependencies: tuple[str, ...] = ()
    resources: ResourceRequest = field(default_factory=ResourceRequest)
    backend: str | None = None
    state: str = Q_QUEUED
    enqueued_seq: int = 0
    enqueued_at: str = ""
    updated_at: str = ""
    attempts: int = 0
    last_reason: str | None = None
    objective_id: str | None = None

    def validate(self) -> QueueEntry:
        if not self.mission_id or not self.project_id:
            model.fail(model.E_QUEUE_INVALID, "mission/project required")
        if self.state not in STATES:
            model.fail(model.E_QUEUE_INVALID, f"state {self.state!r}")
        self.resources.validate()
        if self.enqueued_seq < 0 or self.attempts < 0:
            model.fail(model.E_QUEUE_INVALID, "negative counter")
        if self.mission_id in self.dependencies:
            model.fail(model.E_QUEUE_CYCLE, self.mission_id)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "project_id": self.project_id,
            "objective_id": self.objective_id,
            "priority": self.priority,
            "dependencies": list(self.dependencies),
            "resources": self.resources.to_dict(),
            "backend": self.backend,
            "state": self.state,
            "enqueued_seq": self.enqueued_seq,
            "enqueued_at": self.enqueued_at,
            "updated_at": self.updated_at,
            "attempts": self.attempts,
            "last_reason": self.last_reason,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> QueueEntry:
        raw_deps = data.get("dependencies") or []
        if not isinstance(raw_deps, list):
            model.fail(model.E_MALFORMED, "dependencies must be a list")
        resources = data.get("resources")
        resources = resources if isinstance(resources, Mapping) else {}
        return QueueEntry(
            mission_id=str(data.get("mission_id", "")),
            project_id=str(data.get("project_id", "")),
            objective_id=optional_str(data.get("objective_id")),
            priority=int(data.get("priority", 0)),
            dependencies=tuple(str(item) for item in raw_deps),
            resources=ResourceRequest.from_dict(resources),
            backend=optional_str(data.get("backend")),
            state=str(data.get("state", Q_QUEUED)),
            enqueued_seq=int(data.get("enqueued_seq", 0)),
            enqueued_at=str(data.get("enqueued_at", "")),
            updated_at=str(data.get("updated_at", "")),
            attempts=int(data.get("attempts", 0)),
            last_reason=optional_str(data.get("last_reason")),
        ).validate()


@dataclass(frozen=True)
class QueuePolicy:
    """Explicit bounded admission capacity (never probed or inferred)."""

    max_concurrent: int = 2
    cpu_slots: int = 8
    gpu_slots: int = 1
    gpu_mem_bytes: int = 8 * (1 << 30)
    max_entries: int = model.MAX_QUEUE_ENTRIES

    def validate(self) -> QueuePolicy:
        if not (1 <= self.max_concurrent <= 64):
            model.fail(model.E_QUEUE_INVALID, "max_concurrent out of bounds")
        if not (1 <= self.cpu_slots <= 1024):
            model.fail(model.E_QUEUE_INVALID, "cpu_slots out of bounds")
        if not (0 <= self.gpu_slots <= 16):
            model.fail(model.E_QUEUE_INVALID, "gpu_slots out of bounds")
        if not (0 <= self.gpu_mem_bytes <= (1 << 50)):
            model.fail(model.E_QUEUE_INVALID, "gpu_mem_bytes out of bounds")
        if not (1 <= self.max_entries <= model.MAX_QUEUE_ENTRIES):
            model.fail(model.E_QUEUE_INVALID, "max_entries out of bounds")
        return self

    def to_dict(self) -> dict[str, int]:
        return {
            "max_concurrent": self.max_concurrent,
            "cpu_slots": self.cpu_slots,
            "gpu_slots": self.gpu_slots,
            "gpu_mem_bytes": self.gpu_mem_bytes,
            "max_entries": self.max_entries,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any] | None) -> QueuePolicy:
        data = data if isinstance(data, Mapping) else {}
        return QueuePolicy(
            max_concurrent=int(data.get("max_concurrent", 2)),
            cpu_slots=int(data.get("cpu_slots", 8)),
            gpu_slots=int(data.get("gpu_slots", 1)),
            gpu_mem_bytes=int(data.get("gpu_mem_bytes", 8 * (1 << 30))),
            max_entries=int(data.get("max_entries", model.MAX_QUEUE_ENTRIES)),
        ).validate()


@dataclass(frozen=True)
class QueueState:
    """Durable queue state (project + mission admission truth)."""

    entries: tuple[QueueEntry, ...] = ()
    next_seq: int = 1
    updated_at: str = ""
    schema_version: int = model.SCHEMA_VERSION
    platform_version: str = model.PLATFORM_VERSION

    def validate(self) -> QueueState:
        if self.next_seq < 1:
            model.fail(model.E_QUEUE_INVALID, "next_seq")
        seen: set[str] = set()
        for entry in self.entries:
            entry.validate()
            if entry.mission_id in seen:
                model.fail(model.E_QUEUE_INVALID,
                           f"duplicate mission {entry.mission_id!r}")
            seen.add(entry.mission_id)
        return self

    def by_id(self, mission_id: str) -> QueueEntry | None:
        for entry in self.entries:
            if entry.mission_id == mission_id:
                return entry
        return None

    def active(self) -> tuple[QueueEntry, ...]:
        return tuple(e for e in self.entries if e.state == Q_ACTIVE)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "platform_version": self.platform_version,
            "next_seq": self.next_seq,
            "updated_at": self.updated_at,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> QueueState:
        version = data.get("schema_version", model.SCHEMA_VERSION)
        if version != model.SCHEMA_VERSION:
            model.fail(model.E_UNSUPPORTED_VERSION, str(version))
        raw = data.get("entries") or []
        if not isinstance(raw, list):
            model.fail(model.E_MALFORMED, "entries must be a list")
        return QueueState(
            entries=tuple(
                QueueEntry.from_dict(item)
                for item in raw if isinstance(item, Mapping)),
            next_seq=int(data.get("next_seq", 1)),
            updated_at=str(data.get("updated_at", "")),
        ).validate()


def load_queue(root: str | Path) -> QueueState:
    document = read_optional_json(_state_path(root))
    if document is None:
        return QueueState()
    return QueueState.from_dict(document)


def save_queue(root: str | Path, state: QueueState,
               *, clock: Callable[[], str] = utc_now) -> QueueState:
    state = replace(state, updated_at=clock()).validate()
    write_json(_state_path(root), state.to_dict())
    return state


def _replace_entry(state: QueueState, entry: QueueEntry) -> QueueState:
    entries = tuple(entry if e.mission_id == entry.mission_id else e
                    for e in state.entries)
    return replace(state, entries=entries)


# --- explicit queue operations ------------------------------------------------


def enqueue(
    root: str | Path,
    *,
    mission_id: str,
    project_id: str,
    objective_id: str | None = None,
    priority: int = 0,
    dependencies: Sequence[str] = (),
    resources: ResourceRequest | None = None,
    backend: str | None = None,
    clock: Callable[[], str] = utc_now,
) -> QueueEntry:
    """Durably enqueue one mission (idempotent on the mission id)."""
    project = project_registry.load_project(root, project_id)
    if not project.active:
        model.fail(model.E_PROJECT_ARCHIVED, project_id)
    state = load_queue(root)
    existing = state.by_id(mission_id)
    if existing is not None:
        if existing.state in (Q_CANCELLED, Q_FAILED):
            return _replace_and_save(root, state, replace(
                existing, state=Q_QUEUED, attempts=0,
                last_reason="REQUEUED", updated_at=clock()), clock)
        return existing
    if len(state.entries) >= model.MAX_QUEUE_ENTRIES:
        model.fail(model.E_QUEUE_INVALID, "queue full")
    entry = QueueEntry(
        mission_id=mission_id, project_id=project_id,
        objective_id=objective_id, priority=priority,
        dependencies=tuple(dependencies),
        resources=(resources or ResourceRequest()).validate(),
        backend=backend, state=Q_QUEUED,
        enqueued_seq=state.next_seq, enqueued_at=clock(),
        updated_at=clock()).validate()
    state = replace(state, entries=(*state.entries, entry),
                    next_seq=state.next_seq + 1)
    save_queue(root, state, clock=clock)
    return entry


def _replace_and_save(root: str | Path, state: QueueState, entry: QueueEntry,
                      clock: Callable[[], str]) -> QueueEntry:
    entry = entry.validate()
    save_queue(root, _replace_entry(state, entry), clock=clock)
    return entry


def pause(root: str | Path, mission_id: str, *, reason: str = "OPERATOR_PAUSE",
          clock: Callable[[], str] = utc_now) -> QueueEntry:
    state = load_queue(root)
    entry = _require_entry(state, mission_id)
    if entry.state == Q_PAUSED:
        return entry
    if entry.state in (Q_DONE, Q_CANCELLED, Q_FAILED):
        model.fail(model.E_QUEUE_INVALID, f"cannot pause {entry.state}")
    updated = replace(entry, state=Q_PAUSED, last_reason=reason,
                      updated_at=clock()).validate()
    return _replace_and_save(root, state, updated, clock)


def resume(root: str | Path, mission_id: str, *,
           clock: Callable[[], str] = utc_now) -> QueueEntry:
    state = load_queue(root)
    entry = _require_entry(state, mission_id)
    if entry.state != Q_PAUSED:
        return entry
    updated = replace(entry, state=Q_QUEUED, last_reason="RESUMED",
                      updated_at=clock()).validate()
    return _replace_and_save(root, state, updated, clock)


def cancel(root: str | Path, mission_id: str, *,
           reason: str = "OPERATOR_CANCEL",
           clock: Callable[[], str] = utc_now) -> QueueEntry:
    state = load_queue(root)
    entry = _require_entry(state, mission_id)
    if entry.state == Q_CANCELLED:
        return entry
    updated = replace(entry, state=Q_CANCELLED, last_reason=reason,
                      updated_at=clock()).validate()
    return _replace_and_save(root, state, updated, clock)


def requeue(root: str | Path, mission_id: str, *,
            reason: str = "OPERATOR_REQUEUE",
            clock: Callable[[], str] = utc_now) -> QueueEntry:
    state = load_queue(root)
    entry = _require_entry(state, mission_id)
    if entry.state in (Q_ACTIVE, Q_QUEUED):
        return entry
    updated = replace(entry, state=Q_QUEUED, last_reason=reason,
                      updated_at=clock()).validate()
    return _replace_and_save(root, state, updated, clock)


def mark_state(root: str | Path, mission_id: str, state: str, *,
               reason: str | None = None,
               clock: Callable[[], str] = utc_now) -> QueueEntry:
    """Record the outcome of a dispatched mission (idempotent)."""
    if state not in STATES:
        model.fail(model.E_QUEUE_INVALID, state)
    current = load_queue(root)
    entry = _require_entry(current, mission_id)
    updated = replace(entry, state=state, last_reason=reason,
                      updated_at=clock()).validate()
    return _replace_and_save(root, current, updated, clock)


def _require_entry(state: QueueState, mission_id: str) -> QueueEntry:
    entry = state.by_id(mission_id)
    if entry is None:
        model.fail(model.E_QUEUE_MISSING, mission_id)
    return entry


def _validate_dependencies(state: QueueState) -> None:
    ids = {entry.mission_id for entry in state.entries}
    # Unknown dependencies are treated as permanently pending, never a crash;
    # only a cycle among known members is a hard failure.
    graph = {entry.mission_id: tuple(
        dep for dep in entry.dependencies if dep in ids)
        for entry in state.entries}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            model.fail(model.E_QUEUE_CYCLE, node)
        visiting.add(node)
        for dep in graph.get(node, ()):
            visit(dep)
        visiting.discard(node)
        visited.add(node)

    for node in sorted(graph):
        visit(node)


# --- canonical reconciliation -------------------------------------------------


def canonical_terminal(root: str | Path, mission_id: str) -> str | None:
    """Return a canonical terminal readiness, or ``None`` if still live."""
    mission_root = Path(assembly_store.mission_root(root, mission_id))
    status = read_optional_json(mission_root / "status.json")
    if status is None:
        return None
    readiness = optional_str(status.get("readiness"))
    if readiness in _TERMINAL_READINESS:
        return readiness
    return None


def _sync_canonical(root: str | Path, state: QueueState,
                    clock: Callable[[], str]) -> QueueState:
    """Mark queue entries terminal when canonical state already is (no replay)."""
    changed = False
    entries: list[QueueEntry] = []
    for entry in state.entries:
        readiness = canonical_terminal(root, entry.mission_id)
        if readiness is not None and entry.state not in (Q_DONE, Q_FAILED):
            target = (Q_DONE if readiness == obs_model.RD_READY_FOR_COMMIT
                      else Q_FAILED)
            entries.append(replace(
                entry, state=target, last_reason=R_ALREADY_TERMINAL,
                updated_at=clock()).validate())
            changed = True
        else:
            entries.append(entry)
    if changed:
        state = save_queue(root, replace(state, entries=tuple(entries)),
                           clock=clock)
    return state


# --- deterministic scheduler --------------------------------------------------


@dataclass(frozen=True)
class Admission:
    """One entry's deterministic admission outcome."""

    mission_id: str
    project_id: str
    admitted: bool
    reason: str
    effective_priority: int
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "project_id": self.project_id,
            "admitted": self.admitted,
            "reason": self.reason,
            "effective_priority": self.effective_priority,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class SchedulingDecision:
    """One persisted deterministic scheduling decision."""

    decision_id: str
    reason: str
    selected: tuple[str, ...]
    admissions: tuple[Admission, ...]
    active_before: int
    active_after: int
    capacity: Mapping[str, Any]
    created_at: str
    complete: bool = False
    schema_version: int = model.SCHEMA_VERSION
    platform_version: str = model.PLATFORM_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "platform_version": self.platform_version,
            "decision_id": self.decision_id,
            "reason": self.reason,
            "selected": list(self.selected),
            "admissions": [a.to_dict() for a in self.admissions],
            "active_before": self.active_before,
            "active_after": self.active_after,
            "capacity": dict(sorted(self.capacity.items())),
            "created_at": self.created_at,
            "complete": self.complete,
        }


def _usage(entries: Sequence[QueueEntry]) -> dict[str, int]:
    cpu = sum(e.resources.cpu_slots for e in entries)
    gpu = sum(e.resources.gpu_slots for e in entries if e.resources.consumes_gpu())
    vram = sum(e.resources.gpu_mem_bytes for e in entries
               if e.resources.consumes_gpu())
    return {"cpu_slots": cpu, "gpu_slots": gpu, "gpu_mem_bytes": vram,
            "active": len(entries)}


def resource_accounting(root: str | Path,
                        policy: QueuePolicy | None = None) -> dict[str, Any]:
    """Read-only resource accounting for the queue (visible in operator state)."""
    state = load_queue(root)
    resolved = (policy or QueuePolicy()).validate()
    active = state.active()
    usage = _usage(active)
    return {
        "schema_version": model.SCHEMA_VERSION,
        "platform_version": model.PLATFORM_VERSION,
        "policy": resolved.to_dict(),
        "active": [entry.mission_id for entry in active],
        "usage": usage,
        "queued": len([e for e in state.entries if e.state == Q_QUEUED]),
        "paused": len([e for e in state.entries if e.state == Q_PAUSED]),
        "blocked": len([e for e in state.entries if e.state == Q_BLOCKED]),
        "done": len([e for e in state.entries if e.state == Q_DONE]),
        "failed": len([e for e in state.entries if e.state == Q_FAILED]),
        "cancelled": len([e for e in state.entries if e.state == Q_CANCELLED]),
        "read_only": True,
    }


def schedule_once(
    root: str | Path,
    *,
    policy: QueuePolicy | None = None,
    backend_available: Mapping[str, bool] | None = None,
    provider_available: Mapping[str, bool] | None = None,
    dispatch: bool = False,
    dispatch_fn: Callable[[str], None] | None = None,
    clock: Callable[[], str] = utc_now,
) -> SchedulingDecision:
    """Compute, persist and optionally dispatch one deterministic decision.

    Selected missions are durably marked ``Q_ACTIVE`` (admitted/claimed) so
    repeated cycles can neither double-admit them nor exceed the bounded
    resource budget. When ``dispatch`` is true *and* ``dispatch_fn`` is given,
    the selected missions are handed to it; otherwise the persisted
    :class:`SchedulingDecision` append-only log is the durable dispatch record
    consumed by the operator control plane (ADR-026).
    """
    resolved = (policy or QueuePolicy()).validate()
    availability = dict(backend_available or {})
    providers = dict(provider_available or {})
    state = load_queue(root)
    _validate_dependencies(state)
    state = _sync_canonical(root, state, clock)

    active = list(state.active())
    usage = _usage(active)
    active_ids = {entry.mission_id for entry in active}
    completed = {entry.mission_id for entry in state.entries
                 if entry.state == Q_DONE}
    failed = {entry.mission_id for entry in state.entries
              if entry.state == Q_FAILED}

    candidates = [e for e in state.entries
                  if e.state in (Q_QUEUED, Q_BLOCKED)]
    max_seq = max((e.enqueued_seq for e in candidates), default=state.next_seq)
    admissions: list[Admission] = []
    selected: list[str] = []
    admitted_local = dict(usage)

    def effective_priority(entry: QueueEntry) -> int:
        age = max(0, max_seq - entry.enqueued_seq)
        return entry.priority + min(age, MAX_AGE_BONUS)

    ranked = sorted(
        candidates,
        key=lambda e: (-effective_priority(e), e.enqueued_seq, e.mission_id))

    for entry in ranked:
        priority = effective_priority(entry)
        dependency_reason = _dependency_reason(entry, completed, failed)
        if dependency_reason is not None:
            admissions.append(Admission(
                mission_id=entry.mission_id, project_id=entry.project_id,
                admitted=False, reason=dependency_reason,
                effective_priority=priority))
            continue
        if entry.state == Q_PAUSED:
            admissions.append(Admission(
                mission_id=entry.mission_id, project_id=entry.project_id,
                admitted=False, reason=R_PAUSED,
                effective_priority=priority))
            continue
        if entry.state == Q_BLOCKED:
            entry = replace(entry, state=Q_QUEUED)
        if len(active_ids) + len(selected) >= resolved.max_concurrent:
            admissions.append(Admission(
                mission_id=entry.mission_id, project_id=entry.project_id,
                admitted=False, reason=R_CONCURRENCY_LIMIT,
                effective_priority=priority))
            continue
        if entry.backend and availability.get(entry.backend) is False:
            admissions.append(Admission(
                mission_id=entry.mission_id, project_id=entry.project_id,
                admitted=False, reason=R_BACKEND_UNAVAILABLE,
                effective_priority=priority, detail=entry.backend))
            continue
        if (entry.resources.provider
                and providers.get(entry.resources.provider) is False):
            admissions.append(Admission(
                mission_id=entry.mission_id, project_id=entry.project_id,
                admitted=False, reason=R_REMOTE_PROVIDER_UNAVAILABLE,
                effective_priority=priority, detail=entry.resources.provider))
            continue
        if admitted_local["cpu_slots"] + entry.resources.cpu_slots > (
                resolved.cpu_slots):
            admissions.append(Admission(
                mission_id=entry.mission_id, project_id=entry.project_id,
                admitted=False, reason=R_CPU_LIMIT,
                effective_priority=priority))
            continue
        if entry.resources.consumes_gpu():
            if admitted_local["gpu_slots"] + entry.resources.gpu_slots > (
                    resolved.gpu_slots):
                admissions.append(Admission(
                    mission_id=entry.mission_id, project_id=entry.project_id,
                    admitted=False, reason=R_GPU_LIMIT,
                    effective_priority=priority))
                continue
            if admitted_local["gpu_mem_bytes"] + (
                    entry.resources.gpu_mem_bytes) > resolved.gpu_mem_bytes:
                admissions.append(Admission(
                    mission_id=entry.mission_id, project_id=entry.project_id,
                    admitted=False, reason=R_VRAM_LIMIT,
                    effective_priority=priority))
                continue
        reason = (R_STARVATION_AGED
                  if effective_priority(entry) > entry.priority
                  else R_SELECTED)
        admissions.append(Admission(
            mission_id=entry.mission_id, project_id=entry.project_id,
            admitted=True, reason=reason, effective_priority=priority))
        selected.append(entry.mission_id)
        admitted_local["cpu_slots"] += entry.resources.cpu_slots
        admitted_local["gpu_slots"] += (
            entry.resources.gpu_slots if entry.resources.consumes_gpu() else 0)
        admitted_local["gpu_mem_bytes"] += (
            entry.resources.gpu_mem_bytes
            if entry.resources.consumes_gpu() else 0)

    # mark selected as active, record reasons
    entries: list[QueueEntry] = []
    for entry in state.entries:
        if entry.mission_id in selected:
            entries.append(replace(
                entry, state=Q_ACTIVE, attempts=entry.attempts + 1,
                last_reason=(R_STARVATION_AGED
                             if effective_priority(entry) > entry.priority
                             else R_SELECTED),
                updated_at=clock()).validate())
        else:
            entry_reason = next((a.reason for a in admissions
                                 if a.mission_id == entry.mission_id), None)
            if entry_reason in (R_DEPENDENCY_PENDING, R_DEPENDENCY_FAILED,
                                R_CONCURRENCY_LIMIT, R_CPU_LIMIT, R_GPU_LIMIT,
                                R_VRAM_LIMIT, R_BACKEND_UNAVAILABLE,
                                R_REMOTE_PROVIDER_UNAVAILABLE):
                entries.append(replace(
                    entry, state=(Q_BLOCKED
                                  if entry_reason in (
                                      R_DEPENDENCY_PENDING,
                                      R_DEPENDENCY_FAILED,
                                      R_BACKEND_UNAVAILABLE,
                                      R_REMOTE_PROVIDER_UNAVAILABLE)
                                  else entry.state),
                    last_reason=entry_reason, updated_at=clock()).validate())
            else:
                entries.append(entry)
    state = replace(state, entries=tuple(entries))
    save_queue(root, state, clock=clock)

    active_after = len(selected) + len(active)
    if not candidates and not active:
        overall = R_NO_ACTIVE_WORK
    elif selected:
        overall = R_SELECTED
    else:
        overall = admissions[0].reason if admissions else R_NO_ACTIVE_WORK
    capacity = {
        "policy": resolved.to_dict(),
        "usage_before": usage,
        "usage_after": admitted_local,
    }
    decision = SchedulingDecision(
        decision_id=digest({
            "selected": sorted(selected),
            "active_before": len(active),
            "active_after": active_after,
            "capacity": capacity,
        }, domain=DECISION_DOMAIN),
        reason=overall, selected=tuple(selected),
        admissions=tuple(admissions), active_before=len(active),
        active_after=active_after, capacity=capacity, created_at=clock(),
        complete=not candidates and not active)
    append_jsonl(_decisions_path(root), decision.to_dict())
    if dispatch and dispatch_fn is not None:
        for mission_id in selected:
            dispatch_fn(mission_id)
    return decision


def _dependency_reason(entry: QueueEntry, completed: set[str],
                       failed: set[str]) -> str | None:
    for dep in entry.dependencies:
        if dep in failed:
            return R_DEPENDENCY_FAILED
        if dep not in completed:
            return R_DEPENDENCY_PENDING
    return None


def decisions(root: str | Path) -> list[dict[str, Any]]:
    """Read the append-only scheduling decision history (read-only)."""
    return read_jsonl(_decisions_path(root))


def reconstruct(root: str | Path) -> dict[str, Any]:
    """Deterministically reconstruct queue state and validate dependencies."""
    state = load_queue(root)
    state.validate()
    _validate_dependencies(state)
    return {
        "schema_version": model.SCHEMA_VERSION,
        "platform_version": model.PLATFORM_VERSION,
        "entries": len(state.entries),
        "next_seq": state.next_seq,
        "decision_count": len(decisions(root)),
        "reconstructed": True,
    }


__all__ = [
    "DECISION_DOMAIN",
    "LIVE_STATES",
    "LOCAL",
    "LOCALITIES",
    "MAX_AGE_BONUS",
    "Q_ACTIVE",
    "Q_BLOCKED",
    "Q_CANCELLED",
    "Q_DONE",
    "Q_FAILED",
    "Q_PAUSED",
    "Q_QUEUED",
    "REASONS",
    "REMOTE",
    "R_ALREADY_TERMINAL",
    "R_BACKEND_UNAVAILABLE",
    "R_CONCURRENCY_LIMIT",
    "R_CPU_LIMIT",
    "R_DEPENDENCY_FAILED",
    "R_DEPENDENCY_PENDING",
    "R_GPU_LIMIT",
    "R_LOWER_PRIORITY",
    "R_NO_ACTIVE_WORK",
    "R_PAUSED",
    "R_REMOTE_PROVIDER_UNAVAILABLE",
    "R_SELECTED",
    "R_STARVATION_AGED",
    "R_VRAM_LIMIT",
    "STATES",
    "Admission",
    "QueueEntry",
    "QueuePolicy",
    "QueueState",
    "ResourceRequest",
    "SchedulingDecision",
    "canonical_terminal",
    "cancel",
    "decisions",
    "enqueue",
    "load_queue",
    "mark_state",
    "pause",
    "queue_dir",
    "reconstruct",
    "requeue",
    "resource_accounting",
    "resume",
    "save_queue",
    "schedule_once",
]
