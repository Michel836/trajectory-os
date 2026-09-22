# ADR-029 — Visual execution & decision layer (one data model, many views)

Status: Accepted

Date: 2026-09-21

## Context

The V0 Trajectory Mirror already has a validated portfolio (projects, tasks,
dependencies, readiness, prioritisation, scheduling, outcomes, replanning),
smart document import, a semantic hierarchy and an interactive CRUD
dashboard. That backend intelligence is substantially richer than the
single textual dashboard surface: the operator can read data but cannot
*see* structure, context or trade-offs.

This ADR records the additive presentation layer that makes the existing
intelligence visible without introducing a second source of truth. The
run's scope is explicitly the visual execution and decision layer; it does
not touch import semantics, the domain model or any unrelated subsystem.

## Decision

Add a read-only projection module and a framework-free multi-view cockpit.
The layer is **presentation only** and is derived from the existing
portfolio, outcome ledger and plan functions.

### 1. One derived view payload, many views

`mvp.visualization.build_views(root)` is the single bridge from the
validated portfolio to every visual representation. It returns one
deterministic JSON document containing normalised project/task objects,
the WBS tree, the dependency graph, Gantt lanes, Eisenhower, impact/effort,
portfolio map, treemap, progress, heatmap and goal/outcome flow. Views are
renderings of that one payload — never of per-view stores. There is no
`kanban.json`, `gantt.json` or `graph.json`.

Business rules stay in Python: readiness, priority, dependency direction,
project aggregation, quadrants, colour derivation and the WBS hierarchy are
computed server-side. The browser only lays out and draws, and never
re-implements a rule.

### 2. Presentation preferences are not business data

Personal highlights, identity colours, the active view, density, filters and
the last selection live in a separate, bounded, atomically written
`ui_state.json` (`mvp.uistate`). Reads fail soft (a corrupt file falls back
to defaults so the cockpit still opens); writes fail closed (unknown fields
and invalid colours are rejected).

* identity colour (`domain`/`project`) is user-overridable and stable across
  processes (SHA-1, not Python's salted `hash`);
* status/urgency/impact colours are fixed semantics shared by every view;
* a personal highlight is an overlay and **never** modifies business status.

### 3. No new dependency, no framework rewrite

The cockpit remains Python + stdlib HTTP + vanilla JavaScript and SVG. No
React/Vue and no third-party charting bundle is introduced, preserving
local-first/offline operation and the current startup command
(`scripts/mvp dashboard --root local_data/mvp`).

### 4. Visual operations are not allowed to mutate silently

Every interactive change (status, urgency/impact from Eisenhower, outcome
recording, dependencies, highlights, colours) is sent to the existing
validated, atomic mutation endpoints. Visual-only operations
(selection, view, colour, filters, zoom) never write the portfolio; tests
assert the portfolio bytes are unchanged. UNKNOWN stays UNKNOWN and no date
or effort is invented: the Gantt reports unscheduled work instead of
fabricating a schedule.

## Consequences

* The operator can move from "I see a task" to "I understand where it sits,
  why it matters, what precedes it, what follows it, how much effort it
  needs and what it unlocks", across synchronized views.
* The added `ui_state.json` is the only new durable file; removing the layer
  leaves the portfolio, ledger and plan documents intact.
* The view payload is derived on demand and can grow large; views use
  filtering, collapse/expand, depth limits and scoped graph rendering to
  stay usable with hundreds of tasks.

## Alternatives considered

* **Persist one document per view.** Rejected: multiple stores drift from
  the canonical portfolio and violate "one data model, many views".
* **Rewrite the frontend in React/Vue.** Rejected: unnecessary rewrite and
  dependency; the current architecture is sufficient and local-first.
* **Bundle third-party charting libraries from a CDN.** Rejected: breaks
  offline/local-first operation and adds supply-chain surface for what SVG
  can do directly.
* **Store evidence labels per persisted entity.** Deferred: the durable
  model has no evidence field and this run must not silently change the
  domain schema. Persisted entities render as human-confirmed FACT; the
  `SUGGESTED` visual language is preserved for the import preview and the
  edge/node infrastructure is ready for a future schema extension.
