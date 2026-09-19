# ADR-024 — Human-gated release bundle (M036–M039)

## Status

Accepted.

## Context

M031–M035 (Issue #234, #236) assembled the M023–M030 capabilities into one
trust-gated mission flow that stops at `READY_FOR_COMMIT`. Everything after
that point — committing the reviewed patch, pushing it, opening a pull
request, waiting for CI, merging and closing the release — was still
performed by a human entirely outside the system, with no durable,
reconstructable evidence binding the released artifact back to the exact
mission, review and semantic patch that produced it.

Issue #238 (M036–M039) required a *thin release/handoff layer* that makes that
final segment explicit, deterministic and auditable **without ever letting an
implementation, review or runtime-control component write to Git**.

The existing pieces already answer most of the hard questions and must be
reused unchanged:

* the M030 canonical `status.json` is the single lifecycle/readiness truth;
* the M031 `closure.json` carries `reviewed_patch` / `current_patch` and the
  durable mission identity (`mission_id == run_id`);
* review freshness is already a first-class fail-closed concept (M026/M028
  protocol, M031 human gate, M035 stale-review acceptance case).

## Decision

Introduce `trajectory_os.release`, a new operator-facing layer that consumes
canonical mission evidence and records a *release progression* (stages, not a
competing lifecycle). It reuses the M030 readiness distinction rather than
re-defining it.

### 1. Deterministic GO COMMIT handoff (M036)

`release.evidence` derives a fail-closed `ReviewGateEvidence` from
`status.json` + `closure.json` + `events.jsonl`, requiring:

* lifecycle `COMPLETE` and readiness `READY_FOR_COMMIT`;
* an **active** final independent reviewer;
* a **fresh** review — the latest patch-bearing event is a
  `REVIEW_COMPLETED` with `VALID_PASS` on the exact current patch;
* `reviewed_patch == current_patch`, both exact 64-hex SHA-256 identities.

`release.handoff.build_commit_handoff` then captures the exact branch/HEAD and
emits `commit-handoff.json` plus a compact human summary (expected branch,
baseline HEAD, exact patch SHA-256, proposed commit message, scope summary).
It performs **no** Git write.

`release.handoff.go_commit` is the only commit/push path. It rechecks the
readiness, branch, HEAD and semantic patch identity immediately before
staging/committing/pushing and fails closed on any drift. It requires an
explicit `GO_COMMIT` operator authorization
(`release.authorization.ReleaseAuthorization`); an absent token, an agent
actor or a mismatched action is refused before any Git process is spawned.

### 2. Exact-head PR binding and CI watch (M037)

`release.pull_request.bind_pull_request` creates **exactly one** pull request
(or discovers it) and persists its identity, base branch and exact head SHA;
more than one matching open PR is ambiguous and fails closed. `watch_ci`
queries CI **only** for that exact head SHA and distinguishes `queued`,
`in_progress`, `success`, `failure`, `cancelled` and `missing` (plus
`unknown`); branch-name-only CI is never trusted. Repeated watch/status with
the same inputs is byte-idempotent and read-only with respect to Git.

Both operations depend on a strict `GitHubAdapter` abstraction. The
`FixtureGitHubAdapter` proves the whole contract deterministically in tests
and CI with no credentials or network; `GhCliGitHubAdapter` provides the
optional, separate, read-only real-GitHub dogfood evidence.

### 3. Explicit GO MERGE gate (M038)

`release.merge_gate.build_merge_handoff` verifies PR-open, PR-mergeable,
current PR head == release commit SHA, and a **fresh** exact-head CI success
before persisting `merge-handoff.json`. `go_merge` rechecks all of that and
performs the merge **only** after an explicit `GO_MERGE` operator
authorization, passing the expected head SHA to the adapter (expected-head
protection). The default method is `squash`. There is no background watcher
and no auto-merge.

### 4. Release closure (M039)

`release.closure.build_release_closure` records `release-closure.json`,
linking objective, mission identity, reviewed/final semantic patch SHA-256,
commit SHA, remote branch, PR number, base SHA, exact PR head SHA, CI
workflow/run/status/conclusion, the GO COMMIT and GO MERGE gate evidence,
merge SHA, target-branch verification and the issue-closure relation.
`reconstruct_release` rebuilds the whole chain read-only from durable
artifacts; repeated reconstruction is byte-idempotent.

## Trust boundary

Only the release layer performs Git/GitHub writes, and only through its
adapters, and only with an explicit operator authorization. The deterministic
acceptance matrix and a repository scan prove that
`agents/`, `assembly/`, `benchmark/`, `missions/`, `observability/` and
`runtime_control.py` never issue a Git trust-boundary write and contain no
release-write path.

## Consequences

* The human `GO COMMIT` / `GO MERGE` gates remain authoritative and are now
  backed by an explicit, durable, reconstructable release record.
* The exact reviewed semantic patch SHA-256 is carried unchanged into the
  commit handoff, PR binding, merge handoff and release closure.
* A moved branch/HEAD, a changed patch, a stale review, a moved PR head, a
  missing/failed/pending exact-head CI run or an unauthorized actor all fail
  closed with a stable reason code.
* No competing lifecycle, readiness, trust or control model is introduced;
  release stages are derived pointers over the canonical M030 evidence.
* Release evidence lives in the mission root alongside the canonical
  artifacts; the M030 `events.jsonl` remains owned by the observability
  layer and release actions use an additive `release-events.jsonl`.
