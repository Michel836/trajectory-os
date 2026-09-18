# ADR-005 — Versioned exact execution attestation for mission sub-runs

Status: Accepted

Date: 2026-09-17

## Context

Mission 007 introduced a structured semantic result contract for
model-heavy sub-runs (`src/trajectory_os/missions/semantic.py`): the
canonical `trajectory-pi` wrapper emits one small JSON document bound to
the exact `subrun_id`, and the runner promotes a clean exit (0) to
`COMPLETED` only when that document says `SUCCESS`.

That contract proves *intent/outcome* ("the agent says it completed") but
not *execution*: the document is a producer claim with no binding to the
exact wrapper run, the exact repository state, or the exact patch that was
evaluated. A stale, copied, or hand-written `SUCCESS` document is
indistinguishable from a genuine one. Mission 008 requires model-heavy
semantic success to be bound to the exact wrapper execution and exact
repository evidence, and it requires the runner to verify those identities
independently rather than trust the producer.

## Decision

Extend, do not replace, the frozen M007 contract. The top-level document
keeps `schema_version = 1` and its exact M007 fields. Mission 008 adds one
**optional, independently-versioned** `attestation` object:

```json
"attestation": {
  "schema_version": 1,
  "subrun_id": "<the exact sub-run>",
  "run_id": "<wrapper run identity / run directory name>",
  "repo_head_before": "<40-or-64 lowercase hex>",
  "repo_head_after":  "<40-or-64 lowercase hex>",
  "patch_sha256": "<64 lowercase hex>"
}
```

Rules:

1. **Pure shape contract** (`semantic.validate_attestation`): a complete,
   well-formed, internally consistent attestation is required. Missing ->
   `ATTESTATION_MISSING`; wrong shape/version/type/oversized/bad hex ->
   `ATTESTATION_MALFORMED`; a required field absent/blank -> `ATTESTATION_PARTIAL`;
   `repo_head_before != repo_head_after` (the wrapper never commits) ->
   `ATTESTATION_CONTRADICTORY`.
2. **Independent runner verification** (`runner.verify_attestation`): for
   every valid semantic document the runner re-derives the identities from
   sources it controls — the exact issued `subrun_id`, the wrapper run
   directory `meta.txt` (`run_id`, resolved `workspace`, `head_before`), a
   fresh `git rev-parse HEAD` in the repository, a fresh SHA-256 over the
   exact `worktree.patch` bytes, and a clock-free launch-ordering check
   (`meta.txt` must postdate the runner's own launch evidence). Any
   disagreement or unverifiable artifact fails closed
   (`ATTESTATION_MISMATCH` / `ATTESTATION_STALE`).
3. **Success gating**: `SUCCESS -> COMPLETED` requires a verified
   attestation. An unattested `SUCCESS` (including a legacy M007 v1
   record) is `UNPROVEN`, never silently promoted. Non-`SUCCESS` statuses
   and process-failure precedence are unchanged: a non-zero exit / timeout
   stays `FAILED`/`CRASHED`, and attestation failure never downgrades it.
4. **Backward readability**: legacy M007 sub-run records load unchanged
   (the attestation keys are absent); they are readable but carry no
   verification claim. New records persist `attestation` + a stable
   `attestation_error` code. A persisted `COMPLETED` record that carries
   the new shape but lacks `VERIFIED` attestation is a contradiction and
   fails closed on read.
5. **Deterministic phases untouched**: `VALIDATE` and `CONSOLIDATE` keep
   bounded process-exit semantics and never set the contract environment
   variables.

## Consequences

- Model-heavy completion now requires a real wrapper run directory, the
  exact patch artifact, and repository HEAD agreement — a copied or
  fabricated semantic document cannot back a mission `COMPLETE`.
- The runner performs bounded, read-only I/O (file reads, one `git`
  probe) as part of verification; the pure shape contract remains free of
  I/O and clocks.
- Producers that cannot supply all identities self-gate to the legacy
  v1 shape, which is safe: it can never support `COMPLETED`.
- Legacy M007 missions remain reconstructable; they simply cannot be
  re-read as attested successes.

## Revision — dedicated launch-order anchor

Status: Accepted (2026-09-17)

The M008 decision is unchanged; this revision records a dogfood-driven
correction to rule 2's launch-ordering check.

The original check compared `meta.txt` against the sub-run's stdout/stderr
evidence file, which the runner creates at launch. Normal subprocess output
updates those files' mtimes *after* the wrapper has created `meta.txt`, so a
genuine fresh run could be rejected as `ATTESTATION_STALE`.

The runner now writes a dedicated, immutable per-sub-run launch marker
(`<subrun_id>.launch`) immediately before subprocess launch and compares
`meta.txt` against it. The marker is created exactly once, is never touched
by output capture, and is cleared and re-created fail-closed on re-launch.
No other identity in the attestation verification changes.
