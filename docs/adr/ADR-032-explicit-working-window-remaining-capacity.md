# ADR-032 — Explicit working window and time-of-day aware remaining capacity

Status: Proposed (awaiting human review)

Date: 2026-10-12

## Context

The V0 cockpit answers "what should I do today, and why?" from
`scheduler.plan_day` / `plan_week`. Capacity was modelled as two scalars —
`Capacity.minutes_per_day` (default 6h) and `Capacity.buffer_ratio` (default
30%) — with **no notion of the current instant and no working window**.

As a result, rebuilding the plan at 15:00 still advertised the *whole* day:
capacity 6h, plan capacity 4h12, and the scheduler packed 4h12 of work. The
observed output was:

```
capacity 6h · calendar 0m · buffer 1h48m · planned 3h50m / 4h12m
```

This is not a rendering defect. It is a missing concept in the domain model:
the product cannot distinguish "a day's budget" from "what is left of the
day". The misleading number propagated to every surface (cockpit Today and
header summary, text renderer, CLI, heatmap payload) and into the planner
itself, which kept proposing morning-sized workloads in the afternoon.

## Decision

### 1. Capacity carries an explicit working window

`Capacity` gains `day_start_minutes` / `day_end_minutes` (minutes since local
midnight, defaults 540/1080 = 09:00–18:00) and a derived
`window_minutes`. The window is validated (`end > start`, bounded to a single
day). It is explicit and never inferred from observed behaviour.

The change is backward compatible: `Capacity.from_dict` defaults the window
when the keys are absent, so existing portfolios (which only carry
`minutes_per_day` and `buffer_ratio`) keep loading unchanged.

### 2. A pure budget function separates nominal from remaining

`scheduler.day_budget(portfolio, day, *, now=None)` returns a `DayBudget` with
both the historical whole-day budget and the remaining one.

* **Nominal** (unchanged): `available = minutes_per_day - calendar_minutes`,
  then `buffer = round(available * buffer_ratio)`, then
  `plan_capacity = available - buffer`.
* **Remaining** (today only): the configured daily capacity is prorated across
  the working window by the fraction of the window still ahead; the buffer is
  recomputed on that remaining available time (so the scheduler can never plan
  100% of what is left); calendar commitments are counted only on the part of
  the window still ahead.
* **Any day that is not today keeps the nominal budget** as its remaining
  budget, so future-day planning is byte-for-byte unchanged.

`now` is injected for deterministic tests and defaults to the **local** current
time (never UTC), so the window lines up with the user's own clock.

### 3. The planner packs against the remaining budget for today

`plan_day` / `plan_week` pack tasks against `remaining_plan_capacity`. A plan
rebuilt at 15:00 therefore defers what no longer fits today instead of
pretending the morning is available; the surplus naturally spills into the
following days of the week plan. `plan_capacity` stays the *nominal* field and
is never silently rewritten.

`DayPlan` / `DayBudget` expose, in addition to the existing fields:
`is_today`, `window_minutes`, `elapsed_minutes`, `remaining_window_minutes`,
`remaining_calendar_minutes`, `remaining_buffer_minutes` and
`remaining_plan_capacity`.

### 4. One canonical capacity line on every surface

The cockpit Today view and header summary, the text renderer, the CLI and the
heatmap payload all derive from the same fields and show the *remaining*
buffer/ceiling for today. The cockpit has a single `capacityLabel(plan)` JS
helper so the wording cannot drift between views.

Example (today, 15:00):

```
capacity 6h · calendar 0m · buffer 36m · remaining 1h24 · planned 1h20 / 1h24
```

For a future day the remaining fields equal the nominal ones, so the same line
remains truthful.

### 5. `now` is threaded through the projection layer

`build_views`, `build_cockpit`, `build_tomorrow_view` and `build_review` accept
an optional `now` (tests inject it; production uses local wall-clock time).
This keeps the time-of-day behaviour deterministic and reviewable without a
clock dependency.

## Consequences

* The Today view no longer lies: capacity shrinks as the working window
  elapses, and the recommendation follows.
* The change is app-wide by construction: every surface reads the same
  scheduler fields instead of recomputing capacity.
* The uniform-proration rule is an explicit, documented assumption (focused
  capacity is spread evenly across the window). It requires no new data and
  introduces no ML; a future ADR may refine the curve using real outcomes.
* Existing portfolios load unchanged; only the serialized `capacity` object
  gains two defaulted keys.
* The scheduler remains a pure, deterministic function of
  `(portfolio, day, now)`.

## Alternatives considered

* **Relabel "capacity" only (display-only fix).** Rejected: it leaves the
  planner proposing a morning-sized workload in the afternoon and keeps the
  contradiction between the label and the packed plan.
* **Hard-code a working window in the renderer.** Rejected: a silent
  assumption in one surface, invisible to the CLI/planner, and impossible to
  test deterministically.
* **Infer the window from recorded outcomes.** Rejected for V0: no reliable
  data yet ("No ML before reliable data"), and it would be non-deterministic.
* **Enforce `minutes_per_day <= window_minutes` at load.** Deferred: not
  needed by the current data and it could reject legitimate existing
  portfolios; `day_budget` already caps remaining focus at the remaining
  window.
* **Timezone configuration / per-weekday windows / weekend awareness.**
  Deferred to their own ADR; the window is deliberately a single explicit
  local-time range for V0.
