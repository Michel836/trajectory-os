# ADR-019 — Authoritative events, local web dashboard and LifeOS adapters

Status: Accepted

Date: 2026-09-19

## Context

Program blocks A (M017–M019, ADR-016), B (M020–M022, ADR-017) and C
(M023–M025, ADR-018) delivered the visible operator product layer, the
multi-goal portfolio, the persistent daemon, hardened provider-neutral agent
backends, isolated DeepSeek Harness qualification, operational local resource
arbitration and persistent artifact provenance. Program block D (Missions
026–028, Issue #227) addresses the last three production gaps:

1. **Events and notifications.** Authoritative state is persisted in many
   stores (graph, missions, scheduler, replans, artifacts, proof, gates), but
   there is no single durable, restart-safe, deduplicated projection an
   operator or a downstream consumer can subscribe to.
2. **A local/private web dashboard.** The CLI and TUI are the only operator
   surfaces; there is no browser dashboard that projects the same canonical
   state and exposes the permitted safe controls.
3. **LifeOS integration.** The platform has no explicit, disableable,
   failure-isolated way to hand scoped events/artifacts to external life
   management tools such as Obsidian or Super Productivity.

Two cross-cutting requirements also apply: the reviewer protocol must expose
its semantic distinction (`VALID_PASS` / `VALID_REJECT` /
`REVIEW_PROTOCOL_INVALID`) without weakening fail-closed behaviour, and
provider-grounded token/context telemetry must be persisted where the provider
actually exposes it.

## Decision

### 1. M026 — authoritative event projections (`trajectory_os.events`)

* One :class:`EventRecord` is a bounded, content-addressed statement about
  persisted state. Its identity excludes the observation clock, so
  re-deriving the same canonical state always yields the same `event_id`.
* Events are derived **only** from authoritative stores: the goal graph, the
  mission/phase/sub-run documents, scheduler/resource decisions, replan
  events, artifacts, human gates, the derived goal proof and the safe-stop
  marker. Model prose is never a source of events.
* Categories are a closed set (GOAL, MISSION, BLOCKER, SCHEDULER, RESOURCE,
  REPLAN, AGENT, ARTIFACT, GATE, PROOF); kinds map 1:1 to a category.
* `store.refresh` writes the bounded (≤ 1024), deduplicated projection
  atomically; `store.reconstruct` strictly validates that the projection
  header, log and `projection_id` agree. A restart re-derives the same
  projection (idempotent refresh).
* `trajectory-pi-goal events <goal_id> [--refresh]` exposes it.

### 2. M027 — local/private web dashboard (`trajectory_os.web`)

* The dashboard is a **projection and bounded control** surface. GET routes
  render the M017–M019 snapshot, the M026 event projection, the M020
  portfolio and the M025 artifacts; nothing is recomputed into a parallel
  store.
* It is standard-library only and binds a **loopback** address only; a
  non-loopback `Host` header is rejected (403), an optional bearer token
  (`TRAJECTORY_WEB_TOKEN`) is enforced, and request bodies are bounded.
* The only permitted controls are safe stop, clear stop, refresh events and
  daemon safe stop. No control performs a Git trust-boundary write.
* `trajectory-pi-goal web [--goal ID] [--port N]` serves it.

### 3. M028 — explicit LifeOS adapters (`trajectory_os.lifeos`)

* Adapters are **explicit and disableable**; three ship in V0: Obsidian
  (Markdown note), Super Productivity (import JSON) and a generic JSON
  manifest. The adapter kind set is closed, with a documented extension point
  for future LifeOS consumers.
* The exchange is **scoped** (bounded event categories and artifact
  references for the owning goal only), **provenanced** (exchange/event/
  artifact ids, adapter kind, output digests and status) and **idempotent**
  (the ledger is keyed by `(adapter, exchange_id)`).
* **Failure isolation is structural**: adapters receive an immutable payload,
  write only to an explicitly configured target, and a target inside the
  canonical state root is refused. Adapter exceptions are captured as
  `FAILED` records and never propagate; canonical state is fingerprinted
  before and after and must be unchanged.
* `trajectory-pi-goal lifeos` and `lifeos-export <goal_id> --config FILE`
  expose the ledger and run an exchange.

### 4. Review-protocol semantic distinction (`missions.review_protocol`)

* A pure, deterministic classifier normalizes any reviewer response into
  `VALID_PASS` (PASS + GO COMMIT + zero blocking findings; non-blocking
  `MINORS` permitted), `VALID_REJECT` (REJECT/FAIL + REPAIR/FIX + at least
  one concrete blocker/major) or `REVIEW_PROTOCOL_INVALID` (everything else,
  including contradictions).
* The shipped `trajectory-pi` parser is unchanged: existing `PASS`/`FAIL`/
  `REJECTED` output keeps its exact fail-closed meaning, and the normalized
  classifier is bound to it by regression tests. A FAIL with no blocking
  finding is invalid under the strict distinction and can never be promoted
  to a pass. The reviewer model remains configurable (`--review-model`).

### 5. Provider-grounded telemetry (`agents.telemetry`)

* Every metric is labelled `PROVIDER`, `DERIVED`, `LOCAL` or `UNAVAILABLE`.
  Tokens/cost are read only from explicit provider keys; `total` may be
  derived from prompt+completion; runtime is local. A metric the provider
  never exposes is stored as `None` and is never estimated.
* The bounded ledger (`<root>/telemetry/usage.jsonl`) is idempotent and
  exposed through `trajectory-agent usage --root DIR` and
  `GET /api/telemetry`.

## Consequences

* Operators and future consumers get one durable, restart-safe event stream
  and a browser dashboard over the same canonical state.
* External tools receive scoped, provenanced, idempotent handoffs that cannot
  corrupt canonical state even when an adapter fails.
* Token/context telemetry is honest: unknown metrics stay unknown.
* All new packages are additive; M008–M025 stores, contracts and behaviour
  are preserved. No module performs a Git trust-boundary write.

## Alternatives considered

* **A unified mutable event log written by every engine.** Rejected: it would
  become a competing source of truth; the projection is derived and
  reconstructible instead.
* **A third-party web framework / async server.** Rejected: unnecessary
  dependency and attack surface for a loopback, read-mostly dashboard.
* **Adapters that can write anywhere.** Rejected: an adapter bug could then
  corrupt canonical state; targets are explicitly configured and structurally
  excluded from the canonical root.
* **Estimating unavailable provider tokens/cost.** Rejected: invents data and
  violates the provenance principle.

## References

* ADR-016 (visible operator product layer), ADR-017 (multi-goal portfolio,
  daemon, backends), ADR-018 (isolated harness, local resources, artifacts).
* Issue #227 — Program block D (M026–M028).
* `docs/development/LIFEOS_INTEGRATION.md`.
