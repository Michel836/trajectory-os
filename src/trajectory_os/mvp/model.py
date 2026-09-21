"""MVP — personal execution & decision system domain model (V0).

This is the *personal* trajectory mirror: a structured portfolio of real
projects, their work breakdown, task dependencies, blockers, external waiting
states, effort estimates, hard time constraints and recorded outcomes.

Design invariants:

* **typed and fail closed** — every document is schema-versioned and validated
  before use; unknown fields, bad ids, dangling references and dependency
  cycles are rejected, never silently tolerated;
* **provenance stays explicit** — urgency/impact/effort/deadline are *supplied*
  by the user (or a later LLM adapter) and are never invented by this layer;
  ``UNKNOWN`` stays ``UNKNOWN``;
* **deterministic** — readiness, prioritisation and scheduling are pure
  functions of the portfolio plus an injected clock; nothing reads the wall
  clock unless a callable is injected;
* **human control** — the model only *proposes*; outcomes are recorded by the
  human and nothing is auto-executed.

This module reuses the shared fail-closed primitives and atomic persistence
helpers from :mod:`trajectory_os.intelligence.model`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, NoReturn

#: Schema version of every durable MVP document.
SCHEMA_VERSION = 1

#: Human/machine MVP version string (additive).
MVP_VERSION = "mvp.1"

# --- bounded limits -----------------------------------------------------------
#
# These are fail-closed sanity bounds (reject absurd/corrupt input), NOT
# modelling limits. A brain-dump document may legitimately contain a hundred
# or more candidate topics once semantically expanded, so the portfolio bound
# is deliberately generous: it exists to stop runaway writes, never to force a
# user to discard legitimate material.

MAX_PROJECTS = 256
MAX_TASKS = 2048
MAX_RESOURCES = 64
MAX_EVENTS = 256
MAX_DEPENDENCIES_PER_TASK = 16
MAX_BLOCKERS_PER_TASK = 16
MAX_WAITING_PER_TASK = 8
MAX_RESOURCES_PER_TASK = 8
MAX_STR_LEN = 200
MAX_TEXT_LEN = 4096
MAX_DEADLINE_LEN = 10

#: Stable lowercase slug grammar for ids (1..64 chars).
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

#: Deadline grammar (strict ISO date).
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# --- project statuses (closed set) --------------------------------------------

PS_ACTIVE = "ACTIVE"
PS_WAITING = "WAITING"
PS_BLOCKED = "BLOCKED"
PS_DEFERRED = "DEFERRED"
PS_COMPLETED = "COMPLETED"

PROJECT_STATUSES = frozenset({
    PS_ACTIVE, PS_WAITING, PS_BLOCKED, PS_DEFERRED, PS_COMPLETED,
})

#: Project statuses in which new work may still be started.
PROJECT_OPEN_STATUSES = frozenset({PS_ACTIVE, PS_WAITING, PS_BLOCKED})

# --- task statuses (closed set) -----------------------------------------------

TS_TODO = "TODO"
TS_IN_PROGRESS = "IN_PROGRESS"
TS_BLOCKED = "BLOCKED"
TS_WAITING = "WAITING"
TS_COMPLETED = "COMPLETED"
TS_DEFERRED = "DEFERRED"
TS_ABANDONED = "ABANDONED"

TASK_STATUSES = frozenset({
    TS_TODO, TS_IN_PROGRESS, TS_BLOCKED, TS_WAITING, TS_COMPLETED,
    TS_DEFERRED, TS_ABANDONED,
})

#: Task statuses that are terminal for readiness purposes.
TASK_TERMINAL_STATUSES = frozenset({TS_COMPLETED, TS_ABANDONED})

# --- urgency / impact ---------------------------------------------------------

U_CRITICAL = "CRITICAL"
U_HIGH = "HIGH"
U_MEDIUM = "MEDIUM"
U_LOW = "LOW"

URGENCIES = frozenset({U_CRITICAL, U_HIGH, U_MEDIUM, U_LOW})
URGENCY_RANK = {U_CRITICAL: 4, U_HIGH: 3, U_MEDIUM: 2, U_LOW: 1}

I_HIGH = "HIGH"
I_MEDIUM = "MEDIUM"
I_LOW = "LOW"

IMPACTS = frozenset({I_HIGH, I_MEDIUM, I_LOW})
IMPACT_RANK = {I_HIGH: 3, I_MEDIUM: 2, I_LOW: 1}

# --- outcomes (closed set) ----------------------------------------------------

O_COMPLETED = "COMPLETED"
O_DEFERRED = "DEFERRED"
O_BLOCKED = "BLOCKED"
O_ABANDONED = "ABANDONED"

OUTCOMES = frozenset({O_COMPLETED, O_DEFERRED, O_BLOCKED, O_ABANDONED})

# --- resource kinds -----------------------------------------------------------

RK_PERSON = "person"
RK_TOOL = "tool"
RK_LOCATION = "location"
RK_BUDGET = "budget"
RK_OTHER = "other"

RESOURCE_KINDS = frozenset({RK_PERSON, RK_TOOL, RK_LOCATION, RK_BUDGET,
                            RK_OTHER})

# --- stable fail-closed error codes -------------------------------------------

E_MALFORMED = "MALFORMED_MVP"
E_UNSUPPORTED_VERSION = "UNSUPPORTED_MVP_SCHEMA"
E_UNKNOWN_ID = "UNKNOWN_MVP_ID"
E_DUPLICATE_ID = "DUPLICATE_MVP_ID"
E_UNKNOWN_PROJECT = "UNKNOWN_MVP_PROJECT"
E_UNKNOWN_RESOURCE = "UNKNOWN_MVP_RESOURCE"
E_SELF_REFERENCE = "MVP_SELF_REFERENCE"
E_DEPENDENCY_CYCLE = "MVP_DEPENDENCY_CYCLE"
E_INVALID_DATE = "INVALID_MVP_DATE"
E_OVERFLOW = "MVP_OVERFLOW"


class MvpError(Exception):
    """An MVP contract violation (fail closed)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _fail(code: str, detail: str = "") -> NoReturn:
    raise MvpError(code, detail)


# --- primitive helpers --------------------------------------------------------


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_str(value: object, field: str, *, maximum: int = MAX_STR_LEN,
                 optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        _fail(E_MALFORMED, f"{field} must be a non-empty string")
    if len(value) > maximum:
        _fail(E_MALFORMED, f"{field} exceeds {maximum} chars")
    return value


def _optional_str(value: object, field: str,
                  *, maximum: int = MAX_STR_LEN) -> str | None:
    return _require_str(value, field, maximum=maximum, optional=True)


def _require_id(value: object, field: str) -> str:
    text = _require_str(value, field, maximum=64)
    assert text is not None
    if not ID_RE.fullmatch(text):
        _fail(E_MALFORMED, f"{field}={text!r} is not a valid id")
    return text


def _require_int(value: object, field: str, *, minimum: int = 0,
                 maximum: int | None = None) -> int:
    if not _is_int(value):
        _fail(E_MALFORMED, f"{field} must be an integer")
    assert isinstance(value, int)
    if value < minimum or (maximum is not None and value > maximum):
        _fail(E_MALFORMED, f"{field} out of bounds: {value}")
    return value


def _optional_int(value: object, field: str, *, minimum: int = 0,
                  maximum: int | None = None) -> int | None:
    if value is None:
        return None
    return _require_int(value, field, minimum=minimum, maximum=maximum)


def _require_enum(value: object, field: str, allowed: frozenset[str]) -> str:
    text = _require_str(value, field, maximum=32)
    assert text is not None
    if text not in allowed:
        _fail(E_MALFORMED, f"{field}={text!r} not in {sorted(allowed)}")
    return text


def _require_deadline(value: object, field: str) -> str | None:
    if value is None:
        return None
    text = _require_str(value, field, maximum=MAX_DEADLINE_LEN)
    assert text is not None
    if not DATE_RE.fullmatch(text):
        _fail(E_INVALID_DATE, f"{field}={text!r} must be YYYY-MM-DD")
    try:
        date.fromisoformat(text)
    except ValueError:
        _fail(E_INVALID_DATE, f"{field}={text!r} is not a real date")
    return text


def _require_str_list(value: object, field: str, *, maximum: int) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(
            isinstance(item, str) for item in value):
        _fail(E_MALFORMED, f"{field} must be a list of strings")
    if len(value) > maximum:
        _fail(E_OVERFLOW, f"{field} has too many entries")
    return tuple(_require_id(item, field) for item in value)


def _require_text_list(value: object, field: str, *, maximum: int) -> tuple[str, ...]:
    """Validate a list of free-text strings (not ids)."""
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(
            isinstance(item, str) for item in value):
        _fail(E_MALFORMED, f"{field} must be a list of strings")
    if len(value) > maximum:
        _fail(E_OVERFLOW, f"{field} has too many entries")
    result: list[str] = []
    for item in value:
        text = _require_str(item, field, maximum=MAX_STR_LEN)
        assert text is not None
        result.append(text)
    return tuple(result)


# --- entities -----------------------------------------------------------------


@dataclass(frozen=True)
class Resource:
    """One explicit resource a task may require."""

    resource_id: str
    name: str
    kind: str = RK_OTHER
    available: bool = True
    capacity_minutes_per_day: int | None = None

    def validate(self) -> Resource:
        _require_id(self.resource_id, "resource_id")
        _require_str(self.name, "name")
        _require_enum(self.kind, "kind", RESOURCE_KINDS)
        if not isinstance(self.available, bool):
            _fail(E_MALFORMED, "available must be a boolean")
        if self.capacity_minutes_per_day is not None:
            _require_int(self.capacity_minutes_per_day, "capacity",
                         minimum=1, maximum=1440)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "name": self.name,
            "kind": self.kind,
            "available": self.available,
            "capacity_minutes_per_day": self.capacity_minutes_per_day,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> Resource:
        return Resource(
            resource_id=str(data.get("resource_id", "")),
            name=str(data.get("name", "")),
            kind=str(data.get("kind", RK_OTHER)),
            available=bool(data.get("available", True)),
            capacity_minutes_per_day=_optional_int(
                data.get("capacity_minutes_per_day"), "capacity", minimum=1,
                maximum=1440),
        ).validate()


@dataclass(frozen=True)
class CalendarEvent:
    """One hard calendar constraint (never invented by the scheduler)."""

    event_id: str
    title: str
    day: str  # YYYY-MM-DD
    start_minutes: int  # minutes since midnight
    end_minutes: int  # minutes since midnight, > start
    all_day: bool = False

    def validate(self) -> CalendarEvent:
        _require_id(self.event_id, "event_id")
        _require_str(self.title, "title")
        _require_deadline(self.day, "day")
        _require_int(self.start_minutes, "start_minutes", minimum=0,
                     maximum=1439)
        _require_int(self.end_minutes, "end_minutes", minimum=1,
                     maximum=1440)
        if self.end_minutes <= self.start_minutes:
            _fail(E_MALFORMED, "event must end after it starts")
        if not isinstance(self.all_day, bool):
            _fail(E_MALFORMED, "all_day must be a boolean")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "title": self.title,
            "day": self.day,
            "start_minutes": self.start_minutes,
            "end_minutes": self.end_minutes,
            "all_day": self.all_day,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> CalendarEvent:
        return CalendarEvent(
            event_id=str(data.get("event_id", "")),
            title=str(data.get("title", "")),
            day=str(data.get("day", "")),
            start_minutes=_require_int(data.get("start_minutes"), "start",
                                       minimum=0, maximum=1439),
            end_minutes=_require_int(data.get("end_minutes"), "end",
                                     minimum=1, maximum=1440),
            all_day=bool(data.get("all_day", False)),
        ).validate()


@dataclass(frozen=True)
class Project:
    """One real project in the portfolio."""

    project_id: str
    name: str
    objective: str
    status: str = PS_ACTIVE
    domain: str = "personal"
    urgency: str = U_MEDIUM
    impact: str = I_MEDIUM
    deadline: str | None = None
    description: str = ""

    def validate(self) -> Project:
        _require_id(self.project_id, "project_id")
        _require_str(self.name, "name")
        _require_str(self.objective, "objective", maximum=MAX_TEXT_LEN)
        _require_enum(self.status, "status", PROJECT_STATUSES)
        _require_str(self.domain, "domain")
        _require_enum(self.urgency, "urgency", URGENCIES)
        _require_enum(self.impact, "impact", IMPACTS)
        _require_deadline(self.deadline, "deadline")
        if self.description:
            _require_str(self.description, "description",
                         maximum=MAX_TEXT_LEN)
        return self

    @property
    def open(self) -> bool:
        return self.status in PROJECT_OPEN_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "objective": self.objective,
            "status": self.status,
            "domain": self.domain,
            "urgency": self.urgency,
            "impact": self.impact,
            "deadline": self.deadline,
            "description": self.description,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> Project:
        return Project(
            project_id=str(data.get("project_id", "")),
            name=str(data.get("name", "")),
            objective=str(data.get("objective", "")),
            status=str(data.get("status", PS_ACTIVE)),
            domain=str(data.get("domain", "personal")),
            urgency=str(data.get("urgency", U_MEDIUM)),
            impact=str(data.get("impact", I_MEDIUM)),
            deadline=_require_deadline(data.get("deadline"), "deadline"),
            description=str(data.get("description", "")),
        ).validate()


@dataclass(frozen=True)
class Task:
    """One task inside a project's work breakdown."""

    task_id: str
    project_id: str
    title: str
    description: str = ""
    workstream: str = ""
    deliverable: str = ""
    status: str = TS_TODO
    estimated_minutes: int | None = None
    actual_minutes: int | None = None
    deadline: str | None = None
    urgency: str = U_MEDIUM
    impact: str = I_MEDIUM
    dependencies: tuple[str, ...] = ()
    blocked_by: tuple[str, ...] = ()
    waiting_for: tuple[str, ...] = ()
    resources: tuple[str, ...] = ()
    next_action: str = ""
    created_at: str = ""
    updated_at: str = ""

    def validate(self) -> Task:
        _require_id(self.task_id, "task_id")
        _require_id(self.project_id, "project_id")
        _require_str(self.title, "title")
        if self.description:
            _require_str(self.description, "description",
                         maximum=MAX_TEXT_LEN)
        if self.workstream:
            _require_str(self.workstream, "workstream")
        if self.deliverable:
            _require_str(self.deliverable, "deliverable")
        _require_enum(self.status, "status", TASK_STATUSES)
        if self.estimated_minutes is not None:
            _require_int(self.estimated_minutes, "estimated_minutes",
                         minimum=1, maximum=1440)
        if self.actual_minutes is not None:
            _require_int(self.actual_minutes, "actual_minutes", minimum=0,
                         maximum=1440)
        _require_deadline(self.deadline, "deadline")
        _require_enum(self.urgency, "urgency", URGENCIES)
        _require_enum(self.impact, "impact", IMPACTS)
        _require_str_list(self.dependencies, "dependencies",
                          maximum=MAX_DEPENDENCIES_PER_TASK)
        _require_str_list(self.blocked_by, "blocked_by",
                          maximum=MAX_BLOCKERS_PER_TASK)
        _require_text_list(self.waiting_for, "waiting_for",
                           maximum=MAX_WAITING_PER_TASK)
        _require_str_list(self.resources, "resources",
                          maximum=MAX_RESOURCES_PER_TASK)
        if self.next_action:
            _require_str(self.next_action, "next_action",
                         maximum=MAX_TEXT_LEN)
        return self

    @property
    def terminal(self) -> bool:
        return self.status in TASK_TERMINAL_STATUSES

    @property
    def effort_minutes(self) -> int:
        return self.estimated_minutes or 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "project_id": self.project_id,
            "title": self.title,
            "description": self.description,
            "workstream": self.workstream,
            "deliverable": self.deliverable,
            "status": self.status,
            "estimated_minutes": self.estimated_minutes,
            "actual_minutes": self.actual_minutes,
            "deadline": self.deadline,
            "urgency": self.urgency,
            "impact": self.impact,
            "dependencies": list(self.dependencies),
            "blocked_by": list(self.blocked_by),
            "waiting_for": list(self.waiting_for),
            "resources": list(self.resources),
            "next_action": self.next_action,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> Task:
        return Task(
            task_id=str(data.get("task_id", "")),
            project_id=str(data.get("project_id", "")),
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            workstream=str(data.get("workstream", "")),
            deliverable=str(data.get("deliverable", "")),
            status=str(data.get("status", TS_TODO)),
            estimated_minutes=_optional_int(data.get("estimated_minutes"),
                                            "estimated_minutes", minimum=1,
                                            maximum=1440),
            actual_minutes=_optional_int(data.get("actual_minutes"),
                                         "actual_minutes", minimum=0,
                                         maximum=1440),
            deadline=_require_deadline(data.get("deadline"), "deadline"),
            urgency=str(data.get("urgency", U_MEDIUM)),
            impact=str(data.get("impact", I_MEDIUM)),
            dependencies=tuple(str(x) for x in (data.get("dependencies") or [])),
            blocked_by=tuple(str(x) for x in (data.get("blocked_by") or [])),
            waiting_for=tuple(str(x) for x in (data.get("waiting_for") or [])),
            resources=tuple(str(x) for x in (data.get("resources") or [])),
            next_action=str(data.get("next_action", "")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        ).validate()


@dataclass(frozen=True)
class Capacity:
    """Explicit daily capacity and buffer policy (never inferred)."""

    minutes_per_day: int = 360
    buffer_ratio: float = 0.30

    def validate(self) -> Capacity:
        _require_int(self.minutes_per_day, "minutes_per_day", minimum=60,
                     maximum=960)
        if not isinstance(self.buffer_ratio, (int, float)) \
                or isinstance(self.buffer_ratio, bool):
            _fail(E_MALFORMED, "buffer_ratio must be a number")
        if not 0.0 <= float(self.buffer_ratio) < 1.0:
            _fail(E_MALFORMED, "buffer_ratio must be in [0.0, 1.0)")
        return self

    @property
    def usable_minutes(self) -> int:
        return int(self.minutes_per_day * (1.0 - float(self.buffer_ratio)))

    @property
    def buffer_minutes(self) -> int:
        return self.minutes_per_day - self.usable_minutes

    def to_dict(self) -> dict[str, Any]:
        return {
            "minutes_per_day": self.minutes_per_day,
            "buffer_ratio": self.buffer_ratio,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> Capacity:
        return Capacity(
            minutes_per_day=_require_int(data.get("minutes_per_day", 360),
                                         "minutes_per_day", minimum=60,
                                         maximum=960),
            buffer_ratio=float(data.get("buffer_ratio", 0.30)),
        ).validate()


@dataclass(frozen=True)
class OutcomeRecord:
    """One immutable, human-recorded execution outcome."""

    task_id: str
    outcome: str
    recorded_at: str
    actual_minutes: int | None = None
    note: str = ""

    def validate(self) -> OutcomeRecord:
        _require_id(self.task_id, "task_id")
        _require_enum(self.outcome, "outcome", OUTCOMES)
        _require_str(self.recorded_at, "recorded_at", maximum=64)
        if self.actual_minutes is not None:
            _require_int(self.actual_minutes, "actual_minutes", minimum=0,
                         maximum=1440)
        if self.note:
            _require_str(self.note, "note", maximum=MAX_TEXT_LEN)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "task_id": self.task_id,
            "outcome": self.outcome,
            "recorded_at": self.recorded_at,
            "actual_minutes": self.actual_minutes,
            "note": self.note,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> OutcomeRecord:
        return OutcomeRecord(
            task_id=str(data.get("task_id", "")),
            outcome=str(data.get("outcome", "")),
            recorded_at=str(data.get("recorded_at", "")),
            actual_minutes=_optional_int(data.get("actual_minutes"),
                                         "actual_minutes", minimum=0,
                                         maximum=1440),
            note=str(data.get("note", "")),
        ).validate()


@dataclass(frozen=True)
class Portfolio:
    """The whole structured personal portfolio."""

    schema_version: int
    name: str
    projects: tuple[Project, ...]
    tasks: tuple[Task, ...]
    resources: tuple[Resource, ...] = ()
    calendar: tuple[CalendarEvent, ...] = ()
    capacity: Capacity = field(default_factory=Capacity)
    created_at: str = ""
    updated_at: str = ""

    def validate(self) -> Portfolio:
        if self.schema_version != SCHEMA_VERSION:
            _fail(E_UNSUPPORTED_VERSION, f"schema_version={self.schema_version}")
        _require_str(self.name, "name")
        if len(self.projects) > MAX_PROJECTS:
            _fail(E_OVERFLOW, "too many projects")
        if len(self.tasks) > MAX_TASKS:
            _fail(E_OVERFLOW, "too many tasks")
        if len(self.resources) > MAX_RESOURCES:
            _fail(E_OVERFLOW, "too many resources")
        if len(self.calendar) > MAX_EVENTS:
            _fail(E_OVERFLOW, "too many calendar events")
        self.capacity.validate()

        project_ids = _validate_unique_ids([p.project_id for p in self.projects],
                                           "project")
        task_ids = _validate_unique_ids([t.task_id for t in self.tasks], "task")
        resource_ids = _validate_unique_ids(
            [r.resource_id for r in self.resources], "resource")
        _validate_unique_ids(
            [e.event_id for e in self.calendar], "event")

        for project in self.projects:
            project.validate()
        for resource in self.resources:
            resource.validate()
        for event in self.calendar:
            event.validate()

        for task in self.tasks:
            task.validate()
            if task.project_id not in project_ids:
                _fail(E_UNKNOWN_PROJECT, task.task_id)
            for dep in (*task.dependencies, *task.blocked_by):
                if dep == task.task_id:
                    _fail(E_SELF_REFERENCE, task.task_id)
                if dep not in task_ids:
                    _fail(E_UNKNOWN_ID, f"{task.task_id} -> {dep}")
            for resource_id in task.resources:
                if resource_id not in resource_ids:
                    _fail(E_UNKNOWN_RESOURCE, f"{task.task_id} -> {resource_id}")

        _validate_no_cycles(self.tasks)
        return self

    def project_map(self) -> dict[str, Project]:
        return {p.project_id: p for p in self.projects}

    def task_map(self) -> dict[str, Task]:
        return {t.task_id: t for t in self.tasks}

    def resource_map(self) -> dict[str, Resource]:
        return {r.resource_id: r for r in self.resources}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mvp_version": MVP_VERSION,
            "name": self.name,
            "projects": [p.to_dict() for p in self.projects],
            "tasks": [t.to_dict() for t in self.tasks],
            "resources": [r.to_dict() for r in self.resources],
            "calendar": [e.to_dict() for e in self.calendar],
            "capacity": self.capacity.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> Portfolio:
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            _fail(E_UNSUPPORTED_VERSION,
                  f"schema_version={version!r}")
        raw_projects = data.get("projects") or []
        raw_tasks = data.get("tasks") or []
        raw_resources = data.get("resources") or []
        raw_calendar = data.get("calendar") or []
        if not isinstance(raw_projects, list) or not isinstance(raw_tasks, list):
            _fail(E_MALFORMED, "projects/tasks must be lists")
        raw_capacity = data.get("capacity")
        capacity_data: Mapping[str, Any] = (
            raw_capacity if isinstance(raw_capacity, Mapping) else {})
        return Portfolio(
            schema_version=int(version),
            name=str(data.get("name", "personal-portfolio")),
            projects=tuple(Project.from_dict(item)
                           for item in raw_projects
                           if isinstance(item, Mapping)),
            tasks=tuple(Task.from_dict(item)
                        for item in raw_tasks if isinstance(item, Mapping)),
            resources=tuple(Resource.from_dict(item)
                            for item in raw_resources
                            if isinstance(item, Mapping)),
            calendar=tuple(CalendarEvent.from_dict(item)
                           for item in raw_calendar
                           if isinstance(item, Mapping)),
            capacity=Capacity.from_dict(capacity_data),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        ).validate()


def _validate_unique_ids(ids: Sequence[str], label: str) -> set[str]:
    seen: set[str] = set()
    for identifier in ids:
        if identifier in seen:
            _fail(E_DUPLICATE_ID, f"{label} {identifier!r}")
        seen.add(identifier)
    return seen


def _validate_no_cycles(tasks: Sequence[Task]) -> None:
    """Fail closed on any dependency/blocker cycle reachable from a task."""
    adjacency: dict[str, list[str]] = {}
    for task in tasks:
        adjacency[task.task_id] = [*task.dependencies, *task.blocked_by]
    white, grey, black = 0, 1, 2
    colour = dict.fromkeys(adjacency, white)

    def visit(node: str, stack: list[str]) -> None:
        colour[node] = grey
        stack.append(node)
        for dep in adjacency[node]:
            if colour[dep] == grey:
                start = stack.index(dep)
                _fail(E_DEPENDENCY_CYCLE,
                      " -> ".join([*stack[start:], dep]))
            if colour[dep] == white:
                visit(dep, stack)
        stack.pop()
        colour[node] = black

    for node in sorted(adjacency):
        if colour[node] == white:
            visit(node, [])


__all__ = [
    "I_HIGH", "I_LOW", "I_MEDIUM", "IMPACTS", "IMPACT_RANK",
    "O_ABANDONED", "O_BLOCKED", "O_COMPLETED", "O_DEFERRED", "OUTCOMES",
    "PS_ACTIVE", "PS_BLOCKED", "PS_COMPLETED", "PS_DEFERRED", "PS_WAITING",
    "PROJECT_OPEN_STATUSES", "PROJECT_STATUSES",
    "RK_BUDGET", "RK_LOCATION", "RK_OTHER", "RK_PERSON", "RK_TOOL",
    "RESOURCE_KINDS",
    "SCHEMA_VERSION", "MVP_VERSION",
    "TS_ABANDONED", "TS_BLOCKED", "TS_COMPLETED", "TS_DEFERRED",
    "TS_IN_PROGRESS", "TS_TODO", "TS_WAITING",
    "TASK_STATUSES", "TASK_TERMINAL_STATUSES",
    "U_CRITICAL", "U_HIGH", "U_LOW", "U_MEDIUM", "URGENCIES", "URGENCY_RANK",
    "CalendarEvent", "Capacity", "MvpError", "OutcomeRecord", "Portfolio",
    "Project", "Resource", "Task",
]
