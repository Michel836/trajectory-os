# ADR-031 — Quick Capture, saved views and the Today/Ready decision surface

Status: Proposed (awaiting human dogfood)

Date: 2026-09-22

## Context

The V0 Trajectory Mirror is usable with a real portfolio (hundreds of
projects/tasks, factual-import results, readiness/priority/scheduling,
on-demand enrichment). The next phase is not more intelligence but *usability
and daily flow*:

* the cockpit must stay navigable with hundreds of objects and many views;
* the operator must be able to type a few daily thoughts and have them safely
  turned into a reviewable preview;
* "what should I do now, and why?" must be answerable without reading raw
  JSON;
* a deliberate action set must hand off cleanly to Super Productivity;
* execution outcomes must close the planning loop.

These are additive surface concerns. They must not create a second source of
truth, must not let AI content enter the portfolio silently, and must keep the
deterministic engine (readiness, priority, scheduling) authoritative.

## Decision

### 1. Global search is presentation-only

The cockpit gains one compact, case-insensitive search over project and task
fields. It is combined with (never replaces) the existing global
scope/level/status/evidence filters, is a pure client-side function
(`searchMatches`) with no persistence, and never touches the portfolio.

### 2. Saved views persist presentation state only

A saved view captures `{view, color_by, density, filters, tables}` in the
bounded, fail-closed `ui_state.json` (max 32 views, name ≤ 48 chars). Loading a
view only changes UI preferences; it never reads or writes portfolio data.
Validation is strict on write and fail-soft on read, matching the existing
`ui_state` contract (ADR-029).

### 3. Quick Capture is deterministic, preview-then-confirm, all-or-nothing

A new `mvp.capture` module turns free-form text into one candidate per line,
classifies each line conservatively (new task / possible project / existing
match / duplicate), and matches it against the *relevant* existing context by
reusing the importer's deterministic matcher (`best_project_match`,
`text_similarity`) plus the curated concept hints. There is no LLM over the
whole portfolio and no portfolio re-analysis. The preview is a pure function of
the text plus the current portfolio; confirming applies only the explicit
decisions through the validated `mutations` layer, and the batch is fully
validated before anything is written (no partial confirm). The cockpit exposes
Accept / Edit / Skip / Merge per line.

### 4. Today and Ready reuse the deterministic engine

Two new cockpit views render the backend's existing `scheduler.plan_day`,
`priority.rank_ready_tasks` and `readiness.evaluate` output. The browser does
not compute a score; it shows readiness state, urgency, impact, effort, the
priority reasons and an explicit blocking kind (dependency / blocker /
resource / waiting / project / status). No new opaque scoring is introduced.

### 5. Super Productivity stays a one-way, deliberate handoff

`superproductivity` gains a dry-run preview (`preview_export`), explicit
selection (`export_selected`) and stable traceability: every exported task
carries a deterministic `trajectory-mvp-<task_id>` id (the de-duplication key)
and its original TrajectoryOS `task_id`/`project_id`. The cockpit preview is
read-only and offers a browser download; it never writes into the data root.

### 6. Outcome capture stays schema-free

The enriched outcome form records status, actual minutes and a result/blocker
note through the existing `OutcomeRecord` (note field) and `planned_vs_actual`
aggregation. No durable schema change is required.

### 7. On-demand enrichment is unchanged except for the Next-actions size

Every generated item remains `SUGGESTED` in a sidecar and is accepted only
explicitly. The Next-actions instruction now asks for 3–7 concrete actions
(previously 1–3). There is still no silent fallback and no auto-accept.

## Consequences

* The cockpit stays usable with hundreds of objects; search and saved views are
  cheap and reversible.
* Daily input is fast and safe: a preview is always shown before writing, and a
  failed confirm leaves the portfolio untouched.
* AI suggestions remain visually and structurally distinct from facts.
* The Today/Ready surface is explainable and traceable to the deterministic
  engine; no pseudo-precision is shown.
* The handoff is one-way and idempotent by stable id; broad two-way sync is
  explicitly out of scope.
* The only new durable state is the bounded `saved_views` slice inside the
  existing `ui_state.json`.

## Alternatives considered

* **LLM-based quick capture.** Rejected for now: a deterministic matcher is
  more reliable, instantaneous and free for short daily input, and it keeps the
  local-first default. An explicit engine could be added later behind the
  existing engine-selection contract.
* **Persist search / saved views inside the portfolio.** Rejected: it would mix
  presentation with business data and violate "one data model, many views".
* **New scoring for Today/Ready.** Rejected: the existing priority/readiness
  logic is already explainable; a second score would be opaque and drift.
* **Write the Super Productivity file server-side from the cockpit.** Rejected:
  a browser download keeps the data root untouched and the action deliberate.
* **Add `blockers`/`result` columns to the outcome schema.** Deferred: the
  free-text note already carries them; a schema change needs its own ADR.
