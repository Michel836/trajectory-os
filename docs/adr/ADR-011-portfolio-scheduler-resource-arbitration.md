# ADR-011 — Portfolio scheduler and resource arbitration

Status: Accepted

Date: 2026-09-18

## Context

Mission 012 (Issue #215, ADR-010) delivered a persistent, deterministic goal
decomposition DAG: explicit dependencies, persisted priorities, acceptance
criteria, bounded resource requirements, bounded execution budgets, a
read-only dependency readiness projection and a machine-readable
scheduler-facing projection. M012 deliberately performed **no** scheduling,
admission, queueing or launching.

The remaining V0 execution gap (Issue #217, program #207 checkpoint S13) is to
turn that graph into an executable portfolio without weakening any existing
guarantee:

1. select only dependency-ready, proven-eligible graph nodes;
2. admit them against explicit bounded local resources (CPU, local GPU,
   local GPU VRAM, exclusivity, global concurrency) and declared budgets;
3. persist a deterministic, reproducible decision with stable reason codes;
4. optionally dispatch admitted work through the existing production mission
   path;
5. reconstruct state exactly after a restart;
6. never let model prose, randomness, filesystem order or a process exit code
   determine scheduling eligibility or mission success.

Two structural risks had to be avoided:

* **a second graph or mission engine** — the M012 graph and the canonical
  mission evidence remain the only sources of truth;
* **resource overcommitment by inference** — a model-heavy node must not
  claim local GPU/VRAM merely because it is model-heavy.

## Decision

Add a dedicated, bounded, deterministic portfolio scheduler inside the graph
package (`trajectory_os.graph.scheduler`). It consumes M012 structures
read-only, owns only scheduler state, and composes with the existing mission
orchestrator for dispatch.

### 1. Architectural placement

`trajectory_os.graph.scheduler` contains:

* `identity` — four domain-separated scheduler identity domains;
* `model` — capacity policy, node demand, reservations, decisions, durable
  state (pure, strict);
* `evidence` — read-only mission trust + runtime evidence resolution;
* `arbiter` — pure deterministic candidate ordering and admission;
* `engine` — one scheduling cycle + production-path dispatch composition;
* `store` — atomic durable scheduler store (strict, fail closed);
* `summary` — read-only human/machine operator projections.

No second graph model and no second execution engine are introduced. Dispatch
is one canonical `missions.orchestrator.run_mission` call on the node's own
referenced mission.

### 2. Explicit configured capacity

The scheduler capacity is supplied as a bounded, explicit JSON policy:

```json
{"schema_version": 1, "cpu_slots": 8, "gpu_slots": 2,
 "gpu_mem_bytes": 8589934592, "global_concurrency": 6}
```

* every dimension is a required, bounded integer;
* unknown fields, out-of-range values and unsupported schema versions fail
  closed (`INVALID_CAPACITY_POLICY` / `UNSUPPORTED_SCHEDULER_SCHEMA`);
* capacity is **never** inferred from mission prose, provider identity,
  process inspection or physical GPU telemetry;
* a bounded documented default is used only when no policy file is supplied.

### 3. Explicit per-node demand and execution path

Node demand is derived solely from the M012 `resources` / `budgets`
declaration:

* `cpu_slots` (default 0), `gpu` → 1 local GPU slot only when explicitly
  `true`, `gpu_mem_bytes` only when `gpu` is explicitly `true`, `exclusive`,
  `model_heavy`, and the bounded budgets.

Execution path is classed deterministically:

| class | rule |
| --- | --- |
| `LOCAL_GPU` | `resources.gpu == true` |
| `REMOTE_MODEL` | `gpu` not `true` **and** `model_heavy == true` |
| `LOCAL_CPU` | no GPU and not model-heavy, with declared `cpu_slots` |
| `UNSPECIFIED` | no declared demand |

**Only `LOCAL_GPU` reserves local GPU slots or VRAM.** Remote model-heavy work
(for example DeepSeek-like providers) is admitted on CPU/concurrency grounds
only and reserves zero GPU/VRAM. `model_heavy` alone never claims local
hardware. Malformed/inconsistent resource metadata (for example
`gpu_mem_bytes` without `gpu`) fails closed (`INVALID_RESOURCE_SPEC`).

### 4. Eligibility

The scheduler consumes the M012 readiness projection and the read-only
mission runtime evidence:

* **dependency eligibility is M012-authoritative**: every explicit dependency
  must be `proven` (`COMPLETE`). A dependency-blocked node is never a
  candidate and never dispatched;
* a node is not eligible when its M012 state is `INVALID`,
  `UNRESOLVED`, `COMPLETE` or its own mission failed;
* an own mission that has **started** and is non-terminal is `ACTIVE` (it
  owns its reservation and is never re-admitted);
* an own mission that exists but has **not started** (created/PLANNING) is
  eligible once its dependencies are proven. M012 reports such a node as
  `IN_PROGRESS` (a referenced mission exists); M013 therefore refines that
  state with the authoritative runtime fact "has not started" rather than
  re-deriving dependencies. Dependency readiness itself is never
  re-derived or silently promoted;
* **mission identity is required**: a node without a resolvable canonical
  `mission_ref` is deferred (`MISSING_MISSION_REFERENCE` /
  `UNRESOLVED_EVIDENCE`), never admitted or dispatched.

### 5. Deterministic ordering

Candidate order is `(priority DESC, node_id ASC)`. All candidates are
dependency-ready by construction, and the `node_id` tie-breaker is
contractual, so the order never depends on insertion order, filesystem order
or input order. Timestamps are recorded as evidence only and never affect
ordering or decision identity.

### 6. Deterministic admission precedence

Each candidate receives exactly one stable reason. The precedence is:

`INVALID_RESOURCE_SPEC` > `BUDGET_EXHAUSTED` > `EXCLUSIVE_CONFLICT` >
`CONCURRENCY_LIMIT` > `CPU_CAPACITY` > `GPU_CAPACITY` > `GPU_MEMORY`.

`exclusive` means whole-scheduler exclusivity: an exclusive node is admitted
only when nothing else is reserved and it blocks every subsequent admission
in the same cycle. Budget exhaustion is derived from authoritative mission
evidence (time/sub-run/repair reasons and counters) plus scheduler-owned
bounded attempt counters.

### 7. Reservations

Reservations separate:

* **configured capacity** — the explicit policy;
* **current reservations** — nodes whose referenced mission has started and
  is non-terminal, derived fresh from authoritative evidence each cycle;
* **scheduler-owned reservations** — the subset with a durable scheduler
  dispatch record;
* **physical telemetry** — never a proof of ownership and never an accounting
  input.

Reservations are released only when authoritative mission evidence shows a
terminal state, with an explicit release reason
(`MISSION_COMPLETE`/`MISSION_FAILED`/`MISSION_BLOCKED`/`MISSION_INVALID`).

### 8. Decision identity

Four domain-separated digests are introduced:

* `trajectory-os.portfolio-scheduler-policy.v1` → `policy_id`;
* `trajectory-os.portfolio-scheduler-projection.v1` →
  `input_projection_id` (graph + readiness + runtime evidence + policy +
  reservation snapshot);
* `trajectory-os.portfolio-scheduler-decision.v1` → `decision_id`;
* `trajectory-os.portfolio-scheduler-dispatch.v1` → `dispatch_id`.

Canonical bytes are `domain_utf8 || 0x00 || canonical_json_utf8`. The domains
are distinct from the graph/spec and Mission 010 patch domains. **Decision
identity excludes timestamps**: the same inputs always produce the same
`decision_id`. `created_at` is persisted evidence only.

### 9. Persistence

Under `<root>/goals/<goal_id>/scheduler/`:

* `state.json` — versioned scheduler state (policy snapshot, dispatch
  records, bounded attempt counters, active node ids, decision history ids);
* `decisions/<decision_id>.json` — immutable decision documents; re-persisting
  an identical decision is an idempotent no-op that preserves the original
  evidence timestamp;
* `events.jsonl` — append-only bounded scheduler events.

### 10. Reconstruction

Reconstruction recomputes every decision identity from content and fails
closed on malformed state, unsupported schema, graph/goal identity mismatch,
missing or untracked decision files, duplicate selected identities,
contradictory reservations, duplicate/ambiguous dispatch evidence, impossible
CPU/GPU accounting and history overflow. A restart reconstructs the exact
persisted scheduling decision; a proven dispatch decision is never silently
replayed.

### 11. Dispatch contract

Dispatch is opt-in and composes with the existing production path. It may
launch only dependency-ready, resource-admitted, within-concurrency,
within-budget nodes. Exact goal/graph/node/mission identities are preserved.
A successful scheduler or subprocess exit never proves mission completion:
downstream readiness changes only after authoritative mission evidence proves
upstream completion. Attempts are bounded and recorded append-only.

### 12. Operator surface and trust boundaries

The existing `trajectory-pi-goals` CLI is extended with `capacity`,
`preview`, `schedule`, `dispatch`, `portfolio`, `resources`, `history`,
`why-schedule` and `validate-schedule`, with human-readable and JSON output
for M014/M015. Ordinary scheduling/admission/dispatch introduces **no**
operator micro-gate. M008 exact attestation, M009 independent review, M010
writable semantic fail-closed promotion and patch-identity domains, M011
LAUNCH / GO COMMIT / GO MERGE human trust boundaries, and M012 canonical
graph/evidence ownership are all preserved. No Git trust-boundary write is
performed anywhere.

## Consequences

* The M012 graph is now executable without becoming an execution engine.
* Resource admission is explicit, bounded, deterministic and reproducible.
* Remote model-heavy work never consumes local GPU capacity; local GPU work
  reserves exactly its declared VRAM.
* Scheduling decisions, deferrals and dispatches are durable, auditable and
  exactly reconstructable after restart.
* Human trust boundaries and reduced intervention are unchanged.

## Alternatives considered

* **Infer GPU need from `model_heavy`.** Rejected: it would reserve local GPU
  for remote providers and overcommit local hardware.
* **Probe hardware for capacity.** Rejected: non-deterministic, environment
  dependent, and violates "no unknown evidence overcommit".
* **Reuse the M010 patch identity domain for decisions.** Rejected:
  unjustified equality relation between unrelated hashes.
* **Store reservations as authoritative scheduler state.** Rejected: it would
  drift from canonical mission evidence; reservations are derived read-only
  and validated each cycle.
* **A separate scheduler CLI family.** Rejected: the goal graph and its
  scheduler share one production surface; a second family would blur the
  canonical decomposition boundary.
