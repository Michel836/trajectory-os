# ADR-004 — Deterministic runtime control foundation for Pi runs

Status: Accepted

Date: 2026-09-11

## Context

The `trajectory-pi` producer records, per run, a deterministic evidence
directory (`<workspace>/.trajectory-pi/runs/<name>`) containing `meta.txt`
(run id, wrapper `pid`, agent `pi_pid`, workspace, start/end timestamps, exit
evidence) and a producer transcript (`status.log`). V1.67–V1.72 readers turn
that evidence into a read-only status view.

Operator demand (V1.73–V1.76) is the complementary control-plane question:

- *Can I safely target a recorded run?* — never by record alone, which can
  be stale or point at a reused PID (V1.73, V1.75);
- *Can I stop it gracefully?* — a bounded, provider-agnostic SIGTERM grace
  window without force-kill escalation (V1.74);
- *Is a finished run safe to resume from?* — a recovery-readiness view that
  only proposes, never acts (V1.76).

A naive control path (send a signal to a recorded PID) is unsafe: PID
recycling, long-dead runs and our own process tree make "recorded pid" an
identity claim that must be verified against live evidence before any action.

## Decision

The control-plane is a three-layer structure, all provider-agnostic,
local-only, stdlib-only, and deterministic:

1. **A shared core** (`src/trajectory_os/runtime_control.py`) that composes
   small testable parts:

   - *Safe targeting* — a recorded pid is targetable only when all hold:
     it was producer-recorded, the process is alive, its working directory
     matches the run's recorded workspace (realpath-compared), its start
     time falls within the run's recorded start window (PID-reuse
     detector), and it is not inside the evaluator's own process tree.
     Any unmet condition fails closed with a stable machine reason.
   - *Run classification* — the same observable contract as the V1.67
     reader (`running` / `stale` / `ended` / `unknown`), so control and
     status never disagree about a run.
   - *Recovery readiness* — a read-only decision over producer evidence
     (terminal state, identity consistency, schema-valid transcript tail,
     clean exit evidence) plus a deterministic *resume hint shape*.
   - *Control primitives* — a run-local lock file (conflict on live
     foreign holders, reclaim on dead holders, fail-closed on corrupt
     content), a structured per-run event stream, and a SIGTERM-only signal
     primitive. **No SIGKILL path exists in this layer.**

2. **A control CLI** (`scripts/trajectory-pi-control`) exposing `list`,
   `status` and `stop`. The `stop` command resolves a unique safe target
   (never guessing among candidates), acquires the run lock, signals the
   verified target PID, waits a bounded grace window (default 10 s,
   configurable), and reports `STOPPED` or `GRACE_TIMEOUT` with the full
   evidence. Graceous termination never escalates to force-kill.

3. **A read-only status surface** — the V1.67 reader additionally exposes
   the same per-run control view under a new `runtime_control` key, so an
   operator can reason about targetability and recovery readiness from the
   status surface without the control CLI. The reader never signs, mutates,
   or signals; only `stop` acts.

## Consequences

Positive:

- "stop a Pi run" becomes a deterministic, auditable decision with
  machine-checkable reasons, not a race on a recorded PID;
- PID reuse and stale-record attacks fail closed by construction;
- the same verdict logic powers both the CLI and the status reader, so
  there is one source of truth for "is this run safe to touch";
- the recovery surface is additive and read-only, keeping the control
  plane reversible until V1.77 implements the actual resume action.

Negative:

- control is Linux/`/proc`-bound (start-time and cwd identity are
  `/proc` facts); on other platforms the target simply fails closed,
  which is the desired safe behavior;
- a run whose workspace or start time was not recorded (pre-V1.70
  producers) is never stoppable — an explicit, visible limitation rather
  than a silent risk;
- graceful stop can time out; operators must then decide how to proceed
  (the surface refuses to escalate on their behalf).

Out of scope (deferred), explicitly:

- executing a resume action (V1.77+);
- remote control, daemons, or provider-side cancellation;
- automatic escalation beyond SIGTERM.
