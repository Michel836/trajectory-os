# ADR-038 — Phase 0 daily usability: four workspaces, honest project status, reversible feedback

Status: Proposed (awaiting human dogfood)

Date: 2026-10-28

## Context

The V0 portfolio engine is deterministic and well tested, but the daily
surface is hard to use with a real portfolio:

* twenty top-level views compete for attention;
* the interface is in English while the data is French;
* a project is displayed `BLOCKED` when a single task is blocked, even if
  other tasks are ready (observed on the real portfolio: eight inconsistent
  projects);
* imported projects start `DEFERRED` and disappear from readiness, while 210
  open tasks have no estimate and 81 ready tasks cannot be time-boxed;
* recording completion opens a blocking prompt, the toast is cleared by the
  refresh that follows, and there is no "what changed?" view or undo;
* Quick Capture confirms the whole batch, so one bad line delays the rest.

The roadmap explicitly prioritises usability over new intelligence. Phase 0
must therefore change the surface and the feedback loop without adding an
LLM, an agent, a network call or a new portfolio schema.

## Decision

### 1. Four workspaces, stable internal view keys

The cockpit exposes **Aujourd’hui / Entrée / Portefeuille / Revue**. The
twenty internal view keys (`today`, `inbox`, `list`, `changes`, ...) remain
unchanged and are still routable; only the visible navigation and labels
change. Advanced views stay available behind a `Plus` control. The default
view for a new profile is `today`.

Presentation preferences remain in `ui_state.json`; `changes` is added to the
closed `VIEWS` set. Existing profiles keep their saved view.

### 2. Project status is derived from executability

`outcomes.derive_project_status` now applies this order:

1. all tasks terminal → `COMPLETED`;
2. at least one `READY` or `IN_PROGRESS` task → `ACTIVE`;
3. at least one `WAITING` task → `WAITING`;
4. at least one `BLOCKED` task → `BLOCKED`;
5. all tasks completed/abandoned/deferred/project-closed → `DEFERRED`;
6. otherwise → `ACTIVE`.

A single blocked task therefore no longer hides ready work. A completed task
inside a deferred project does not silently reactivate it.

The repair of already-persisted statuses is **explicit and human-triggered**:
`outcomes.rederive_project_statuses` is exposed by the health card and the
`POST /api/projects/rederive` endpoint. It never touches task status,
readiness, priorities or dependencies, and parked projects are preserved.

### 3. A read-only portfolio-health projection

`visualization.build_views` exposes bounded, deterministic attention signals:

* deferred projects;
* projects without an open task;
* open and ready tasks without an estimate;
* stored `BLOCKED` projects that still have a ready task;
* waiting items older than seven days.

The cockpit shows them in a collapsed **Santé du portefeuille** card on
Today. The card surfaces facts; it never auto-activates, auto-estimates or
auto-closes anything.

### 4. Append-only change journal, with legacy outcome projection

`mvp.changes` adds one sidecar, `changes.jsonl`:

* `OUTCOME`, `CREATE`, `UPDATE`, `UNDO` records with object identity,
  before/after values, source and an optional `references` link;
* journaling is **best-effort after** the canonical portfolio write: a
  journal failure never turns a successful human action into an error;
* undo is a new `UNDO` record that references the original change; history is
  never rewritten or deleted;
* the cockpit feed merges the journal with outcomes recorded before the
  journal existed, projected as `source=legacy` and non-undoable. No file is
  written by this read.

`outcomes.undo_last` restores the previous task status and actual minutes only
when the task still matches the outcome being undone; otherwise it fails
closed. The immutable outcome ledger is not modified.

### 5. One-click Done and per-line capture

Every task representation (Today, Ready, List, Kanban, Waiting, Tomorrow)
offers a `✓` action that records `COMPLETED` immediately, without a prompt.
The resulting toast survives the refresh and offers **Annuler** when the
journal recorded an undoable change.

Quick Capture keeps the batch action but also offers **Appliquer cette
ligne**. The server recomputes the preview from the submitted text and applies
only the explicit decisions, so applying one line never applies the others.

### 6. French surface, internal codes unchanged

Visible labels are translated through a small `FR` mapping at render time.
Internal status codes, view keys and API values stay stable. Data titles are
never translated.

## Consequences

* The cockpit opens on a decision surface rather than a raw list, and all
  twenty views remain reachable in at most two clicks.
* Eight real inconsistent project statuses are now visible and can be repaired
  explicitly; the derivation rule prevents recurrence.
* Completion feedback survives the refresh and is reversible. The outcome
  ledger remains an immutable audit; an undone outcome stays in the ledger and
  continues to be counted by planned-vs-actual statistics, which measure
  *recorded* outcomes rather than current task state.
* `changes.jsonl` is a new bounded sidecar. Removing the feature leaves the
  portfolio, outcome ledger and plans intact.
* No new dependency, no network call, no LLM, no agent and no portfolio schema
  change are introduced.

## Alternatives considered

* **Rewrite the frontend in a framework.** Rejected: the stdlib + vanilla
  JavaScript surface is sufficient for Phase 0 and preserves local-first
  operation.
* **Auto-repair project statuses on load.** Rejected: it would silently
  rewrite durable state. The repair is explicit and journaled.
* **Delete or rewrite the outcome ledger on undo.** Rejected: the ledger is
  append-only audit evidence.
* **Undo every mutation.** Deferred: Phase 0 only offers undo for outcome
  records, where the before/after state is unambiguous. Creation undo needs a
  deletion policy and is deliberately out of scope.
* **Translate by changing internal codes.** Rejected: it would break routes,
  saved views, tests and API consumers. Translation happens at render time.
