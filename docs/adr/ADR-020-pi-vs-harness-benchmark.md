# ADR-020 — Pi vs DeepSeek Harness decision-grade benchmark

Status: Accepted

Date: 2026-09-19

## Context

Program M017–M028 delivered the visible/persistent product layer, the
multi-goal portfolio, the persistent daemon, hardened provider-neutral agent
backends (ADR-014), the isolated DeepSeek Harness qualification path
(ADR-018) and provider-grounded telemetry (ADR-019). Before the runtime
backend choice is made long term, TrajectoryOS needs a controlled A/B
benchmark between the proven `Pi -> DeepSeek Flash` path and a qualified
`DeepSeek Harness -> DeepSeek Flash` path. Recent real runs showed very high
raw token volume and reviewer-protocol friction, so the decision must be based
on measured evidence (quality, reliability, cache/context behaviour, tokens,
cost, resources) rather than on a single intuitive metric.

The M029 mission (Issue #229) requires:

* the same task definitions, repository baseline, validation gates, review
  semantics and model target for both backends;
* fresh isolated workspaces per trial and repeated trials to expose variance;
* authoritative per-request / per-phase / per-trial / aggregate telemetry,
  with every unavailable metric stated explicitly — never guessed;
* failed/blocked/unavailable trials preserved as evidence;
* a machine-readable artifact plus a human decision report that does not
  auto-promote a backend;
* no autonomous Git trust-boundary write;
* Harness qualification that fails closed if its runtime identity or
  handshake cannot be proven.

## Decision

Introduce `trajectory_os.benchmark` as a dedicated, provider-neutral
decision layer. It reuses the proven agent-backend contract, the M026
provider-grounded telemetry, the M026/M028 strict review protocol and the
M023 isolated Harness qualification; it does **not** introduce a second
backend abstraction.

### 1. Canonical workloads (`trajectory_os.benchmark.workloads`)

Five workloads are defined once and reused verbatim for every backend and
repetition: a small targeted repair, a medium feature implementation, a
multi-file integration change, an interruption/resume scenario and one
intentional blocked/fail-closed case. Each ships a tiny self-contained fixture
repository and a deterministic validation command. No workload depends on the
network, the TrajectoryOS source tree or operator state.

### 2. Git-free exact patch identity (`trajectory_os.benchmark.patch`)

The benchmark never runs a Git trust-boundary write. It captures a
deterministic content snapshot of the isolated workspace before and after
execution and derives the exact unified diff and its SHA-256. The digest is
reproducible, filesystem-order independent and never guessed. (The production
mission path keeps its M008 Git-attested semantic contract; the benchmark's
isolated diff identity is a distinct, additive domain.)

### 3. Trust gates (`validation`, `review`, `model.trial_decision`)

Every trial is gated by the workload's deterministic validation command and,
where a promotable patch exists, the strict M026/M028 review protocol
(`VALID_PASS` / `VALID_REJECT` / `REVIEW_PROTOCOL_INVALID`). The intentional
fail-closed workload passes only when every path it declares in its
workload-owned `protected_paths` is untouched and no forbidden patch was
produced (added, modified or removed). A trial whose reviewer was not
actually invoked is never displayed as reviewed, and (by default)
`require_review` blocks a would-be pass rather than silently dropping the
gate. One pure function, `model.trial_decision`, owns the complete
`(status, reason)` decision — including cancelled/backend-unavailable
precedence and fail-closed violations — and the engine only assembles its
inputs.

### 4. Reviewer identity

The active reviewer identity is persisted explicitly. For M029 the final
independent reviewer is `qwen3.8:27b-q4_K_M`. The status document always
distinguishes the implementation agent, the inline reviewer (inactive unless
explicitly enabled) and the final independent reviewer, so a stale/default
`qwen3.6` reviewer can never be shown as active.

### 5. Telemetry (`trajectory_os.benchmark.metrics` + `agents.telemetry`)

Every metric carries an explicit provenance: `PROVIDER`, `DERIVED`, `LOCAL`
or `UNAVAILABLE`. An `UNAVAILABLE` metric is `null` and carries a stable
reason. The final `validate_telemetry` boundary rejects any metric that is
`UNAVAILABLE` with a value or labelled with a source but missing a value.
Aggregation only uses non-`UNAVAILABLE` samples and always reports the
contributing-sample count; efficiency ratios are computed from trust-gated
successes only, while failed/redundant consumption is reported separately.

### 6. Durable artifacts and observability (`store`, `events`, `status`)

One run root is the canonical source of truth: `manifest.json`,
`state.json`, `events.jsonl`, `trials/<id>.json`, `summary.json` and
`report.md`. The status document exposes run/phase/trial/backend/provider/
model/requests/tokens/cache/tps/cost/validation/review/patch plus the three
actor roles. `follow` polls until a terminal state (`READY_FOR_COMMIT`,
`BLOCKED`, `FAILED`, `CANCELLED`, `COMPLETE`). Reconstruction fails closed on
malformed or identity-mismatched durable state.

### 7. Honest failure

A missing/incompatible Harness runtime or credential is recorded as a
`UNAVAILABLE` trial (and `CANARY_UNAVAILABLE`/`CANARY_INCOMPATIBLE`
qualification evidence). No successful Harness measurement is fabricated when
Harness cannot execute. Pipeline-validation (`FIXTURE`) evidence is explicitly
labelled non-authoritative and excluded from the runtime decision.

### 8. Decision output

`report.md` compares both backends, includes every failed/blocked/unavailable
trial, and presents the three options (Harness primary / Pi fallback, Pi
primary / Harness fallback, hybrid by workload class) with the measured
evidence required for each. It never auto-promotes a backend.

## Consequences

* The runtime decision is reproducible and auditable from durable artifacts.
* Unavailable metrics are visible rather than silently estimated, so the
  operator can see exactly which claims are unsupported.
* Harness promotion is fail-closed: it requires a QUALIFIED runtime and live
  trials that pass the same trust gates as Pi.
* The benchmark adds a new, bounded dependency-free package (standard library
  plus existing core modules); no new third-party dependency is introduced.
* The CLI is exposed as `scripts/trajectory-benchmark` and can be driven by
  `scripts/mission029_dogfood.py`.

## References

* `docs/development/BENCHMARK.md`
* `docs/adr/ADR-014-bounded-agent-backend-deepseek-canary.md`
* `docs/adr/ADR-018-isolated-harness-local-resources-artifact-provenance.md`
* `docs/adr/ADR-019-authoritative-events-web-lifeos.md`
