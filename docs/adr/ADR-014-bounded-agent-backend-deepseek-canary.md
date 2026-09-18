# ADR-014 — Bounded agent-backend abstraction with DeepSeek Harness canary

Status: Accepted

Date: 2026-09-18

## Context

TrajectoryOS has always executed agent work through the Pi CLI, which is the
validated primary local autonomous developer (see `AI_DEVELOPMENT_STACK.md`).
A new official harness is now available: DeepSeek Harness, distributed as the
`dsh` runtime with an official Python SDK (`deepseek-harness-sdk`, module
`deepseek_harness`) and a structured SDK runtime (`dsh --profile sdk`) that
serves newline-delimited JSON-RPC 2.0 over stdio. DeepSeek Harness is a
developer preview and must not become an irreversible dependency.

The repository already selected `--agent-backend pi|dsh` in the shell wrapper,
but it had no Trajectory_OS-owned contract: it shelled out and relied on
free-form stdout for completion, had no capability probe, no structured
lifecycle evidence, no cancellation/timeout proof, no fallback provenance and
no canary. That is not sufficient to reason about completion reliability or
to compare backends.

## Decision

Introduce a bounded, provider-agnostic agent-backend contract in
`trajectory_os.agents` and make DeepSeek Harness an adapter behind it.

### 1. Stable Trajectory_OS-owned contract

`AgentBackend` exposes exactly:

* `probe()` — a deterministic capability probe (availability + stable reason
  code), never a model launch;
* `run(request, cancel=...)` — one bounded run returning lifecycle/status,
  structured events, final response, cancellation/timeout outcome, completion
  evidence and errors.

The core depends on this contract and on `AgentRequest` / `AgentResult` /
`BackendProbe` / `CompletionEvidence` / `CanaryOutcome`. It never imports a
specific SDK API. Provider-specific model naming is adapter-owned and never
globally rewritten.

### 2. Backends

* **`pi`** — the proven subprocess backend. Completion follows Pi's exact
  terminal-marker contract (last non-blank line is an uppercase
  `*_COMPLETE` marker with a handoff section). Pi remains the proven fallback.
* **`deepseek-harness`** — the official adapter. It targets the official
  runtime interface `dsh --profile sdk`, speaking the documented JSON-RPC
  methods (`initialize`, `session/prompt`, `shutdown`) and notifications
  (`session.event`, `session.status`, `subagent.started`,
  `subagent.finished`). Completion is proven by **structured lifecycle
  evidence** (an observed `session.status` `idle` transition and/or a
  structured result), never by grepping stdout. The adapter records
  `serverInfo.name == deepseek-harness-sdk-runtime` as a protocol check.
  When the official Python SDK is absent but the runtime is present, this is
  reported explicitly; when neither is available the result is deterministic
  `UNAVAILABLE`.

### 3. Fallback and canary

`run_with_fallback` runs the primary backend and deterministically falls back
to Pi when the primary is unavailable, incompatible, times out, or does not
produce reliable completion evidence. The fallback result always records
`fallback_from` provenance and never reinterprets a failure as success.

`run_canary` attempts at most one bounded representative task. By default it
requires the official Python SDK; when the SDK is absent it records
`CANARY_UNAVAILABLE` with `SDK_MISSING`, preserves the primary probe and the
Pi fallback probe as evidence, and never weakens validation or review gates.
`--allow-runtime` permits an operator-authorized runtime-transport canary.

### 4. Trust boundaries

No backend may commit, push, merge, reset, restore, clean, stash, rebase,
switch or checkout, and the abstraction performs no Git write at all. The
canary is bounded and never launches a full mission. TrajectoryOS does not
depend irreversibly on the developer-preview harness: removing the adapter
leaves the proven Pi path intact.

## Consequences

* TrajectoryOS can represent and probe two backends through one stable
  contract without coupling the core to DeepSeek Harness internals.
* Completion reliability, lifecycle evidence, runtime and fallback
  provenance are captured as structured, machine-readable facts suitable for
  later comparison.
* The current canary result is a deterministic `CANARY_UNAVAILABLE`
  (`SDK_MISSING`) because the official Python SDK is not installed in the
  canary environment; the official `dsh --profile sdk` runtime is present and
  the proven Pi fallback is available.
* The canary result and the deterministic fallback are recorded as evidence,
  and the mission continues on the proven Pi path.

## Alternatives considered

* **Depend directly on the DeepSeek Harness SDK throughout the core.**
  Rejected: a developer-preview, vendor-specific, irreversible dependency.
* **Grep stdout for DeepSeek Harness completion.** Rejected: the structured
  JSON-RPC lifecycle/result evidence is authoritative and reproducible.
* **Treat the shell `--agent-backend dsh` path as the abstraction.**
  Rejected: no capability probe, no structured lifecycle evidence, no
  cancellation/timeout contract and no fallback provenance.
* **Replace Pi with DeepSeek Harness.** Rejected: Pi remains the validated
  primary and proven fallback until real-work evidence supports a change.
