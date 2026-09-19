# ADR-016 — Visible operator product layer (unified CLI, live TUI)

Status: Accepted

Date: 2026-09-18

## Context

Missions 008–016 delivered the trust and orchestration foundation: exact
execution attestation, fail-closed semantic promotion, a persistent goal
decomposition graph, a deterministic portfolio scheduler, explicit
cross-mission reuse, bounded adaptive replanning and a derived goal-level
proof. Those layers were mechanically correct but still required an operator
to understand and combine many separate surfaces (graph, scheduler, mission,
reuse, replan, proof) and, for live execution, to read raw
``.trajectory-pi`` run logs.

Issue #224 asks for the first operator-visible product layer: one bounded,
real strategic goal that can be launched through the production path and
watched end to end, without introducing a second source of truth.

## Decision

Add an additive product layer under ``trajectory_os.goals`` and a unified
operator CLI (``scripts/trajectory-pi-goal``). It is a **projection and
control** layer only.

### 1. One canonical snapshot

``goals.snapshot.build_snapshot`` composes the existing canonical stores into
one deterministic read-only document: goal identity, graph generation,
decomposition, dependency readiness, scheduling, the active mission with
agent/provider/model/locality and elapsed runtime, resource attribution
(CPU/GPU/VRAM), validation/review phase state, blockers, replans, evidence
and proof identity, and the current human gate. Status, inspect, dashboard,
evidence, proof and the live TUI are renderings of the same document.

### 2. Bounded end-to-end runner

``goals.runner.run_goal`` is a bounded composition loop over the existing
engines — it introduces no second execution engine and no second store:

1. provision the canonical missions the graph references (idempotent);
2. resolve the persisted cross-mission reuse projection;
3. run one portfolio scheduling cycle and dispatch admitted work through the
   production mission path (``MissionPathDispatcher`` → ``run_mission``);
4. continue bounded in-flight missions that hit a session bound;
5. derive and record the M016 goal proof;
6. repeat until COMPLETE, a stall, a safe stop, or the cycle bound.

A successful dispatch is never treated as proof: only authoritative mission
evidence advances the derived proof.

### 3. Safe stop is a request, never a kill

``goals.runner.request_stop`` writes a bounded local marker. An in-flight
sub-run is never terminated; the loop declines to launch new work after the
current bounded step returns. ``resume`` clears the marker and continues.

### 4. One pure live TUI

``goals.tui`` renders a single frame as a pure function of the snapshot and
refreshes with an ANSI clear + home redraw. No third-party TUI dependency is
introduced, so the frame is deterministic and directly testable without a
TTY.

### 5. No new trust boundary

No product-layer command performs a Git commit/push/merge/reset/restore/
clean/stash/rebase/switch/checkout. Existing M008–M016 semantics are
untouched: the layer is additive.

## Consequences

* A strategic goal is launchable and observable end to end through one
  coherent CLI, and restart/reconstruction reproduces the exact proof
  identity.
* Operators can see blockers, replans and the current human gate without
  reading raw runtime logs.
* Removing the layer leaves every canonical M008–M016 store and contract
  intact.

## Alternatives considered

* **Persist a mutable "goal run" state machine.** Rejected: it would become
  a second source of truth that can drift from the canonical stores.
* **Kill in-flight work on stop.** Rejected: irreversible and violates the
  bounded-safe-stop contract; an in-flight bounded sub-run is allowed to
  finish.
* **Adopt a third-party TUI framework.** Rejected: unnecessary dependency
  for a one-screen bounded dashboard; a pure renderer is sufficient and
  testable.
* **Infer running agent/model from process tables.** Rejected: the canonical
  sub-run/phase command and mission state are authoritative; inference would
  risk false attribution.
