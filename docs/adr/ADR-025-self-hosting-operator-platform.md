# ADR-025 — Self-hosting operator platform (M040–M047)

## Status

Accepted.

## Context

M017–M039 delivered a large, working set of operator capabilities: a canonical
observability contract (M030), end-to-end mission assembly (M031), recovery
(M033), mission control (M034), production acceptance (M035) and a
human-gated release bundle (M036–M039). Each was coherent on its own, but
together they still required an operator to remember many commands, artifacts
and identities, and there was no single deterministic policy, no unified
event model and no full-lifecycle recovery beyond mission execution.

Issue #240 (M040–M047) required consolidating those pieces into one operator
product that can develop and release TrajectoryOS itself, **without**
introducing a competing lifecycle, readiness, mission identity, release
identity, observability or trust model. `mission_id == run_id` must remain
authoritative, and the human `GO COMMIT` / `GO MERGE` gates must remain the
only way release Git writes happen.

## Decision

Add `trajectory_os.operator`, a thin composition layer that reuses the
existing architecture and adds only additive documents to the canonical
mission root.

### 1. Deterministic policy layer (M044)

`operator.policy` defines four immutable profiles (`safe`, `fast-local`,
`benchmark`, `release`). Resolution is a pure function of `(profile, explicit
overrides)` with a content-addressed `policy_id`; the environment is never
consulted. The `release` profile always retains `GO COMMIT`, `GO MERGE` and
exact-head CI, and an override that disables any of them fails closed. The
resolved policy is persisted as `policy.json` before any work runs.

### 2. Backend/reviewer routing (M045)

`operator.routing` resolves one persisted `RoutingDecision` with explicit
implementation / inline-review / final-review identities. An unavailable
backend fails closed unless the policy explicitly allows a deterministic,
recorded fallback. A disabled role never exposes a model (no phantom
reviewer), and the final independent reviewer remains exactly
`qwen3.8:27b-q4_K_M` unless the operator recorded an explicit override. The
decision is written to `routing.json` (+ append-only history) before use.

### 3. Unified durable events (M043)

`operator.events` defines an additive, append-only operator event envelope
spanning mission and release. Events are content-addressed (identity excludes
`sequence`/`ts`), deterministically ordered, replayable and idempotent under
duplication. It is a **projection/replay substrate only**: the canonical
`status.json`, `closure.json` and release artifacts remain authoritative, and
`derive_events` maps M030–M039 evidence read-only for migration compatibility.

### 4. Full-lifecycle recovery (M042)

`operator.recovery` extends M033 beyond execution to the whole release chain.
`decide_recovery` is a pure read that discovers already-completed irreversible
actions (commit, push, PR, merge, closure) instead of repeating them, and
fails closed on any local/remote contradiction. The decision is persisted
idempotently as `lifecycle-recovery.json` and repeated recover/resume is
byte-idempotent.

### 5. Unified control plane (M041)

`operator.control_plane` composes assembly, observability, runtime control and
release into one surface exposed by `scripts/trajectory`. Observation
(`status`, `dashboard`, `follow`, `pr-status`, `reconstruct`) is strictly
read-only; control and release commands are explicit; Git release writes still
require the existing `GO COMMIT` / `GO MERGE` authorizations.

### 6. One-screen state and product acceptance (M046, M047)

`operator.state` builds one read-only projection over the canonical artifacts.
`operator.acceptance` runs the deterministic 35-case product matrix proving
the happy path, self-hosting dogfood, fail-closed gates, crash/recovery
idempotence, exact-head CI, human merge authorization, event replay, semantic
identity, trust boundaries and legacy compatibility.

### 7. Trust boundary (unchanged)

Implementation, review and runtime-control components still never perform a
Git release write. Only the release layer may, and only behind an explicit
operator authorization. Automated acceptance scans assert this.

## Consequences

* One operator command replaces many ad-hoc invocations while every prior
  script remains a compatible entrypoint.
* `M040–M047` durable evidence lives under `docs/missions/m040-m047/` and is
  reconstructable without prose.
* The new documents (`policy.json`, `routing.json`, `operator-events.jsonl`,
  `lifecycle-recovery.json`, `self-hosting-evidence.json`) are additive; no
  canonical artifact changes shape.
* A future TrajectoryOS program can start from one objective and reach release
  closure requiring only `GO COMMIT` and `GO MERGE` from the human operator.
