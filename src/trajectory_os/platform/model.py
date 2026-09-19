"""M048–M055 — persistent autonomous operator platform: shared vocabulary.

This package is an additive composition layer *above* the existing M017–M047
architecture. It introduces no competing mission/release lifecycle, readiness
model, semantic patch identity, event model, trust model or human gate. Every
durable document here is either:

* a new identity that lives *above* missions (project, queue entry, inbox
  notification, routing evidence, backup manifest), or
* a read-only projection derived from the canonical M030/M031/M036–M047
  artifacts.

Trust invariants (unchanged):

* no module in this package performs a Git release write (commit, push, merge,
  reset, restore, clean, stash, rebase, checkout, switch);
* observation is read-only; mutation is explicit and auditable;
* only the existing release layer may cross a commit/push/merge boundary and
  only behind an explicit human ``GO COMMIT`` / ``GO MERGE`` authorization;
* recovery and restore are idempotent and fail closed under contradiction.
"""

from __future__ import annotations

from typing import NoReturn

#: Schema version of every platform document.
SCHEMA_VERSION = 1

#: Human/machine platform version string (additive).
PLATFORM_VERSION = "m055.1"

#: One shared storage namespace below the canonical mission root.
PLATFORM_DIR = "platform"

PROJECTS_DIR = "projects"
PROJECT_INDEX_NAME = "projects.json"
PROJECT_NAME = "project.json"

SUPERVISOR_DIR = "supervisor"
SUPERVISOR_STATE_NAME = "state.json"
SUPERVISOR_OWNER_NAME = "owner.lock"
SUPERVISOR_HEARTBEAT_NAME = "heartbeat.json"
SUPERVISOR_STOP_NAME = "stop.json"

QUEUE_DIR = "queue"
QUEUE_STATE_NAME = "state.json"
QUEUE_DECISIONS_NAME = "decisions.jsonl"

INBOX_DIR = "inbox"
INBOX_RECORDS_NAME = "notifications.jsonl"
INBOX_INDEX_NAME = "inbox.json"

LIFEOS_DIR = "lifeos"
LIFEOS_RECORDS_NAME = "lifeos-records.jsonl"

EFFICIENCY_DIR = "efficiency"
EFFICIENCY_EVIDENCE_NAME = "routing-evidence.json"
EFFICIENCY_HISTORY_NAME = "routing-history.jsonl"

HARDENING_DIR = "hardening"
BACKUP_MANIFEST_NAME = "backup-manifest.json"
RESTORE_REPORT_NAME = "restore-report.json"
DR_REPORT_NAME = "disaster-recovery.json"
CONFIG_NAME = "platform-config.json"
SYSTEMD_UNIT_NAME = "trajectory-os.service"

# --- bounded limits -----------------------------------------------------------

MAX_PROJECTS = 128
MAX_OBJECTIVES = 256
MAX_MISSIONS_PER_OBJECTIVE = 512
MAX_STR_LEN = 512
MAX_DESC_LEN = 8192
MAX_ROUTING_PREFS = 16
MAX_QUEUE_ENTRIES = 512
MAX_QUEUE_DECISIONS = 4096
MAX_INBOX_RECORDS = 8192
MAX_TEXT_LEN = 2048
MAX_TIMESTAMP_LEN = 64
MAX_CYCLES_BOUND = 100000
MAX_SUPERVISOR_HISTORY = 4096

# --- stable fail-closed error codes (closed set) ------------------------------

E_MALFORMED = "MALFORMED_PLATFORM_DOCUMENT"
E_UNSUPPORTED_VERSION = "UNSUPPORTED_PLATFORM_SCHEMA"
E_PROJECT_MISSING = "PROJECT_MISSING"
E_PROJECT_EXISTS = "PROJECT_EXISTS"
E_PROJECT_INVALID = "PROJECT_INVALID"
E_PROJECT_ARCHIVED = "PROJECT_ARCHIVED"
E_PROJECT_ENVIRONMENT = "PROJECT_ENVIRONMENT_MUTATION_FORBIDDEN"
E_SUPERVISOR_OWNED = "SUPERVISOR_ALREADY_OWNED"
E_SUPERVISOR_NOT_OWNED = "SUPERVISOR_NOT_OWNER"
E_SUPERVISOR_STALE = "SUPERVISOR_STALE_OWNER"
E_SUPERVISOR_CONTRADICTION = "SUPERVISOR_CONTRADICTION"
E_QUEUE_INVALID = "QUEUE_ENTRY_INVALID"
E_QUEUE_MISSING = "QUEUE_ENTRY_MISSING"
E_QUEUE_DEPENDENCY = "QUEUE_DEPENDENCY_BLOCKED"
E_QUEUE_EXISTS = "QUEUE_ENTRY_EXISTS"
E_QUEUE_CYCLE = "QUEUE_DEPENDENCY_CYCLE"
E_INBOX_MISSING = "INBOX_RECORD_MISSING"
E_INBOX_INVALID = "INBOX_RECORD_INVALID"
E_API_BIND_FORBIDDEN = "API_BIND_FORBIDDEN"
E_API_UNAUTHORIZED = "API_UNAUTHORIZED"
E_API_CSRF = "API_CSRF_INVALID"
E_LIFEOS_TARGET = "LIFEOS_TARGET_INVALID"
E_LIFEOS_UNAVAILABLE = "LIFEOS_VAULT_UNAVAILABLE"
E_LIFEOS_SECRET = "LIFEOS_SECRET_DETECTED"
E_EFFICIENCY_INVALID = "EFFICIENCY_RECORD_INVALID"
E_EFFICIENCY_UNSUPPORTED_CLAIM = "EFFICIENCY_UNSUPPORTED_CLAIM"
E_BACKUP_INVALID = "BACKUP_INVALID"
E_RESTORE_CONTRADICTION = "RESTORE_CONTRADICTION"
E_CORRUPTION = "CORRUPTION_DETECTED"
E_STATE_CONTRADICTION = "CANONICAL_STATE_CONTRADICTION"
E_MIGRATION_INVALID = "SCHEMA_MIGRATION_INVALID"
E_GIT_WRITE_FORBIDDEN = "PLATFORM_GIT_WRITE_FORBIDDEN"

PLATFORM_ERROR_CODES = frozenset({
    E_MALFORMED, E_UNSUPPORTED_VERSION, E_PROJECT_MISSING, E_PROJECT_EXISTS,
    E_PROJECT_INVALID, E_PROJECT_ARCHIVED, E_PROJECT_ENVIRONMENT,
    E_SUPERVISOR_OWNED, E_SUPERVISOR_NOT_OWNED, E_SUPERVISOR_STALE,
    E_SUPERVISOR_CONTRADICTION, E_QUEUE_INVALID, E_QUEUE_MISSING,
    E_QUEUE_DEPENDENCY, E_QUEUE_EXISTS, E_QUEUE_CYCLE, E_INBOX_MISSING,
    E_INBOX_INVALID, E_API_BIND_FORBIDDEN, E_API_UNAUTHORIZED, E_API_CSRF,
    E_LIFEOS_TARGET, E_LIFEOS_UNAVAILABLE, E_LIFEOS_SECRET, E_EFFICIENCY_INVALID,
    E_EFFICIENCY_UNSUPPORTED_CLAIM, E_BACKUP_INVALID, E_RESTORE_CONTRADICTION,
    E_CORRUPTION, E_STATE_CONTRADICTION, E_MIGRATION_INVALID,
    E_GIT_WRITE_FORBIDDEN,
})


class PlatformError(Exception):
    """A fail-closed platform refusal (stable ``code``)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def fail(code: str, detail: str = "") -> NoReturn:
    raise PlatformError(code, detail)


__all__ = [
    "BACKUP_MANIFEST_NAME",
    "CONFIG_NAME",
    "DR_REPORT_NAME",
    "EFFICIENCY_DIR",
    "EFFICIENCY_EVIDENCE_NAME",
    "EFFICIENCY_HISTORY_NAME",
    "E_API_BIND_FORBIDDEN",
    "E_API_CSRF",
    "E_API_UNAUTHORIZED",
    "E_BACKUP_INVALID",
    "E_CORRUPTION",
    "E_EFFICIENCY_INVALID",
    "E_EFFICIENCY_UNSUPPORTED_CLAIM",
    "E_GIT_WRITE_FORBIDDEN",
    "E_INBOX_INVALID",
    "E_INBOX_MISSING",
    "E_LIFEOS_SECRET",
    "E_LIFEOS_TARGET",
    "E_LIFEOS_UNAVAILABLE",
    "E_MALFORMED",
    "E_MIGRATION_INVALID",
    "E_PROJECT_ARCHIVED",
    "E_PROJECT_ENVIRONMENT",
    "E_PROJECT_EXISTS",
    "E_PROJECT_INVALID",
    "E_PROJECT_MISSING",
    "E_QUEUE_CYCLE",
    "E_QUEUE_DEPENDENCY",
    "E_QUEUE_EXISTS",
    "E_QUEUE_INVALID",
    "E_QUEUE_MISSING",
    "E_RESTORE_CONTRADICTION",
    "E_STATE_CONTRADICTION",
    "E_SUPERVISOR_CONTRADICTION",
    "E_SUPERVISOR_NOT_OWNED",
    "E_SUPERVISOR_OWNED",
    "E_SUPERVISOR_STALE",
    "E_UNSUPPORTED_VERSION",
    "HARDENING_DIR",
    "INBOX_DIR",
    "INBOX_INDEX_NAME",
    "INBOX_RECORDS_NAME",
    "LIFEOS_DIR",
    "LIFEOS_RECORDS_NAME",
    "MAX_CYCLES_BOUND",
    "MAX_DESC_LEN",
    "MAX_INBOX_RECORDS",
    "MAX_MISSIONS_PER_OBJECTIVE",
    "MAX_OBJECTIVES",
    "MAX_PROJECTS",
    "MAX_QUEUE_DECISIONS",
    "MAX_QUEUE_ENTRIES",
    "MAX_ROUTING_PREFS",
    "MAX_STR_LEN",
    "MAX_SUPERVISOR_HISTORY",
    "MAX_TEXT_LEN",
    "MAX_TIMESTAMP_LEN",
    "PLATFORM_DIR",
    "PLATFORM_ERROR_CODES",
    "PLATFORM_VERSION",
    "PROJECTS_DIR",
    "PROJECT_INDEX_NAME",
    "PROJECT_NAME",
    "PlatformError",
    "QUEUE_DECISIONS_NAME",
    "QUEUE_DIR",
    "QUEUE_STATE_NAME",
    "RESTORE_REPORT_NAME",
    "SCHEMA_VERSION",
    "SUPERVISOR_DIR",
    "SUPERVISOR_HEARTBEAT_NAME",
    "SUPERVISOR_OWNER_NAME",
    "SUPERVISOR_STATE_NAME",
    "SUPERVISOR_STOP_NAME",
    "SYSTEMD_UNIT_NAME",
    "fail",
]
