"""M028 — LifeOS integration domain (explicit, disableable, fail isolated).

LifeOS adapters exchange a *scoped* projection of canonical state (events and
artifact references) with external life-management tools. They are advisory
sinks: the canonical graph/mission/scheduler/proof/artifact stores are read
only, and every adapter write is confined to an explicitly configured target
directory that is structurally forbidden from living inside the canonical
state root. An adapter failure is captured as a record and can never corrupt
canonical state or stop the other adapters.

Design invariants:

* **explicit** — every adapter is named in configuration and can be disabled;
* **scoped** — only the owning goal's bounded events/artifact references are
  exchanged; the scope is part of the exchange identity;
* **provenance** — every exchange records the exact exchange/event/artifact
  ids, adapter kind, output digests and status;
* **idempotent** — the ledger is keyed by ``(adapter, exchange_id)``: an
  unchanged projection re-exchanges as a no-op;
* **isolated** — adapter exceptions are captured, never propagated into the
  canonical path, and never affect the other adapters.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, NoReturn

#: Schema version of every durable LifeOS document.
SCHEMA_VERSION = 1

#: Human/machine LifeOS version string (additive).
LIFEOS_VERSION = "m028.1"

# --- adapter kinds (closed set) ----------------------------------------------

AK_OBSIDIAN = "obsidian"
AK_SUPER_PRODUCTIVITY = "super-productivity"
AK_JSON_MANIFEST = "json-manifest"

ADAPTER_KINDS = frozenset({
    AK_OBSIDIAN, AK_SUPER_PRODUCTIVITY, AK_JSON_MANIFEST,
})

# --- exchange statuses (closed set) ------------------------------------------

XS_OK = "OK"
XS_SKIPPED = "SKIPPED"
XS_FAILED = "FAILED"
XS_DISABLED = "DISABLED"

EXCHANGE_STATUSES = frozenset({XS_OK, XS_SKIPPED, XS_FAILED, XS_DISABLED})

# --- bounded limits ----------------------------------------------------------

MAX_ADAPTERS = 16
MAX_EVENTS = 512
MAX_ARTIFACTS = 256
MAX_RECORDS = 256
MAX_PATH_LEN = 4096
MAX_STR_LEN = 256
MAX_OPTIONS = 32
MAX_OPTION_VALUE_LEN = 1024

# --- stable fail-closed error codes ------------------------------------------

E_MALFORMED = "MALFORMED_LIFEOS"
E_UNSUPPORTED_VERSION = "UNSUPPORTED_LIFEOS_SCHEMA"
E_INVALID_CONFIG = "INVALID_LIFEOS_CONFIG"
E_TARGET_INSIDE_ROOT = "LIFEOS_TARGET_INSIDE_CANONICAL_ROOT"
E_TARGET_MISSING = "LIFEOS_TARGET_MISSING"
E_OVERFLOW = "LIFEOS_OVERFLOW"
E_CANONICAL_MUTATED = "LIFEOS_CANONICAL_STATE_MUTATED"


class LifeOSError(Exception):
    """A LifeOS integration contract violation (fail closed)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _fail(code: str, detail: str = "") -> NoReturn:
    raise LifeOSError(code, detail)


def _require_str(value: object, field: str, *, maximum: int = MAX_STR_LEN,
                 optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        _fail(E_MALFORMED, f"{field} must be a non-empty string")
    if len(value) > maximum:
        _fail(E_MALFORMED, f"{field} exceeds {maximum} chars")
    return value


@dataclass(frozen=True)
class ExchangeScope:
    """Explicit, bounded scope of one adapter exchange."""

    event_categories: tuple[str, ...] = ()
    include_artifacts: bool = True
    max_events: int = MAX_EVENTS
    max_artifacts: int = MAX_ARTIFACTS

    def validate(self) -> ExchangeScope:
        if self.max_events < 0 or self.max_events > MAX_EVENTS:
            _fail(E_INVALID_CONFIG, "max_events out of bounds")
        if self.max_artifacts < 0 or self.max_artifacts > MAX_ARTIFACTS:
            _fail(E_INVALID_CONFIG, "max_artifacts out of bounds")
        for category in self.event_categories:
            _require_str(category, "event_category")
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            "event_categories": list(self.event_categories),
            "include_artifacts": self.include_artifacts,
            "max_events": self.max_events,
            "max_artifacts": self.max_artifacts,
        }

    @staticmethod
    def from_dict(data: object) -> ExchangeScope:
        if data is None:
            return ExchangeScope()
        if not isinstance(data, Mapping):
            _fail(E_MALFORMED, "scope must be an object")
        categories = data.get("event_categories") or []
        if not isinstance(categories, list):
            _fail(E_MALFORMED, "event_categories must be a list")
        return ExchangeScope(
            event_categories=tuple(str(item) for item in categories),
            include_artifacts=bool(data.get("include_artifacts", True)),
            max_events=int(data.get("max_events", MAX_EVENTS)),
            max_artifacts=int(data.get("max_artifacts", MAX_ARTIFACTS)),
        ).validate()


@dataclass(frozen=True)
class AdapterConfig:
    """One explicitly configured (and disableable) adapter."""

    kind: str
    enabled: bool = True
    target: str | None = None
    options: Mapping[str, str] = field(default_factory=dict)

    def validate(self) -> AdapterConfig:
        if self.kind not in ADAPTER_KINDS:
            _fail(E_INVALID_CONFIG, f"unknown adapter kind {self.kind!r}")
        if self.enabled:
            _require_str(self.target, "target", maximum=MAX_PATH_LEN)
        if len(self.options) > MAX_OPTIONS:
            _fail(E_INVALID_CONFIG, "too many options")
        for key, value in self.options.items():
            _require_str(key, "option key")
            _require_str(value, f"option {key}",
                         maximum=MAX_OPTION_VALUE_LEN)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "enabled": self.enabled,
            "target": self.target,
            "options": dict(self.options),
        }

    @staticmethod
    def from_dict(data: object) -> AdapterConfig:
        if not isinstance(data, Mapping):
            _fail(E_MALFORMED, "adapter config must be an object")
        options = data.get("options") or {}
        if not isinstance(options, Mapping):
            _fail(E_MALFORMED, "adapter options must be an object")
        return AdapterConfig(
            kind=str(data.get("kind")),
            enabled=bool(data.get("enabled", True)),
            target=(str(data["target"]) if data.get("target") is not None
                    else None),
            options={str(k): str(v) for k, v in options.items()},
        ).validate()


@dataclass(frozen=True)
class LifeOSConfig:
    """Explicit LifeOS integration configuration (never inferred)."""

    adapters: tuple[AdapterConfig, ...] = ()
    scope: ExchangeScope = field(default_factory=ExchangeScope)

    def validate(self) -> LifeOSConfig:
        if len(self.adapters) > MAX_ADAPTERS:
            _fail(E_INVALID_CONFIG, "too many adapters")
        kinds = [adapter.kind for adapter in self.adapters]
        if len(set(kinds)) != len(kinds):
            _fail(E_INVALID_CONFIG, "duplicate adapter kind")
        for adapter in self.adapters:
            adapter.validate()
        self.scope.validate()
        return self

    @property
    def enabled(self) -> tuple[AdapterConfig, ...]:
        return tuple(a for a in self.adapters if a.enabled)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "lifeos_version": LIFEOS_VERSION,
            "adapters": [a.to_dict() for a in self.adapters],
            "scope": self.scope.identity_payload(),
        }

    @staticmethod
    def from_dict(data: object) -> LifeOSConfig:
        if not isinstance(data, Mapping):
            _fail(E_MALFORMED, "config must be an object")
        version = data.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            _fail(E_UNSUPPORTED_VERSION, f"schema_version={version!r}")
        raw = data.get("adapters") or []
        if not isinstance(raw, list):
            _fail(E_MALFORMED, "adapters must be a list")
        return LifeOSConfig(
            adapters=tuple(AdapterConfig.from_dict(item) for item in raw),
            scope=ExchangeScope.from_dict(data.get("scope")),
        ).validate()


@dataclass(frozen=True)
class OutputFile:
    """One file an adapter wrote (content-addressed, bounded provenance)."""

    path: str
    sha256: str
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256,
                "size_bytes": self.size_bytes}


@dataclass(frozen=True)
class ExchangeRecord:
    """Durable provenance of one (adapter, exchange) attempt."""

    adapter: str
    exchange_id: str
    goal_id: str
    status: str
    event_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    outputs: tuple[OutputFile, ...]
    created_at: str
    error: str | None = None
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "adapter": self.adapter,
            "exchange_id": self.exchange_id,
            "goal_id": self.goal_id,
            "status": self.status,
            "event_ids": list(self.event_ids),
            "artifact_ids": list(self.artifact_ids),
            "outputs": [output.to_dict() for output in self.outputs],
            "created_at": self.created_at,
            "error": self.error,
        }

    @staticmethod
    def build(*, adapter: str, exchange_id: str, goal_id: str, status: str,
              event_ids: Sequence[str], artifact_ids: Sequence[str],
              outputs: Sequence[OutputFile], created_at: str,
              error: str | None = None) -> ExchangeRecord:
        if adapter not in ADAPTER_KINDS:
            _fail(E_MALFORMED, f"unknown adapter {adapter!r}")
        if status not in EXCHANGE_STATUSES:
            _fail(E_MALFORMED, f"unknown status {status!r}")
        _require_str(exchange_id, "exchange_id")
        _require_str(created_at, "created_at", maximum=64)
        return ExchangeRecord(
            adapter=adapter, exchange_id=exchange_id, goal_id=goal_id,
            status=status, event_ids=tuple(event_ids),
            artifact_ids=tuple(artifact_ids), outputs=tuple(outputs),
            created_at=created_at, error=error)

    @staticmethod
    def from_dict(data: object) -> ExchangeRecord:
        if not isinstance(data, Mapping):
            _fail(E_MALFORMED, "record must be an object")
        version = data.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            _fail(E_UNSUPPORTED_VERSION, "record schema")
        raw_outputs = data.get("outputs") or []
        if not isinstance(raw_outputs, list):
            _fail(E_MALFORMED, "outputs must be a list")
        outputs = tuple(
            OutputFile(
                path=str(item["path"]),
                sha256=str(item["sha256"]),
                size_bytes=int(item["size_bytes"]),
            )
            for item in raw_outputs if isinstance(item, Mapping)
        )
        return ExchangeRecord.build(
            adapter=str(data.get("adapter")),
            exchange_id=str(data.get("exchange_id")),
            goal_id=str(data.get("goal_id")),
            status=str(data.get("status")),
            event_ids=tuple(str(i) for i in (data.get("event_ids") or [])),
            artifact_ids=tuple(
                str(i) for i in (data.get("artifact_ids") or [])),
            outputs=outputs,
            created_at=str(data.get("created_at")),
            error=(str(data["error"]) if data.get("error") is not None
                   else None),
        )
