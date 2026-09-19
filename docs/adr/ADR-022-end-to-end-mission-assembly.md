# ADR-022 — End-to-end mission assembly over the canonical observability contract

Status: Accepted

Date: 2026-09-20

## Context

M023–M030 delivered the product surfaces, isolated runtime, artifacts,
benchmark and canonical live-run observability as separate capabilities.
M031 (Issue #234) requires one coherent, trust-gated operator flow from a
single objective to `READY_FOR_COMMIT` / `BLOCKED` with durable closure
evidence.

The assembly had to satisfy two conflicting-looking constraints:

* expose a mission-level identity, plan and closure an operator can resume
  and reconstruct; and
* never introduce a second status model, backend abstraction or autonomous
  Git trust-boundary write.

## Decision

Introduce `trajectory_os.assembly` as a **thin mission-level composition**
over the existing M030 observability contract and the M030 live-run
orchestrator.

### 1. One mission root, `mission_id == run_id`

A mission is a durable identity plus objective, constraints, definition of
done, backend/provider/model intent, trust policy and starting baseline. The
mission root is `mission.json`, `plan.json`, `closure.json` **plus** the
canonical M030 run artifacts (`events.jsonl`, `status.json`,
`telemetry.json`, `summary.json`). The M030 `RunCoordinator` is invoked with
`run_id == mission_id`, so:

* the mission identity survives every phase, including interruption/resume;
* `status.json` remains the single canonical status truth — the assembly layer
  only *writes* the same `CanonicalStatus` contract at mission-level phases
  (intake, preflight, plan) and never models status itself.

### 2. Reuse, never duplicate

* Preflight: `observability.preflight` (M030), run before planning.
* Execution/validation/review/repair: `observability.run.RunCoordinator`
  (M030), which itself reuses the M029 deterministic validation, exact patch
  identity and strict review protocol.
* Reviewer identity/roles: `observability.model` (M030).
* Rendering/follow: `observability.projection` / `observability.follow`
  (M030).
* Backend intent: the canonical backend/provider/model strings; no new
  backend abstraction.

The assembly layer adds only `MissionDefinition`, `MissionPlan`,
`MissionClosure` and the operator CLI.

### 3. Human gate defended in depth

Automation stops at `READY_FOR_COMMIT`. The mission layer re-checks the
canonical status after execution: readiness requires a `COMPLETE` lifecycle,
an active final independent reviewer and `reviewed_patch == current_patch`.
Any violation is downgraded to `BLOCKED` with a stable reason
(`HUMAN_GATE_VIOLATION`) before closure. This defends against a stale review
or a phantom reviewer even if a lower layer regressed.

### 4. Bounded repair and fail-closed negatives

The M030 coordinator owns the bounded repair loop; the mission plan records
the retry budget. Preflight rejection stops before plan/execution; a
validation reject cannot advance; repair-budget exhaustion yields `BLOCKED`;
and a lifecycle `COMPLETE` with non-ready readiness is rendered as blocked,
never as success.

### 5. Closure and reconstruction

`closure.json` aggregates the durable artifacts (identity, DoD, baseline,
plan, steps executed, attempts/repairs, validation/review results,
current/reviewed patch identity, telemetry summary, terminal
lifecycle/readiness, next operator action and authoritative artifact paths).
`reconstruct` rebuilds the mission from those artifacts without reading prose
logs. Current *status* is still read from `status.json` alone.

### 6. Trust constraints

No assembly module performs a Git trust-boundary write. A trust policy that
requests one is rejected at validation time. Observation commands are
read-only.

## Consequences

* One operator command (`scripts/trajectory-mission`) starts, follows, resumes,
  reports and reconstructs missions while composing the existing M030
  surfaces.
* A mission and a canonical run share one identity and one status truth, so
  no reconciliation/parallel state model is needed.
* Interruption/resume preserves identity and evidence because the canonical
  event stream is append-only and the execution-final status is reused rather
  than recomputed.
* The closure artifact is sufficient to audit what happened and what the
  operator must do next.
