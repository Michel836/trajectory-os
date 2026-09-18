# ADR-013 — Bounded adaptive replanning over immutable graph generations

Status: Accepted

Date: 2026-09-18

## Context

Missions 012–014 built a canonical, deterministic goal decomposition graph
(ADR-010), a deterministic portfolio scheduler and resource arbiter
(ADR-011) and explicit cross-mission evidence reuse (ADR-012). The remaining
V0 execution gap (Issue #220, program #207 checkpoint S15) is that a goal
graph was immutable once created: a failed mission, a discovered blocker, an
invalidated assumption or newly proven evidence could not safely change the
plan without either rewriting history or abandoning the graph.

Replanning is the most dangerous operation in the system because it can
silently erase the evidence a decision was based on. Four structural risks
had to be avoided:

1. **Destructive history rewrite.** Overwriting the graph in place would
   destroy the exact structure and identity that prior scheduler decisions
   and reuse projections were bound to.
2. **Implicit replanning.** Inferring a replan from free-form operator prose,
   model output, filesystem state or mission telemetry would make the plan
   depend on untrusted, non-reproducible input.
3. **Semantic success promotion.** A replan that marked a node complete, or
   copied success across nodes, would become a second source of truth beside
   canonical mission evidence.
4. **Stale-generation dispatch.** A scheduler decision computed for an older
   generation could be replayed or dispatched after the graph changed.

## Decision

Add a dedicated, bounded, deterministic replanning layer inside the graph
package (`trajectory_os.graph.replan`). It evolves the canonical M012 graph
through explicit machine-readable triggers, archives every prior generation,
and composes with the M013 scheduler so only the newest validated generation
is ever visible.

### 1. Explicit machine-readable triggers

A replan is driven only by one `ReplanTrigger` with a closed `kind`:

* `MISSION_FAILURE` — a referenced mission failed (requires mission provenance);
* `BLOCKER` — an explicit mission/blocker/code or detail;
* `INVALIDATED_ASSUMPTION` — an explicit detail or provenance;
* `NEW_PROVEN_EVIDENCE` — a mission or exact artifact content digest;
* `OPERATOR_REQUEST` — an explicit bounded operator detail.

Each trigger pins the exact `generation_id` and `graph_id` it was observed
against and carries bounded `EvidenceProvenance`. Free-form prose is never
parsed; no filesystem, process, environment or model state is ever treated as
a trigger.

### 2. Deterministic policy, declarative changes, plan identity

A bounded `ReplanPolicy` (generation/change limits, removal/supersession/reuse
gating, evidence requirement flag) gates every plan and has its own
domain-separated digest. A plan is a deterministic set of declarative
operations on the normalized node set: `ADD_NODE`, `REMOVE_NODE`,
`SUPERSEDE_NODE` (rewires dependents), `ADD_DEPENDENCY`,
`REMOVE_DEPENDENCY`, `INVALIDATE_REUSE`. The resulting graph is built through
the canonical `GoalGraph.build`, so dependency, cycle, reference, resource
and M014 reuse validation all apply; a plan is validated **before** it can be
activated, and a rejected plan writes no generation.

### 3. Immutable generations and append-only events

`<root>/goals/<goal_id>/replan/` owns:

* `current.json` — pointer to the active generation;
* `generations/<generation_id>.json` — the exact generation metadata plus its
  archived normalized graph;
* `events.jsonl` — the append-only replan event history (accepted activations
  and rejected plans).

The active graph document stays at the canonical `graph.json` path so the M012
graph store and M013 scheduler consume the same surface unchanged. Applying a
plan archives the previous generation exactly, atomically activates the new
one and appends an event. Generation, plan, trigger, policy and event
identities are domain-separated digests over normalized content and never
include a timestamp.

### 4. Scheduler generation binding and stale-generation guard

When a replan history exists, the active `generation_id` is bound into the
scheduler input projection and therefore into the scheduling decision
identity. Replanning archives the superseded scheduler state and decisions
under `scheduler/superseded/<generation>/` and deterministically re-binds a
fresh state to the new generation; a decision computed for an older
generation can never be replayed or dispatched. A persisted reuse projection
whose `graph_id` no longer matches the active graph is never trusted:
mandatory reuse inputs fail closed as unresolved until re-resolved.

### 5. No semantic promotion

Replanning changes graph structure only. It never marks a node complete,
never copies mission success and never promotes semantic evidence. Readiness
and completion remain derived solely from canonical mission evidence, and
M008 attestation, M009 review, M010 patch identity, M011 human gates and
M012–M014 invariants are unchanged.

### 6. Operator surface and M016 projection

The `trajectory-pi-goals` CLI is extended with `replan-preview`,
`replan-apply`, `replan-history`, `replan-why`, `current-generation`
(`generation`), `replan-validate` and the machine-readable `replan-project`
M016-facing projection. All inspect/explain commands are read-only; only
`replan-apply` writes. No command performs a Git trust-boundary write.

## Consequences

* A goal can adapt to explicit failure/blocker/evidence triggers without
  destroying the generation a prior decision was based on.
* Stale triggers, cycles, invalid dependencies/resources/reuse, policy
  violations and no-op plans fail closed with stable reason codes.
* The scheduler only ever sees the newest validated generation, and
  stale-generation work cannot be dispatched.
* Reconstruction after interruption reproduces the exact generation chain,
  identities and event history; the M016 machine projection is deterministic.
* M012–M014 behavior is byte-identical when no replan is declared.

## Alternatives considered

* **Mutate the graph in place.** Rejected: destroys the exact structure prior
  decisions and reuse projections were bound to.
* **Keep only the latest graph and a text log.** Rejected: not exactly
  reconstructible and not independently verifiable.
* **Let the scheduler keep dispatching after a replan until reset.** Rejected:
  stale-generation dispatch is unsafe.
* **Parse operator prose into a plan.** Rejected: non-reproducible and
  untrusted input.
* **A second graph store for replanned graphs.** Rejected: the canonical M012
  graph remains the single source of decomposition truth.
