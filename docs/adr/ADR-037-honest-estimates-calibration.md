# ADR-037 — Honest estimates: deterministic calibration with uncertainty

Status: Proposed (awaiting human review)

Date: 2026-10-12

## Context

Effort estimates are systematically biased — usually optimistic. The outcome
ledger (planned vs actual) already records the real duration of completed
tasks, but nothing turned that data back into better estimates. Yet "No
prediction without uncertainty" and "No ML before reliable data" mean the
answer must not be a magic number or an opaque model.

## Decision

### 1. A measured bias factor per scope

`calibration` computes the ratio `actual / estimated` over completed outcomes
that have both an estimate and an actual, at three scopes: **global**, per
**project** and per **domain**. The factor is the mean ratio; `low`/`high` are
the observed min/max, i.e. an explicit interval rather than false precision.

### 2. A data gate

A scope is only reported as calibrated with at least `MIN_SAMPLES` (5) real
outcomes. Below that the scope reports `sufficient=false` and no factor. For a
task, `calibration_for_task` prefers the project, then the domain, then global,
and skips any insufficient scope so a thin project does not block a well-sampled
domain.

### 3. Human-triggered application only

`mutations.apply_calibrated_estimate(task_id)` replaces one task's estimate with
its calibrated value. It is never automatic, and it fails closed when the task
has no estimate or no scope has enough data. The cockpit exposes it as a
`Calibrate…` action and the views payload exposes the factors and intervals; the
CLI `mvp calibration` summarises them.

### 4. Pure and deterministic

No ML, no model, no network: the module only reads the ledger and the
portfolio, so the result is reproducible and explainable.

## Consequences

* Estimates improve as real outcomes accumulate, without guessing.
* Uncertainty is always shown as an interval; no single number is presented as
  truth.
* Nothing is calibrated before there is data — the app says so explicitly.
* The calibration data already existed (planned vs actual); this closes the
  loop.

## Alternatives considered

* **A learned model (regression).** Rejected for now: not justified before
  reliable data, and less explainable than a measured ratio with an interval.
* **Automatic re-estimation of all tasks.** Rejected: it would silently rewrite
  human estimates; a human triggers each application.
* **A single global factor only.** Rejected: project/domain scopes are more
  faithful when data allows, with a documented fallback order.
