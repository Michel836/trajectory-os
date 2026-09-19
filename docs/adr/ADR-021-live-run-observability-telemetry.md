# ADR-021 — Canonical live-run observability and telemetry contract

Status: Accepted

Date: 2026-09-19

## Context

M029 exposed real operator-facing ambiguities while running the Pi vs
DeepSeek Harness benchmark:

* `BLOCKED -> COMPLETE` could look like success even when readiness was
  blocked;
* reviewer identity could be misleading when stale/default values leaked into
  the display;
* status could be reconstructed from logs instead of an authoritative state
  source;
* preflight-detectable failures could waste validation/review/repair cycles;
* telemetry availability differs by backend/provider and must never be
  guessed;
* operators needed `pgrep`, `pstree`, `tail`, `lsof`, etc. to know whether a
  run was genuinely active or terminal.

The M030 mission (Issue #232) requires one canonical, backend-neutral
observability and telemetry contract shared by CLI/TUI/Web, with lifecycle
completion and trust readiness kept independent.

## Decision

Introduce `trajectory_os.observability` as the single canonical observability
layer. Every runtime adapter (the M030 `RunCoordinator` and the M029
benchmark runtime) maps its authoritative durable state into the same
contract:

```
runtime adapters -> events.jsonl -> status.json -> CLI / TUI / Web
```

Frontends never reconstruct current truth by grepping historical logs.

### 1. Canonical status contract (`model.CanonicalStatus`)

The status persists `run_id`, lifecycle `state`/`stage`/`phase`/`attempt`,
`current_backend`/`current_provider`/`current_model`, the separate
`inline_review_enabled`/`inline_reviewer` and
`final_review_enabled`/`final_reviewer` roles,
`previous_gate`/`previous_result`, `reviewed_patch`/`current_patch`,
`next_action`, `last_meaningful_event_at`, `heartbeat_at`, `terminal_reason`
and an explicit `readiness`.

Two orthogonal axes are modelled and must never be conflated:

* **lifecycle** — `PENDING`/`PREFLIGHT`/`RUNNING`/`IMPLEMENTING`/`VALIDATING`/
  `REVIEWING`/`REPAIRING`/`COMPLETE`;
* **readiness** — `INDETERMINATE`/`READY_FOR_COMMIT`/`BLOCKED`/`FAILED`/
  `CANCELLED`/`UNKNOWN`.

The model's `validate()` fails closed if `READY_FOR_COMMIT` is claimed
without a `COMPLETE` lifecycle, and the projection authorises a success
rendering from `readiness == READY_FOR_COMMIT` alone. A lifecycle `COMPLETE`
with a blocked readiness renders as blocked, never as success.

### 2. Reviewer identity hardening (`model.ReviewerStatus`)

Three roles are explicit: implementation agent, inline reviewer, final
independent reviewer. Each carries `enabled`/`active`; `display_model` is
`None` for any role that is not enabled **and** active. This is the single
choke point that eliminates phantom/default reviewer display. `--no-review`
shows no reviewer at all, and `qwen3.8:27b-q4_K_M` appears only when it is
the actually-invoked final independent reviewer.

### 3. Telemetry modes and metrics (`telemetry`)

Modes are `off`, `standard` (lightweight, bounded) and `benchmark` (finer
sampling, percentiles, local resource sampling). Every metric is a
`model.Metric` with explicit provenance (`PROVIDER`/`DERIVED`/`LOCAL`/
`UNAVAILABLE`). An unavailable metric is `null` with a stable reason and is
never estimated. Derived metrics (tokens/cost/seconds per successful task,
cache-hit ratio, repair/review-reject/protocol-error rates, wasted
failed-cycle tokens/cost/time, time-to-first-useful-patch) are computed only
where the source values exist. Collection, aggregation and presentation are
separate layers (`telemetry`, `projection`).

### 4. Durable artifacts (`store`)

Canonical run artifacts are `events.jsonl`, `status.json`, `telemetry.json`
and `summary.json`, written atomically. Observation surfaces are read-only.

### 5. Shared projection (`projection`)

`canonical_document -> StatusViewModel -> CLI/TUI/Web` is the only rendering
path. The three surfaces cannot disagree about current truth.

### 6. Fail-fast preflight (`preflight`)

Knowable configuration errors (including invalid provider/model
combinations) are rejected before any implementation, validation, review or
repair cycle. A rejection preserves the checks as evidence, writes an
explicit `BLOCKED` status and stops the run.

### 7. Bounded follow (`follow`)

The follow loop polls on a 10–15 second heartbeat, exits at every terminal
lifecycle/readiness outcome, prints an explicit `RUN TERMINÉ` banner and may
use `notify-send` opportunistically — never as a correctness dependency. The
`trajectory-pi-status --follow` reader delegates to this loop so it contains
no local `sleep`/`while True` polling.

### 8. Trust constraints

No observability or benchmark module performs a Git trust-boundary write.
Observation surfaces are read-only; runtime controls remain a separate
concern. No automatic backend promotion is derived from telemetry.

## Consequences

* Operators get one unambiguous status from every surface, with lifecycle and
  readiness clearly separate.
* Reviewer identity can no longer leak a stale/default model.
* Expensive phases are protected by preflight.
* Telemetry is honest by construction: a metric is either grounded with
  provenance or `null` with a reason.
* The M029 benchmark artifacts are mapped into the canonical contract through
  a thin adapter (`observability.adapter`), so the benchmark remains the
  runtime adapter and does not duplicate the contract.
