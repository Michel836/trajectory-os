# ADR-012 — Explicit cross-mission evidence reuse

Status: Accepted

Date: 2026-09-18

## Context

Missions 003–013 built a bounded, resumable, fail-closed mission
orchestrator (M003–M011), a persistent deterministic goal decomposition
graph (M012, ADR-010) and a deterministic portfolio scheduler/resource
arbiter (M013, ADR-011). The remaining V0 execution gap (Issue #219, program
#207 checkpoint S14) is to let a downstream mission consume a **proven
upstream artifact/evidence item** through an explicit reference without
weakening any existing guarantee.

Three structural risks had to be avoided:

1. **Copying semantic success.** A reuse record that stores, mirrors or
   promotes upstream completion would become a second source of truth and
   could be consumed independently of the canonical mission evidence.
2. **Implicit state leakage.** Discovering "nearby" files, processes,
   environment variables or model context and treating them as cross-mission
   evidence would make trust depend on the host, not on explicit proof.
3. **Silent trust promotion.** Legacy/historical evidence (pre-M008 records
   without exact attestation) must remain distinguishable and must never be
   silently upgraded to freshly attested trust.

## Decision

Add a dedicated, bounded, deterministic reuse layer inside the graph package
(`trajectory_os.graph.reuse`). It extends the canonical M012 graph with
explicit, per-node reuse declarations, resolves them strictly read-only
against the canonical mission store, persists one exact projection plus an
append-only consumption history, and composes with the M013 scheduler.

### 1. Explicit declaration on the canonical graph

Reuse lives on the existing M012 graph node as an optional, bounded
`reuse_inputs` list. Each `ReuseInput` declares:

* `input_id` — stable slug, unique within the consumer node;
* `producer_node_id` — **must be an explicit direct dependency** of the
  consumer (so reuse can never introduce an implicit edge, an impossible
  dependency relation or a cycle);
* `evidence_kind` — closed set; `PHASE_EVIDENCE` (a proven phase's bounded
  evidence artifact) is the only M014 kind;
* `phase_id` — the exact producer phase whose evidence is consumed;
* `required` — whether an unresolved input blocks scheduler admission;
* `min_trust` — the minimum producer trust level (`PROVEN` or `LEGACY`);
* `producer_mission_id` (optional) — explicit expected producer mission
  identity, validated against the producer node's canonical reference;
* `expected_sha256` (optional) — an exact content identity binding.

The field is emitted only when declared, so pre-M014 graphs keep their exact
M012 `spec_sha256`/`graph_id` (backward compatibility). New declarations are
covered by the graph identity and are reconstructed exactly.

### 2. Read-only resolution

`trajectory_os.graph.reuse.resolver` resolves declarations strictly
read-only:

* the producer must be a direct dependency with a resolvable mission;
* the producer mission must be **proven complete** by the canonical
  orchestrator re-validation (M012/M008 evidence), never by the reuse layer;
* the referenced producer phase must be `PASSED` with `COMPLETED` sub-run
  evidence;
* trust is `PROVEN` only when the proving sub-run carries independently
  verified M008 attestation; otherwise it is `LEGACY`;
* a `min_trust` the evidence cannot satisfy, a failed producer, a malformed
  mission/evidence document, an expected-identity mismatch or a phase that is
  no longer `PASSED` all fail closed with a stable reason code.

Nothing is ever discovered implicitly: only explicit graph declarations are
resolved, and only from the canonical mission store under the same root.

### 3. Reuse is input provenance, never completion proof

A resolved reuse input records producer → consumer provenance, the exact
artifact content identity, the producer's patch identity (when present), the
proving sub-run and its attestation. It **never** advances, completes or
promotes either mission. Consumer readiness/completion remains derived solely
from the consumer's own authoritative mission evidence.

### 4. Identity domains (additive, never overloaded)

Three domain-separated digests are introduced:

* `trajectory-os.cross-mission-reuse-artifact.v1` →
  `artifact_content_sha256` (exact resolved evidence artifact identity);
* `trajectory-os.cross-mission-reuse-projection.v1` → `projection_id`
  (the exact resolved projection);
* `trajectory-os.cross-mission-reuse-consumption.v1` → `consumption_id`
  (one append-only consumption event).

Canonical bytes are `domain_utf8 || 0x00 || canonical_json_utf8`. The domains
are distinct from the graph/spec, scheduler, Mission 010 patch and Mission
008 attestation domains; no equality relation is implied. Projection identity
excludes every timestamp, so the same inputs always produce the same
`projection_id`. `consumed_at` is persisted evidence only.

### 5. Persistence and reconstruction

Under `<root>/goals/<goal_id>/reuse/`:

* `projection.json` — the exact versioned resolved projection (strict schema);
* `consumptions.jsonl` — append-only producer → consumer consumption events.

Every write is atomic (temp file + fsync + rename) or append-only JSONL.
Reconstruction recomputes the projection identity from content **and**
re-resolves the live projection from canonical mission evidence, failing
closed (`REUSE_EVIDENCE_STALE`) on drift, plus on schema/identity mismatch,
graph mismatch, missing projection, duplicate/untracked/contradictory
consumption records and history overflow. Re-appending an identical
consumption is an idempotent no-op that preserves the original evidence
timestamp.

### 6. Scheduler composition

The M013 scheduler remains the admission/resource authority. When a persisted
reuse projection exists, its exact `projection_id` and every consumer's
resolution reason are bound into the scheduler's `input_projection_id`, and a
node with an unresolved **mandatory** reuse input is deferred with the stable
reuse reason before any resource admission. With no persisted projection, a
declared mandatory input fails closed as unresolved — implicit evidence is
never trusted. Resource admission stays separate from semantic evidence
trust. Scheduler reconstruction revalidates the persisted reuse projection
for graphs that declare reuse inputs. When no reuse is declared the
scheduling input payload is byte-identical to M013.

### 7. Operator surface and trust boundaries

The existing `trajectory-pi-goals` CLI is extended with `reuse`,
`reuse-resolve`, `reuse-validate`, `reuse-history`, `why-reuse` and
`provenance`, and the compact `portfolio` summary reports unresolved reuse
blockers. All inspect/explain commands are read-only; only `reuse-resolve`
writes the projection and consumption log. M008 exact attestation, M009
independent review, M010 writable semantic fail-closed promotion and patch
identity domains, M011 LAUNCH / GO COMMIT / GO MERGE human trust boundaries,
M012 canonical graph ownership and M013 scheduler authority are all
preserved. No Git trust-boundary write is performed anywhere.

## Consequences

* Downstream work can consume an explicit upstream proof without copying or
  promoting it, and multiple consumers may reference one immutable proof
  without mutating it.
* Missing, stale, malformed, ambiguous, contradictory, duplicate, cyclic and
  identity-mismatched references fail closed with stable reason codes.
* Legacy evidence stays explicitly `LEGACY` and cannot be silently promoted.
* Reuse projections and consumption history reconstruct exactly after a
  restart and bind the scheduler decision to the exact evidence consumed.
* M012–M013 behavior is unchanged when no reuse is declared.

## Alternatives considered

* **Persist a copy of upstream evidence in the consumer mission.** Rejected:
  it would create a second source of truth and could be promoted independently.
* **Auto-discover "latest" upstream evidence by filesystem order.** Rejected:
  non-deterministic and an implicit-trust channel.
* **Store reuse trust in the graph/readiness projection.** Rejected: reuse
  must not become a completion proof; readiness stays canonical.
* **Reuse the M010 patch identity domain for artifacts.** Rejected:
  unjustified equality relation between unrelated hashes.
* **A second reuse graph/scheduler.** Rejected: declarations live on the M012
  graph and admission stays with the M013 scheduler.
