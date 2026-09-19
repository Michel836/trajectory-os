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

## Pi vs DeepSeek Harness benchmark (M029)

M029 adds a decision-grade, repeatable A/B benchmark between the proven
`Pi -> DeepSeek Flash` runtime and a qualified
`DeepSeek Harness -> DeepSeek Flash` runtime:

    scripts/trajectory-benchmark run --mode live --backend both --repetitions 2
    scripts/trajectory-benchmark follow --run-id RUN_ID
    scripts/trajectory-benchmark status --run-id RUN_ID
    scripts/trajectory-benchmark report --run-id RUN_ID
    scripts/trajectory-benchmark reconstruct --run-id RUN_ID

* the same five canonical workloads, fresh isolated workspace per trial, and
  the same validation + strict-review trust gates for both backends;
* authoritative per-request/per-phase/per-trial/aggregate telemetry with an
  explicit provenance (`PROVIDER` / `DERIVED` / `LOCAL` / `UNAVAILABLE`) for
  every metric — an unavailable metric is recorded as `null` with a reason,
  never guessed;
* an exact Git-free semantic patch identity, and failed/blocked/unavailable
  trials preserved as evidence;
* a machine-readable artifact suite (`manifest.json`, `state.json`,
  `events.jsonl`, `trials/*.json`, `summary.json`, `report.md`) plus a human
  report that never auto-promotes a backend;
* Harness qualification that fails closed when its runtime identity or
  handshake cannot be proven; no successful Harness measurement is fabricated;
* no Git trust-boundary write.

See `docs/adr/ADR-020-pi-vs-harness-benchmark.md` and
`docs/development/BENCHMARK.md`.

## Live run observability and telemetry (M030)

M030 converges every runtime adapter onto one canonical, backend-neutral
observability contract shared by the CLI, TUI and Web surfaces:

    runtime adapters -> events.jsonl -> status.json -> CLI / TUI / Web

    scripts/trajectory-observability run --run-id RUN_ID --reviewer pass
    scripts/trajectory-observability status --run-id RUN_ID
    scripts/trajectory-observability follow --run-id RUN_ID
    scripts/trajectory-observability telemetry --run-id RUN_ID
    scripts/trajectory-observability preflight --provider ollama --model deepseek-flash
    scripts/trajectory-pi-status --run RUN_ID --follow

* the canonical status persists `run_id`, lifecycle `state`/`stage`/`phase`/
  `attempt`, backend/provider/model, the separate inline/final reviewer
  roles, `previous_gate`/`previous_result`, `reviewed_patch`/`current_patch`,
  `next_action`, heartbeat/last-event timestamps and an explicit readiness;
* lifecycle completion and trust readiness are independent — a lifecycle
  `COMPLETE` state never implies `READY_FOR_COMMIT`;
* reviewer identity is role-explicit: an inactive reviewer is never displayed
  with a phantom/default model (`--no-review` shows no reviewer);
* telemetry modes `off` / `standard` / `benchmark` bound overhead; every
  metric carries provenance and an unavailable metric is `null` + a stable
  reason, never estimated;
* deterministic derived metrics include tokens/cost/seconds per successful
  task, cache-hit ratio, repair/review-reject/protocol-error rates, wasted
  failed-cycle tokens/cost/time and time-to-first-useful-patch;
* fail-fast preflight rejects knowable errors (including invalid provider/
  model combinations) before validation/review/repair;
* read-only follow heartbeat (default 12 s) exits at every terminal/
  readiness outcome with an explicit `RUN TERMINÉ` banner, with optional
  `notify-send` that is never a correctness dependency;
* no Git trust-boundary write and no local polling loop in the reader.

See `docs/adr/ADR-021-live-run-observability-telemetry.md`.

## End-to-end mission assembly (M031)

M031 assembles the M023–M030 capabilities into one trust-gated operator flow
from a single objective to `READY_FOR_COMMIT` / `BLOCKED` with durable
closure evidence:

    Mission -> Preflight -> Plan -> Execution -> Validation
            -> Review -> Repair -> Human Gate -> Closure

    scripts/trajectory-mission start --objective "..." --workload small-targeted-repair --reviewer reject-then-pass
    scripts/trajectory-mission status --mission-id MISSION_ID
    scripts/trajectory-mission follow --mission-id MISSION_ID
    scripts/trajectory-mission resume --mission-id MISSION_ID
    scripts/trajectory-mission reconstruct --mission-id MISSION_ID
    scripts/trajectory-mission closure --mission-id MISSION_ID

* one mission root converges on `mission.json`, `plan.json`, `events.jsonl`,
  `status.json`, `telemetry.json`, `summary.json` and `closure.json`; the
  canonical M030 `status.json` remains the single status truth
  (`mission_id == run_id`);
* a durable mission identity (objective, constraints, definition of done,
  backend/provider/model intent, trust policy, starting baseline) survives
  every phase, including interruption/resume;
* execution, deterministic validation, exact patch identity, strict review,
  bounded repair, telemetry and projections are reused unchanged from
  M029/M030 — no second backend or status abstraction;
* the human gate stops at `READY_FOR_COMMIT`; a preflight reject stops before
  planning/execution, a validation reject can never advance, a stale review
  or inactive reviewer is downgraded to `BLOCKED`, and repair-budget
  exhaustion yields `BLOCKED`;
* `closure.json` plus `reconstruct` rebuild the mission without reading prose
  logs, and no command performs a Git trust-boundary write.

See `docs/adr/ADR-022-end-to-end-mission-assembly.md`.

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
