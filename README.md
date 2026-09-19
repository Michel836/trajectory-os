# TrajectoryOS

**Adaptive AI Execution & Decision Intelligence**

> From intentions to outcomes.
> From tasks to trajectories.

TrajectoryOS is an experimental local-first platform designed to transform
complex goals and intentions into structured, executable trajectories and to
learn progressively from real execution.

## Current milestone

### V0 — Trajectory Mirror

The first milestone focuses on transforming unstructured information into a
structured portfolio of:

- goals;
- programs;
- projects;
- deliverables;
- tasks;
- ideas;
- decisions;
- research;
- waiting items;
- resources.

## Architecture philosophy

TrajectoryOS does not assume that an LLM should solve every problem.

The target architecture combines:

- LLMs for semantic understanding;
- graph algorithms for dependencies;
- operations research for constrained scheduling;
- machine learning for prediction;
- event data for continuous learning;
- human validation for consequential decisions.

## Current technology baseline

- Python 3.13
- uv
- Pydantic
- SQLAlchemy
- SQLite
- DuckDB
- NetworkX
- pytest
- Ruff
- mypy

## Development philosophy

> **Think V5. Build V0. Prove V0. Then earn V1.**

Complexity is introduced only when the previous layer has demonstrated value.

## Operator product layer (M017-M019)

One unified operator CLI projects and controls the canonical goal-execution
state. It never creates a second source of truth and never performs a Git
trust-boundary write:

    scripts/trajectory-pi-goal start  --spec <goal.json> [--repo DIR]
    scripts/trajectory-pi-goal resume <goal_id>
    scripts/trajectory-pi-goal stop   <goal_id> [--reason TEXT]
    scripts/trajectory-pi-goal status|inspect <goal_id> [--json]
    scripts/trajectory-pi-goal explain <goal_id> [node_id]
    scripts/trajectory-pi-goal dashboard|evidence|proof <goal_id> [--json]
    scripts/trajectory-pi-goal tui <goal_id>
    scripts/trajectory-pi-goal list

`start` validates the goal spec, provisions the canonical bounded missions it
references, creates the goal graph, and drives the production mission path end
to end (dependency readiness -> scheduling -> dispatch -> evidence -> derived
goal proof). `stop` is a safe request only: an in-flight bounded sub-run is
never killed, and no new work is launched after the current step returns.
`tui` renders the same canonical snapshot as a live one-screen dashboard with
no third-party TUI dependency.

## Multi-goal portfolio, daemon and backends (M020-M022)

The same CLI now coordinates multiple goals and a restart-safe daemon without
introducing a second source of truth and without any Git trust-boundary write:

    scripts/trajectory-pi-goal portfolio [--json]
    scripts/trajectory-pi-goal portfolio-run [--goal ID ...] [--policy FILE]
    scripts/trajectory-pi-goal daemon [--json]
    scripts/trajectory-pi-goal daemon-start  [--goal ID ...] [--max-cycles N]
    scripts/trajectory-pi-goal daemon-stop   [--reason TEXT]
    scripts/trajectory-pi-goal daemon-resume [--goal ID ...] [--max-cycles N]

* `portfolio-run` arbitrates one explicit portfolio capacity across isolated
  member goals and advances only the selected goals through the existing
  production path; each goal keeps its own graph generation, mission state,
  scheduler decisions and independent completion proof.
* `daemon-start`/`daemon-resume` run a bounded, restart-safe loop that
  persists authoritative cycle accounting, reconstructs exact state after a
  process interruption, and stops safely between cycles when requested.
* The provider-neutral agent backends expose exact provider/model route
  identity, capability discovery, bounded retries, cancellation and
  normalized lifecycle evidence; Pi remains the proven fallback.

See `docs/adr/ADR-017-multi-goal-portfolio-persistent-daemon-backends.md`.

## Isolated harness, local resources and artifact provenance (M023-M025)

The production surface now also isolates the developer-preview DeepSeek
Harness SDK, arbitrates the real local machine, and gives every goal durable
workspaces and auditable artifacts:

    scripts/trajectory-pi-goal resource-status [--policy FILE] [--json]
    scripts/trajectory-pi-goal artifacts <goal_id> [--lineage ID] [--json]
    scripts/trajectory-agent qualify-isolated [--allow-runtime] [--json]

* `qualify-isolated` discovers and probes the official SDK in its own virtual
  environment through a bounded `python -I` subprocess (the project
  interpreter never receives its `PYTHONPATH`) and records structured
  SDK/runtime identity, handshake, lifecycle, completion/error, timeout and
  cancellation evidence with a deterministic Pi fallback. No success is
  invented when the provider is unavailable or incompatible.
* `resource-status` is a read-only live view of discovered CPU/RAM/NVIDIA
  capacity, reviewer VRAM/CPU reservations, persisted active reservations and
  bounded concurrency. Remote inference is distinguished from local GPU use,
  and the local reviewer's reserved pool is isolated from agent/job
  workloads.
* `artifacts` exposes per-goal/per-mission workspaces and content-addressed
  artifact provenance/lineage. Artifacts are namespaced by goal, so implicit
  cross-goal leakage is rejected; provenance references the M014 reuse input
  and M016 goal-proof/criterion identities.

See `docs/adr/ADR-018-isolated-harness-local-resources-artifact-provenance.md`.

## Events, web dashboard and LifeOS (M026-M028)

Program block D adds one authoritative event stream, a local/private web
projection and explicit LifeOS adapters — all additive projections over the
same canonical state, and none of them a Git trust-boundary write:

    scripts/trajectory-pi-goal events <goal_id> [--refresh] [--json]
    scripts/trajectory-pi-goal web [--goal ID] [--port N] [--token T]
    scripts/trajectory-pi-goal lifeos [--json]
    scripts/trajectory-pi-goal lifeos-export <goal_id> --config FILE [--json]
    scripts/trajectory-agent usage --root DIR [--json]
    scripts/trajectory-pi-missions review-protocol RESPONSE.txt [--json]

* `events` is a durable, restart-safe, bounded and deduplicated projection
derived only from authoritative stores (goals, missions, blockers,
scheduler/resource state, replans, agents/models, artifacts, human gates and
the final proof). `--refresh` re-derives and persists it.
* `web` serves a loopback-only dashboard over the snapshot/event/portfolio/
artifact projections, with a closed set of safe controls (stop, clear-stop,
refresh-events, daemon-stop) and an optional bearer token. It never performs
a Git write.
* `lifeos-export` hands a scoped, provenanced, idempotent projection to
explicitly configured Obsidian, Super Productivity or JSON-manifest adapters;
adapter failures are isolated and cannot corrupt canonical state.
* `usage` exposes provider-grounded token/context telemetry. Metrics the
provider never exposes are stored as unknown, never estimated.
* `review-protocol` normalizes a reviewer response to `VALID_PASS`,
`VALID_REJECT` or `REVIEW_PROTOCOL_INVALID` without weakening fail-closed
behaviour.

See `docs/adr/ADR-019-authoritative-events-web-lifeos.md` and
`docs/development/LIFEOS_INTEGRATION.md`.

## Current status

TrajectoryOS is under active experimental development.

The current executable:

    uv run trajectory-os

Quality gate:

    uv run pytest
    uv run ruff check .
    uv run mypy src

No production-ready release is available yet.

## License

TrajectoryOS is licensed under the
[GNU Affero General Public License v3.0 only](LICENSE) (`AGPL-3.0-only`)
unless a separate written license agreement applies.

Commercial use is permitted under `AGPL-3.0-only` when its terms are followed.
Alternative proprietary or commercial licensing may also be available
separately from the copyright holder.

See [LICENSING.md](LICENSING.md) for the project licensing policy and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for third-party dependency
license information.
