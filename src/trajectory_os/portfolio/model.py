"""M020 — bounded multi-goal portfolio domain model (pure, fail closed).

This module owns the deterministic representation of a portfolio capacity
policy, one read-only goal view, one portfolio scheduling decision and the
durable portfolio state. It performs no I/O and no clock reads.

Isolation invariants (ADR-017):

* a portfolio never becomes a second source of truth for a member goal: it
  only references the canonical per-goal graph/generation, scheduler decision
  and independent completion proof;
* every entry carries the exact ``goal_id``/``graph_id``/``generation_id`` it
  was computed from, so a superseded generation can never be scheduled as if
  it were current;
* decision identity is a domain-separated digest over normalized content and
  never includes a timestamp;
* unknown, malformed, duplicated or cyclic content fails closed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, NoReturn

from trajectory_os.portfolio import identity as portfolio_identity

#: Schema version of every durable portfolio document.
SCHEMA_VERSION = 1

#: Human/machine portfolio version string (additive, never a digest input).
PORTFOLIO_VERSION = "m020.1"

# --- hard bounded limits ------------------------------------------------------

MAX_GOALS = 64
MAX_ENTRIES = 64
MAX_DECISIONS = 256
MAX_EVENTS = 4096
MAX_TIMESTAMP_LEN = 64
MAX_REASON_LEN = 64
MAX_DETAIL_LEN = 256

# --- stable fail-closed error codes ------------------------------------------

E_MALFORMED = "MALFORMED_PORTFOLIO"
E_UNSUPPORTED_VERSION = "UNSUPPORTED_PORTFOLIO_SCHEMA"
E_INVALID_POLICY = "INVALID_PORTFOLIO_POLICY"
E_DUPLICATE_GOAL = "DUPLICATE_PORTFOLIO_GOAL"
E_UNKNOWN_DEPENDENCY = "UNKNOWN_PORTFOLIO_DEPENDENCY"
E_DEPENDENCY_CYCLE = "PORTFOLIO_DEPENDENCY_CYCLE"
E_IDENTITY_MISMATCH = "PORTFOLIO_IDENTITY_MISMATCH"
E_GRAPH_MISMATCH = "PORTFOLIO_GRAPH_MISMATCH"
E_ISOLATION_VIOLATION = "PORTFOLIO_ISOLATION_VIOLATION"
E_OVERFLOW = "PORTFOLIO_OVERFLOW"

# --- stable, machine-readable reason codes -----------------------------------

R_SELECTED = "SELECTED"
R_TERMINAL_COMPLETE = "GOAL_COMPLETE"
R_DEPENDENCY_PENDING = "DEPENDENCY_PENDING"
R_DEPENDENCY_FAILED = "DEPENDENCY_FAILED"
R_NO_READY_WORK = "NO_READY_WORK"
R_CONCURRENCY_LIMIT = "PORTFOLIO_CONCURRENCY_LIMIT"
R_SAFE_STOP = "SAFE_STOP_REQUESTED"
R_STALE_GENERATION = "STALE_GENERATION"
R_BUDGET_EXHAUSTED = "PORTFOLIO_BUDGET_EXHAUSTED"

REASON_CODES = frozenset({
    R_SELECTED, R_TERMINAL_COMPLETE, R_DEPENDENCY_PENDING,
    R_DEPENDENCY_FAILED, R_NO_READY_WORK, R_CONCURRENCY_LIMIT, R_SAFE_STOP,
    R_STALE_GENERATION, R_BUDGET_EXHAUSTED,
})

# --- entry outcomes (closed set) ---------------------------------------------

O_SELECTED = "SELECTED"
O_EXCLUDED = "EXCLUDED"
OUTCOMES = frozenset({O_SELECTED, O_EXCLUDED})

# --- policy bounds ------------------------------------------------------------

MAX_ACTIVE_GOALS = 32
MAX_GLOBAL_CONCURRENCY = 64
MAX_CPU_SLOTS = 1024
MAX_GPU_SLOTS = 16
MAX_GPU_MEM_BYTES = 1 << 50


class PortfolioError(Exception):
    """A portfolio contract violation (fail closed)."""

    def __init__(self, code: str, path: str, detail: str = "") -> None:
        super().__init__(f"{code}: {path}" + (f" ({detail})" if detail else ""))
        self.code = code
        self.path = path
        self.detail = detail


def _fail(code: str, path: str, detail: str = "") -> NoReturn:
    raise PortfolioError(code, path, detail)


def _require_mapping(value: object, path: str, detail: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(E_MALFORMED, path, detail)
    return value


def _require_list(value: object, path: str, detail: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(E_MALFORMED, path, detail)
    return value


def _require_str(value: object, path: str, detail: str, *,
                 maximum: int = MAX_DETAIL_LEN,
                 optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        _fail(E_MALFORMED, path, f"{detail}: non-empty string required")
    if len(value) > maximum:
        _fail(E_MALFORMED, path, f"{detail}: exceeds {maximum} chars")
    return value


def _require_int(value: object, path: str, detail: str, *, minimum: int,
                 maximum: int, code: str = E_MALFORMED) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(code, path, f"{detail}: integer required")
    if value < minimum or value > maximum:
        _fail(code, path, f"{detail}: out of bounds [{minimum}, {maximum}]")
    return value


def _require_bool(value: object, path: str, detail: str) -> bool:
    if not isinstance(value, bool):
        _fail(E_MALFORMED, path, f"{detail}: boolean required")
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


# --- capacity policy ----------------------------------------------------------


@dataclass(frozen=True)
class PortfolioPolicy:
    """Explicit, configured portfolio capacity (never probed, never inferred).

    ``max_active_goals`` bounds how many member goals may hold live work at
    once. ``global_concurrency`` bounds the total number of live node
    reservations across the whole portfolio. ``cpu_slots``/``gpu_slots``/
    ``gpu_mem_bytes`` bound the aggregate admitted demand.
    """

    cpu_slots: int = 8
    gpu_slots: int = 1
    gpu_mem_bytes: int = 8 * (1 << 30)
    global_concurrency: int = 4
    max_active_goals: int = 2

    def validate(self) -> PortfolioPolicy:
        _require_int(self.cpu_slots, "policy", "cpu_slots", minimum=1,
                     maximum=MAX_CPU_SLOTS, code=E_INVALID_POLICY)
        _require_int(self.gpu_slots, "policy", "gpu_slots", minimum=0,
                     maximum=MAX_GPU_SLOTS, code=E_INVALID_POLICY)
        _require_int(self.gpu_mem_bytes, "policy", "gpu_mem_bytes", minimum=0,
                     maximum=MAX_GPU_MEM_BYTES, code=E_INVALID_POLICY)
        _require_int(self.global_concurrency, "policy", "global_concurrency",
                     minimum=1, maximum=MAX_GLOBAL_CONCURRENCY,
                     code=E_INVALID_POLICY)
        _require_int(self.max_active_goals, "policy", "max_active_goals",
                     minimum=1, maximum=MAX_ACTIVE_GOALS,
                     code=E_INVALID_POLICY)
        if self.max_active_goals > self.global_concurrency:
            _fail(E_INVALID_POLICY, "policy",
                  "max_active_goals exceeds global_concurrency")
        return self

    def identity_payload(self) -> dict[str, int]:
        return {
            "schema_version": SCHEMA_VERSION,
            "cpu_slots": self.cpu_slots,
            "gpu_slots": self.gpu_slots,
            "gpu_mem_bytes": self.gpu_mem_bytes,
            "global_concurrency": self.global_concurrency,
            "max_active_goals": self.max_active_goals,
        }

    @property
    def policy_id(self) -> str:
        return portfolio_identity.policy_id(self.identity_payload())

    @property
    def capacity_id(self) -> str:
        return self.policy_id

    def to_dict(self) -> dict[str, int]:
        return dict(self.identity_payload())

    @staticmethod
    def from_dict(doc: object, path: str = "policy") -> PortfolioPolicy:
        mapping = _require_mapping(doc, path, "policy object required")
        _known_keys(mapping, tuple(PortfolioPolicy.__dataclass_fields__)
                    + ("schema_version",), path)
        _require_keys(
            mapping,
            ("cpu_slots", "gpu_slots", "gpu_mem_bytes", "global_concurrency",
             "max_active_goals"),
            path)
        version = mapping.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            _fail(E_UNSUPPORTED_VERSION, path, f"schema_version={version!r}")
        policy = PortfolioPolicy(
            cpu_slots=_require_int(
                mapping["cpu_slots"], path, "cpu_slots", minimum=1,
                maximum=MAX_CPU_SLOTS, code=E_INVALID_POLICY),
            gpu_slots=_require_int(
                mapping["gpu_slots"], path, "gpu_slots", minimum=0,
                maximum=MAX_GPU_SLOTS, code=E_INVALID_POLICY),
            gpu_mem_bytes=_require_int(
                mapping["gpu_mem_bytes"], path, "gpu_mem_bytes", minimum=0,
                maximum=MAX_GPU_MEM_BYTES, code=E_INVALID_POLICY),
            global_concurrency=_require_int(
                mapping["global_concurrency"], path, "global_concurrency",
                minimum=1, maximum=MAX_GLOBAL_CONCURRENCY,
                code=E_INVALID_POLICY),
            max_active_goals=_require_int(
                mapping["max_active_goals"], path, "max_active_goals",
                minimum=1, maximum=MAX_ACTIVE_GOALS,
                code=E_INVALID_POLICY),
        )
        return policy.validate()


DEFAULT_POLICY = PortfolioPolicy()


# --- explicit cross-goal dependencies -----------------------------------------


@dataclass(frozen=True)
class PortfolioDependencies:
    """Deterministic cross-goal dependency edges (goal -> prerequisites)."""

    edges: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def for_goal(self, goal_id: str) -> tuple[str, ...]:
        for node_id, deps in self.edges:
            if node_id == goal_id:
                return deps
        return ()

    def to_dict(self) -> dict[str, list[str]]:
        return {goal: list(deps) for goal, deps in self.edges}

    @staticmethod
    def normalize(doc: object,
                  path: str = "dependencies") -> PortfolioDependencies:
        """Validate and normalize ``{goal: [prereq, ...]}`` (fail closed)."""
        if doc is None:
            return PortfolioDependencies()
        mapping = _require_mapping(doc, path, "dependencies object required")
        if len(mapping) > MAX_GOALS:
            _fail(E_OVERFLOW, path, "too many dependency roots")
        edges: list[tuple[str, tuple[str, ...]]] = []
        for goal in sorted(mapping):
            goal_id = _require_str(goal, path, "goal id", maximum=128)
            assert goal_id is not None
            raw = _require_list(mapping[goal], f"{path}[{goal_id}]",
                                "dependency list required")
            if len(raw) > MAX_GOALS:
                _fail(E_OVERFLOW, f"{path}[{goal_id}]", "too many dependencies")
            deps: list[str] = []
            for item in raw:
                dep = _require_str(item, f"{path}[{goal_id}]", "dependency id",
                                   maximum=128)
                assert dep is not None
                if dep == goal_id:
                    _fail(E_DEPENDENCY_CYCLE, f"{path}[{goal_id}]",
                          "self dependency")
                deps.append(dep)
            edges.append((goal_id, tuple(sorted(set(deps)))))
        return PortfolioDependencies(edges=tuple(edges))


def validate_dependencies(deps: PortfolioDependencies,
                          goal_ids: Sequence[str]) -> None:
    """Fail closed on unknown dependencies or dependency cycles."""
    known = set(goal_ids)
    graph: dict[str, tuple[str, ...]] = {}
    for goal, prereqs in deps.edges:
        if goal not in known:
            # An edge for a non-member goal is inert only when it is not a
            # prerequisite of a member; treat an unknown *root* as malformed.
            _fail(E_UNKNOWN_DEPENDENCY, f"dependencies[{goal}]",
                  "unknown portfolio goal")
        for dep in prereqs:
            if dep not in known:
                _fail(E_UNKNOWN_DEPENDENCY, f"dependencies[{goal}]",
                      f"unknown prerequisite {dep!r}")
        graph[goal] = prereqs
    # Iterative depth-first cycle detection (deterministic order).
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str) -> None:
        stack = [(node, iter(graph.get(node, ())))]
        visiting.add(node)
        while stack:
            current, it = stack[-1]
            advanced = next(it, None)
            if advanced is None:
                visiting.discard(current)
                done.add(current)
                stack.pop()
                continue
            if advanced in done:
                continue
            if advanced in visiting:
                _fail(E_DEPENDENCY_CYCLE, "dependencies",
                      f"cycle via {advanced!r}")
            visiting.add(advanced)
            stack.append((advanced, iter(graph.get(advanced, ()))))

    for node in sorted(graph):
        if node not in done:
            visit(node)


# --- read-only goal view ------------------------------------------------------


@dataclass(frozen=True)
class PortfolioGoalView:
    """Read-only projection of one member goal (never a second store)."""

    goal_id: str
    graph_id: str
    generation_id: str | None
    priority: int
    complete: bool
    final_state: str
    final_reason: str
    ready_nodes: tuple[str, ...]
    blocked_nodes: tuple[str, ...]
    in_progress_nodes: tuple[str, ...]
    active_reservations: int
    reserved_cpu_slots: int
    reserved_gpu_slots: int
    reserved_gpu_mem_bytes: int
    active_missions: tuple[str, ...]
    scheduler_policy_id: str | None
    stale_generation: bool
    stalled: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "graph_id": self.graph_id,
            "generation_id": self.generation_id,
            "priority": self.priority,
            "complete": self.complete,
            "final_state": self.final_state,
            "final_reason": self.final_reason,
            "ready_nodes": list(self.ready_nodes),
            "blocked_nodes": list(self.blocked_nodes),
            "in_progress_nodes": list(self.in_progress_nodes),
            "active_reservations": self.active_reservations,
            "reserved_cpu_slots": self.reserved_cpu_slots,
            "reserved_gpu_slots": self.reserved_gpu_slots,
            "reserved_gpu_mem_bytes": self.reserved_gpu_mem_bytes,
            "active_missions": list(self.active_missions),
            "scheduler_policy_id": self.scheduler_policy_id,
            "stale_generation": self.stale_generation,
            "stalled": self.stalled,
        }


# --- portfolio entry ----------------------------------------------------------


@dataclass(frozen=True)
class PortfolioEntry:
    """One per-goal portfolio decision entry (goal isolated by construction)."""

    goal_id: str
    graph_id: str
    generation_id: str | None
    priority: int
    outcome: str
    reason: str
    ready_nodes: tuple[str, ...]
    active_reservations: int
    scheduler_decision_id: str | None = None
    dispatched: tuple[str, ...] = ()
    dispatch_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "graph_id": self.graph_id,
            "generation_id": self.generation_id,
            "priority": self.priority,
            "outcome": self.outcome,
            "reason": self.reason,
            "ready_nodes": list(self.ready_nodes),
            "active_reservations": self.active_reservations,
            "scheduler_decision_id": self.scheduler_decision_id,
            "dispatched": list(self.dispatched),
            "dispatch_status": self.dispatch_status,
        }

    @staticmethod
    def from_dict(doc: object, path: str) -> PortfolioEntry:
        mapping = _require_mapping(doc, path, "entry object required")
        _known_keys(mapping, tuple(PortfolioEntry.__dataclass_fields__), path)
        _require_keys(mapping, tuple(PortfolioEntry.__dataclass_fields__), path)
        outcome = mapping["outcome"]
        if outcome not in OUTCOMES:
            _fail(E_MALFORMED, path, f"unknown outcome: {outcome!r}")
        reason = mapping["reason"]
        if reason not in REASON_CODES:
            _fail(E_MALFORMED, path, f"unknown reason: {reason!r}")
        scheduler_decision = mapping["scheduler_decision_id"]
        if scheduler_decision is not None and not isinstance(
                scheduler_decision, str):
            _fail(E_MALFORMED, path, "scheduler_decision_id")
        generation = mapping["generation_id"]
        if generation is not None and not isinstance(generation, str):
            _fail(E_MALFORMED, path, "generation_id")
        dispatch_status = mapping["dispatch_status"]
        if dispatch_status is not None and not isinstance(dispatch_status, str):
            _fail(E_MALFORMED, path, "dispatch_status")
        goal_id = _require_str(mapping["goal_id"], path, "goal_id",
                               maximum=128)
        graph_id = _require_str(mapping["graph_id"], path, "graph_id",
                                maximum=128)
        assert goal_id is not None and graph_id is not None
        ready = _require_list(mapping["ready_nodes"], path, "ready_nodes")
        dispatched = _require_list(mapping["dispatched"], path, "dispatched")
        return PortfolioEntry(
            goal_id=goal_id,
            graph_id=graph_id,
            generation_id=generation,
            priority=_require_int(mapping["priority"], path, "priority",
                                  minimum=0, maximum=1_000_000),
            outcome=str(outcome),
            reason=str(reason),
            ready_nodes=tuple(str(item) for item in ready),
            active_reservations=_require_int(
                mapping["active_reservations"], path, "active_reservations",
                minimum=0, maximum=MAX_GLOBAL_CONCURRENCY),
            scheduler_decision_id=scheduler_decision,
            dispatched=tuple(str(item) for item in dispatched),
            dispatch_status=dispatch_status,
        )


# --- portfolio decision -------------------------------------------------------


@dataclass(frozen=True)
class PortfolioDecision:
    """One deterministic portfolio scheduling decision (no timestamp identity)."""

    portfolio_id: str
    goals: tuple[str, ...]
    policy: PortfolioPolicy
    created_at: str
    entries: tuple[PortfolioEntry, ...]
    decision_id: str = ""

    def selected(self) -> tuple[PortfolioEntry, ...]:
        return tuple(e for e in self.entries if e.outcome == O_SELECTED)

    def counts(self) -> dict[str, int]:
        selected = self.selected()
        return {
            "goals_total": len(self.entries),
            "selected": len(selected),
            "excluded": len(self.entries) - len(selected),
            "dispatched": sum(len(e.dispatched) for e in self.entries),
            "active_reservations": sum(
                e.active_reservations for e in self.entries),
        }

    def entry(self, goal_id: str) -> PortfolioEntry | None:
        for item in self.entries:
            if item.goal_id == goal_id:
                return item
        return None

    def identity_payload(self) -> dict[str, Any]:
        """Deterministic payload (excludes the decision timestamp)."""
        return {
            "schema_version": SCHEMA_VERSION,
            "portfolio_version": PORTFOLIO_VERSION,
            "portfolio_id": self.portfolio_id,
            "goals": list(self.goals),
            "policy": self.policy.identity_payload(),
            "entries": [e.to_dict() for e in self.entries],
        }

    def compute_decision_id(self) -> str:
        return portfolio_identity.decision_id(self.identity_payload())

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "policy_id": self.policy.policy_id,
            "created_at": self.created_at,
            "decision_id": self.decision_id,
        }

    @staticmethod
    def build(*, portfolio_id: str, goals: Sequence[str],
              policy: PortfolioPolicy, created_at: str,
              entries: Sequence[PortfolioEntry]) -> PortfolioDecision:
        if len(entries) > MAX_ENTRIES:
            _fail(E_OVERFLOW, "decision", "too many entries")
        ordered = tuple(sorted(entries, key=lambda e: e.goal_id))
        decision = PortfolioDecision(
            portfolio_id=portfolio_id, goals=tuple(sorted(goals)),
            policy=policy, created_at=created_at, entries=ordered)
        return _replace_decision_id(decision)

    @staticmethod
    def from_dict(doc: object, path: str = "decision") -> PortfolioDecision:
        mapping = _require_mapping(doc, path, "decision object required")
        _known_keys(
            mapping,
            ("schema_version", "portfolio_version", "portfolio_id", "goals",
             "policy", "entries", "policy_id", "created_at", "decision_id"),
            path)
        _require_keys(
            mapping,
            ("portfolio_id", "goals", "policy", "entries", "created_at",
             "decision_id"),
            path)
        version = mapping.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            _fail(E_UNSUPPORTED_VERSION, path, f"schema_version={version!r}")
        policy = PortfolioPolicy.from_dict(mapping["policy"], f"{path}.policy")
        raw_goals = _require_list(mapping["goals"], path, "goals")
        goals = tuple(str(item) for item in raw_goals)
        raw_entries = _require_list(mapping["entries"], path, "entries")
        entries = tuple(
            PortfolioEntry.from_dict(item, f"{path}.entries[{index}]")
            for index, item in enumerate(raw_entries))
        portfolio_id = _require_str(mapping["portfolio_id"], path,
                                    "portfolio_id", maximum=128)
        assert portfolio_id is not None
        decision = PortfolioDecision(
            portfolio_id=portfolio_id, goals=goals, policy=policy,
            created_at=str(mapping["created_at"]), entries=entries)
        expected = decision.compute_decision_id()
        stored = mapping["decision_id"]
        if stored != expected:
            _fail(E_IDENTITY_MISMATCH, path,
                  f"decision_id {stored!r} != {expected!r}")
        return _replace_decision_id(decision)


def _replace_decision_id(decision: PortfolioDecision) -> PortfolioDecision:
    return replace(decision, decision_id=decision.compute_decision_id())


# --- durable portfolio state --------------------------------------------------


@dataclass(frozen=True)
class PortfolioState:
    """Durable portfolio state: append-only decision history (never forks)."""

    portfolio_id: str
    policy: PortfolioPolicy
    decision_ids: tuple[str, ...]
    last_decision_id: str | None
    updated_at: str
    cycle_count: int = 0
    superseded_goals: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "portfolio_version": PORTFOLIO_VERSION,
            "portfolio_id": self.portfolio_id,
            "policy": self.policy.identity_payload(),
            "policy_id": self.policy.policy_id,
            "decision_ids": list(self.decision_ids),
            "last_decision_id": self.last_decision_id,
            "updated_at": self.updated_at,
            "cycle_count": self.cycle_count,
            "superseded_goals": list(self.superseded_goals),
        }

    @staticmethod
    def initial(*, portfolio_id: str, policy: PortfolioPolicy,
                updated_at: str) -> PortfolioState:
        return PortfolioState(
            portfolio_id=portfolio_id, policy=policy, decision_ids=(),
            last_decision_id=None, updated_at=updated_at)

    @staticmethod
    def from_dict(doc: object, path: str = "state") -> PortfolioState:
        mapping = _require_mapping(doc, path, "state object required")
        _known_keys(
            mapping,
            ("schema_version", "portfolio_version", "portfolio_id", "policy",
             "policy_id", "decision_ids", "last_decision_id", "updated_at",
             "cycle_count", "superseded_goals"),
            path)
        _require_keys(
            mapping,
            ("portfolio_id", "policy", "decision_ids", "last_decision_id",
             "updated_at"),
            path)
        version = mapping.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            _fail(E_UNSUPPORTED_VERSION, path, f"schema_version={version!r}")
        policy = PortfolioPolicy.from_dict(mapping["policy"], f"{path}.policy")
        raw_ids = _require_list(mapping["decision_ids"], path, "decision_ids")
        decision_ids = tuple(str(item) for item in raw_ids)
        if len(decision_ids) > MAX_DECISIONS:
            _fail(E_OVERFLOW, path, "too many decision ids")
        for decision_id in decision_ids:
            if not portfolio_identity.is_valid_digest(decision_id):
                _fail(E_MALFORMED, path, f"bad decision id: {decision_id!r}")
        last = mapping["last_decision_id"]
        if last is not None:
            if not isinstance(last, str) \
                    or not portfolio_identity.is_valid_digest(last):
                _fail(E_MALFORMED, path, "last_decision_id")
            if last not in decision_ids:
                _fail(E_IDENTITY_MISMATCH, path,
                      "last_decision_id is not tracked")
        goal = _require_str(mapping["portfolio_id"], path, "portfolio_id",
                            maximum=128)
        assert goal is not None
        superseded_raw = _require_list(
            mapping.get("superseded_goals", []), path, "superseded_goals")
        return PortfolioState(
            portfolio_id=goal,
            policy=policy,
            decision_ids=decision_ids,
            last_decision_id=last,
            updated_at=str(mapping["updated_at"]),
            cycle_count=_require_int(
                mapping.get("cycle_count", 0), path, "cycle_count",
                minimum=0, maximum=1_000_000),
            superseded_goals=tuple(str(item) for item in superseded_raw),
        )


# --- isolation check ----------------------------------------------------------


def assert_isolation(entries: Sequence[PortfolioEntry]) -> None:
    """Fail closed if two entries share a goal identity or decision identity.

    This is the executable statement of M020 isolation: a portfolio decision
    can never collapse two goals into one identity, and a per-goal scheduler
    decision can never be attributed to two goals.
    """
    seen_goals: set[str] = set()
    seen_decisions: set[str] = set()
    for entry in entries:
        if entry.goal_id in seen_goals:
            _fail(E_ISOLATION_VIOLATION, "entries",
                  f"duplicate goal {entry.goal_id!r}")
        seen_goals.add(entry.goal_id)
        decision = entry.scheduler_decision_id
        if decision is not None:
            if decision in seen_decisions:
                _fail(E_ISOLATION_VIOLATION, "entries",
                      f"shared scheduler decision {decision!r}")
            seen_decisions.add(decision)
