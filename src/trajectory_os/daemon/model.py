"""M021 — bounded persistent-daemon domain model (pure, fail closed).

The daemon is a *bounded composition loop* over the M020 portfolio engine: it
owns no goal state of its own and never becomes a second source of truth. It
persists only its authoritative runtime state and an append-only cycle log so
that a restarted process resumes exactly where it stopped.

Design invariants (ADR-017):

* authoritative work state stays in the canonical graph/mission/scheduler/
  portfolio stores; the daemon state records only cycle accounting and the
  exact decision identities already persisted;
* a safe stop is a request file — a bounded cycle is never interrupted;
* a malformed, oversized or unsupported state fails closed;
* no module performs a Git trust-boundary write.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn

from trajectory_os.portfolio import identity as portfolio_identity
from trajectory_os.portfolio import model as portfolio_model

#: Schema version of every durable daemon document.
SCHEMA_VERSION = 1

#: Human/machine daemon version string (additive, never a digest input).
DAEMON_VERSION = "m021.1"

# --- hard bounded limits ------------------------------------------------------

MAX_CYCLES_BOUND = 4096
DEFAULT_MAX_CYCLES = 32
MAX_CYCLE_HISTORY = 1024
MAX_GOALS = portfolio_model.MAX_GOALS
MAX_TIMESTAMP_LEN = 64

# --- stable fail-closed error codes ------------------------------------------

E_MALFORMED = "MALFORMED_DAEMON"
E_UNSUPPORTED_VERSION = "UNSUPPORTED_DAEMON_SCHEMA"
E_INVALID_CONFIG = "INVALID_DAEMON_CONFIG"
E_OVERFLOW = "DAEMON_OVERFLOW"
E_IDENTITY_MISMATCH = "DAEMON_IDENTITY_MISMATCH"

# --- daemon statuses (closed set) --------------------------------------------

DS_RUNNING = "RUNNING"
DS_COMPLETE = "COMPLETE"
DS_STOPPED = "STOPPED"
DS_CYCLE_BOUND = "CYCLE_BOUND"
DS_IDLE = "IDLE"
DS_ERROR = "ERROR"

STATUSES = frozenset({
    DS_RUNNING, DS_COMPLETE, DS_STOPPED, DS_CYCLE_BOUND, DS_IDLE, DS_ERROR,
})

#: Portfolio cycle statuses a daemon cycle may record (mirrors M020).
CYCLE_STATUSES = frozenset({
    "PLANNED", "DISPATCHED", "NO_DISPATCH", "STOPPED",
})


class DaemonError(Exception):
    """A daemon contract violation (fail closed)."""

    def __init__(self, code: str, path: str, detail: str = "") -> None:
        super().__init__(f"{code}: {path}" + (f" ({detail})" if detail else ""))
        self.code = code
        self.path = path
        self.detail = detail


def _fail(code: str, path: str, detail: str = "") -> NoReturn:
    raise DaemonError(code, path, detail)


def _require_mapping(value: object, path: str, detail: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(E_MALFORMED, path, detail)
    return value


def _require_list(value: object, path: str, detail: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(E_MALFORMED, path, detail)
    return value


def _require_int(value: object, path: str, detail: str, *, minimum: int,
                 maximum: int, code: str = E_MALFORMED) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(code, path, f"{detail}: integer required")
    if value < minimum or value > maximum:
        _fail(code, path, f"{detail}: out of bounds [{minimum}, {maximum}]")
    return value


def _require_str(value: object, path: str, detail: str, *,
                 maximum: int = 256, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        _fail(E_MALFORMED, path, f"{detail}: non-empty string required")
    if len(value) > maximum:
        _fail(E_MALFORMED, path, f"{detail}: exceeds {maximum} chars")
    return value


def _known_keys(mapping: Mapping[str, Any], allowed: Sequence[str],
                path: str) -> None:
    unknown = sorted(set(mapping) - set(allowed))
    if unknown:
        _fail(E_MALFORMED, path, f"unknown keys: {unknown}")


def _require_keys(mapping: Mapping[str, Any], required: Sequence[str],
                  path: str) -> None:
    missing = sorted(set(required) - set(mapping))
    if missing:
        _fail(E_MALFORMED, path, f"missing keys: {missing}")


# --- configuration ------------------------------------------------------------


@dataclass(frozen=True)
class DaemonConfig:
    """Bounded daemon session configuration (never inferred)."""

    max_cycles: int = DEFAULT_MAX_CYCLES
    session_subruns: int = 1
    policy: portfolio_model.PortfolioPolicy = \
        portfolio_model.DEFAULT_POLICY
    dependencies: portfolio_model.PortfolioDependencies = \
        portfolio_model.PortfolioDependencies()

    def validate(self) -> DaemonConfig:
        _require_int(self.max_cycles, "config", "max_cycles", minimum=1,
                     maximum=MAX_CYCLES_BOUND, code=E_INVALID_CONFIG)
        _require_int(self.session_subruns, "config", "session_subruns",
                     minimum=1, maximum=64, code=E_INVALID_CONFIG)
        self.policy.validate()
        if len(self.dependencies.edges) > MAX_GOALS:
            _fail(E_OVERFLOW, "config", "too many dependency edges")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "max_cycles": self.max_cycles,
            "session_subruns": self.session_subruns,
            "policy": self.policy.identity_payload(),
            "dependencies": self.dependencies.to_dict(),
        }

    @staticmethod
    def from_dict(doc: object, path: str = "config") -> DaemonConfig:
        mapping = _require_mapping(doc, path, "config object required")
        _known_keys(
            mapping,
            ("schema_version", "max_cycles", "session_subruns", "policy",
             "dependencies"),
            path)
        _require_keys(mapping, ("max_cycles", "session_subruns", "policy"),
                      path)
        version = mapping.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            _fail(E_UNSUPPORTED_VERSION, path, f"schema_version={version!r}")
        return DaemonConfig(
            max_cycles=_require_int(
                mapping["max_cycles"], path, "max_cycles", minimum=1,
                maximum=MAX_CYCLES_BOUND, code=E_INVALID_CONFIG),
            session_subruns=_require_int(
                mapping["session_subruns"], path, "session_subruns",
                minimum=1, maximum=64, code=E_INVALID_CONFIG),
            policy=portfolio_model.PortfolioPolicy.from_dict(
                mapping["policy"], f"{path}.policy"),
            dependencies=portfolio_model.PortfolioDependencies.normalize(
                mapping.get("dependencies"), f"{path}.dependencies"),
        ).validate()


# --- cycle record -------------------------------------------------------------


@dataclass(frozen=True)
class DaemonCycle:
    """One persisted daemon cycle (bounded, identity-preserving)."""

    cycle: int
    portfolio_id: str
    decision_id: str | None
    status: str
    selected: tuple[str, ...]
    completed_goals: tuple[str, ...]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "cycle": self.cycle,
            "portfolio_id": self.portfolio_id,
            "decision_id": self.decision_id,
            "status": self.status,
            "selected": list(self.selected),
            "completed_goals": list(self.completed_goals),
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(doc: object, path: str = "cycle") -> DaemonCycle:
        mapping = _require_mapping(doc, path, "cycle object required")
        _known_keys(mapping, tuple(DaemonCycle.__dataclass_fields__)
                    + ("schema_version",), path)
        _require_keys(
            mapping,
            ("cycle", "portfolio_id", "decision_id", "status", "selected",
             "completed_goals", "created_at"),
            path)
        version = mapping.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            _fail(E_UNSUPPORTED_VERSION, path, f"schema_version={version!r}")
        status = mapping["status"]
        if status not in CYCLE_STATUSES:
            _fail(E_MALFORMED, path, f"unknown status: {status!r}")
        decision = mapping["decision_id"]
        if decision is not None and not portfolio_identity.is_valid_digest(
                decision):
            _fail(E_MALFORMED, path, "decision_id")
        portfolio = _require_str(mapping["portfolio_id"], path,
                                 "portfolio_id", maximum=128)
        assert portfolio is not None
        selected = _require_list(mapping["selected"], path, "selected")
        completed = _require_list(mapping["completed_goals"], path,
                                  "completed_goals")
        return DaemonCycle(
            cycle=_require_int(mapping["cycle"], path, "cycle", minimum=1,
                               maximum=1_000_000),
            portfolio_id=portfolio,
            decision_id=decision,
            status=str(status),
            selected=tuple(str(item) for item in selected),
            completed_goals=tuple(str(item) for item in completed),
            created_at=str(mapping["created_at"]),
        )


# --- durable daemon state -----------------------------------------------------


@dataclass(frozen=True)
class DaemonState:
    """Durable daemon runtime state (restart-safe, never a work source)."""

    status: str
    cycles_executed: int
    portfolio_id: str | None
    decision_ids: tuple[str, ...]
    last_cycle: DaemonCycle | None
    started_at: str
    updated_at: str
    schema_version: int = SCHEMA_VERSION

    @property
    def running(self) -> bool:
        return self.status == DS_RUNNING

    @property
    def stopped(self) -> bool:
        return self.status == DS_STOPPED

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "daemon_version": DAEMON_VERSION,
            "status": self.status,
            "cycles_executed": self.cycles_executed,
            "portfolio_id": self.portfolio_id,
            "decision_ids": list(self.decision_ids),
            "last_cycle": (None if self.last_cycle is None
                           else self.last_cycle.to_dict()),
            "started_at": self.started_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def initial(*, started_at: str) -> DaemonState:
        return DaemonState(
            status=DS_RUNNING, cycles_executed=0, portfolio_id=None,
            decision_ids=(), last_cycle=None, started_at=started_at,
            updated_at=started_at)

    @staticmethod
    def from_dict(doc: object, path: str = "state") -> DaemonState:
        mapping = _require_mapping(doc, path, "state object required")
        _known_keys(
            mapping,
            ("schema_version", "daemon_version", "status", "cycles_executed",
             "portfolio_id", "decision_ids", "last_cycle", "started_at",
             "updated_at"),
            path)
        _require_keys(
            mapping,
            ("status", "cycles_executed", "decision_ids", "started_at",
             "updated_at"),
            path)
        version = mapping.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            _fail(E_UNSUPPORTED_VERSION, path, f"schema_version={version!r}")
        status = mapping["status"]
        if status not in STATUSES:
            _fail(E_MALFORMED, path, f"unknown status: {status!r}")
        raw_ids = _require_list(mapping["decision_ids"], path, "decision_ids")
        decision_ids = tuple(str(item) for item in raw_ids)
        if len(decision_ids) > MAX_CYCLE_HISTORY:
            _fail(E_OVERFLOW, path, "too many decision ids")
        for decision_id in decision_ids:
            if not portfolio_identity.is_valid_digest(decision_id):
                _fail(E_MALFORMED, path, f"bad decision id: {decision_id!r}")
        raw_cycle = mapping.get("last_cycle")
        last_cycle = (None if raw_cycle is None
                      else DaemonCycle.from_dict(raw_cycle,
                                                 f"{path}.last_cycle"))
        portfolio = mapping.get("portfolio_id")
        if portfolio is not None and not isinstance(portfolio, str):
            _fail(E_MALFORMED, path, "portfolio_id")
        return DaemonState(
            status=str(status),
            cycles_executed=_require_int(
                mapping["cycles_executed"], path, "cycles_executed",
                minimum=0, maximum=1_000_000),
            portfolio_id=portfolio,
            decision_ids=decision_ids,
            last_cycle=last_cycle,
            started_at=str(mapping["started_at"]),
            updated_at=str(mapping["updated_at"]),
        )
