# ADR-010 — Goal decomposition graph

Status: Accepted

Date: 2026-09-18

## Context

Missions 003–011 built a bounded, resumable, fail-closed multi-phase
mission orchestrator with exact execution attestation (M008), independent
review and patch identity (M009), writable semantic promotion (M010) and
an explicit reduced-intervention human gate model (M011). The remaining
gap for the V0 execution program (Issue #207, checkpoint S12 / #215) is the
**strategic layer above one mission**: turning one strategic goal into
explicit, dependent mission nodes while preserving every existing
fail-closed guarantee.

Two structural risks had to be avoided:

1. **A second source of truth.** A goal graph that stores mission state,
   completion flags, attestations or proofs would inevitably drift from the
   canonical mission evidence and could be promoted independently.
2. **An execution engine.** M012 must not schedule, queue or launch work;
   that is M013's responsibility.

## Decision

Introduce a dedicated, bounded, deterministic goal decomposition graph
package (`trajectory_os.graph`) with a pure model, a strict store and a
read-only readiness projection. The graph is an *orchestration/projection*
structure only; canonical mission evidence stays authoritative.

### 1. Graph contract

A goal graph is one strategic goal plus explicitly dependent mission nodes:

* `goal_id` — stable canonical slug; one persisted graph per goal;
* `objective` — bounded text (reuses the mission objective bound);
* `nodes` — explicit mission nodes, each with:
  * `node_id` — stable canonical slug (unique in the graph);
  * `title` — bounded mission objective for the node;
  * `priority` — integer in `[0, 100]`; **higher executes first**;
  * `depends_on` — explicit dependency ids (never inferred from prose);
  * `acceptance_criteria` — a non-empty list of explicit, persisted
    structured criteria (`criterion_id`, `statement`, optional
    `verification`). There is no "vague prose implies success" path;
  * `mission_ref` — optional explicit canonical mission identity
    (`mission_id`, `required`); the graph stores the reference only;
  * `resources` — bounded CPU/GPU/GPU-memory/exclusivity/model-heavy
    declaration (CPU/GPU/GPU-mem validity reuses the canonical
    `runs.resources.ResourceRequirement` validator);
  * `budgets` — bounded sub-run/time/repair/attempt budget references;
* `edges` — the explicit dependency edge set, derived deterministically
  from `depends_on` (`from` must complete before `to`);
* `provenance` — canonical creation provenance (identity domains, creation
  timestamp, creator and repository root/baseline revision).

The **normalized graph** is deterministic: nodes are sorted by `node_id`,
dependencies and criteria are sorted, and edges are sorted by
`(from, to)`.

### 2. Identity domains (additive, never overloaded)

Two domain-separated digests are introduced:

* `trajectory-os.goal-decomposition-spec.v1` → `spec_sha256` (the
  normalized declarative spec identity);
* `trajectory-os.goal-decomposition-graph.v1` → `graph_id` (the normalized
  graph projection identity, including the explicit edge set).

Canonical bytes are `domain_utf8 || 0x00 || canonical_json_utf8` where
`canonical_json` is `json.dumps(..., sort_keys=True,
separators=(",", ":"), ensure_ascii=True)` over already-normalized
(sort-stable) content. The domains are **distinct** from the Mission 010
patch identity domains; a graph identity is never compared to a patch
identity and no equality relation is implied. Reconstruction recomputes
both digests and fails closed (`GRAPH_IDENTITY_MISMATCH`) on any mismatch.

### 3. Fail-closed invariants

Normalization/creation/store reconstruction reject, with stable codes:
cycles, self dependencies, missing dependencies, duplicate node ids,
duplicate/conflicting edges, duplicate mission references, malformed
schema/fields, unsupported schema version, oversized graph/fields, invalid
priorities, invalid/bounded resource or budget declarations, and
unresolved/malformed/contradictory **required** mission references.
Nothing is ever repaired or rewritten silently.

### 4. Readiness projection (M013 interface)

`trajectory_os.graph.readiness` is a pure projection over a graph plus
resolved, read-only mission evidence. Semantically distinct states:

| state | meaning |
| --- | --- |
| `READY` | eligible: every upstream dependency is `COMPLETE` |
| `COMPLETE` | own referenced mission is proven green **and** every upstream is `COMPLETE` |
| `IN_PROGRESS` | own referenced mission exists but is not proven complete |
| `BLOCKED` | an upstream dependency is not proven, or the own mission failed |
| `UNRESOLVED` | a required reference cannot be resolved |
| `INVALID` | malformed/contradictory evidence (fail closed) |

Precedence is fixed: `INVALID` > `UNRESOLVED` > own
`COMPLETE`/`IN_PROGRESS`/`FAILED` > dependency satisfaction. A node with a
proven-complete mission but an unproven upstream dependency is a
contradiction and is reported `INVALID`. A dependency-blocked node is
never `READY` or eligible.

The graph **never copies** mission trust evidence into its own state.
Mission references are resolved read-only at projection time: a
`COMPLETE` mission is only accepted as proven after the canonical
orchestrator re-validates that every persisted `PASSED` phase still has
`COMPLETED` sub-run evidence. Non-terminal missions are never passed
through reconstruction, so a readiness projection can never terminalize
in-flight evidence.

### 5. Persistence

`<root>/goals/<goal_id>/graph.json` is written atomically (temp file +
fsync + rename) through the same helper the mission store uses, plus a
bounded append-only `<root>/goals/<goal_id>/events.jsonl` creation event.
Reads are strict: unknown fields, unsupported schema versions, malformed
values and identity mismatches fail closed. Reconstruction yields the same
identity, normalized nodes, normalized edges, topological order, readiness
projection and provenance.

### 6. Topological order

Kahn's algorithm with the explicit stable key `(priority DESC,
node_id ASC)`. The order never depends on dictionary insertion order,
input order or filesystem order; the tie-breaker is contractual.

### 7. Operator surface

A new `trajectory-pi-goals` CLI (`trajectory_os.graph.cli`) exposes
`create`, `show`, `nodes`, `edges`, `order`, `ready`, `blocked`, `why`,
`project`, `validate` and `list`, with human-readable and JSON output. It
introduces **no operator micro-gate**: only `create` writes graph state,
and it never launches or mutates a mission. `project` is the
machine-readable M013 scheduler-facing projection.

## Consequences

- One strategic goal is now a persistent, bounded, deterministic DAG whose
  readiness M013 can consume without re-deriving structure.
- Mission execution/completion/proof remains solely canonical mission
  evidence; the graph only references it and re-validates it read-only.
- M008/M009/M010/M011 are untouched: no promotion path, no patch-identity
  comparison, no Git write, and no new human gate for ordinary graph
  operations.
- Legacy mission records remain readable; graph state is additive and
  separate.
- M012 deliberately does **not** implement scheduling/arbitration; that is
  explicitly deferred to M013.

## Alternatives considered

- **Store mission state in the graph.** Rejected: creates a competing
  source of truth that can drift and be promoted independently.
- **Derive dependencies from prose / model output.** Rejected: violates
  "no LLM where an algorithm is more reliable" and the explicit-edge
  fail-closed contract.
- **Fold graph commands into the mission CLI.** Rejected: mixing the
  orchestration projection with the execution store would blur the
  read-only reference boundary; a thin dedicated shim follows the existing
  `trajectory-pi-missions` / `trajectory-pi-runs` pattern.
- **Reuse the M010 patch-identity domains.** Rejected: unjustified equality
  relation between unrelated hashes.
