# ADR-026 — Persistent autonomous operator platform (M048–M055)

## Status

Accepted.

## Context

M017–M047 delivered a coherent operator product that can carry one mission
from objective to release closure under explicit human gates. Issue #242
(M048–M055) requires turning that product into a *persistent local operator*
that manages several projects and missions, survives interruption and machine
restart, schedules work with persisted deterministic resource-aware decisions,
surfaces meaningful human gates, exposes one coherent local dashboard/API,
projects outcomes into LifeOS, and can be backed up, restored and reconstructed
— **without weakening the M017–M047 trust model**.

The existing architecture already provides the canonical mission/release
lifecycle, readiness, semantic patch identity, operator events, policy,
routing, human gates, observability, LifeOS adapters and resource
orchestration. M048–M055 must reuse them rather than compete with them.

## Decision

Add `trajectory_os.platform`, an additive composition layer *above* the
existing canonical state. Every new durable document lives under
`<mission_root>/platform/`; every fact about a mission, release, artifact or
patch is derived read-only from the canonical M030/M031/M036–M047 stores.

### 1. Project registry (M048)

`platform.projects` adds a durable `Project` identity above objectives and
missions with a stable content-derived `project_id`, explicit workspace,
repository, default policy profile, routing preferences and an
`ACTIVE`/`ARCHIVED` lifecycle. The environment is never consulted: an
environment-derived mutation fails closed. The project registry stores only
project metadata and explicit mission linkage; canonical mission/release
artifacts remain authoritative and are always derived read-only.

### 2. Persistent supervisor (M049)

`platform.supervisor` adds a terminal-independent, single-owner, restart-safe
supervisor. Ownership is an atomic `O_EXCL` lock carrying an explicit process
identity; a duplicate live owner fails closed; a dead owner is detected as a
crash and may be recovered explicitly. The supervisor persists only runtime
accounting and a heartbeat, reconstructs from canonical project/mission state,
honours an explicit stop request between cycles, and generates a systemd
user-service unit. It never performs a release Git write.

### 3. Multi-mission queue and scheduler (M050)

`platform.queue` adds a durable admission queue with explicit priority,
dependencies, bounded concurrency, local GPU/VRAM and remote-provider
constraints. Every persisted `SchedulingDecision` carries a deterministic
reason and a content-derived decision id; starvation resistance is
deterministic waiting-time aging. A mission already terminal in canonical
state is never re-executed after restart. The scheduler only selects work and
delegates dispatch to the existing operator control plane.

### 4. Notifications and human-gate inbox (M051)

`platform.inbox` adds a durable attention inbox with deterministic notification
identity (no duplicate storms after recovery), explicit
`UNREAD`/`ACKNOWLEDGED`/`RESOLVED` state and an optional `notify-send` sink
that reports explicit `UNAVAILABLE` when absent. The inbox never authorizes a
human gate: `GO COMMIT` and `GO MERGE` remain separate explicit actions.

### 5. Local API and web dashboard (M052)

`platform.api` builds one canonical read-only projection and serves it over a
localhost-only HTTP surface with a closed set of explicit mutations protected
by a session token *and* a CSRF token. The web layer duplicates no business
logic. Contradictory canonical state fails closed.

### 6. LifeOS projection (M053)

`platform.lifeos` projects projects, mission journals and provenance into a
configured Obsidian vault as an outbound sink. Notes are deterministic and
idempotent, contain only allow-listed canonical fields (so secrets cannot
leak), never become canonical runtime state, and degrade explicitly when the
vault is unavailable without stopping unrelated execution.

### 7. Evidence-based routing (M054)

`platform.efficiency` aggregates existing M029/M030 trial telemetry into
per-route evidence where every metric carries `PROVIDER`/`DERIVED`/`LOCAL`/
`UNAVAILABLE` provenance; an unavailable metric is `None` plus a reason. A
recommendation is only made when comparable evidence exists and is persisted
separately from policy authorization (`ADVISORY_ONLY`,
`policy_mutated == False`). The final reviewer remains
`qwen3.8:27b-q4_K_M`; the Harness remains developer-preview unless a real
handshake is proven.

### 8. Production hardening and disaster recovery (M055)

`platform.hardening` adds deterministic versioned configuration with secrets
kept separate, bootstrap/preflight, systemd generation, durable-state backup
that excludes transient PID/lock/heartbeat state, deterministic restore into a
clean fixture, stale-lock and partial-corruption detection, schema migration
verification, full reconstruction of projects/missions/releases/scheduler/
inbox/routing/LifeOS and a disaster-recovery drill.

### 9. Trust boundary (unchanged)

Implementation, review, supervisor, scheduler and observation layers never
perform a Git release write. Only the existing release layer may cross a
commit/push/merge boundary and only behind an explicit human `GO COMMIT` /
`GO MERGE` authorization, an exact reviewed semantic patch identity, a fresh
final review and exact-head CI. The M048–M055 acceptance matrix asserts this.

## Consequences

* One `scripts/trajectory` surface now also exposes `project`, `daemon`,
  `queue`, `inbox`, `api`, `lifeos`, `routing-evidence`, `bootstrap`,
  `systemd-unit`, `backup`, `restore`, `dr`, `corruption`, `migrate`,
  `platform-acceptance` and `platform-dogfood`.
* The new documents are additive; no canonical artifact changes shape.
* Durable M048–M055 evidence lives under `docs/missions/m048-m055/`.
* A future program can run Trajectory_OS as a persistent local operator that
  manages multiple projects without weakening the release trust model.
