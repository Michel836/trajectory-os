"""M049 — terminal-independent persistent supervisor / daemon.

The supervisor is a durable, single-owner long-running process that drives the
platform scheduler between cycles. It owns **no** authoritative work state:
every cycle is delegated to the canonical scheduler / project registry /
mission runtime, and the supervisor persists only its own bounded process
accounting and heartbeat.

Design invariants:

* **single owner** — ownership is an atomic ``O_EXCL`` lock carrying an
  explicit process identity (pid, host, token, start time); a second live
  owner fails closed;
* **crash detection and startup recovery** — a lock whose owner is dead is
  detected as a crash, claimed explicitly, and the supervisor reconstructs
  from the canonical project/mission state;
* **clean shutdown** — an explicit stop request is honoured between cycles;
  an in-flight bounded cycle is never killed;
* **terminal independence** — the process holds no interactive terminal and
  the CLI can start it detached in its own session;
* **no Git trust-boundary write** — this module never performs a release Git
  write.
"""

from __future__ import annotations

import json
import os
import socket
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any

from trajectory_os.operator._util import (
    digest,
    optional_int,
    optional_str,
    read_optional_json,
    utc_now,
    write_json,
)
from trajectory_os.platform import model
from trajectory_os.platform import projects as project_registry

#: Domain id for a supervisor ownership token.
OWNER_DOMAIN = "trajectory-os.platform-supervisor-owner.v1"

# --- supervisor statuses (closed set) ----------------------------------------

SS_RUNNING = "RUNNING"
SS_STOPPED = "STOPPED"
SS_CRASHED = "CRASHED"
SS_RECOVERED = "RECOVERED"
SS_COMPLETE = "COMPLETE"
SS_ERROR = "ERROR"

STATUSES = frozenset({
    SS_RUNNING, SS_STOPPED, SS_CRASHED, SS_RECOVERED, SS_COMPLETE, SS_ERROR,
})

#: Ownership claim outcomes (closed set).
CLAIM_FRESH = "FRESH"
CLAIM_RECOVERED = "RECOVERED_STALE"
CLAIM_DENIED = "DENIED_LIVE_OWNER"


def host_identity() -> str:
    return socket.gethostname() or "localhost"


def process_alive(pid: int) -> bool:
    """Best-effort liveness probe for a recorded pid (never raises)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def supervisor_dir(root: str | Path) -> Path:
    return Path(root) / model.PLATFORM_DIR / model.SUPERVISOR_DIR


def _path(root: str | Path, name: str) -> Path:
    return supervisor_dir(root) / name


def owner_path(root: str | Path) -> Path:
    """Explicit path of the ownership lock (public for diagnostics)."""
    return _path(root, model.SUPERVISOR_OWNER_NAME)


@dataclass(frozen=True)
class OwnerIdentity:
    """The explicit identity of the process that owns the supervisor."""

    pid: int
    host: str
    token: str
    started_at: str
    root: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "host": self.host,
            "token": self.token,
            "started_at": self.started_at,
            "root": self.root,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> OwnerIdentity:
        pid = optional_int(data.get("pid"))
        if pid is None:
            model.fail(model.E_MALFORMED, "owner pid required")
        return OwnerIdentity(
            pid=pid,
            host=str(data.get("host", "")),
            token=str(data.get("token", "")),
            started_at=str(data.get("started_at", "")),
            root=str(data.get("root", "")),
        )


@dataclass(frozen=True)
class SupervisorState:
    """Durable supervisor runtime accounting (never a work source)."""

    status: str
    owner: OwnerIdentity | None
    cycles: int
    started_at: str
    updated_at: str
    heartbeat_at: str | None
    stop_requested: bool
    last_reason: str | None
    project_ids: tuple[str, ...]
    schema_version: int = model.SCHEMA_VERSION
    platform_version: str = model.PLATFORM_VERSION

    def validate(self) -> SupervisorState:
        if self.status not in STATUSES:
            model.fail(model.E_MALFORMED, f"status {self.status!r}")
        if self.cycles < 0:
            model.fail(model.E_MALFORMED, "negative cycles")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "platform_version": self.platform_version,
            "status": self.status,
            "owner": None if self.owner is None else self.owner.to_dict(),
            "cycles": self.cycles,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "heartbeat_at": self.heartbeat_at,
            "stop_requested": self.stop_requested,
            "last_reason": self.last_reason,
            "project_ids": list(self.project_ids),
        }

    @staticmethod
    def initial(*, started_at: str, owner: OwnerIdentity,
                project_ids: tuple[str, ...]) -> SupervisorState:
        return SupervisorState(
            status=SS_RUNNING, owner=owner, cycles=0, started_at=started_at,
            updated_at=started_at, heartbeat_at=None, stop_requested=False,
            last_reason=None, project_ids=project_ids).validate()

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> SupervisorState:
        version = data.get("schema_version", model.SCHEMA_VERSION)
        if version != model.SCHEMA_VERSION:
            model.fail(model.E_UNSUPPORTED_VERSION, str(version))
        raw_owner = data.get("owner")
        owner = (None if raw_owner is None
                 else OwnerIdentity.from_dict(raw_owner))
        raw_ids = data.get("project_ids") or []
        if not isinstance(raw_ids, list):
            model.fail(model.E_MALFORMED, "project_ids must be a list")
        return SupervisorState(
            status=str(data.get("status", "")),
            owner=owner,
            cycles=int(data.get("cycles", 0)),
            started_at=str(data.get("started_at", "")),
            updated_at=str(data.get("updated_at", "")),
            heartbeat_at=optional_str(data.get("heartbeat_at")),
            stop_requested=bool(data.get("stop_requested", False)),
            last_reason=optional_str(data.get("last_reason")),
            project_ids=tuple(str(item) for item in raw_ids),
        ).validate()


# --- ownership ----------------------------------------------------------------


def build_owner(root: str | Path, *, pid: int | None = None,
                token: str | None = None, host: str | None = None,
                started_at: str | None = None) -> OwnerIdentity:
    pid_value = os.getpid() if pid is None else pid
    started = started_at or utc_now()
    ident = OwnerIdentity(
        pid=pid_value, host=host or host_identity(),
        token=token or digest(
            {"pid": pid_value, "root": str(root), "started_at": started},
            domain=OWNER_DOMAIN),
        started_at=started, root=str(root))
    return ident


def read_owner(root: str | Path) -> OwnerIdentity | None:
    document = read_optional_json(_path(root, model.SUPERVISOR_OWNER_NAME))
    if document is None:
        return None
    try:
        return OwnerIdentity.from_dict(document)
    except model.PlatformError:
        return None


def claim_ownership(root: str | Path, owner: OwnerIdentity,
                    ) -> tuple[str, OwnerIdentity | None]:
    """Atomically claim single-owner ownership of the supervisor.

    Returns ``(outcome, previous_owner)``. ``DENIED_LIVE_OWNER`` means another
    live process already owns the supervisor.
    """
    path = _path(root, model.SUPERVISOR_OWNER_NAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = read_owner(root)
    if previous is not None and previous.pid != owner.pid:
        if process_alive(previous.pid):
            return CLAIM_DENIED, previous
        # dead owner: explicit stale-owner recovery is allowed.
        write_json(path, owner.to_dict())
        return CLAIM_RECOVERED, previous
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        # A concurrent create won the race.
        concurrent = read_owner(root)
        if concurrent is not None and process_alive(concurrent.pid):
            return CLAIM_DENIED, concurrent
        write_json(path, owner.to_dict())
        return CLAIM_RECOVERED, concurrent
    try:
        payload = json.dumps(owner.to_dict(), sort_keys=True).encode("utf-8")
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    return CLAIM_FRESH, previous


def release_ownership(root: str | Path, owner: OwnerIdentity) -> None:
    path = _path(root, model.SUPERVISOR_OWNER_NAME)
    current = read_owner(root)
    if current is None:
        return
    if current.token != owner.token and current.pid != owner.pid:
        model.fail(model.E_SUPERVISOR_NOT_OWNED, owner.token)
    try:
        path.unlink()
    except FileNotFoundError:
        return


def detect_crash(root: str | Path) -> dict[str, Any]:
    """Read-only crash classification of the supervisor (never mutates)."""
    state = load_state(root)
    owner = read_owner(root)
    if state is None:
        return {"status": "ABSENT", "crash_detected": False,
                "owner": None if owner is None else owner.to_dict()}
    running = state.status == SS_RUNNING
    alive = owner is not None and process_alive(owner.pid)
    crash = running and not alive
    return {
        "status": state.status,
        "crash_detected": crash,
        "owner_alive": alive,
        "owner": None if owner is None else owner.to_dict(),
    }


# --- durable state ------------------------------------------------------------


def load_state(root: str | Path) -> SupervisorState | None:
    document = read_optional_json(_path(root, model.SUPERVISOR_STATE_NAME))
    if document is None:
        return None
    return SupervisorState.from_dict(document)


def save_state(root: str | Path, state: SupervisorState) -> None:
    write_json(_path(root, model.SUPERVISOR_STATE_NAME), state.to_dict())


def write_heartbeat(root: str | Path, owner: OwnerIdentity,
                    *, cycle: int, at: str) -> None:
    write_json(_path(root, model.SUPERVISOR_HEARTBEAT_NAME), {
        "schema_version": model.SCHEMA_VERSION,
        "owner_token": owner.token,
        "pid": owner.pid,
        "cycle": cycle,
        "at": at,
    })


def read_heartbeat(root: str | Path) -> dict[str, Any] | None:
    return read_optional_json(_path(root, model.SUPERVISOR_HEARTBEAT_NAME))


def request_stop(root: str | Path, *, reason: str = "OPERATOR_STOP",
                 clock: Callable[[], str] = utc_now) -> dict[str, Any]:
    document = {
        "schema_version": model.SCHEMA_VERSION,
        "reason": reason,
        "requested_at": clock(),
    }
    write_json(_path(root, model.SUPERVISOR_STOP_NAME), document)
    state = load_state(root)
    if state is not None:
        save_state(root, replace(state, stop_requested=True,
                                 updated_at=clock()).validate())
    return document


def clear_stop(root: str | Path) -> None:
    import contextlib

    path = _path(root, model.SUPERVISOR_STOP_NAME)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    state = load_state(root)
    if state is not None:
        save_state(root, replace(state, stop_requested=False,
                                 updated_at=utc_now()).validate())


def stop_requested(root: str | Path) -> bool:
    return _path(root, model.SUPERVISOR_STOP_NAME).is_file()


# --- reconstruction -----------------------------------------------------------


def reconstruct(root: str | Path) -> dict[str, Any]:
    """Reconstruct supervisor + project state from canonical documents.

    Fails closed when the supervisor state contradicts the ownership lock.
    """
    state = load_state(root)
    owner = read_owner(root)
    project_report = project_registry.reconstruct_projects(root)
    projects = project_registry.list_projects(root)
    contradiction = False
    reason: str | None = None
    if state is not None and state.status == SS_RUNNING:
        if owner is None:
            contradiction = True
            reason = "RUNNING_WITHOUT_OWNER"
        elif not process_alive(owner.pid):
            contradiction = True
            reason = "RUNNING_WITH_DEAD_OWNER"
    if (state is not None and owner is not None
            and state.owner is not None and state.owner.token != owner.token):
        contradiction = True
        reason = "OWNER_TOKEN_MISMATCH"
    if contradiction:
        model.fail(model.E_SUPERVISOR_CONTRADICTION, reason or "")
    return {
        "schema_version": model.SCHEMA_VERSION,
        "platform_version": model.PLATFORM_VERSION,
        "supervisor": None if state is None else state.to_dict(),
        "owner": None if owner is None else owner.to_dict(),
        "projects": project_report,
        "project_ids": [project.project_id for project in projects],
        "crash": detect_crash(root),
        "reconstructed": True,
    }


def recover(root: str | Path, *, actor: str = "operator",
            clock: Callable[[], str] = utc_now) -> dict[str, Any]:
    """Idempotent startup recovery: claim a stale owner and mark RECOVERED."""
    outcome, previous = claim_ownership(
        root, build_owner(root, started_at=clock()))
    if outcome == CLAIM_DENIED:
        detail = "unknown" if previous is None else str(previous.pid)
        model.fail(model.E_SUPERVISOR_OWNED, detail)
    state = load_state(root)
    owner = read_owner(root)
    if state is None:
        state = SupervisorState.initial(started_at=clock(),
                                        owner=owner,  # type: ignore[arg-type]
                                        project_ids=())
    recovered = replace(
        state, status=SS_RECOVERED, owner=owner,
        stop_requested=stop_requested(root),
        last_reason="STARTUP_RECOVERY", updated_at=clock()).validate()
    save_state(root, recovered)
    return {
        "schema_version": model.SCHEMA_VERSION,
        "outcome": outcome,
        "previous_owner": (None if previous is None else previous.to_dict()),
        "state": recovered.to_dict(),
        "actor": actor,
    }


# --- supervisor loop ----------------------------------------------------------


@dataclass(frozen=True)
class SupervisorReport:
    """One bounded supervisor session outcome."""

    status: str
    reason: str
    cycles: int
    state: SupervisorState

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reason": self.reason,
                "cycles": self.cycles, "state": self.state.to_dict()}


def default_cycle(root: str | Path, cycle: int, *,
                  dispatch_fn: Callable[[str], None] | None = None,
                  ) -> dict[str, Any]:
    """One scheduler cycle delegated to the queue/scheduler path.

    Selection is always persisted as a durable ``SchedulingDecision`` (the
    admission/dispatch record). When ``dispatch_fn`` is supplied, the selected
    missions are additionally handed to it — this is the delegation point to
    the existing operator control plane described by ADR-026. Without it the
    cycle is admission-only and the persisted decision is the dispatch record,
    so admitted work is never silently lost.
    """
    from trajectory_os.platform import queue as queue_module

    decision = queue_module.schedule_once(
        root, dispatch=dispatch_fn is not None, dispatch_fn=dispatch_fn)
    return decision.to_dict()


def run_supervisor(
    root: str | Path,
    *,
    project_ids: tuple[str, ...] | None = None,
    max_cycles: int = 1,
    cycle_fn: Callable[[str | Path, int], Mapping[str, Any]] = default_cycle,
    dispatch_fn: Callable[[str], None] | None = None,
    owner: OwnerIdentity | None = None,
    clock: Callable[[], str] = utc_now,
    sleep_fn: Callable[[float], None] = time.sleep,
    cycle_interval_s: float = 0.0,
) -> SupervisorReport:
    """Run one bounded, single-owner, restart-safe supervisor session."""
    if max_cycles < 0 or max_cycles > model.MAX_CYCLES_BOUND:
        model.fail(model.E_SUPERVISOR_CONTRADICTION, "max_cycles out of bounds")
    resolved_projects = (tuple(project_ids)
                         if project_ids is not None
                         else tuple(p.project_id
                                    for p in project_registry.list_projects(
                                        root)))
    candidate = owner or build_owner(root, started_at=clock())
    resolved_cycle: Callable[[str | Path, int], Mapping[str, Any]] = cycle_fn
    if dispatch_fn is not None and cycle_fn is default_cycle:
        resolved_cycle = partial(default_cycle, dispatch_fn=dispatch_fn)
    outcome, previous = claim_ownership(root, candidate)
    if outcome == CLAIM_DENIED:
        model.fail(model.E_SUPERVISOR_OWNED, str(previous.pid)
                   if previous is not None else "unknown")
    if outcome == CLAIM_RECOVERED and previous is not None:
        # record the crash detection before resuming
        state = load_state(root)
        if state is not None and state.status == SS_RUNNING:
            save_state(root, replace(
                state, status=SS_CRASHED, updated_at=clock(),
                last_reason="STALE_OWNER_DETECTED").validate())

    state = load_state(root)
    if state is None:
        state = SupervisorState.initial(started_at=clock(), owner=candidate,
                                        project_ids=resolved_projects)
    state = replace(state, status=SS_RUNNING, owner=candidate,
                    project_ids=resolved_projects, stop_requested=False,
                    updated_at=clock()).validate()
    # Persist RUNNING *before* the first cycle. Crash detection is defined as
    # "persisted RUNNING with a dead owner", so a process death inside the
    # narrow claim->save window is classified ABSENT/STALE by detect_crash and
    # never produces a false crash for the next startup.
    save_state(root, state)
    write_heartbeat(root, candidate, cycle=0, at=clock())

    cycles = 0
    reason = "CYCLE_BOUND"
    status = SS_RUNNING
    try:
        while cycles < max_cycles:
            if stop_requested(root):
                reason = "STOP_REQUESTED"
                status = SS_STOPPED
                break
            cycles += 1
            try:
                decision = resolved_cycle(root, cycles)
            except model.PlatformError as exc:
                reason = f"CYCLE_ERROR:{exc.code}"
                status = SS_ERROR
                break
            state = replace(
                state, cycles=cycles, heartbeat_at=clock(),
                updated_at=clock(),
                last_reason=str(decision.get("reason", reason))
                if isinstance(decision, Mapping) else reason,
            ).validate()
            save_state(root, state)
            write_heartbeat(root, candidate, cycle=cycles, at=clock())
            if isinstance(decision, Mapping) and decision.get("complete"):
                reason = "NO_ACTIVE_WORK"
                status = SS_COMPLETE
                break
            if cycle_interval_s > 0 and cycles < max_cycles:
                sleep_fn(cycle_interval_s)
        if status == SS_RUNNING:
            reason = "CYCLE_BOUND"
            status = SS_STOPPED
    finally:
        state = replace(state, status=status, updated_at=clock(),
                        heartbeat_at=clock(), stop_requested=False,
                        last_reason=reason).validate()
        save_state(root, state)
        release_ownership(root, candidate)
    return SupervisorReport(status=status, reason=reason, cycles=cycles,
                            state=state)


def status_document(root: str | Path) -> dict[str, Any]:
    """Read-only supervisor status projection."""
    state = load_state(root)
    owner = read_owner(root)
    crash = detect_crash(root)
    heartbeat = read_heartbeat(root)
    return {
        "schema_version": model.SCHEMA_VERSION,
        "platform_version": model.PLATFORM_VERSION,
        "present": state is not None,
        "status": None if state is None else state.status,
        "owner": None if owner is None else owner.to_dict(),
        "alive": owner is not None and process_alive(owner.pid),
        "cycles": 0 if state is None else state.cycles,
        "started_at": None if state is None else state.started_at,
        "updated_at": None if state is None else state.updated_at,
        "heartbeat": heartbeat,
        "stop_requested": stop_requested(root),
        "crash": crash,
        "project_ids": [] if state is None else list(state.project_ids),
        "read_only": True,
    }


__all__ = [
    "CLAIM_DENIED",
    "CLAIM_FRESH",
    "CLAIM_RECOVERED",
    "OWNER_DOMAIN",
    "SS_COMPLETE",
    "SS_CRASHED",
    "SS_ERROR",
    "SS_RECOVERED",
    "SS_RUNNING",
    "SS_STOPPED",
    "STATUSES",
    "OwnerIdentity",
    "SupervisorReport",
    "SupervisorState",
    "build_owner",
    "claim_ownership",
    "clear_stop",
    "default_cycle",
    "detect_crash",
    "host_identity",
    "load_state",
    "process_alive",
    "read_heartbeat",
    "read_owner",
    "reconstruct",
    "recover",
    "release_ownership",
    "request_stop",
    "run_supervisor",
    "save_state",
    "status_document",
    "stop_requested",
    "supervisor_dir",
    "owner_path",
    "write_heartbeat",
]
