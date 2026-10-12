# ADR-033 — Shared classification contract, Inbox and task↔project conversion

Status: Proposed (awaiting human review)

Date: 2026-10-12

## Context

The project/task boundary was implicit and implemented twice:

* Quick Capture classified a line with its own marker list and similarity
  cascade (`capture._PROJECT_MARKERS`);
* the document importer owned the rich taxonomy (AREA/PROJECT/WORKSTREAM/
  TASK/IDEA/SOMEDAY/…) and applied it through an LLM prompt, with
  deterministic post-passes.

Consequences observed:

* two sets of criteria that could disagree;
* no way to say "I don't know yet": a captured line was forced into a project
  or a task, and the durable model collapsed `IDEA`/`SOMEDAY` into a plain
  (active) project;
* no operation to move *between* the two: a task that turns out to be an
  effort could not become a project, and a project that turns out to be a
  single action could not become a task.

## Decision

### 1. One shared, deterministic classification contract

`trajectory_os.mvp.classification` becomes the single source of truth for:

* the **taxonomy** (`KINDS`) and the mapping from a kind to the durable
  entity it becomes (`PROJECT_LIKE` → Project, `TASK_LIKE` → Task,
  `GROUPING`, `NOTE_LIKE`);
* `normalize_text` (moved here from `importer`);
* a deterministic `classify_text(title) -> Classification` that returns the
  `kind`, the target `entity`, an explicit `confidence`, an `ambiguous` flag,
  an `inbox` flag and human-readable `reasons`.

The importer imports the taxonomy from this module and keeps the module-level
names (`importer.PROJECT_LIKE`, …) for backward compatibility. Quick Capture
applies the same function directly. **The criteria are shared; the agency is
not** — the capture path stays deterministic and local (no LLM), while the
importer keeps using an LLM for whole-document decomposition.

`classify_text` is deliberately conservative and ordered: an explicit
"someday" or "idea" phrasing wins over a project marker (parking is safer than
inventing a project), a project marker wins over an action verb, and a line
with no signal is returned as `ambiguous` rather than asserted.

### 2. A persisted Inbox via parked project statuses

`IDEA` and `SOMEDAY` are added to `PROJECT_STATUSES` but **not** to
`PROJECT_OPEN_STATUSES`, so a parked project:

* remains a durable, editable, visible object (matching `PROJECT_LIKE`);
* is excluded from readiness, priority and scheduling — its tasks evaluate as
  `PROJECT_CLOSED`;
* cannot be silently "activated" by a task edit: `derive_project_status`
  preserves parked statuses.

This reuses the existing entity and is backward compatible (additional
statuses, no schema bump). A dedicated `Idea` entity is deliberately avoided.

### 3. Explicit, reversible conversion operations

`mutations` gains:

* `promote_task(task_id, …)` — create a project from a task and move the task
  into it (non-destructive: every task field is preserved);
* `demote_project(project_id, target_project_id)` — move a project's tasks to
  another project and remove the now-empty project (the reverse of promote);
* `park_project(project_id, kind=IDEA|SOMEDAY | status=…)` — park in the Inbox
  or leave it (explicit status).

All three are validated, atomic (fail-closed) and exposed through the HTTP API
and the CLI. They never run automatically; a human decides.

### 4. Quick Capture uses the contract and gains an Inbox outcome

`capture` no longer owns markers: it calls `classify_text`. A line classified
as `IDEA`/`SOMEDAY` becomes the new `possible_idea` category and is suggested
`accept`, which creates a **parked** project. A line classified project-like
stays `possible_project`; otherwise it stays `new_task`.

### 5. Automated reclassification is deliberately out of scope

Detecting "this task should be a project" (or the reverse) and proposing it is
a next increment; this ADR only provides the shared criteria and the manual,
reversible operations. Any future suggestion must stay human-validated and
never auto-applied.

## Consequences

* One taxonomy, one normalization, one classifier — capture and import can no
  longer drift apart.
* "I don't know yet" is a first-class, persisted outcome (Inbox) instead of a
  forced choice.
* The project/task boundary is now operationally actionable, not just
  conceptual.
* The default capture path stays deterministic, instant, free and offline.

## Alternatives considered

* **Run the import pipeline (LLM) on every capture line.** Rejected: cost,
  latency, non-determinism and offline failure, contradicting "no LLM where an
  algorithm is more reliable" and the local-first default.
* **Add a dedicated `Idea` entity.** Rejected for V0: the taxonomy already maps
  `IDEA`/`SOMEDAY` to Project; a parked status reuses the entity and avoids a
  migration.
* **Automatically reclassify existing objects.** Rejected: no silent
  mutation, and the criteria are not reliable enough to act unsupervised.
* **Keep capture's private marker list.** Rejected: it is exactly the drift
  this ADR removes.
