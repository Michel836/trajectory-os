"""MVP — lightweight Week + Tomorrow planning sidecar.

The cockpit already answers *what is ready* and *what fits today*. This module
adds the missing continuity layer between days without turning TrajectoryOS
into a calendar planner:

* a **Tomorrow plan** — a human-selected set of up to 3 main tasks and up to 2
  optional tasks, prepared the day before and surfaced first on the next day;
* a **Week plan** — 3 to 5 outcome-level priorities for the current week;
* a **review** projection — what was completed, what is unfinished or blocked,
  and which carry-over candidates exist.

Design invariants, aligned with the rest of the MVP:

* **presentation/planning only** — the sidecar never mutates the portfolio.
  It lives in its own atomically written document (``planning.json``); the
  portfolio remains the single source of truth for status, readiness and
  effort;
* **human in control** — nothing is scheduled silently and nothing rolls over
  automatically. A task only enters tomorrow's plan when the human saves it;
* **fail closed on write, fail soft on read** — invalid writes raise, while a
  malformed sidecar falls back to an empty plan so the cockpit still opens;
* **no new opaque scoring** — the Tomorrow candidate list reuses the existing
  deterministic readiness and priority explanations verbatim.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any, NoReturn

from trajectory_os.intelligence import model as intel_model
from trajectory_os.mvp import model, priority, readiness, scheduler, store

#: Durable planning sidecar filename (relative to a data root).
PLANNING_NAME = "planning.json"

#: Planning schema version (independent from the portfolio schema).
PLANNING_SCHEMA_VERSION = 1

#: Tomorrow plan bounds (deliberately small; do not silently schedule).
MAX_MAIN_TASKS = 3
MAX_OPTIONAL_TASKS = 2

#: Weekly outcome bounds.
MIN_WEEK_OUTCOMES = 3
MAX_WEEK_OUTCOMES = 5

#: Bounded text limits.
MAX_TITLE_LEN = 200
MAX_NOTE_LEN = 1000

#: How far ahead a deadline still counts as "near".
NEAR_DEADLINE_DAYS = 7

#: Weekly outcome lifecycle states.
OUTCOME_OPEN = "OPEN"
OUTCOME_DONE = "DONE"
OUTCOME_DROPPED = "DROPPED"
OUTCOME_STATES = (OUTCOME_OPEN, OUTCOME_DONE, OUTCOME_DROPPED)

#: Presentation-only ordering keys for the four planning/review views.
TOMORROW_SORTS = ("planned", "priority", "urgency", "impact", "effort",
                  "name", "project")
WEEK_SORTS = ("manual", "project", "progress", "urgency")

_WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                  "Saturday", "Sunday")


class PlanningError(model.MvpError):
    """A planning-sidecar contract violation (fail closed on write)."""


def _fail(detail: str) -> NoReturn:
    raise PlanningError(model.E_MALFORMED, detail)


# --- primitive validation -----------------------------------------------------


def _require_date(value: object, field_name: str) -> str:
    if not isinstance(value, str) or len(value) != 10 \
            or not model.DATE_RE.fullmatch(value):
        _fail(f"{field_name} must be YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError:
        _fail(f"{field_name}={value!r} is not a real date")
    return value


def _clean_text(value: object, field_name: str, *, maximum: int,
                optional: bool = False) -> str:
    if value is None and optional:
        return ""
    if not isinstance(value, str):
        _fail(f"{field_name} must be a string")
    text = value.strip()
    if not text and not optional:
        _fail(f"{field_name} must not be empty")
    if len(text) > maximum:
        _fail(f"{field_name} exceeds {maximum} chars")
    return text


def _clean_id(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{field_name} must be a non-empty id")
    if not model.ID_RE.fullmatch(value):
        _fail(f"{field_name}={value!r} is not a valid id")
    return value


def _clean_id_list(value: object, field_name: str, *, maximum: int,
                   ) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        _fail(f"{field_name} must be a list")
    if len(value) > maximum:
        _fail(f"{field_name} allows at most {maximum} entries")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        identifier = _clean_id(item, field_name)
        if identifier in seen:
            _fail(f"{field_name} has a duplicate entry")
        seen.add(identifier)
        result.append(identifier)
    return tuple(result)


def monday_of(day: date) -> date:
    """Return the Monday that starts the ISO week containing ``day``."""
    return day - timedelta(days=day.weekday())


def week_bounds(week_start: str) -> tuple[str, str]:
    """Return ``(start, end)`` ISO dates for the seven-day week."""
    start = date.fromisoformat(_require_date(week_start, "week_start"))
    return start.isoformat(), (start + timedelta(days=6)).isoformat()


# --- domain dataclasses -------------------------------------------------------


@dataclass(frozen=True)
class WeeklyOutcome:
    """One human-selected outcome-level priority for a week."""

    outcome_id: str
    title: str
    project_id: str | None = None
    note: str = ""
    state: str = OUTCOME_OPEN
    progress: str = ""
    carried_from: str | None = None

    def validate(self) -> WeeklyOutcome:
        _clean_id(self.outcome_id, "outcome_id")
        _clean_text(self.title, "title", maximum=MAX_TITLE_LEN)
        if self.project_id is not None:
            _clean_id(self.project_id, "project_id")
        _clean_text(self.note, "note", maximum=MAX_NOTE_LEN, optional=True)
        if self.state not in OUTCOME_STATES:
            _fail(f"state={self.state!r} not in {list(OUTCOME_STATES)}")
        _clean_text(self.progress, "progress", maximum=MAX_TITLE_LEN,
                    optional=True)
        if self.carried_from is not None:
            _require_date(self.carried_from, "carried_from")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome_id": self.outcome_id,
            "project_id": self.project_id,
            "title": self.title,
            "note": self.note,
            "state": self.state,
            "progress": self.progress,
            "carried_from": self.carried_from,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> WeeklyOutcome:
        return WeeklyOutcome(
            outcome_id=str(data.get("outcome_id", "")),
            title=str(data.get("title", "")),
            project_id=(str(data["project_id"])
                        if data.get("project_id") is not None else None),
            note=str(data.get("note", "")),
            state=str(data.get("state", OUTCOME_OPEN)),
            progress=str(data.get("progress", "")),
            carried_from=(str(data["carried_from"])
                          if data.get("carried_from") is not None else None),
        ).validate()


@dataclass(frozen=True)
class WeekPlan:
    """A bounded set of weekly outcomes plus an optional note."""

    week_start: str
    outcomes: tuple[WeeklyOutcome, ...] = ()
    note: str = ""
    saved_at: str = ""

    def validate(self) -> WeekPlan:
        _require_date(self.week_start, "week_start")
        _clean_text(self.note, "note", maximum=MAX_NOTE_LEN, optional=True)
        if len(self.outcomes) > MAX_WEEK_OUTCOMES:
            _fail(f"week plan allows at most {MAX_WEEK_OUTCOMES} outcomes")
        seen: set[str] = set()
        for outcome in self.outcomes:
            outcome.validate()
            if outcome.outcome_id in seen:
                _fail(f"duplicate outcome_id {outcome.outcome_id!r}")
            seen.add(outcome.outcome_id)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "week_start": self.week_start,
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
            "note": self.note,
            "saved_at": self.saved_at,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> WeekPlan:
        raw = data.get("outcomes") or []
        outcomes: list[WeeklyOutcome] = []
        if isinstance(raw, (list, tuple)):
            for item in raw:
                if isinstance(item, Mapping):
                    outcomes.append(WeeklyOutcome.from_dict(item))
        return WeekPlan(
            week_start=str(data.get("week_start", "")),
            outcomes=tuple(outcomes),
            note=str(data.get("note", "")),
            saved_at=str(data.get("saved_at", "")),
        ).validate()

    def outcome_by_id(self, outcome_id: str) -> WeeklyOutcome | None:
        for outcome in self.outcomes:
            if outcome.outcome_id == outcome_id:
                return outcome
        return None


@dataclass(frozen=True)
class TomorrowPlan:
    """A human-selected main/optional task set for one calendar day."""

    date: str
    main_task_ids: tuple[str, ...] = ()
    optional_task_ids: tuple[str, ...] = ()
    note: str = ""
    saved_at: str = ""

    def validate(self) -> TomorrowPlan:
        _require_date(self.date, "date")
        main = _clean_id_list(self.main_task_ids, "main_task_ids",
                              maximum=MAX_MAIN_TASKS)
        optional = _clean_id_list(self.optional_task_ids,
                                  "optional_task_ids",
                                  maximum=MAX_OPTIONAL_TASKS)
        if not main and not optional:
            _fail("a tomorrow plan needs at least one selected task")
        overlap = set(main) & set(optional)
        if overlap:
            _fail("a task cannot be both main and optional")
        _clean_text(self.note, "note", maximum=MAX_NOTE_LEN, optional=True)
        return self

    def all_task_ids(self) -> tuple[str, ...]:
        return (*self.main_task_ids, *self.optional_task_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "main_task_ids": list(self.main_task_ids),
            "optional_task_ids": list(self.optional_task_ids),
            "note": self.note,
            "saved_at": self.saved_at,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> TomorrowPlan:
        return TomorrowPlan(
            date=str(data.get("date", "")),
            main_task_ids=tuple(str(x) for x in
                                (data.get("main_task_ids") or [])),
            optional_task_ids=tuple(str(x) for x in
                                    (data.get("optional_task_ids") or [])),
            note=str(data.get("note", "")),
            saved_at=str(data.get("saved_at", "")),
        ).validate()


@dataclass(frozen=True)
class PlanningState:
    """The whole planning sidecar (presentation state, never business data)."""

    schema_version: int = PLANNING_SCHEMA_VERSION
    week_plan: WeekPlan | None = None
    tomorrow_plan: TomorrowPlan | None = None

    def validate(self) -> PlanningState:
        if self.schema_version != PLANNING_SCHEMA_VERSION:
            _fail(f"unsupported planning schema_version={self.schema_version}")
        if self.week_plan is not None:
            self.week_plan.validate()
        if self.tomorrow_plan is not None:
            self.tomorrow_plan.validate()
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "week_plan": (self.week_plan.to_dict()
                          if self.week_plan is not None else None),
            "tomorrow_plan": (self.tomorrow_plan.to_dict()
                              if self.tomorrow_plan is not None else None),
        }

    @staticmethod
    def from_dict(data: object) -> PlanningState:
        if not isinstance(data, Mapping):
            _fail("planning state must be an object")
        raw_week = data.get("week_plan")
        raw_tomorrow = data.get("tomorrow_plan")
        return PlanningState(
            schema_version=int(data.get("schema_version",
                                        PLANNING_SCHEMA_VERSION)),
            week_plan=(WeekPlan.from_dict(raw_week)
                       if isinstance(raw_week, Mapping) else None),
            tomorrow_plan=(TomorrowPlan.from_dict(raw_tomorrow)
                           if isinstance(raw_tomorrow, Mapping) else None),
        ).validate()


# --- persistence --------------------------------------------------------------


def planning_path(root: str | Path) -> Path:
    return Path(root) / PLANNING_NAME


def empty_planning() -> PlanningState:
    return PlanningState()


def utc_timestamp() -> str:
    """Canonical UTC timestamp for plan saves (presentation metadata)."""
    return intel_model.utc_now()


def load_planning(root: str | Path) -> PlanningState:
    """Load the sidecar; malformed files fall back to an empty plan."""
    document = intel_model.read_json(planning_path(root))
    if document is None:
        return empty_planning()
    try:
        return PlanningState.from_dict(document).validate()
    except (ValueError, model.MvpError):
        return empty_planning()


def save_planning(root: str | Path,
                  state: PlanningState) -> PlanningState:
    """Validate and atomically persist the planning sidecar (fail closed)."""
    state.validate()
    intel_model.write_json(planning_path(root), state.to_dict())
    return state


# --- pure state transitions ---------------------------------------------------


def _outcome_id(week_start: str, project_id: str | None, title: str,
                index: int) -> str:
    key = f"{week_start}|{project_id or ''}|{index}|{title}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return "wo-" + digest[:12]


def _coerce_outcome(raw: object, week_start: str,
                    index: int) -> WeeklyOutcome:
    if isinstance(raw, WeeklyOutcome):
        return raw.validate()
    if not isinstance(raw, Mapping):
        _fail("each weekly outcome must be an object")
    title = _clean_text(raw.get("title"), "title", maximum=MAX_TITLE_LEN)
    project_id = raw.get("project_id")
    project = (str(project_id) if project_id not in (None, "") else None)
    outcome_id = raw.get("outcome_id")
    if not isinstance(outcome_id, str) or not outcome_id:
        outcome_id = _outcome_id(week_start, project, title, index)
    return WeeklyOutcome(
        outcome_id=outcome_id,
        title=title,
        project_id=project,
        note=_clean_text(raw.get("note"), "note", maximum=MAX_NOTE_LEN,
                         optional=True),
        state=str(raw.get("state", OUTCOME_OPEN)),
        progress=_clean_text(raw.get("progress"), "progress",
                             maximum=MAX_TITLE_LEN, optional=True),
        carried_from=(str(raw["carried_from"])
                      if raw.get("carried_from") is not None else None),
    ).validate()


def plan_week(state: PlanningState, *, week_start: str,
              outcomes: Sequence[object], note: str = "",
              saved_at: str = "") -> PlanningState:
    """Create or replace the week plan (3..5 outcomes, or empty to clear)."""
    _require_date(week_start, "week_start")
    if not outcomes:
        return replace(state, week_plan=None).validate()
    if not MIN_WEEK_OUTCOMES <= len(outcomes) <= MAX_WEEK_OUTCOMES:
        _fail(f"a week plan needs {MIN_WEEK_OUTCOMES}.."
              f"{MAX_WEEK_OUTCOMES} outcomes")
    built = tuple(_coerce_outcome(raw, week_start, index)
                  for index, raw in enumerate(outcomes))
    plan = WeekPlan(week_start=week_start, outcomes=built,
                    note=note, saved_at=saved_at)
    return replace(state, week_plan=plan).validate()


def plan_tomorrow(state: PlanningState, portfolio: model.Portfolio, *,
                  day: str, main_task_ids: Sequence[str],
                  optional_task_ids: Sequence[str], note: str = "",
                  saved_at: str = "") -> PlanningState:
    """Create or replace the tomorrow plan; empty selections clear it."""
    _require_date(day, "date")
    main = tuple(main_task_ids)
    optional = tuple(optional_task_ids)
    if not main and not optional:
        return replace(state, tomorrow_plan=None).validate()
    known = portfolio.task_map()
    terminal = model.TASK_TERMINAL_STATUSES
    for task_id in (*main, *optional):
        task = known.get(task_id)
        if task is None:
            _fail(f"cannot plan unknown task {task_id!r}")
        if task.status in terminal:
            _fail(f"cannot plan terminal task {task_id!r}")
    plan = TomorrowPlan(
        date=day,
        main_task_ids=_clean_id_list(main, "main_task_ids",
                                     maximum=MAX_MAIN_TASKS),
        optional_task_ids=_clean_id_list(optional, "optional_task_ids",
                                         maximum=MAX_OPTIONAL_TASKS),
        note=note,
        saved_at=saved_at,
    ).validate()
    return replace(state, tomorrow_plan=plan).validate()


def set_outcome_state(state: PlanningState, outcome_id: str,
                      new_state: str) -> PlanningState:
    """Close/reopen/drop one weekly outcome (explicit human action)."""
    if new_state not in OUTCOME_STATES:
        _fail(f"state={new_state!r} not in {list(OUTCOME_STATES)}")
    plan = state.week_plan
    if plan is None:
        _fail("no week plan loaded")
    if plan.outcome_by_id(outcome_id) is None:
        _fail(f"unknown outcome {outcome_id!r}")
    updated = tuple(
        replace(outcome, state=new_state)
        if outcome.outcome_id == outcome_id else outcome
        for outcome in plan.outcomes)
    return replace(state, week_plan=replace(plan, outcomes=updated)
                   ).validate()


def restate_outcome(state: PlanningState, outcome_id: str, *,
                    title: str | None = None, note: str | None = None,
                    project_id: str | None = None) -> PlanningState:
    """Restate an outcome without changing its identity."""
    plan = state.week_plan
    if plan is None:
        _fail("no week plan loaded")
    target = plan.outcome_by_id(outcome_id)
    if target is None:
        _fail(f"unknown outcome {outcome_id!r}")
    updated = tuple(
        replace(
            outcome,
            title=(_clean_text(title, "title", maximum=MAX_TITLE_LEN)
                   if title is not None else outcome.title),
            note=(_clean_text(note, "note", maximum=MAX_NOTE_LEN,
                              optional=True)
                  if note is not None else outcome.note),
            project_id=(project_id if project_id is not None
                        else outcome.project_id),
        ).validate()
        if outcome.outcome_id == outcome_id else outcome
        for outcome in plan.outcomes)
    return replace(state, week_plan=replace(plan, outcomes=updated)
                   ).validate()


def carry_outcomes(state: PlanningState, outcome_ids: Sequence[str], *,
                   new_week_start: str) -> PlanningState:
    """Carry explicitly selected outcomes into a new week (human action only).

    This never runs automatically: the caller must name the outcomes and the
    new week. Carried outcomes keep their title/note/project and are reset to
    ``OPEN`` with a ``carried_from`` provenance marker.
    """
    _require_date(new_week_start, "new_week_start")
    source = state.week_plan
    if source is None:
        _fail("no week plan loaded")
    selected: list[WeeklyOutcome] = []
    for outcome_id in outcome_ids:
        existing = source.outcome_by_id(outcome_id)
        if existing is None:
            _fail(f"unknown outcome {outcome_id!r}")
        selected.append(replace(
            existing,
            outcome_id=_outcome_id(new_week_start, existing.project_id,
                                   existing.title, len(selected)),
            state=OUTCOME_OPEN,
            carried_from=source.week_start,
        ).validate())
    plan = WeekPlan(week_start=new_week_start, outcomes=tuple(selected),
                    note="", saved_at="")
    return replace(state, week_plan=plan).validate()


# --- projections --------------------------------------------------------------


def _candidate_sources(near: str, readiness_state: str, urgency: str,
                       deadline: str | None, task_id: str,
                       planned_today: set[str],
                       outcomes_today: set[str]) -> list[str]:
    sources: list[str] = []
    if readiness_state in readiness.READY_STATES:
        sources.append("ready")
    if urgency in (model.U_CRITICAL, model.U_HIGH) \
            or (deadline is not None and deadline <= near):
        sources.append("urgent")
    if task_id in planned_today or task_id in outcomes_today:
        sources.append("unfinished_today")
    if readiness_state in (readiness.RS_BLOCKED, readiness.RS_WAITING):
        sources.append("blocked")
    return sources


def build_tomorrow_view(root: str | Path, *, today: date | None = None,
                        target: date | None = None,
                        portfolio: model.Portfolio | None = None,
                        state: PlanningState | None = None,
                        ) -> dict[str, Any]:
    """Candidates for the next day plus the saved plan, if any."""
    portfolio = portfolio or store.load_portfolio(root)
    if portfolio is None:
        raise model.MvpError(model.E_MALFORMED, "no portfolio loaded")
    today = today or date.today()
    target = target or (today + timedelta(days=1))
    state = state or load_planning(root)
    target_str = target.isoformat()
    near_str = (target + timedelta(days=NEAR_DEADLINE_DAYS)).isoformat()

    ranked = priority.rank_ready_tasks(portfolio, today=today)
    rank_map = {item.task_id: item for item in ranked}
    readiness_map = {item.task_id: item
                     for item in readiness.evaluate(portfolio)}
    projects = portfolio.project_map()
    day_plan = scheduler.plan_day(portfolio, ranked, today)
    planned_today = {item.task_id for item in day_plan.planned}
    # a saved plan for *today* is itself part of "what was planned today".
    if state.tomorrow_plan is not None \
            and state.tomorrow_plan.date == today.isoformat():
        planned_today.update(state.tomorrow_plan.all_task_ids())
    outcomes_today: set[str] = set()
    for record in store.read_outcomes(root):
        if record.recorded_at[:10] == today.isoformat() \
                and record.outcome != model.O_COMPLETED:
            outcomes_today.add(record.task_id)

    saved = state.tomorrow_plan
    if saved is not None and saved.date == target_str:
        planned_flags = {
            **{tid: "MAIN" for tid in saved.main_task_ids},
            **{tid: "OPTIONAL" for tid in saved.optional_task_ids},
        }
    else:
        planned_flags = {}
        saved = None

    candidates: list[dict[str, Any]] = []
    for task in sorted(portfolio.tasks, key=lambda t: t.task_id):
        if task.status in model.TASK_TERMINAL_STATUSES:
            continue
        state_item = readiness_map[task.task_id]
        project = projects[task.project_id]
        sources = _candidate_sources(
            near_str, state_item.state, task.urgency, task.deadline,
            task.task_id, planned_today, outcomes_today)
        if not sources and planned_flags.get(task.task_id) is None:
            continue
        ranked_item = rank_map.get(task.task_id)
        why: list[str] = []
        if "ready" in sources:
            why.append("ready now")
        if "urgent" in sources:
            if task.deadline is not None and task.deadline <= target_str:
                why.append(f"deadline {task.deadline}")
            elif task.deadline is not None \
                    and task.deadline <= (target + timedelta(
                        days=NEAR_DEADLINE_DAYS)).isoformat():
                why.append(f"deadline soon: {task.deadline}")
            if task.urgency in (model.U_CRITICAL, model.U_HIGH):
                why.append(f"{task.urgency.lower()} urgency")
        if "unfinished_today" in sources:
            why.append("unfinished from today")
        if "blocked" in sources:
            reason = state_item.reasons[0] if state_item.reasons else "blocked"
            why.append(f"blocked: {reason}")
        if ranked_item is not None:
            for reason in ranked_item.reasons:
                if reason not in why:
                    why.append(reason)
        candidates.append({
            "task_id": task.task_id,
            "title": task.title,
            "project_id": task.project_id,
            "project": project.name,
            "domain": project.domain,
            "status": task.status,
            "readiness": state_item.state,
            "ready": state_item.ready,
            "blocked": state_item.state in (readiness.RS_BLOCKED,
                                            readiness.RS_WAITING),
            "blocked_reason": (state_item.reasons[0]
                               if state_item.reasons else ""),
            "reasons": list(state_item.reasons),
            "urgency": task.urgency,
            "impact": task.impact,
            "estimated_minutes": task.estimated_minutes,
            "deadline": task.deadline,
            "priority_rank": (ranked_item.rank
                              if ranked_item is not None else None),
            "priority_reasons": (list(ranked_item.reasons)
                                 if ranked_item is not None else []),
            "sources": sources,
            "why": why or ["selected"],
            "planned": planned_flags.get(task.task_id),
        })

    return {
        "target": target_str,
        "target_weekday": _WEEKDAY_NAMES[target.weekday()],
        "today": today.isoformat(),
        "main_count": len(saved.main_task_ids) if saved is not None else 0,
        "optional_count": (len(saved.optional_task_ids)
                           if saved is not None else 0),
        "saved_plan": saved.to_dict() if saved is not None else None,
        "candidates": candidates,
        "max_main": MAX_MAIN_TASKS,
        "max_optional": MAX_OPTIONAL_TASKS,
        "sorts": list(TOMORROW_SORTS),
    }


def _project_progress(portfolio: model.Portfolio,
                      project_id: str) -> tuple[str, int, int]:
    tasks = [t for t in portfolio.tasks if t.project_id == project_id]
    done = sum(1 for t in tasks if t.status == model.TS_COMPLETED)
    total = len(tasks)
    if total == 0:
        return "", done, total
    pct = int(round(done / total * 100))
    return f"{done}/{total} tasks ({pct}%)", done, total


def build_week_view(root: str | Path, *, today: date | None = None,
                    week_start: str | None = None,
                    portfolio: model.Portfolio | None = None,
                    state: PlanningState | None = None,
                    ) -> dict[str, Any]:
    """Outcome-level weekly priorities plus deadlines and threats."""
    portfolio = portfolio or store.load_portfolio(root)
    if portfolio is None:
        raise model.MvpError(model.E_MALFORMED, "no portfolio loaded")
    today = today or date.today()
    start = (date.fromisoformat(week_start) if week_start
             else monday_of(today))
    start_str = start.isoformat()
    end_str = (start + timedelta(days=6)).isoformat()
    state = state or load_planning(root)

    week_plan = state.week_plan
    if week_plan is not None and week_plan.week_start != start_str:
        week_plan = None

    projects = portfolio.project_map()
    outcomes: list[dict[str, Any]] = []
    if week_plan is not None:
        for outcome in week_plan.outcomes:
            project_name = ""
            progress_signal = outcome.progress
            progress_ratio: float | None = None
            urgency = model.U_MEDIUM
            if outcome.project_id is not None:
                project = projects.get(outcome.project_id)
                if project is not None:
                    project_name = project.name
                    urgency = project.urgency
                done_total = _project_progress(portfolio, outcome.project_id)
                if not progress_signal:
                    progress_signal = done_total[0]
                if done_total[2]:
                    progress_ratio = round(done_total[1] / done_total[2], 3)
            outcomes.append({
                **outcome.to_dict(),
                "project": project_name,
                "progress_signal": progress_signal,
                "progress_ratio": progress_ratio,
                "urgency": urgency,
            })

    deadlines: list[dict[str, Any]] = []
    for task in sorted(portfolio.tasks,
                       key=lambda t: (t.deadline or "", t.task_id)):
        if task.status in model.TASK_TERMINAL_STATUSES:
            continue
        if task.deadline is None or not (start_str <= task.deadline <= end_str):
            continue
        project = projects[task.project_id]
        deadlines.append({
            "task_id": task.task_id,
            "title": task.title,
            "project_id": task.project_id,
            "project": project.name,
            "deadline": task.deadline,
            "urgency": task.urgency,
            "impact": task.impact,
            "status": task.status,
        })

    readiness_map = {item.task_id: item
                     for item in readiness.evaluate(portfolio)}
    threats: list[dict[str, Any]] = []
    for task in sorted(portfolio.tasks, key=lambda t: t.task_id):
        if task.status in model.TASK_TERMINAL_STATUSES:
            continue
        state_item = readiness_map[task.task_id]
        if state_item.state not in (readiness.RS_BLOCKED,
                                    readiness.RS_WAITING):
            continue
        project = projects[task.project_id]
        this_week = (task.deadline is not None
                     and start_str <= task.deadline <= end_str)
        pressing = (this_week or task.urgency in (model.U_CRITICAL,
                                                  model.U_HIGH))
        if not pressing:
            continue
        threats.append({
            "task_id": task.task_id,
            "title": task.title,
            "project_id": task.project_id,
            "project": project.name,
            "readiness": state_item.state,
            "reason": state_item.reasons[0] if state_item.reasons else "",
            "reasons": list(state_item.reasons),
            "deadline": task.deadline,
            "urgency": task.urgency,
            "waiting_for": list(task.waiting_for),
        })

    open_projects = [p for p in portfolio.projects if p.open]
    ready_by_project: set[str] = {
        item.project_id
        for item in priority.rank_ready_tasks(portfolio, today=today)}
    next_action_by_project: set[str] = {
        t.project_id for t in portfolio.tasks if t.next_action.strip()}
    without_next: list[dict[str, Any]] = []
    for project in open_projects:
        if project.project_id in ready_by_project:
            continue
        if project.project_id in next_action_by_project:
            continue
        project_tasks = [t for t in portfolio.tasks
                         if t.project_id == project.project_id]
        if not project_tasks:
            reason = "no tasks defined"
        else:
            reason = "no ready task and no explicit next action"
        without_next.append({
            "project_id": project.project_id,
            "name": project.name,
            "objective": project.objective,
            "status": project.status,
            "urgency": project.urgency,
            "impact": project.impact,
            "reason": reason,
        })

    saved_tomorrow = state.tomorrow_plan
    tomorrow_summary = None
    if saved_tomorrow is not None:
        tomorrow_summary = saved_tomorrow.to_dict()
        tomorrow_summary["main_titles"] = _titles_for(
            portfolio, saved_tomorrow.main_task_ids)
        tomorrow_summary["optional_titles"] = _titles_for(
            portfolio, saved_tomorrow.optional_task_ids)

    return {
        "week_start": start_str,
        "week_end": end_str,
        "week_label": f"{start_str} → {end_str}",
        "today": today.isoformat(),
        "outcomes": outcomes,
        "outcome_count": len(outcomes),
        "min_outcomes": MIN_WEEK_OUTCOMES,
        "max_outcomes": MAX_WEEK_OUTCOMES,
        "deadlines": deadlines,
        "threats": threats,
        "projects_without_next_action": without_next,
        "tomorrow": tomorrow_summary,
        "note": week_plan.note if week_plan is not None else "",
        "sorts": list(WEEK_SORTS),
    }


def _titles_for(portfolio: model.Portfolio,
                task_ids: Sequence[str]) -> list[str]:
    tasks = portfolio.task_map()
    return [tasks[tid].title for tid in task_ids if tid in tasks]


def build_review(root: str | Path, *, today: date | None = None,
                 portfolio: model.Portfolio | None = None,
                 state: PlanningState | None = None,
                 ) -> dict[str, Any]:
    """End-of-day review: completed, unfinished, blocked, carry candidates."""
    portfolio = portfolio or store.load_portfolio(root)
    if portfolio is None:
        raise model.MvpError(model.E_MALFORMED, "no portfolio loaded")
    today = today or date.today()
    today_str = today.isoformat()
    state = state or load_planning(root)
    tasks = portfolio.task_map()
    projects = portfolio.project_map()
    readiness_map = {item.task_id: item
                     for item in readiness.evaluate(portfolio)}

    records = store.read_outcomes(root)
    completed: list[dict[str, Any]] = []
    seen_completed: set[str] = set()
    for record in records:
        if record.recorded_at[:10] != today_str \
                or record.outcome != model.O_COMPLETED:
            continue
        task = tasks.get(record.task_id)
        if task is None or record.task_id in seen_completed:
            continue
        seen_completed.add(record.task_id)
        completed.append({
            "task_id": task.task_id,
            "title": task.title,
            "project_id": task.project_id,
            "project": projects[task.project_id].name,
            "outcome": record.outcome,
            "actual_minutes": record.actual_minutes,
            "note": record.note,
            "recorded_at": record.recorded_at,
        })

    saved = state.tomorrow_plan
    if saved is not None and saved.date == today_str:
        planned_ids = list(saved.all_task_ids())
        plan_source = "saved"
    else:
        ranked = priority.rank_ready_tasks(portfolio, today=today)
        planned_ids = [item.task_id
                       for item in scheduler.plan_day(
                           portfolio, ranked, today).planned]
        plan_source = "baseline"

    unfinished: list[dict[str, Any]] = []
    for task_id in planned_ids:
        task = tasks.get(task_id)
        if task is None or task.status in model.TASK_TERMINAL_STATUSES:
            continue
        unfinished.append(_review_item(task, projects, readiness_map))

    blocked: list[dict[str, Any]] = []
    for task in sorted(portfolio.tasks, key=lambda t: t.task_id):
        if task.status in model.TASK_TERMINAL_STATUSES:
            continue
        state_item = readiness_map[task.task_id]
        if state_item.state not in (readiness.RS_BLOCKED,
                                    readiness.RS_WAITING):
            continue
        blocked.append(_review_item(task, projects, readiness_map))

    carry: list[dict[str, Any]] = []
    seen_carry: set[str] = set()
    for item in [*unfinished, *blocked]:
        if item["task_id"] in seen_carry:
            continue
        seen_carry.add(item["task_id"])
        carry.append(item)

    week_plan = state.week_plan
    week_start = monday_of(today).isoformat()
    weekly_outcomes: list[dict[str, Any]] = []
    if week_plan is not None and week_plan.week_start == week_start:
        for outcome in week_plan.outcomes:
            project_id = outcome.project_id
            weekly_outcomes.append({
                **outcome.to_dict(),
                "project": (projects[project_id].name
                            if project_id in projects else ""),
            })

    return {
        "day": today_str,
        "plan_source": plan_source,
        "completed": completed,
        "unfinished_planned": unfinished,
        "blocked": blocked,
        "carry_candidates": carry,
        "weekly_outcomes": weekly_outcomes,
        "tomorrow_plan": saved.to_dict() if saved is not None else None,
        "tomorrow_is_today": bool(saved is not None
                                  and saved.date == today_str),
        "todays_plan": saved.to_dict() if saved is not None
        and saved.date == today_str else None,
    }


def _review_item(task: model.Task, projects: Mapping[str, model.Project],
                 readiness_map: Mapping[str, readiness.TaskReadiness],
                 ) -> dict[str, Any]:
    state_item = readiness_map[task.task_id]
    project = projects[task.project_id]
    return {
        "task_id": task.task_id,
        "title": task.title,
        "project_id": task.project_id,
        "project": project.name,
        "status": task.status,
        "readiness": state_item.state,
        "ready": state_item.ready,
        "blocked": state_item.state in (readiness.RS_BLOCKED,
                                        readiness.RS_WAITING),
        "blocked_reason": (state_item.reasons[0]
                           if state_item.reasons else ""),
        "urgency": task.urgency,
        "impact": task.impact,
        "estimated_minutes": task.estimated_minutes,
        "deadline": task.deadline,
    }


def build_today_handoff(root: str | Path, portfolio: model.Portfolio, *,
                        today: date,
                        readiness_map: Mapping[
                            str, readiness.TaskReadiness] | None = None,
                        state: PlanningState | None = None,
                        ) -> dict[str, Any]:
    """Resolve the saved tomorrow plan into Today's override section.

    The saved plan is a *human override*: planned tasks are surfaced first and
    their current readiness is shown honestly (a planned task that became
    blocked is displayed with its blocking reason, never as executable).
    """
    state = state or load_planning(root)
    today_str = today.isoformat()
    plan = state.tomorrow_plan
    if plan is None or plan.date != today_str:
        return {
            "active": False,
            "date": today_str,
            "plan_date": plan.date if plan is not None else None,
            "main": [],
            "optional": [],
            "completed": [],
            "blocked_count": 0,
            "ready_count": 0,
        }
    if readiness_map is None:
        readiness_map = {item.task_id: item
                         for item in readiness.evaluate(portfolio)}
    tasks = portfolio.task_map()
    projects = portfolio.project_map()
    records = store.read_outcomes(root)
    completed_ids = {
        record.task_id for record in records
        if record.recorded_at[:10] == today_str
        and record.outcome == model.O_COMPLETED}

    def resolve(task_id: str, role: str) -> dict[str, Any] | None:
        task = tasks.get(task_id)
        if task is None:
            return None
        state_item = readiness_map[task.task_id]
        project = projects[task.project_id]
        blocked = state_item.state in (readiness.RS_BLOCKED,
                                       readiness.RS_WAITING)
        return {
            "task_id": task.task_id,
            "title": task.title,
            "project_id": task.project_id,
            "project": project.name,
            "role": role,
            "status": task.status,
            "readiness": state_item.state,
            "ready": state_item.ready,
            "blocked": blocked,
            "blocked_reason": (state_item.reasons[0]
                               if state_item.reasons else ""),
            "reasons": list(state_item.reasons),
            "urgency": task.urgency,
            "impact": task.impact,
            "estimated_minutes": task.estimated_minutes,
            "deadline": task.deadline,
            "completed": task.task_id in completed_ids
            or task.status == model.TS_COMPLETED,
        }

    main = [item for item in
            (resolve(tid, "MAIN") for tid in plan.main_task_ids)
            if item is not None]
    optional = [item for item in
                (resolve(tid, "OPTIONAL") for tid in plan.optional_task_ids)
                if item is not None]
    everything = [*main, *optional]
    return {
        "active": True,
        "date": today_str,
        "plan_date": plan.date,
        "note": plan.note,
        "main": main,
        "optional": optional,
        "completed": [item for item in everything if item["completed"]],
        "blocked_count": sum(1 for item in everything if item["blocked"]),
        "ready_count": sum(1 for item in everything if item["ready"]),
    }


def build_planning_view(root: str | Path, *, today: date | None = None,
                        ) -> dict[str, Any]:
    """One aggregate payload for the planning layer (Tomorrow/Week/Review)."""
    portfolio = store.load_portfolio(root)
    if portfolio is None:
        raise model.MvpError(model.E_MALFORMED, "no portfolio loaded")
    today = today or date.today()
    state = load_planning(root)
    return {
        "planning": state.to_dict(),
        "tomorrow": build_tomorrow_view(
            root, today=today, portfolio=portfolio, state=state),
        "week": build_week_view(
            root, today=today, portfolio=portfolio, state=state),
        "review": build_review(
            root, today=today, portfolio=portfolio, state=state),
    }


__all__ = [
    "MAX_MAIN_TASKS", "MAX_OPTIONAL_TASKS", "MAX_WEEK_OUTCOMES",
    "MIN_WEEK_OUTCOMES", "OUTCOME_DONE", "OUTCOME_DROPPED", "OUTCOME_OPEN",
    "OUTCOME_STATES", "PLANNING_NAME", "PLANNING_SCHEMA_VERSION",
    "TOMORROW_SORTS", "WEEK_SORTS",
    "PlanningError", "PlanningState", "TomorrowPlan", "WeekPlan",
    "WeeklyOutcome",
    "build_planning_view", "build_review", "build_today_handoff",
    "build_tomorrow_view", "build_week_view", "carry_outcomes",
    "empty_planning", "load_planning", "monday_of", "plan_tomorrow",
    "plan_week", "planning_path", "restate_outcome", "save_planning",
    "set_outcome_state", "utc_timestamp", "week_bounds",
]
