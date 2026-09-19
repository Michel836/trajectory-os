# ADR-017 — Multi-goal portfolio, persistent daemon and production agent backends

Status: Accepted

Date: 2026-09-18

## Context

Block A (Missions 017–019, ADR-016) delivered one operator-visible goal
product layer: a bounded end-to-end goal runner, a unified CLI and a live
TUI, all derived from the canonical M012–M016 stores. Program block B
(Missions 020–022, Issue #225) requires the next production step:

* run and inspect **multiple simultaneous strategic goals** under bounded
  scheduling without any state/evidence/proof leakage between them;
* provide a **restart-safe long-running runtime** that persists authoritative
  state, resumes after interruption, exposes safe stop/resume and remains
  observable through the M017–M019 CLI/TUI;
* harden the **provider-neutral agent backend** abstraction for production
  (lifecycle normalization, bounded retries, cancellation, timeout,
  capability discovery, exact provider/model identity, structured evidence,
  deterministic fail-closed unavailable/incompatible states).

The existing M013 scheduler already arbitrates resources for **one** goal.
Adding multi-goal coordination must not create a second graph, scheduler,
mission engine, evidence store or completion proof.

## Decision

### 1. M020 — a portfolio coordinator over the per-goal canonical stores

Add ``trajectory_os.portfolio``. It is a deterministic **coordination** layer:

* it reads each member goal's canonical graph/readiness/scheduler/proof state
  and computes one portfolio decision that records, per goal, the exact
  ``goal_id``/``graph_id``/``generation_id`` and the scheduler's own freshly
  computed admission preview;
* it selects goals under one explicit ``PortfolioPolicy`` (aggregate
  CPU/GPU/VRAM demand, total node concurrency and a maximum number of active
  goals), with stable ordering ``(priority DESC, goal_id ASC)`` and closed
  machine-readable exclusion reasons;
* it supports explicit cross-goal dependencies (``goal -> prerequisites``)
  with fail-closed validation of unknown edges and cycles;
* a selected goal is advanced through the existing production goal path
  (:mod:`trajectory_os.goals.runner` → mission orchestrator + scheduler), so
  its graph generation, mission state, scheduler decisions and independent
  proof remain canonical;
* isolation is structural and executable: per-goal state stays under its own
  goal root, and ``assert_isolation`` rejects any decision that collapses two
  goals into one identity or reuses one scheduler decision for two goals.

The portfolio persists only its own decision history and event log under
``<root>/portfolio/``; it is never the source of truth for member goals.

### 2. M021 — a bounded persistent daemon

Add ``trajectory_os.daemon``. It is a **bounded composition loop** over the
portfolio engine:

* it owns no authoritative work state; every cycle is delegated to the
  canonical portfolio/goal path, and the daemon persists only cycle
  accounting and the exact portfolio decision identities already persisted;
* ``DaemonState`` + an append-only cycle log make a restarted process resume
  exactly (``reconstruct`` strictly validates that the cycle log, the
  cumulative cycle count and the decision identities agree);
* safe stop is a bounded request file checked **between** cycles; an
  in-flight bounded cycle is never interrupted, so no mission lifecycle
  evidence is lost;
* no daemon command performs a Git trust-boundary write.

### 3. M022 — production agent-backend hardening (additive)

Extend ``trajectory_os.agents`` with provider-neutral, additive modules:

* ``cancellation.CancellationToken`` — one thread-safe flag implementing the
  existing opaque ``cancel`` contract;
* ``route.ProviderRoute`` — the exact ``(backend, provider, model,
  transport)`` tuple with a domain-separated ``route_id``; default routes are
  documented per backend and never silently rewrite provider model names;
* ``capabilities.CapabilityReport`` — deterministic capability discovery
  derived only from the existing probe (never launches a model), with explicit
  unavailable/unknown results;
* ``retry.RetryPolicy`` / ``run_with_retries`` — bounded, fail-closed retry
  composition that never retries cancellation/unavailable/incompatible states
  and never rewrites a failed result as success;
* ``lifecycle.LifecycleSummary`` — one normalized lifecycle vocabulary over
  the existing ``AgentEvent`` stream.

Existing M015 identity semantics (``probe_id``/``run_id``/``evidence_id``/
``canary_id``) are unchanged; the new domains are additive. Pi remains the
proven fallback; DeepSeek Harness remains the developer-preview primary.

### 4. Observability

The M017–M019 snapshot gains additive ``portfolio`` and ``daemon`` sections
(``present: false`` when absent), the TUI renders them when present, and the
unified CLI exposes ``portfolio``/``portfolio-run``/``daemon``/``daemon-*``
subcommands. M008–M019 behavior is unchanged.

## Consequences

* Multiple strategic goals can run concurrently with per-goal isolation and
  one deterministic portfolio arbitration, while each still produces its own
  independent completion proof.
* Long sessions survive process interruption: the daemon reconstructs exact
  state and resumes; safe stop never loses in-flight evidence.
* Agent backends expose machine-readable lifecycle, capability, route and
  retry evidence, and fail closed on unavailable/incompatible states.
* Removing the new package leaves every canonical M008–M019 store and
  contract intact.

## Alternatives considered

* **Persist a mutable multi-goal orchestration state machine.** Rejected: it
  would become a second source of truth that can drift from canonical
  per-goal stores; the coordinator is deliberately read-only over member
  goals.
* **Merge all goals into one scheduling graph.** Rejected: it destroys
  per-goal identity, evidence and proof isolation.
* **An in-process background thread pool as the daemon.** Rejected:
  irreproducible, unobservable and unsafe under restart; a bounded,
  restart-safe, persisted loop is deterministic and testable.
* **Add retry/cancellation/capability state to the existing M015 result
  identity.** Rejected: it would change proven M015 run identities. The new
  evidence is layered additively instead.

## References

* ADR-010 (goal decomposition graph), ADR-011 (portfolio scheduler),
  ADR-014 (bounded agent backends), ADR-015 (derived goal proof),
  ADR-016 (visible operator product layer).
* Issue #225 — Program block B (M020–M022).
