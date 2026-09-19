# ADR-018 — Isolated DeepSeek Harness qualification, local resource orchestration and artifact provenance

Status: Accepted

Date: 2026-09-18

## Context

Program blocks A (M017–M019, ADR-016) and B (M020–M022, ADR-017) delivered
the visible operator product layer, the multi-goal portfolio, the persistent
daemon and the hardened provider-neutral agent backends. Program block C
(Missions 023–025, Issue #226) addresses three production gaps that remain:

1. **Harness isolation.** The official developer-preview DeepSeek Harness SDK
   (`deepseek-harness-sdk` / `deepseek_harness`) is installed in its own
   virtual environment under `$HOME/.cache/trajectory-os/deepseek-harness-sdk`
   and targets a different Python minor version (3.14) than the project
   (3.13). Importing it into the project interpreter — or exporting its
   `PYTHONPATH` — is ABI-incompatible and can fail the canonical quality gate
   for reasons unrelated to the change under test.
2. **Local resource orchestration.** The proven `runs.resources` policy is a
   *pure* per-dimension admission function over an operator-supplied
   capacity document. It never discovers the real machine, never tracks live
   reservations, and does not model the local reviewer's separate GPU usage or
   the difference between remote inference and local GPU work.
3. **Workspaces and artifacts.** Mission sub-runs materialize per-run
   workspaces (V1.92), but there is no durable per-goal / per-mission
   workspace and no explicit, content-addressed artifact provenance with
   lineage, so generated reports/datasets/models/intermediate results cannot
   be reconstructed or audited across a goal's lifetime.

## Decision

### 1. M023 — isolated SDK qualification over the M015/M016 contract

Add `trajectory_os.agents.harness_qualification`:

* it resolves the isolated SDK environment by filesystem scan only (Python
  interpreter, runtime executable, distribution version); it never imports the
  SDK into the project interpreter;
* it runs the isolated interpreter in isolated mode (`python -I`) as a
  bounded subprocess with a sanitized environment that drops every `PYTHON*`
  variable, to record SDK/runtime identity;
* it constructs the existing `DeepSeekHarnessBackend` with the isolated
  runtime and SDK facts **injected** through the adapter's existing
  dependency-injection surface, then reuses `qualification.qualify` (which
  reuses the M015 canary/contract unchanged);
* it records a bounded structured qualification (`checks`): SDK identity,
  runtime identity, handshake, lifecycle, completion/error semantics,
  timeout/cancellation capability, evidence structure and the Pi + DeepSeek
  Flash comparison, with deterministic `UNAVAILABLE`/`INCOMPATIBLE` fallback;
* M016 qualification is preserved unchanged; M023 is additive.

No secret is read, logged or persisted.

### 2. M024 — operational local CPU/GPU/VRAM arbitration

Add `trajectory_os.resources`:

* `probe.discover` is a bounded, read-only discovery of CPU affinity, RAM
  (`/proc/meminfo`) and NVIDIA/RTX resources (`nvidia-smi --query-gpu`), where
  every dimension may be explicitly UNKNOWN rather than guessed;
* `ResourceArbiter` keeps an explicit reservation ledger and performs
  deterministic admission control with hard bounded concurrency, preventing
  oversubscription;
* remote inference is structurally distinguished from local GPU use: remote
  workloads drop GPU/VRAM demand;
* the local reviewer owns an explicit reserved CPU/VRAM pool that agent/job
  workloads can never consume; the reviewer may never oversubscribe the
  machine as a whole;
* reservations can be persisted atomically and reconstructed after restart;
* the pure `runs.resources` policy remains the single per-dimension admission
  primitive; M024 makes it operational rather than duplicating it;
* live state is exposed through `trajectory-pi-goal resource-status` and the
  live TUI snapshot (`local_resources`), without making read-only snapshot
  construction depend on hardware.

### 3. M025 — persistent workspaces and artifact provenance

Add `trajectory_os.artifacts`:

* every goal and mission gets a durable, product-owned workspace under
  `<root>/workspaces/<goal_id>[/missions/<mission_id>]`;
* every generated/imported file becomes a content-addressed `ArtifactRecord`
  with an owning goal/mission/phase/sub-run, a closed kind (`FILE`, `REPORT`,
  `DATASET`, `MODEL`, `INTERMEDIATE`), a producer, size, SHA-256, parent
  artifact identities and optional M014 reuse / M016 proof/criterion
  references;
* the store is namespaced by goal: implicit cross-goal visibility is
  structurally impossible, and crossing a goal boundary requires an explicit
  `shared_with` grant (fail closed otherwise);
* lineage reconstruction is bounded and fail-closed on missing parents,
  unshared cross-goal parents or cycles;
* artifacts are exposed through the goal snapshot (`artifacts`) and the
  `trajectory-pi-goal artifacts <goal_id> [--lineage ID]` command.

The store performs no Git trust-boundary write and writes only inside the
product-owned state root.

## Consequences

* `scripts/quality.sh` must keep `unset PYTHONPATH`: the project venv remains
  the single authoritative environment, and the isolated SDK is only ever
  touched through bounded subprocesses.
* The new packages are additive; all M008–M022 behavior, stores and contracts
  are preserved.
* External dependencies are not added: discovery uses the standard library and
  the already-present `nvidia-smi` (optional, unknown when absent).
* Hardware-specific dogfood evidence is local (`.artifacts/`), never
  committed as authoritative state.

## Alternatives considered

* **Import the SDK into the project interpreter** (M016 approach): rejected —
  ABI-incompatible and contaminates the quality gate.
* **A second scheduler for local resources**: rejected — the pure
  `runs.resources` policy already exists; M024 wraps it operationally.
* **A single global artifact namespace**: rejected — it would permit implicit
  cross-goal leakage.
