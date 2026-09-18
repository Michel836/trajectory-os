# ADR-015 — Derived goal-level proof and compact operator control

Status: Accepted

Date: 2026-09-18

## Context

Missions 008–015 built exact execution attestation (ADR-005), fail-closed
semantic promotion (ADR-006/007), a persistent goal decomposition graph
(ADR-010), a deterministic portfolio scheduler and resource arbiter
(ADR-011), explicit cross-mission evidence reuse (ADR-012) and bounded
adaptive replanning over immutable graph generations (ADR-013). The final V0
gap (Issue #222, program #207 checkpoint S16) is the one question none of
those layers answered: **is the original strategic goal actually proven
satisfied from mission-level evidence?**

Four failure modes had to be avoided:

1. **Inferred success.** Treating a process exit code, model prose, a
   timestamp, scheduler admission, resource availability, a reuse record or
   a replan as evidence that the goal is achieved.
2. **A second source of truth.** Persisting a "goal complete" flag that can
   drift from the canonical mission/graph/scheduler/reuse/replan state.
3. **Silent trust promotion.** Counting stale, legacy (pre-M008), unproven,
   contradictory or unresolved evidence as proof.
4. **Opaque operations.** Requiring an operator to read many stores to learn
   whether a goal is provable, which criteria are unmet, and what the exact
   next human decision is.

## Decision

Add a bounded, deterministic, fail-closed **goal-proof projection** inside
the graph package (`trajectory_os.graph.proof`) plus a compact operator
dashboard. The projection is **derived evidence**, never a second source of
truth.

### 1. Exact acceptance-criterion → evidence bindings

Every acceptance criterion declared by the *active* generation of the goal
graph is bound to the exact evidence that proves it:

* the referenced mission identity, state and reason;
* the explicit phase (when the criterion's `verification` is exactly one
  canonical phase id) or every `PASSED` phase of the mission;
* for each contributing phase, the terminal sub-run identity, persisted
  classification, semantic status and exact-attestation outcome;
* a domain-separated content digest over those bounded identities.

A criterion is `PROVEN` only when the mission is proven complete, the named
phase (if any) is `PASSED`, and every contributing model-heavy phase carries
a verified exact M008 attestation. A `verification` value that name-shapes as
a phase id but resolves to no phase fails closed as an **ambiguous mapping**.
Model-heavy phases without verified attestation are `LEGACY` and never
promote.

### 2. COMPLETE only with zero unresolved risk

The final goal state is `COMPLETE` only when **both** hold:

* there is at least one configured acceptance criterion and every criterion
  is `PROVEN`; and
* zero risks remain across nodes, missions, dependency readiness, scheduler
  decisions/resources, cross-mission reuse, replanning, graph generation and
  reconstruction.

Otherwise the state is `INCOMPLETE` with a single stable reason code
selected by a fixed risk precedence (`IMPOSSIBLE` → `CONTRADICTORY` →
`INVALID` → `STALE` → `LEGACY` → `UNRESOLVED` → `BLOCKED` → `UNPROVEN`).

### 3. Timestamp-free identity and reconstruction proof

`proof_id` is a SHA-256 over the domain-separated canonical JSON of the
normalized projection, excluding every timestamp. A `computed_at` value is
persisted as evidence only. Reconstruction recomputes the live projection
from canonical stores and fails closed (`RECONSTRUCTION_MISMATCH`) unless it
reproduces the exact persisted `proof_id`. The projection and a bounded
append-only proof/operator event history are stored under
`<root>/goals/<goal_id>/proof/`.

### 4. Bounded operator control

The CLI (`trajectory-pi-goals`) exposes `goal`, `goal-proof`,
`goal-explain`, `goal-criteria`, `goal-risks`, `goal-record`,
`goal-validate` and `goal-history`. The machine document is complete for
automation; the human dashboard is compact (roughly one terminal screen) and
shows the goal state/reason, generation, proof identity, counts, critical
path, scheduler/resources, reuse, replans, risks, every criterion with its
exact proving evidence, the current human gate, and bounded why/explain
information.

### 5. DeepSeek Harness qualification

M016 exercises the existing M015 provider-neutral contract
(`trajectory_os.agents`) with one bounded structured canary. Structured
runtime lifecycle evidence is authoritative: a session that reaches `idle`
after an explicitly errored `turn/end` is **not** a completion, and the
adapter records the stable reason code (`CREDENTIALS_MISSING`,
`INCOMPATIBLE_PROTOCOL`, …) instead of a false pass. When the SDK/runtime or
credentials are unavailable or incompatible the qualification records a
deterministic `CANARY_UNAVAILABLE`/`CANARY_INCOMPATIBLE` and the proven Pi
path remains the fallback. Qualification evidence is secret-free (lifecycle
kinds/methods and completion evidence only — never raw payloads or model
text).

## Consequences

* One strategic goal now has a deterministic, inspectable, reconstructible
  answer to "is it actually proven?", with the exact proof chain for every
  acceptance criterion.
* `COMPLETE` can no longer be inferred from any single lower-level signal;
  stale, legacy, unresolved, blocked, contradictory and invalid state always
  yield a stable non-complete reason.
* The projection is derived only: removing it leaves every canonical
  M008–M015 store and contract intact.
* The human trust gates (`LAUNCH`, `GO COMMIT`, `GO MERGE`) remain
  authoritative; the goal proof only *describes* the current gate and never
  performs a Git trust-boundary write.

## Alternatives considered

* **Store a mutable `goal_complete` flag on the graph.** Rejected: becomes a
  second source of truth that can drift from mission evidence.
* **Infer completion from scheduler admission or reuse records.** Rejected:
  admission proves capacity, and reuse is input provenance only — neither
  proves an acceptance criterion.
* **Treat every PASSED phase as exact proof.** Rejected: pre-M008 legacy
  evidence and unverified semantic results must never be silently promoted.
* **Require the operator to read five stores manually.** Rejected: the
  dashboard/JSON projection is required for bounded human control.
