# ADR-006 — Fail-closed semantic promotion for read-only REVIEW sub-runs

Status: Accepted

Date: 2026-09-17

## Context

Mission 002 makes `VERIFY` and `REVIEW` read-only: the wrapper never
invokes the implementation agent. It deterministically validates the
current worktree and, for `REVIEW`, runs an independent read-only review
of the exact patch. The wrapper's terminal classification for these runs
is `READ_ONLY_VERIFIED` (its own pipeline exited cleanly) or
`READ_ONLY_FAILED`.

Mission 007 added the semantic result contract: the wrapper maps its
terminal classification to a semantic status through a closed mapping,
and the runner promotes `SUCCESS` to `COMPLETED` only with a verified
Mission 008 exact execution attestation. Before this decision,
`READ_ONLY_VERIFIED` mapped unconditionally to `SUCCESS`.

That mapping conflated two different claims. `READ_ONLY_VERIFIED` proves
only that the read-only wrapper pipeline itself exited cleanly; it says
nothing about whether the independent review of the exact patch actually
passed. A `REVIEW` run whose reviewer returned `ERROR`, `STALE`,
`TIMEOUT`, `REJECTED` or `FAIL`, or whose post-review identity
verification did not pass, still emitted `READ_ONLY_VERIFIED` and was
therefore promoted to semantic `SUCCESS`. The orchestrator then marked
the REVIEW phase `PASSED` and could complete the mission without any
successful review. Dogfood run `m009-r3` demonstrated the failure: the
review sub-run persisted `semantic_status=SUCCESS` while carrying
`semantic_readiness=NEEDS_REVIEW` and the reason `INDEPENDENT REVIEW
ERROR (NEVER TREATED AS PASS)`.

## Decision

A read-only terminal classification never implies semantic `SUCCESS` by
itself. The classification→status mapping is explicit per mode:

1. **PLAN** (unchanged): `AGENT_COMPLETED` / `READ_ONLY_VERIFIED` maps to
   `SUCCESS` only when all three deterministic read-only invariants hold
   (HEAD unchanged, semantic staging unchanged, exact PLAN worktree
   fingerprint unchanged). A proven violation is `FAILED`; missing proof
   is `UNKNOWN`.
2. **REVIEW**: `READ_ONLY_VERIFIED` maps to `SUCCESS` only when the
   wrapper's independent review returned `PASS`, the post-review exact
   patch verification returned `PASS`, and final readiness is
   `READY_FOR_COMMIT`. A deterministically known non-ready state
   (`NEEDS_REVIEW`, `BLOCKED`) maps to `FAILED`; missing or otherwise
   unproven evidence maps to `UNKNOWN`.
3. **Any other mode**: `READ_ONLY_VERIFIED` maps to `UNKNOWN`. A future
   read-only mode must define and prove its own promotion contract
   explicitly; it never inherits `SUCCESS` from a clean process exit.

`UNKNOWN` and `FAILED` are both fail-closed: neither marks the phase
`PASSED`. The existing runner gate is unchanged — a sub-run is
`COMPLETED` only with semantic `SUCCESS` plus a verified exact execution
attestation.

## Consequences

- A `REVIEW` phase can no longer be marked `PASSED` on the strength of
  the wrapper's process exit alone. A failed, stale, timed-out, rejected
  or unverified independent review fails closed and the mission does not
  complete.
- The decision is confined to the wrapper's classification→semantic
  status mapping. The deterministic review pipeline, the frozen Mission
  007 contract, the Mission 008 attestation contract and the
  orchestrator's phase logic are untouched.
- The rule is covered by focused tests that execute the mapping function
  in isolation for PLAN, REVIEW (pass and fail), an unknown read-only
  mode, and `READ_ONLY_FAILED`.
