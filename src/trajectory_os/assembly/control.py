"""M034 — mission-level operator control (explicit, mission-scoped, fail closed).

The mission-level control surface is deliberately separate from the
observation surface:

* observation (``status`` / ``follow`` / ``reconstruct``) is strictly
  read-only and always reads the canonical ``status.json``;
* control (``request-stop`` / ``pause`` / ``cancel``) writes an append-only
  mission-scoped ``control.jsonl`` record and, where the control terminates
  the mission, the canonical M030 readiness/closure.

Control and observation therefore never share a state machine: the control
log records *what an operator asked for*; the canonical status records *what
the mission lifecycle/readiness actually is*.

Trust constraints:

* every control targets an explicit ``mission_id``; an unknown or stale id
  fails closed and is never resolved to a "most recent" mission;
* ``cancel`` produces canonical ``CANCELLED`` (lifecycle ``COMPLETE``), never
  a generic failure or a success;
* a terminal trust decision (``READY_FOR_COMMIT`` / ``BLOCKED`` / ``FAILED``)
  is never silently overridden — a late cancel is refused;
* ``request-stop`` / ``pause`` record a durable graceful stop request that the
  mission orchestrator honours at the next safe phase boundary; both are
  resumable and preserve evidence;
* no control operation ever spawns a process or performs a Git
  trust-boundary write (commit/push/merge/reset/restore/clean/stash/rebase/
  checkout/switch).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trajectory_os.assembly import closure as assembly_closure
from trajectory_os.assembly import model, recovery, store
from trajectory_os.observability import model as obs_model
from trajectory_os.observability import store as obs_store

# --- control actions (closed set) --------------------------------------------

ACTION_REQUEST_STOP = "REQUEST_STOP"
ACTION_PAUSE = "PAUSE"
ACTION_CANCEL = "CANCEL"
ACTION_CLEAR_STOP = "CLEAR_STOP"

CONTROL_ACTIONS = frozenset({
    ACTION_REQUEST_STOP, ACTION_PAUSE, ACTION_CANCEL, ACTION_CLEAR_STOP,
})

# --- control results (closed set) --------------------------------------------

RESULT_ACCEPTED = "ACCEPTED"
RESULT_REFUSED = "REFUSED"
RESULT_ALREADY_TERMINAL = "ALREADY_TERMINAL"
RESULT_ALREADY_CANCELLED = "ALREADY_CANCELLED"
RESULT_UNSUPPORTED = "UNSUPPORTED"
RESULT_CLEARED = "CLEARED"

CONTROL_RESULTS = frozenset({
    RESULT_ACCEPTED, RESULT_REFUSED, RESULT_ALREADY_TERMINAL,
    RESULT_ALREADY_CANCELLED, RESULT_UNSUPPORTED, RESULT_CLEARED,
})

#: Durable control documents (append-only log + mutable request latch).
CONTROL_LOG_NAME = "control.jsonl"
STOP_REQUEST_NAME = "stop-request.json"

#: Bound on the control log (newest records retained).
MAX_CONTROL_RECORDS = 512

#: Readiness values that finalize a trust decision.
_FINAL_READINESS = frozenset({
    obs_model.RD_READY_FOR_COMMIT, obs_model.RD_BLOCKED,
    obs_model.RD_FAILED, obs_model.RD_CANCELLED,
})


@dataclass(frozen=True)
class ControlOutcome:
    """The bounded result of one mission-level control action."""

    mission_id: str
    action: str
    result: str
    reason: str
    readiness: str | None
    lifecycle: str | None
    terminal: bool
    sequence: int

    def validate(self) -> ControlOutcome:
        if self.action not in CONTROL_ACTIONS:
            raise model.AssemblyError(model.R_MALFORMED, self.action)
        if self.result not in CONTROL_RESULTS:
            raise model.AssemblyError(model.R_MALFORMED, self.result)
        if not self.mission_id:
            raise model.AssemblyError(model.R_MALFORMED, "mission_id")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "action": self.action,
            "result": self.result,
            "reason": self.reason,
            "readiness": self.readiness,
            "lifecycle": self.lifecycle,
            "terminal": self.terminal,
            "sequence": self.sequence,
        }


# --- mission targeting --------------------------------------------------------


def _require_mission(root: str | Path, mission_id: str) -> Path:
    """Resolve an explicit mission target or fail closed."""
    if not isinstance(mission_id, str) or not mission_id.strip():
        raise model.AssemblyError(model.R_MISSION_MISSING, repr(mission_id))
    if not store.mission_exists(root, mission_id):
        raise model.AssemblyError(model.R_MISSION_MISSING, mission_id)
    return store.mission_root(root, mission_id)


def _status(root: str | Path,
            mission_id: str) -> dict[str, Any] | None:
    mission_root = store.mission_root(root, mission_id)
    return recovery.load_identity_status(mission_root, mission_id)


def _is_terminal(status: Mapping[str, Any] | None) -> bool:
    if status is None:
        return False
    lifecycle = str(status.get("state", ""))
    readiness = str(status.get("readiness", ""))
    return (lifecycle in obs_model.TERMINAL_LIFECYCLE_STATES
            or readiness in _FINAL_READINESS)


# --- durable control log ------------------------------------------------------


def _append_control(root: str | Path, mission_id: str, *,
                    action: str, result: str, reason: str, actor: str,
                    readiness: str | None, lifecycle: str | None,
                    terminal: bool, at: str) -> int:
    mission_root = _require_mission(root, mission_id)
    records = control_log(root, mission_id)
    sequence = len(records) + 1
    record = {
        "mission_id": mission_id,
        "sequence": sequence,
        "action": action,
        "result": result,
        "reason": reason,
        "actor": actor,
        "readiness": readiness,
        "lifecycle": lifecycle,
        "terminal": terminal,
        "at": at,
    }
    path = mission_root / CONTROL_LOG_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, separators=(",", ":"),
                      default=str)
    with path.open("ab") as handle:
        handle.write(line.encode("utf-8") + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    return sequence


def control_log(root: str | Path, mission_id: str) -> list[dict[str, Any]]:
    """Read the mission-scoped append-only control log (fail closed)."""
    path = store.mission_root(root, mission_id) / CONTROL_LOG_NAME
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise model.AssemblyError(
            model.R_MALFORMED, f"control log: {type(exc).__name__}") from exc
    out: list[dict[str, Any]] = []
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        try:
            document = json.loads(line)
        except json.JSONDecodeError as exc:
            raise model.AssemblyError(
                model.R_MALFORMED, f"control log line {index}") from exc
        if not isinstance(document, dict):
            raise model.AssemblyError(
                model.R_MALFORMED, f"control log line {index}")
        if document.get("mission_id") != mission_id:
            raise model.AssemblyError(
                model.R_IDENTITY_MISMATCH,
                f"control record for {document.get('mission_id')!r}")
        out.append(document)
    return out[-MAX_CONTROL_RECORDS:]


# --- graceful stop request ----------------------------------------------------


def _write_stop_request(root: str | Path, mission_id: str, *,
                        action: str, reason: str, actor: str,
                        requested: bool, at: str) -> None:
    mission_root = _require_mission(root, mission_id)
    obs_store.write_json(mission_root / STOP_REQUEST_NAME, {
        "mission_id": mission_id,
        "requested": requested,
        "action": action,
        "reason": reason,
        "actor": actor,
        "at": at,
    })


def stop_requested(root: str | Path, mission_id: str) -> bool:
    """True when a durable graceful stop/pause has been requested."""
    path = store.mission_root(root, mission_id) / STOP_REQUEST_NAME
    try:
        document = obs_store.read_json(path)
    except obs_store.CanonicalStoreError:
        return False
    if document.get("mission_id") != mission_id:
        raise model.AssemblyError(
            model.R_IDENTITY_MISMATCH, "stop request identity mismatch")
    return bool(document.get("requested", False))


def clear_stop_request(root: str | Path, mission_id: str, *,
                       reason: str = "resume",
                       actor: str = "operator",
                       clock: Any = model.utc_now) -> ControlOutcome:
    """Consume a stop request before a resume (keeps the append-only log)."""
    _require_mission(root, mission_id)
    status = _status(root, mission_id)
    at = clock()
    _write_stop_request(root, mission_id, action=ACTION_CLEAR_STOP,
                        reason=reason, actor=actor, requested=False, at=at)
    sequence = _append_control(
        root, mission_id, action=ACTION_CLEAR_STOP, result=RESULT_CLEARED,
        reason=reason, actor=actor, readiness=_opt(status, "readiness"),
        lifecycle=_opt(status, "state"), terminal=_is_terminal(status), at=at)
    return ControlOutcome(
        mission_id=mission_id, action=ACTION_CLEAR_STOP,
        result=RESULT_CLEARED, reason=reason, readiness=_opt(status,
                                                             "readiness"),
        lifecycle=_opt(status, "state"), terminal=_is_terminal(status),
        sequence=sequence).validate()


# --- control actions ----------------------------------------------------------


def request_stop(root: str | Path, mission_id: str, *,
                 reason: str = "operator requested stop",
                 actor: str = "operator",
                 clock: Any = model.utc_now) -> ControlOutcome:
    """Request a graceful, resumable stop at the next safe phase boundary."""
    return _graceful(root, mission_id, action=ACTION_REQUEST_STOP,
                     reason=reason, actor=actor, clock=clock)


def pause(root: str | Path, mission_id: str, *,
          reason: str = "operator paused mission",
          actor: str = "operator",
          clock: Any = model.utc_now) -> ControlOutcome:
    """Pause a mission: a resumable graceful stop request."""
    return _graceful(root, mission_id, action=ACTION_PAUSE, reason=reason,
                     actor=actor, clock=clock)


def _graceful(root: str | Path, mission_id: str, *, action: str,
              reason: str, actor: str, clock: Any) -> ControlOutcome:
    _require_mission(root, mission_id)
    status = _status(root, mission_id)
    at = clock()
    if _is_terminal(status):
        sequence = _append_control(
            root, mission_id, action=action, result=RESULT_ALREADY_TERMINAL,
            reason="mission already reached a terminal trust decision",
            actor=actor, readiness=_opt(status, "readiness"),
            lifecycle=_opt(status, "state"), terminal=True, at=at)
        return ControlOutcome(
            mission_id=mission_id, action=action,
            result=RESULT_ALREADY_TERMINAL,
            reason="mission already reached a terminal trust decision",
            readiness=_opt(status, "readiness"),
            lifecycle=_opt(status, "state"), terminal=True,
            sequence=sequence).validate()
    _write_stop_request(root, mission_id, action=action, reason=reason,
                        actor=actor, requested=True, at=at)
    sequence = _append_control(
        root, mission_id, action=action, result=RESULT_ACCEPTED,
        reason=reason, actor=actor, readiness=_opt(status, "readiness"),
        lifecycle=_opt(status, "state"), terminal=False, at=at)
    return ControlOutcome(
        mission_id=mission_id, action=action, result=RESULT_ACCEPTED,
        reason=reason, readiness=_opt(status, "readiness"),
        lifecycle=_opt(status, "state"), terminal=False,
        sequence=sequence).validate()


def cancel(root: str | Path, mission_id: str, *,
           reason: str = "operator cancelled mission",
           actor: str = "operator",
           clock: Any = model.utc_now) -> ControlOutcome:
    """Cancel a mission: canonical ``CANCELLED`` (never a generic failure)."""
    mission_root = _require_mission(root, mission_id)
    mission = store.load_mission(root, mission_id)
    status = _status(root, mission_id)
    at = clock()
    readiness = _opt(status, "readiness")
    lifecycle = _opt(status, "state")

    if readiness == obs_model.RD_CANCELLED:
        sequence = _append_control(
            root, mission_id, action=ACTION_CANCEL,
            result=RESULT_ALREADY_CANCELLED,
            reason="mission already CANCELLED (idempotent)",
            actor=actor, readiness=readiness, lifecycle=lifecycle,
            terminal=True, at=at)
        return ControlOutcome(
            mission_id=mission_id, action=ACTION_CANCEL,
            result=RESULT_ALREADY_CANCELLED,
            reason="mission already CANCELLED (idempotent)",
            readiness=readiness, lifecycle=lifecycle, terminal=True,
            sequence=sequence).validate()
    if _is_terminal(status):
        sequence = _append_control(
            root, mission_id, action=ACTION_CANCEL, result=RESULT_REFUSED,
            reason=("mission already finalized; a terminal trust decision "
                    "is never silently overridden"),
            actor=actor, readiness=readiness, lifecycle=lifecycle,
            terminal=True, at=at)
        return ControlOutcome(
            mission_id=mission_id, action=ACTION_CANCEL, result=RESULT_REFUSED,
            reason=("mission already finalized; a terminal trust decision "
                    "is never silently overridden"),
            readiness=readiness, lifecycle=lifecycle, terminal=True,
            sequence=sequence).validate()

    cancelled = _build_cancelled_status(mission, at=at)
    obs_store.write_status(mission_root, cancelled)
    _emit_canonical_event(
        mission_root, mission_id, kind="CONTROL_CANCEL_REQUESTED",
        at=at, detail={"reason": reason, "actor": actor})
    plan = (store.load_plan(root, mission_id)
            if store.plan_exists(root, mission_id) else None)
    closure = assembly_closure.build_closure(
        root, mission, plan=plan, status=cancelled.to_dict(),
        created_at=at)
    store.write_closure(root, closure)
    # A terminal cancel supersedes any pending graceful stop latch.
    _write_stop_request(root, mission_id, action=ACTION_CANCEL, reason=reason,
                        actor=actor, requested=False, at=at)
    recovery.write_recovery_record(root, recovery.detect_resume(root,
                                                                mission_id))
    _emit_canonical_event(
        mission_root, mission_id, kind="MISSION_CLOSED", at=at,
        result=obs_model.RESULT_FAIL,
        detail={"readiness": obs_model.RD_CANCELLED,
                "terminal_reason": cancelled.terminal_reason})
    sequence = _append_control(
        root, mission_id, action=ACTION_CANCEL, result=RESULT_ACCEPTED,
        reason=reason, actor=actor, readiness=obs_model.RD_CANCELLED,
        lifecycle=obs_model.LC_COMPLETE, terminal=True, at=at)
    return ControlOutcome(
        mission_id=mission_id, action=ACTION_CANCEL, result=RESULT_ACCEPTED,
        reason=reason, readiness=obs_model.RD_CANCELLED,
        lifecycle=obs_model.LC_COMPLETE, terminal=True,
        sequence=sequence).validate()


# --- canonical status construction -------------------------------------------


def _build_cancelled_status(mission: model.MissionDefinition, *,
                            at: str) -> obs_model.CanonicalStatus:
    policy = mission.trust_policy
    inline = (obs_model.ReviewerStatus.disabled(
        obs_model.ROLE_INLINE_REVIEWER,
        reason="inline review disabled by operator")
        if not policy.inline_review_enabled else obs_model.ReviewerStatus(
            role=obs_model.ROLE_INLINE_REVIEWER, enabled=True, active=False,
            backend="ollama", provider="ollama",
            model=obs_model.INLINE_REVIEWER_MODEL,
            reason="inline review configured but not invoked").validate())
    final = (obs_model.ReviewerStatus.disabled(
        obs_model.ROLE_FINAL_INDEPENDENT_REVIEWER,
        reason="final independent review disabled by operator")
        if not policy.require_review else obs_model.ReviewerStatus(
            role=obs_model.ROLE_FINAL_INDEPENDENT_REVIEWER, enabled=True,
            active=False, backend="ollama", provider="ollama",
            model=policy.final_reviewer_model,
            reason="mission cancelled before final review").validate())
    return obs_model.CanonicalStatus.build(
        run_id=mission.mission_id, state=obs_model.LC_COMPLETE,
        stage=obs_model.STAGE_DONE, phase="CANCELLED", attempt=0,
        current_backend=mission.backend, current_provider=mission.provider,
        current_model=mission.model,
        inline_review_enabled=policy.inline_review_enabled,
        inline_reviewer=inline, final_review_enabled=policy.require_review,
        final_reviewer=final, previous_gate=obs_model.GATE_NONE,
        previous_result=None, reviewed_patch=None, current_patch=None,
        next_action="operator: resume or abandon the mission",
        last_meaningful_event_at=at, heartbeat_at=at,
        terminal_reason="mission cancelled by operator",
        terminal_reason_code=obs_model.R_CANCELLED,
        readiness=obs_model.RD_CANCELLED, current="cancelled", next=None,
        telemetry_mode=mission.telemetry_mode, telemetry=None, updated_at=at)


def _emit_canonical_event(mission_root: Path, mission_id: str, *,
                          kind: str, at: str, result: str = obs_model.RESULT_NA,
                          detail: Mapping[str, Any] | None = None) -> None:
    sequence = obs_store.event_count(mission_root) + 1
    event = obs_model.CanonicalEvent.build(
        run_id=mission_id, sequence=sequence, kind=kind, at=at,
        stage=obs_model.STAGE_DONE,
        phase="CANCELLED" if kind != "MISSION_CLOSED" else "DONE",
        attempt=0, gate=obs_model.GATE_NONE, result=result,
        actor="operator", detail=dict(detail or {}))
    obs_store.append_event(mission_root, event)


def _opt(document: Mapping[str, Any] | None, key: str) -> str | None:
    if document is None:
        return None
    value = document.get(key)
    return value if isinstance(value, str) and value else None


__all__ = [
    "ACTION_CANCEL",
    "ACTION_CLEAR_STOP",
    "ACTION_PAUSE",
    "ACTION_REQUEST_STOP",
    "CONTROL_ACTIONS",
    "CONTROL_LOG_NAME",
    "CONTROL_RESULTS",
    "ControlOutcome",
    "RESULT_ACCEPTED",
    "RESULT_ALREADY_CANCELLED",
    "RESULT_ALREADY_TERMINAL",
    "RESULT_CLEARED",
    "RESULT_REFUSED",
    "RESULT_UNSUPPORTED",
    "STOP_REQUEST_NAME",
    "cancel",
    "clear_stop_request",
    "control_log",
    "pause",
    "request_stop",
    "stop_requested",
]
