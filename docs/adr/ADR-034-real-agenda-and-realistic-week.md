# ADR-034 — Real agenda and realistic week

Status: Proposed (awaiting human review)

Date: 2026-10-12

## Context

ADR-032 made capacity *time-of-day aware*: the remaining capacity shrinks as
the working window elapses. But the week was still flat — the same capacity on
every one of the seven days — and the hard calendar could only be edited by
hand in `portfolio.json`. As a result, a plan could not reflect real meetings,
weekends or days off, so "today" and "this week" were only as truthful as the
JSON the user maintained manually.

To be lived in for a full working week, the app must hold the user's actual
time constraints and let them be edited from the daily surface.

## Decision

### 1. Bounded weekly recurrence on calendar events

`CalendarEvent` gains `recurrence` (`""` = one-off, `WEEKLY` = repeats on the
same weekday as `day`). `scheduler._events_on` expands recurrences for the day
being planned. This is deliberately **not** an RRULE engine: one bounded,
explicit repeat, no external calendar, no write-back.

### 2. Explicit days off

`Portfolio` gains `days_off` (validated `YYYY-MM-DD` dates). A day off has zero
plan capacity but still reports its events, so the agenda stays truthful.

### 3. Explicit working weekdays

`Capacity` gains `working_days` (default Monday–Friday, 0 = Monday). A weekday
outside that set is non-working and has zero capacity. The week is therefore
no longer seven equal days.

### 4. `day_budget` marks working days

`day_budget` returns `is_working_day`, and sets `plan_capacity` /
`remaining_plan_capacity` to zero on a non-working day while keeping the
nominal `total_capacity` and the day's events. `DayBudget` and `DayPlan` expose
it, and every renderer (cockpit capacity line, text renderer, CLI) shows
`day off` for such a day. `plan_week` naturally spills work from non-working
days into the next working day.

### 5. Calendar and days off are editable from the daily surface

`mutations` gains `create_event` / `update_event` / `delete_event` and
`set_day_off`, exposed through the HTTP API
(`/api/events`, `/api/events/<id>`, `/api/events/<id>/delete`,
`/api/calendar/day-off`) and the cockpit, which gains an **Agenda** block in
the Today view (today's events, add form with an optional weekly repeat,
delete, and a day-off toggle).

All operations are validated, atomic and backward compatible: existing
portfolios load unchanged through defaulted `days_off` and `working_days`.

## Consequences

* "Today" and "this week" now reflect real meetings, weekends and days off.
* The user no longer edits `portfolio.json` to keep the calendar current.
* Capacity stays deterministic and local; no dependency, no external service.
* Days off never delete planned work — it is deferred to the next working day.

## Alternatives considered

* **Full RRULE / external calendar sync.** Rejected for now: a bounded weekly
  repeat covers the common case without a dependency or a sync surface.
* **Days off as all-day events.** Rejected: an all-day event reduces focus time
  but does not express "this day has no capacity"; an explicit list is clearer.
* **Per-weekday windows now.** Deferred: working vs non-working days is the
  blocker; per-day windows are a later refinement.
* **Editing the JSON by hand.** Rejected: it contradicts "reduce cognitive
  load" and keeps the calendar stale.
