# ADR-007 — Writable semantic promotion contract

Status: Accepted

Date: 2026-09-18

## Context

Mission 007 introduced the structured semantic result contract: the
canonical `trajectory-pi` wrapper maps its terminal classification to a
semantic status, and the runner promotes `SUCCESS` to `COMPLETED` only
with a verified Mission 008 exact execution attestation.

For the writable modes (`IMPLEMENT`, `REPAIR`, `RECOVERY`, `SMOKE`) the
wrapper's `AGENT_COMPLETED` classification mapped unconditionally to
semantic `SUCCESS`. But `AGENT_COMPLETED` proves only that the agent
process exited cleanly and emitted a completion marker. It does not prove
that the repository reached a state a human would accept as ready. The
wrapper derives a separate deterministic readiness
(`READY_FOR_COMMIT` / `READY_FOR_REVIEW` / `NEEDS_REVIEW` / `BLOCKED`)
from its own gates (snapshot completeness, diff check, validation,
independent review, `--require-changes`, staging identity).

A writable run could therefore persist `semantic_status=SUCCESS` while
carrying a non-green readiness such as `NEEDS_REVIEW` (for example an
unsatisfied `--require-changes` gate), and the orchestrator would mark the
phase `PASSED`. `AGENT_COMPLETED` had silently become the success claim
that readiness was supposed to gate.

## Decision

For the writable modes only, `AGENT_COMPLETED` never implies semantic
`SUCCESS` by itself. The promotion is re-derived from the wrapper's
deterministic readiness:

1. `READY_FOR_COMMIT` or `READY_FOR_REVIEW` -> `SUCCESS`.
2. `NEEDS_REVIEW` or `BLOCKED` -> `FAILED`.
3. absent or unrecognized readiness -> `UNKNOWN`.

No new semantic status is introduced; the promotion selects only among
the existing Mission 007 statuses. The frozen `schema_version=1` and the
Mission 008 attestation contract are untouched.

The rule is enforced twice (defense-in-depth):

* **producer**: the wrapper's `semantic_status_for_classification` applies
  the mapping before it emits the semantic result;
* **consumer**: the runner (and the durable record read path) refuses to
  turn a persisted `SUCCESS` whose *known* readiness is `NEEDS_REVIEW` or
  `BLOCKED` into `COMPLETED`. An absent readiness is legacy evidence and
  stays readable; a run with a green readiness is unaffected.

Non-`SUCCESS` statuses keep their fail-closed mapping, and process-failure
precedence is unchanged: a non-zero exit / timeout is still authoritative
and is never upgraded by semantic evidence. Read-only modes (`PLAN`,
`VERIFY`, `REVIEW`) keep their own explicit contracts (ADR-006); the
writable mapping never applies to them.

Mission 010 also records the operator `--require-changes` outcome as an
optional bounded provenance field (`NOT_REQUIRED`, `SATISFIED`,
`UNSATISFIED`, `UNPROVEN`) on the semantic result and on the durable
sub-run record. The field is optional so legacy evidence stays readable;
a malformed value fails closed at validation. When the gate is
unsatisfied the wrapper already downgrades readiness to `NEEDS_REVIEW`,
so the promotion contract turns it into `FAILED`.

## Consequences

- A writable phase can no longer be marked `PASSED` on the strength of a
  clean agent process plus a green-looking semantic document. An
  unsatisfied `--require-changes`, a failed validation, or a non-green
  readiness fails closed.
- A legacy record with no readiness claim remains readable and its
  historical behavior is unchanged; no data migration is required and no
  schema version moves.
- The consumer rule is mode-aware only where the mandatory contract is
  defined: a *known* non-green readiness fails closed for every mode,
  while an unrecognized readiness fails closed only for writable modes.
- The rule is covered by focused tests for green/non-green/absent/
  unrecognized readiness, the end-to-end runner path with a verified
  attestation, and the durable round-trip of `semantic_require_changes`.
