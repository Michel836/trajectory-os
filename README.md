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

## Real operator bundle (M032–M035)

M032–M035 turn the assembled flow into a real, recoverable, controllable
operator system:

    scripts/trajectory-mission start --objective "..." --workload-file workload.json --mode LIVE
    scripts/trajectory-mission status --mission-id MISSION_ID
    scripts/trajectory-mission request-stop --mission-id MISSION_ID
    scripts/trajectory-mission pause --mission-id MISSION_ID
    scripts/trajectory-mission cancel --mission-id MISSION_ID
    scripts/trajectory-mission recovery --mission-id MISSION_ID
    scripts/trajectory-mission control-log --mission-id MISSION_ID
    scripts/trajectory-mission acceptance --out acceptance.json

* a real repository objective can be run as a mission-scoped `WorkloadSpec`
  through the unchanged production path (`pi` + `deepseek-flash`, fresh
  `qwen3.8:27b-q4_K_M` review), with exact semantic patch identity and a
  terminal `READY_FOR_COMMIT` or explicit fail-closed state;
* recovery is deterministic: real `SIGKILL`/`SIGTERM` leaves append-only
  evidence intact and resume keeps the identical `mission_id == run_id` with
  an explicit safe resume point; repeated resume after terminal is
  byte-idempotent;
* mission-level control is separate from observation: `request-stop` / `pause`
  are durable graceful latches, `cancel` produces canonical `CANCELLED` plus
  closure, stale/unknown mission ids fail closed, and no control performs a
  Git trust-boundary write;
* the M035 acceptance matrix (14 cases) is deterministic and machine-readable.

Durable evidence lives under `docs/missions/m032-m035/`; the selected real
objective, mission ids, patch identities, telemetry and terminal readiness are
recorded there. See
`docs/adr/ADR-023-mission-operator-control-recovery-acceptance.md`.

## Human-gated release bundle (M036-M039)

M036-M039 add the missing operator-authorized release segment after
`READY_FOR_COMMIT`, without ever letting implementation, review or
runtime-control components write to Git:

    scripts/trajectory-release handoff       --mission-id MISSION_ID
    scripts/trajectory-release go-commit     --mission-id MISSION_ID --authorize-commit TOKEN
    scripts/trajectory-release bind-pr       --mission-id MISSION_ID
    scripts/trajectory-release watch-ci      --mission-id MISSION_ID
    scripts/trajectory-release merge-handoff --mission-id MISSION_ID
    scripts/trajectory-release go-merge      --mission-id MISSION_ID --authorize-merge TOKEN
    scripts/trajectory-release closure       --mission-id MISSION_ID --issue 238
    scripts/trajectory-release reconstruct   --mission-id MISSION_ID
    scripts/trajectory-release acceptance    --out acceptance.json

* `handoff` emits a deterministic GO COMMIT handoff bound to the exact
  reviewed semantic patch SHA-256 (branch, baseline HEAD, proposed commit
  message and scope summary); it performs no Git write;
* `go-commit` rechecks readiness/branch/HEAD/patch/review freshness
  immediately before staging/committing/pushing and requires an explicit
  operator authorization token; a moved branch/HEAD, a stale patch or an
  unauthorized actor fails closed;
* `bind-pr` creates or discovers exactly one pull request bound to the exact
  commit SHA; `watch-ci` queries CI ONLY for that exact head SHA and
  distinguishes queued / in_progress / success / failure / cancelled /
  missing (no branch-name-only trust); repeated status/watch are read-only
  and byte-idempotent;
* `merge-handoff` requires PR-open + mergeable + exact PR head + fresh
  exact-head CI success; `go-merge` rechecks and merges only after an explicit
  operator authorization, with expected-head protection and the default
  `squash` method (no background or auto-merge);
* `closure` records `release-closure.json`, linking objective, mission
  identity, reviewed/final patch SHA-256, commit SHA, remote branch, PR
  number, base SHA, exact PR head, CI workflow/run/status/conclusion, the GO
  COMMIT and GO MERGE gate evidence, merge SHA, target-branch verification
  and issue closure; `reconstruct` rebuilds the chain read-only and
  idempotently;
* the deterministic M036-M039 acceptance matrix (18 cases) proves every
  fail-closed gate with a real local Git repository and a fixture GitHub
  adapter; a separate read-only real-GitHub probe provides dogfood evidence
  without mutating anything remotely.

Durable evidence lives under `docs/missions/m036-m039/`. See
`docs/adr/ADR-024-human-gated-release-bundle.md`.

## Self-hosting operator platform (M040-M047)

M040-M047 consolidate M017-M039 into one operator product that can develop
and release TrajectoryOS itself, without introducing a competing lifecycle,
readiness, mission identity, release identity, observability or trust model
(`mission_id == run_id` remains authoritative).

One unified entrypoint supersedes the ad-hoc invocations while every prior
script remains a compatible thin entrypoint:

    scripts/trajectory start     --objective "..." --workspace DIR --profile release
    scripts/trajectory status    --mission-id MISSION_ID --json
    scripts/trajectory dashboard --mission-id MISSION_ID
    scripts/trajectory follow    --mission-id MISSION_ID --iterations 3
    scripts/trajectory pause     --mission-id MISSION_ID
    scripts/trajectory request-stop --mission-id MISSION_ID
    scripts/trajectory cancel    --mission-id MISSION_ID
    scripts/trajectory resume    --mission-id MISSION_ID
    scripts/trajectory recover   --mission-id MISSION_ID
    scripts/trajectory handoff   --mission-id MISSION_ID
    scripts/trajectory go-commit --mission-id MISSION_ID --authorize-commit TOKEN
    scripts/trajectory bind-pr   --mission-id MISSION_ID
    scripts/trajectory pr-status --mission-id MISSION_ID
    scripts/trajectory watch-ci  --mission-id MISSION_ID
    scripts/trajectory merge-handoff --mission-id MISSION_ID
    scripts/trajectory go-merge  --mission-id MISSION_ID --authorize-merge TOKEN
    scripts/trajectory closure   --mission-id MISSION_ID --issue 240
    scripts/trajectory reconstruct --mission-id MISSION_ID
    scripts/trajectory dogfood   --json
    scripts/trajectory acceptance --out acceptance.json

* **M040** true self-release dogfood runs the production path with the
  deterministic fixtures for the full chain and the current implementation
  over the real repository up to (but never through) the human GO COMMIT
  gate; fixture proof and live dogfood evidence are separated;
* **M041** one control plane reuses assembly, observability, runtime control
  and release; observation is strictly read-only and mutations are explicit;
* **M042** full-lifecycle recovery discovers already-completed
  commit/push/PR/merge/closure instead of repeating them, fails closed on any
  local/remote contradiction and is idempotent;
* **M043** one additive, append-only, replayable event envelope spans mission
  and release without replacing `status.json` / `closure.json` / release
  artifacts;
* **M044** a small deterministic policy layer with `safe`, `fast-local`,
  `benchmark` and `release` profiles; the release profile always retains both
  human gates and exact-head CI, resolution is persisted and deterministic and
  the environment can never mutate policy;
* **M045** consolidated routing persists the exact implementation, inline
  reviewer and final reviewer identities (final reviewer remains
  `qwen3.8:27b-q4_K_M`), fails closed on an unavailable backend and records a
  deterministic fallback reason when one is used;
* **M046** a read-only one-screen operator state projection derived from the
  canonical artifacts;
* **M047** a deterministic 35-case product acceptance matrix proves the full
  happy path, trust boundaries, crash/recovery idempotence, exact-head CI,
  human merge authorization, event replay, semantic identity and that no
  success is inferred from prose.

Durable evidence lives under `docs/missions/m040-m047/`. See
`docs/adr/ADR-025-self-hosting-operator-platform.md`.

## Persistent autonomous operator platform (M048-M055)

M048-M055 turn the M017-M047 operator into a persistent local operator that
manages multiple projects and missions, survives interruption and restart,
schedules work with persisted deterministic resource-aware decisions, surfaces
human gates, exposes one local dashboard/API, projects into LifeOS and can be
backed up, restored and reconstructed — without introducing a competing
lifecycle, readiness, mission/release identity, event, patch-identity or trust
model, and without any Git release write from the platform layers:

    scripts/trajectory project create|list|show|update|archive|objective-add|link|status|linkage|reconstruct
    scripts/trajectory daemon start|status|stop|resume|crash|reconstruct
    scripts/trajectory queue add|list|schedule|accounting|pause|resume|cancel|requeue|reconstruct
    scripts/trajectory inbox refresh|list|gates|ack|resolve|notify
    scripts/trajectory api projection|dashboard|serve
    scripts/trajectory lifeos sync|status --vault DIR
    scripts/trajectory routing-evidence [--trials FILE] [--benchmark-root DIR]
    scripts/trajectory bootstrap|systemd-unit
    scripts/trajectory backup|restore|dr|corruption|migrate
    scripts/trajectory platform-acceptance --out acceptance.json
    scripts/trajectory platform-dogfood --json

* **M048** a durable project registry above objectives and missions with a
  stable identity, explicit policy/routing preferences, active/archived
  lifecycle and read-only project -> objective -> mission -> release ->
  artifact linkage; project metadata never replaces canonical artifacts and
  the environment can never mutate policy;
* **M049** a terminal-independent, single-owner, restart-safe supervisor with
  an explicit process identity, heartbeat, duplicate-owner prevention, crash
  detection, startup recovery, deterministic restart/resume and a systemd
  user-service unit; it never performs a release Git write;
* **M050** a durable multi-mission queue with priority, dependencies, bounded
  concurrency, local GPU/VRAM and remote-provider admission, deterministic
  persisted scheduling reasons, starvation resistance, pause/resume/cancel/
  requeue and no duplicate execution after restart; it orchestrates the
  existing runtime/control paths;
* **M051** a durable, deduplicated human-gate inbox for the required
  lifecycle events with unread/acknowledged/resolved state and an optional
  `notify-send` sink that reports explicit `UNAVAILABLE`; it never authorizes
  a human gate;
* **M052** one canonical read-only projection served by a localhost-only API
  and self-contained web dashboard with explicit, token/CSRF-protected
  mutations and no business-logic duplication; contradictory canonical state
  fails closed;
* **M053** an idempotent Obsidian/LifeOS projection of project notes and
  mission journals with deterministic frontmatter, canonical provenance,
  secret-safe allow-listing and explicit degraded status when the vault is
  unavailable;
* **M054** evidence-based routing over the M029/M030 telemetry: every metric
  carries `PROVIDER`/`DERIVED`/`LOCAL`/`UNAVAILABLE` provenance, unavailable
  metrics are `null` with a reason, a recommendation requires comparable
  evidence and is persisted separately from policy authorization, and the
  final reviewer stays `qwen3.8:27b-q4_K_M`;
* **M055** production hardening: versioned config with secrets separated,
  bootstrap/preflight, systemd generation, durable-state backup excluding
  transient PID/lock/runtime junk, deterministic restore into a clean fixture,
  stale-lock and partial-corruption detection, schema migration verification,
  full reconstruction and a disaster-recovery drill.

Durable evidence lives under `docs/missions/m048-m055/`. See
`docs/adr/ADR-026-persistent-autonomous-operator-platform.md`.

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
