"""M051 — durable notifications and human-gate inbox.

The inbox is a durable *attention* surface derived read-only from the canonical
operator/release/supervisor state. It is a sink: it never authorizes a human
gate, never performs a Git write and never becomes a second source of truth.

Design invariants:

* deterministic notification identity — ``(type, mission_id, key)`` is a
  domain-separated digest, so recovery cannot produce a duplicate storm;
* explicit ``UNREAD`` / ``ACKNOWLEDGED`` / ``RESOLVED`` state;
* ``GO COMMIT`` / ``GO MERGE`` remain separate explicit human actions; the
  notification merely reports that a gate is pending;
* an optional ``notify-send`` adapter is a pure sink and reports an explicit
  ``UNAVAILABLE`` status with a reason when absent.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from trajectory_os.assembly import store as assembly_store
from trajectory_os.observability import model as obs_model
from trajectory_os.operator import model as operator_model
from trajectory_os.operator._util import (
    append_jsonl,
    digest,
    optional_str,
    read_jsonl,
    utc_now,
    write_json,
)
from trajectory_os.platform import model
from trajectory_os.platform import supervisor as supervisor_module
from trajectory_os.release import model as release_model
from trajectory_os.release import store as release_store

#: Domain id for a notification identity.
NOTIFICATION_DOMAIN = "trajectory-os.platform-notification.v1"

# --- notification types (closed set) ------------------------------------------

N_READY_FOR_COMMIT = "READY_FOR_COMMIT"
N_GO_COMMIT_REQUIRED = "GO_COMMIT_REQUIRED"
N_READY_FOR_MERGE = "READY_FOR_MERGE"
N_GO_MERGE_REQUIRED = "GO_MERGE_REQUIRED"
N_BLOCKED = "BLOCKED"
N_VALIDATION_FAILURE = "VALIDATION_FAILURE"
N_REVIEW_FAILURE = "REVIEW_FAILURE"
N_REVIEW_PROTOCOL_ERROR = "REVIEW_PROTOCOL_ERROR"
N_CI_FAILURE = "CI_FAILURE"
N_CI_CANCELLED = "CI_CANCELLED"
N_CI_MISSING = "CI_MISSING"
N_MISSION_COMPLETED = "MISSION_COMPLETED"
N_DAEMON_RECOVERY = "DAEMON_RECOVERY"

TYPES = frozenset({
    N_READY_FOR_COMMIT, N_GO_COMMIT_REQUIRED, N_READY_FOR_MERGE,
    N_GO_MERGE_REQUIRED, N_BLOCKED, N_VALIDATION_FAILURE, N_REVIEW_FAILURE,
    N_REVIEW_PROTOCOL_ERROR, N_CI_FAILURE, N_CI_CANCELLED, N_CI_MISSING,
    N_MISSION_COMPLETED, N_DAEMON_RECOVERY,
})

#: Notification states (closed set).
NS_UNREAD = "UNREAD"
NS_ACKNOWLEDGED = "ACKNOWLEDGED"
NS_RESOLVED = "RESOLVED"
STATES = frozenset({NS_UNREAD, NS_ACKNOWLEDGED, NS_RESOLVED})

#: Notification severities (closed set).
SEV_INFO = "INFO"
SEV_ATTENTION = "ATTENTION"
SEV_BLOCKING = "BLOCKING"
SEVERITIES = frozenset({SEV_INFO, SEV_ATTENTION, SEV_BLOCKING})

_SEVERITY = {
    N_READY_FOR_COMMIT: SEV_ATTENTION,
    N_GO_COMMIT_REQUIRED: SEV_ATTENTION,
    N_READY_FOR_MERGE: SEV_ATTENTION,
    N_GO_MERGE_REQUIRED: SEV_ATTENTION,
    N_BLOCKED: SEV_BLOCKING,
    N_VALIDATION_FAILURE: SEV_BLOCKING,
    N_REVIEW_FAILURE: SEV_BLOCKING,
    N_REVIEW_PROTOCOL_ERROR: SEV_BLOCKING,
    N_CI_FAILURE: SEV_BLOCKING,
    N_CI_CANCELLED: SEV_BLOCKING,
    N_CI_MISSING: SEV_BLOCKING,
    N_MISSION_COMPLETED: SEV_INFO,
    N_DAEMON_RECOVERY: SEV_INFO,
}


def inbox_dir(root: str | Path) -> Path:
    return Path(root) / model.PLATFORM_DIR / model.INBOX_DIR


def _records_path(root: str | Path) -> Path:
    return inbox_dir(root) / model.INBOX_RECORDS_NAME


def _index_path(root: str | Path) -> Path:
    return inbox_dir(root) / model.INBOX_INDEX_NAME


def notification_id(notification_type: str, mission_id: str,
                    key: str) -> str:
    return digest(
        {"type": notification_type, "mission_id": mission_id, "key": key},
        domain=NOTIFICATION_DOMAIN)


@dataclass(frozen=True)
class Notification:
    """One durable attention notification (never a human gate)."""

    notification_id: str
    type: str
    severity: str
    mission_id: str
    title: str
    detail: str
    key: str
    state: str
    created_at: str
    updated_at: str
    sequence: int = 0
    project_id: str | None = None
    schema_version: int = model.SCHEMA_VERSION
    platform_version: str = model.PLATFORM_VERSION

    def validate(self) -> Notification:
        if self.type not in TYPES:
            model.fail(model.E_INBOX_INVALID, f"type {self.type!r}")
        if self.severity not in SEVERITIES:
            model.fail(model.E_INBOX_INVALID, f"severity {self.severity!r}")
        if self.state not in STATES:
            model.fail(model.E_INBOX_INVALID, f"state {self.state!r}")
        if self.notification_id != notification_id(
                self.type, self.mission_id, self.key):
            model.fail(model.E_INBOX_INVALID, "notification identity mismatch")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "platform_version": self.platform_version,
            "notification_id": self.notification_id,
            "type": self.type,
            "severity": self.severity,
            "mission_id": self.mission_id,
            "project_id": self.project_id,
            "title": self.title,
            "detail": self.detail,
            "key": self.key,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "sequence": self.sequence,
        }

    @staticmethod
    def build(*, type: str, mission_id: str, key: str, title: str,
              detail: str = "", project_id: str | None = None,
              created_at: str = "", updated_at: str = "") -> Notification:
        stamp = created_at or utc_now()
        return Notification(
            notification_id=notification_id(type, mission_id, key),
            type=type, severity=_SEVERITY[type], mission_id=mission_id,
            project_id=project_id, title=title, detail=detail, key=key,
            state=NS_UNREAD, created_at=stamp,
            updated_at=updated_at or stamp).validate()

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> Notification:
        version = data.get("schema_version", model.SCHEMA_VERSION)
        if version != model.SCHEMA_VERSION:
            model.fail(model.E_UNSUPPORTED_VERSION, str(version))
        return Notification(
            notification_id=str(data.get("notification_id", "")),
            type=str(data.get("type", "")),
            severity=str(data.get("severity", SEV_INFO)),
            mission_id=str(data.get("mission_id", "")),
            project_id=optional_str(data.get("project_id")),
            title=str(data.get("title", "")),
            detail=str(data.get("detail", "")),
            key=str(data.get("key", "")),
            state=str(data.get("state", NS_UNREAD)),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            sequence=int(data.get("sequence", 0)),
        ).validate()


# --- durable folding ----------------------------------------------------------


def load_notifications(root: str | Path) -> dict[str, Notification]:
    """Fold the append-only record log into current notification state."""
    folded: dict[str, Notification] = {}
    for document in read_jsonl(_records_path(root)):
        try:
            record = Notification.from_dict(document)
        except model.PlatformError:
            continue
        folded[record.notification_id] = record
    return folded


def list_notifications(root: str | Path, *,
                       state: str | None = None) -> list[Notification]:
    """Deterministic read-only notification list."""
    records = list(load_notifications(root).values())
    if state is not None:
        records = [record for record in records if record.state == state]
    records.sort(key=lambda record: (record.created_at, record.type,
                                     record.mission_id,
                                     record.notification_id))
    return records


def _append(root: str | Path, record: Notification) -> None:
    append_jsonl(_records_path(root), record.to_dict())


def record_notification(root: str | Path, notification: Notification) -> bool:
    """Append a notification unless its deterministic id already exists."""
    existing = load_notifications(root)
    if notification.notification_id in existing:
        return False
    _append(root, notification)
    _write_index(root)
    return True


def acknowledge(root: str | Path, notification_id_value: str, *,
                clock: Callable[[], str] = utc_now) -> Notification:
    return _transition(root, notification_id_value, NS_ACKNOWLEDGED, clock)


def resolve(root: str | Path, notification_id_value: str, *,
            clock: Callable[[], str] = utc_now) -> Notification:
    return _transition(root, notification_id_value, NS_RESOLVED, clock)


def _transition(root: str | Path, notification_id_value: str, state: str,
                clock: Callable[[], str]) -> Notification:
    existing = load_notifications(root)
    record = existing.get(notification_id_value)
    if record is None:
        model.fail(model.E_INBOX_MISSING, notification_id_value)
    if record.state == state:
        return record
    updated = replace(record, state=state, updated_at=clock()).validate()
    _append(root, updated)
    _write_index(root)
    return updated


def _write_index(root: str | Path) -> None:
    records = list_notifications(root)
    index = {
        "schema_version": model.SCHEMA_VERSION,
        "platform_version": model.PLATFORM_VERSION,
        "unread": sum(1 for r in records if r.state == NS_UNREAD),
        "acknowledged": sum(1 for r in records
                            if r.state == NS_ACKNOWLEDGED),
        "resolved": sum(1 for r in records if r.state == NS_RESOLVED),
        "notifications": [r.to_dict() for r in records],
    }
    write_json(_index_path(root), index)


# --- derivation ---------------------------------------------------------------


def iter_missions(root: str | Path) -> list[str]:
    """Deterministic list of canonical mission ids below the root."""
    base = Path(root)
    if not base.is_dir():
        return []
    missions: list[str] = []
    for path in sorted(base.iterdir()):
        if not path.is_dir() or path.name == model.PLATFORM_DIR:
            continue
        if ((path / assembly_store.MISSION_NAME).is_file()
                or (path / "status.json").is_file()):
            missions.append(path.name)
    return missions


def _mission_project(root: str | Path, mission_id: str) -> str | None:
    from trajectory_os.platform import projects as project_registry

    for project in project_registry.list_projects(root):
        for objective in project.objectives:
            if mission_id in objective.mission_ids:
                return project.project_id
    return None


def _candidate_notifications(root: str | Path) -> list[Notification]:
    candidates: list[Notification] = []
    for mission_id in iter_missions(root):
        candidates.extend(_mission_notifications(root, mission_id))
    candidates.extend(_supervisor_notifications(root))
    return candidates


def _mission_notifications(root: str | Path,
                           mission_id: str) -> list[Notification]:
    from trajectory_os.operator._util import read_optional_json

    mission_root = Path(assembly_store.mission_root(root, mission_id))
    status = read_optional_json(mission_root / "status.json")
    release_state = read_optional_json(
        mission_root / release_store.RELEASE_STATE_NAME)
    ci_status = read_optional_json(mission_root / release_store.CI_STATUS_NAME)
    merge_result = read_optional_json(
        mission_root / release_store.MERGE_RESULT_NAME)
    release_closure = read_optional_json(
        mission_root / release_store.RELEASE_CLOSURE_NAME)
    closure = read_optional_json(
        mission_root / assembly_store.CLOSURE_NAME)
    routing = read_optional_json(mission_root / operator_model.ROUTING_NAME)
    del routing
    project_id = _mission_project(root, mission_id)
    out: list[Notification] = []

    readiness = optional_str((status or {}).get("readiness"))
    stage = optional_str((release_state or {}).get("stage"))
    patch = (optional_str((status or {}).get("reviewed_patch"))
             or optional_str((status or {}).get("current_patch")) or "none")

    if readiness == obs_model.RD_READY_FOR_COMMIT:
        out.append(_make(N_READY_FOR_COMMIT, mission_id, patch, project_id,
                         "Mission ready for commit",
                         "Independent review passed; GO COMMIT is pending."))
    if stage == release_model.RST_COMMIT_HANDOFF:
        out.append(_make(N_GO_COMMIT_REQUIRED, mission_id, patch, project_id,
                         "GO COMMIT required",
                         "The authorized release path is waiting for the "
                         "explicit human GO COMMIT action."))
    ci_green = bool((ci_status or {}).get("green"))
    if ci_green and not bool((merge_result or {}).get("merged")):
        out.append(_make(N_READY_FOR_MERGE, mission_id,
                         optional_str((ci_status or {}).get("head_sha"))
                         or "none", project_id,
                         "Ready for merge",
                         "Exact-head CI is green; GO MERGE is pending."))
    if stage == release_model.RST_MERGE_HANDOFF:
        out.append(_make(N_GO_MERGE_REQUIRED, mission_id, patch, project_id,
                         "GO MERGE required",
                         "The authorized release path is waiting for the "
                         "explicit human GO MERGE action."))
    if readiness in (obs_model.RD_BLOCKED, obs_model.RD_FAILED):
        out.append(_make(N_BLOCKED, mission_id, readiness, project_id,
                         "Mission blocked" if readiness == obs_model.RD_BLOCKED
                         else "Mission failed",
                         f"Canonical readiness is {readiness}."))
    if closure is not None:
        results = closure.get("validation_results")
        if isinstance(results, list):
            failed = [r for r in results
                      if isinstance(r, Mapping)
                      and r.get("result") not in ("PASS", "passed", True)]
            if failed:
                out.append(_make(
                    N_VALIDATION_FAILURE, mission_id,
                    f"{len(results)}:{len(failed)}", project_id,
                    "Validation failure",
                    f"{len(failed)} of {len(results)} validation runs failed."))
    review_status = optional_str((status or {}).get("review_status"))
    if review_status in ("VALID_REJECT", "REJECT", "FAIL"):
        out.append(_make(N_REVIEW_FAILURE, mission_id, review_status or "reject",
                         project_id, "Review failure",
                         "The independent reviewer rejected the patch."))
    if review_status == "REVIEW_PROTOCOL_INVALID":
        out.append(_make(N_REVIEW_PROTOCOL_ERROR, mission_id, "protocol",
                         project_id, "Review protocol error",
                         "The reviewer response violated the strict protocol."))
    ci_state = optional_str((ci_status or {}).get("state"))
    ci_head = optional_str((ci_status or {}).get("head_sha")) or "none"
    if ci_state == release_model.CI_FAILURE:
        out.append(_make(N_CI_FAILURE, mission_id, ci_head, project_id,
                         "CI failure", "Exact-head CI failed."))
    elif ci_state == release_model.CI_CANCELLED:
        out.append(_make(N_CI_CANCELLED, mission_id, ci_head, project_id,
                         "CI cancelled", "Exact-head CI was cancelled."))
    elif ci_state == release_model.CI_MISSING:
        out.append(_make(N_CI_MISSING, mission_id, ci_head, project_id,
                         "CI missing", "No exact-head CI run was found."))
    if (release_closure or {}).get("status") == "CLOSED":
        out.append(_make(N_MISSION_COMPLETED, mission_id,
                         optional_str((release_closure or {}).get("merge_sha"))
                         or "closed", project_id,
                         "Mission completed",
                         "Release closure recorded after the merge."))
    return out


def _supervisor_notifications(root: str | Path) -> list[Notification]:
    state = supervisor_module.load_state(root)
    if state is None:
        return []
    out: list[Notification] = []
    if state.status in (supervisor_module.SS_CRASHED,
                        supervisor_module.SS_RECOVERED):
        out.append(_make(
            N_DAEMON_RECOVERY, "supervisor", state.status, None,
            "Supervisor recovery",
            f"Supervisor status is {state.status}."))
    return out


def _make(notification_type: str, mission_id: str, key: str,
          project_id: str | None, title: str,
          detail: str) -> Notification:
    return Notification.build(
        type=notification_type, mission_id=mission_id, key=key,
        project_id=project_id, title=title, detail=detail)


def refresh(root: str | Path, *,
            clock: Callable[[], str] = utc_now) -> dict[str, Any]:
    """Derive and persist deduplicated notifications (idempotent)."""
    created = 0
    for candidate in _candidate_notifications(root):
        stamp = clock()
        candidate = replace(candidate, created_at=stamp,
                            updated_at=stamp).validate()
        if record_notification(root, candidate):
            created += 1
    _write_index(root)
    records = list_notifications(root)
    return {
        "schema_version": model.SCHEMA_VERSION,
        "platform_version": model.PLATFORM_VERSION,
        "created": created,
        "total": len(records),
        "unread": sum(1 for r in records if r.state == NS_UNREAD),
        "acknowledged": sum(1 for r in records
                            if r.state == NS_ACKNOWLEDGED),
        "resolved": sum(1 for r in records if r.state == NS_RESOLVED),
    }


def inbox_document(root: str | Path) -> dict[str, Any]:
    """Read-only inbox projection."""
    records = list_notifications(root)
    return {
        "schema_version": model.SCHEMA_VERSION,
        "platform_version": model.PLATFORM_VERSION,
        "total": len(records),
        "unread": sum(1 for r in records if r.state == NS_UNREAD),
        "acknowledged": sum(1 for r in records
                            if r.state == NS_ACKNOWLEDGED),
        "resolved": sum(1 for r in records if r.state == NS_RESOLVED),
        "notifications": [r.to_dict() for r in records],
        "read_only": True,
    }


# --- notify-send sink ---------------------------------------------------------


def notify_send_adapter(*, notification_id_value: str, title: str,
                        body: str, which: Callable[[str], str | None] | None =
                        None) -> dict[str, Any]:
    """Optional notify-send sink; never a correctness dependency."""
    import subprocess

    lookup = which or shutil.which
    binary = lookup("notify-send")
    if binary is None:
        return {"status": "UNAVAILABLE", "reason": "notify-send not installed",
                "notification_id": notification_id_value}
    try:
        subprocess.run([binary, title, body], check=True, timeout=5,
                       capture_output=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "FAILED", "reason": f"{type(exc).__name__}",
                "notification_id": notification_id_value}
    return {"status": "DELIVERED", "notification_id": notification_id_value}


def pending_human_gates(root: str | Path) -> list[dict[str, Any]]:
    """Read-only list of pending human gates (never an authorization)."""
    records = [r for r in list_notifications(root)
               if r.type in (N_GO_COMMIT_REQUIRED, N_GO_MERGE_REQUIRED)]
    return [{"mission_id": r.mission_id, "type": r.type, "state": r.state,
             "notification_id": r.notification_id} for r in records]


def reconstruct(root: str | Path) -> dict[str, Any]:
    """Read-only reconstruction of the inbox from durable records."""
    records = list_notifications(root)
    return {
        "schema_version": model.SCHEMA_VERSION,
        "platform_version": model.PLATFORM_VERSION,
        "records": len(records),
        "unread": sum(1 for r in records if r.state == NS_UNREAD),
        "reconstructed": True,
    }


__all__ = [
    "NOTIFICATION_DOMAIN",
    "NS_ACKNOWLEDGED",
    "NS_RESOLVED",
    "NS_UNREAD",
    "N_BLOCKED",
    "N_CI_CANCELLED",
    "N_CI_FAILURE",
    "N_CI_MISSING",
    "N_DAEMON_RECOVERY",
    "N_GO_COMMIT_REQUIRED",
    "N_GO_MERGE_REQUIRED",
    "N_MISSION_COMPLETED",
    "N_READY_FOR_COMMIT",
    "N_READY_FOR_MERGE",
    "N_REVIEW_FAILURE",
    "N_REVIEW_PROTOCOL_ERROR",
    "N_VALIDATION_FAILURE",
    "SEVERITIES",
    "SEV_ATTENTION",
    "SEV_BLOCKING",
    "SEV_INFO",
    "STATES",
    "TYPES",
    "Notification",
    "acknowledge",
    "inbox_dir",
    "inbox_document",
    "iter_missions",
    "list_notifications",
    "load_notifications",
    "notification_id",
    "notify_send_adapter",
    "pending_human_gates",
    "record_notification",
    "reconstruct",
    "refresh",
    "resolve",
]
