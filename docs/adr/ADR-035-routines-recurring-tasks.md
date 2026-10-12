# ADR-035 — Routines (recurring tasks) materialized idempotently

Status: Proposed (awaiting human review)

Date: 2026-10-12

## Context

The V0 model has one-off tasks and (since ADR-034) recurring *calendar
events*, but no recurring *work*: a weekly review, a daily standup note, a
routine errand. Without it, the user re-creates the same tasks by hand, which
is exactly the cognitive load TrajectoryOS must reduce.

## Decision

### 1. A dedicated `Routine` entity

`Routine` carries `routine_id`, `title`, `project_id`, `weekdays` (0 = Monday),
an optional `estimated_minutes`, an optional `workstream` and an `active`
flag. It is validated and persisted in `Portfolio.routines`. A routine is a
*template*, never executed directly.

### 2. Materialization is explicit and idempotent

`mutations.materialize_routines(day)` creates one concrete Task for each active
routine due on that day. The created task id is deterministic
(`<routine_id>-<day>`), so running it again for the same day creates nothing.
Materialization is an explicit action (CLI `mvp routines --materialize`, API
`POST /api/routines/materialize`), never a side effect of a read-only view.

### 3. Bounded recurrence

Only a weekly pattern (`weekdays`) is supported, matching the bounded
`WEEKLY` recurrence of calendar events (ADR-034): no RRULE engine, no external
service. A routine can be deactivated instead of deleted.

### 4. Backward compatible

`routines` defaults to empty; existing portfolios load unchanged, and
`to_dict`/`from_dict` keep the schema version.

## Consequences

* Recurring work is defined once and appears in the plan on the right days.
* The read-only guarantee of the view layer is preserved (materialization is a
  mutation, not a projection).
* Re-running a materialization is always safe (idempotent).
* Full routine management UI is a later refinement; the CLI, the API and the
  views payload are the V0 surface.

## Alternatives considered

* **A `recurrence` field on `Task`.** Rejected: tasks are instances; making
  them also templates blurs provenance and the duplicate guard.
* **Materialize during view building.** Rejected: it would make a read-only
  projection mutate the portfolio.
* **Full RRULE / cron.** Rejected for V0: a weekly weekday set covers the
  common case without a dependency.
