# ADR-036 — Decisions register

Status: Proposed (awaiting human review)

Date: 2026-10-12

## Context

The V0 milestone is meant to hold *decisions* as well as work, but the durable
model only had projects and tasks. A consequential choice (a vendor, a
direction, a trade-off) was therefore either lost or buried in a task note,
with no record of the question, the options, the choice or the rationale.

## Decision

### 1. A dedicated `Decision` entity

`Decision` carries `decision_id`, `title`, an optional `question`, a `status`
(`OPEN` / `DECIDED` / `DEFERRED` / `SUPERSEDED`), the considered `options`, the
`choice`, the `rationale`, an optional `project_id`, an optional `deadline`
and timestamps. It is validated and persisted in `Portfolio.decisions`.

### 2. A settled decision must carry a choice

A `DECIDED` / `SUPERSEDED` decision without a `choice` fails validation: the
register never records a decision that was not actually made. Settling stamps
`decided_at`; reopening clears it.

### 3. Human-authored only

TrajectoryOS never decides. There is no AI decision-making and no automatic
reclassification of decisions; the mutations only record what the human chose.

### 4. Backward compatible

`decisions` defaults to empty; existing portfolios load unchanged and the
schema version is untouched.

## Consequences

* "Why did we choose this?" is answerable from the durable record.
* Decisions can be reviewed, deferred and superseded explicitly.
* Research is deliberately **not** a separate entity in this increment: a
  research effort is a task/project, and only a *decision* needs its own
  object. A research register can follow if usage requires it.

## Alternatives considered

* **Store decisions as tasks.** Rejected: a decision is not executable work and
  its options/rationale would be lost in a note.
* **Store decisions as project descriptions.** Rejected: a decision is not a
  container and often spans projects.
* **Let an LLM propose decisions.** Rejected: consequential choices are
  human-authored and human-owned.
