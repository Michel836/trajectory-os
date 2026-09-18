# ADR-009 — Reduced-intervention execution and explicit human gates

Status: Accepted

Date: 2026-09-18

## Context

Missions 003–010 built a bounded, resumable, fail-closed multi-phase
orchestrator. The internal loop already transitions
`PLAN -> IMPLEMENT -> VALIDATE -> REVIEW -> CONSOLIDATE` autonomously and
already runs the bounded `REPAIR -> re-validate -> re-review` loop on
non-green evidence. Every internal transition is deterministic, persisted
and reconstructible; no process exit code alone proves success.

What the system did **not** make explicit was the *human* side of the
contract. After the autonomous lifecycle reached `COMPLETE`, the operator
had to infer from the mission state that a commit decision was pending;
there was no persisted representation of "the exact human trust boundary
we are stopped at", no way to record the human GO COMMIT / GO MERGE
decisions as durable evidence, and no operator projection tying the gate
to the supporting evidence and remaining autonomous budget. Interviewed
against Issue #213, the required trust-boundary model is:

```
LAUNCH -> (autonomous execution) -> GO COMMIT -> GO MERGE
```

Only those three points should require a human; every ordinary internal
transition is machine-internal.

## Decision

Introduce a **pure, derived operator gate projection** and two explicit
human approval markers. No new mission state, no new phase kind, no
schema-version move, and no Git write by the system.

### 1. Gate vocabulary

A single pure module (`trajectory_os.missions.gate`) derives exactly one
gate from canonical persisted state:

| gate | meaning | human action |
| --- | --- | --- |
| `LAUNCH` | created, not yet launched | launch the mission |
| `AUTONOMOUS` | launched and progressing inside the bounded loop | none |
| `GO_COMMIT` | autonomous lifecycle green (`COMPLETE`) | review + commit |
| `GO_MERGE` | human commit recorded | push / PR / CI / merge |
| `STOP` | bounded fail-closed (`BLOCKED`/`FAILED`) | one consolidated decision |
| `DONE` | human merge recorded | none |

`HUMAN_GATES = {LAUNCH, GO_COMMIT, GO_MERGE, STOP}`; every other gate is
autonomous or closed. The derivation is:

1. `merge_approved_at` set -> `DONE`;
2. `commit_approved_at` set -> `GO_MERGE`;
3. `mission_state == COMPLETE` -> `GO_COMMIT`;
4. `mission_state in {BLOCKED, FAILED}` -> `STOP`;
5. `started_at is None` -> `LAUNCH`;
6. otherwise -> `AUTONOMOUS`.

An approval is checked before the state derivation so a recorded human
decision can never be silently re-offered.

### 2. Persisted approvals (additive, backward compatible)

Two optional, bounded fields are added to the mission document:

* `commit_approved_at` (`str | None`) and `commit_revision`
  (`str | None`);
* `merge_approved_at` (`str | None`).

They are absent on legacy evidence (readable as `None`; the gate then
derives from the mission state alone exactly as before). They record the
*human* decision only — the corresponding Git commit/push/merge is
performed externally by the human. `orchestrator.approve_commit` and
`orchestrator.approve_merge` fail closed unless the mission is at exactly
the matching gate, so an approval can never skip VALIDATE/REVIEW (the
mission must be `COMPLETE`) and can never be replayed.

### 3. Operator projection

`summary.mission_summary` gains an additive `operator_gate` block that
exposes, at minimum:

* `gate`, `human_action_required`, `reason`, `mission_reason`;
* `next_human_action` — the exact operator command, never a Git write;
* `autonomous_budget` — repairs used/remaining, sub-runs used/remaining,
  wall-clock budget and whether the loop is waiting for a human;
* `evidence` — phases passed/total, phase states, final review state,
  the M009 attestation counters and the M010 patch-identity domains;
* `repository` — repository root, cwd and the baseline revision.

The human-readable rendering (`render_summary`) prints the same gate,
budget and next action, so operator and machine output share one source
of truth. `reconstruct` includes the same gate in its report, so resume
identifies the exact current gate and replays no proven work.

### 4. Non-green behaviour (unchanged, now explicit)

A non-green deterministic result still routes autonomously into the
bounded repair loop when budget and policy permit, and otherwise stops
fail-closed at one consolidated `STOP` gate. No intermediate micro-gate
is introduced between internal stages.

## Consequences

- One operator `LAUNCH` (`start`/`run`) autonomously drives the bounded
  lifecycle to a single explicit `GO_COMMIT`; the operator then records
  the human commit decision and the system reports `GO_MERGE`.
- The current human gate is reconstructible from persisted evidence
  (`mission.json` state + approvals), never guessed and never replayed.
- All M008/M009/M010 fail-closed guarantees are untouched: the gate layer
  adds no promotion path, performs no Git write, and the approval commands
  cannot advance a non-green mission. Process exit codes still never prove
  success; exact attestation, readiness gating, exact-patch-bound
  read-only review, independent patch-identity domains, bounded retries
  and durable evidence all remain mandatory.
- Legacy mission documents load unchanged and derive the same gates they
  implicitly had; no migration is required.
- `approve-commit` / `approve-merge` extend the operator CLI; `start`,
  `run`, `resume`, `reconstruct`, `status` and `summary` gain additive
  gate output only.
