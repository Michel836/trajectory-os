# ADR-023 — Mission operator control, deterministic recovery and production acceptance

Status: Accepted

Date: 2026-09-20

## Context

M031 (Issue #234) assembled the M023–M030 capabilities into one trust-gated
mission flow from objective intake to `READY_FOR_COMMIT` / `BLOCKED`. Issue
#236 (M032–M035) required turning that assembly into a *real* operator-ready
system:

* run one real non-fixture Trajectory_OS mission through the assembled path;
* survive real process death with an explicit, safe resume point;
* expose mission-scoped operator controls without conflating control and
  observation;
* prove the whole system with a deterministic production acceptance matrix.

The existing constraints remained binding: one canonical status truth
(`status.json`), no second lifecycle/readiness model, no autonomous Git
trust-boundary write, and provenance-preserving telemetry.

## Decision

Extend `trajectory_os.assembly` with three thin mission-level modules and a
small durable-document addition. None of them introduces a competing state,
backend, telemetry or trust model.

### 1. Real-task workloads are mission-scoped (M032)

A mission may embed an explicit `WorkloadSpec` (`MissionDefinition.workload`).
The embedded spec is validated verbatim and must match `workload_id`; a
mission without an embedded spec resolves through the canonical workload
registry. This lets a *real repository* objective (an isolated copy of the
Trajectory_OS source tree with an explicit deterministic validation command)
travel through the unchanged M030 run coordinator, without polluting the
canonical benchmark workload set with repository-specific fixtures.

### 2. Deterministic recovery and resume-point selection (M033)

`assembly.recovery` classifies the durable mission root into exactly one
recovery decision:

* `TERMINAL_COMPLETE` — a durable closure exists: idempotent, no re-execution;
* `EXECUTION_FINALIZED` — a durable `RUN_COMPLETED` exists: close only;
* `RESUME_EXECUTION` / `RESUME_PLAN` / `RESUME_PREFLIGHT` — explicit safe
  restart point derived from durable documents;
* an incomplete/stale `COMPLETE` status without durable `RUN_COMPLETED`
  evidence is detected and mapped to re-execution rather than trusted.

Recovery only reads canonical artifacts. A status document whose `run_id`
differs from the requested mission id fails closed (`IDENTITY_MISMATCH`). The
decision is persisted as an idempotent `recovery.json` checkpoint; an
identical decision never rewrites the file, so repeated resume is
byte-idempotent.

### 3. Mission-scoped operator control (M034)

`assembly.control` owns mission-level control, deliberately separate from
observation:

* control records are append-only in `control.jsonl` (the *control* state
  machine: what an operator asked for);
* `status.json` remains the canonical *observation* state
  (lifecycle/readiness);
* `request-stop` / `pause` write a durable, resumable graceful-stop latch
  that the orchestrator honours at the next safe phase boundary;
* `cancel` writes canonical `CANCELLED` (lifecycle `COMPLETE`) plus closure,
  and is idempotent;
* a terminal trust decision (`READY_FOR_COMMIT` / `BLOCKED` / `FAILED`) is
  never silently overridden by a late control;
* every control targets an explicit `mission_id`; unknown/stale ids fail
  closed; no control spawns a process or performs a Git trust-boundary write.

The mission CLI exposes `request-stop`, `pause`, `cancel`, `control-log`,
`recovery` and `acceptance`, while `status` / `follow` / `reconstruct` remain
strictly read-only and identity-checked.

### 4. Production acceptance (M035)

`assembly.acceptance` runs the fourteen-case release matrix deterministically
against the real orchestrator, recovery and control code with bounded fixture
executors and scripted reviewers. It emits a machine-readable report and a
compact human summary; the mission CLI exposes it as `acceptance`.

## Consequences

* The canonical M030 status contract remains the single status truth; control
  and recovery add durable *sibling* documents (`control.jsonl`,
  `stop-request.json`, `recovery.json`) rather than new status fields.
* A real repository mission can be audited with exact semantic patch identity
  and a fresh independent review on the exact final patch.
* Real process death leaves append-only evidence intact and resumes with the
  identical `mission_id == run_id` and an explicit resume point.
* The human `GO COMMIT` boundary is unchanged: automations stop at
  `READY_FOR_COMMIT` and never commit, push, merge, reset, restore, clean,
  stash, rebase or switch branches.
