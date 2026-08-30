# TrajectoryOS AI Development Stack

## Purpose

This document defines the operational development strategy for using local and cloud AI systems while preserving TrajectoryOS engineering discipline.

It supplements:

- `AGENTS.md`;
- `docs/development/PROCESS.md`;
- `docs/development/DEFINITION_OF_DONE.md`;
- `docs/development/AGENT_WORKFLOW.md`.

It does **not** change product/runtime architecture. It records how development work is routed, validated, benchmarked, and improved.

Tracked by GitHub Issue #36.
**Revised by GitHub Issue #43** to record the V1.11 / V1.12 validated routing baseline.
Where pre-V1.11/V1.12 assumptions are contradicted by later evidence, they are superseded here;
historical incidents are preserved as evidence, not as current routing.

---

## Status vocabulary

Configuration statements in this document use three explicit states:

- **CURRENT** — observed to be in use now.
- **PROPOSED** — candidate configuration or routing decision to test.
- **VALIDATED** — supported by repeatable evidence on real TrajectoryOS work and accepted as the working baseline.

Do not silently promote a `PROPOSED` setting to `VALIDATED` merely because a model or tool recommends it.

---

## Authority model

TrajectoryOS development follows this authority hierarchy:

> **AI proposes. Deterministic code validates. Human decides. Persistence records the accepted change.**

For software engineering specifically:

1. AI systems may inspect, reason, propose, edit in authorized branches/worktrees, test, review, and explain.
2. Deterministic checks remain executable truth for syntax, tests, linting, typing, and CI.
3. A second model may provide adversarial review but does not override deterministic evidence.
4. No model receives autonomous merge authority.
5. Architecture, security posture, scope expansion, and consequential irreversible actions remain human-authorized.

---

## Strategic objective

The development stack is optimized for **marginal value**, not for maximizing use of either cloud or local models.

Default rule:

> **Use cloud intelligence where judgment has the highest marginal value; use local models to absorb high-volume implementation and review loops.**

This avoids two inefficient extremes:

- exhausting paid cloud quota on repetitive repository-edit/test/fix loops that local models can perform well;
- refusing cloud reasoning when it materially improves correctness, architecture, research quality, or human time.

---

## Reference workstation profile

**CURRENT — non-sensitive reference profile**

The primary local development workstation used to validate this strategy has approximately:

- 24 GiB NVIDIA GPU VRAM;
- 64 GiB system RAM;
- high-end desktop CPU;
- Linux/Kubuntu development environment;
- local Ollama runtime;
- Git/GitHub workflow;
- Python project tooling through the repository quality gate.

Hostnames, tokens, private paths containing personal data, and credentials must never be committed.

The hardware profile is evidence context, not a permanent project requirement.

---

## Current local model inventory relevant to engineering

**CURRENT — principal installed candidates**

| Model | Current operational role | Evidence / notes |
|---|---|---|
| `qwen3.8-dev3090` | **preferred primary local autonomous developer** (Pi, medium thinking) | V1.11/V1.12 real-work evidence: strong repository comprehension, sustained implement/repair loops. V1.10 output-flow incidents were harness/budget issues, not model defects |
| `qwen3-coder:30b` | **bounded precision editor** (Aider) | coding-specialized; effective for explicit small, file-scoped edits and mechanical repairs; V1.11 showed repeated fixture/model mistakes on substantive work despite correct core semantics |
| `qwen3.6:35b` | **independent read-only adversarial reviewer** | V1.12: used as the independent reviewer; issue + diff + tests, never as a competing editor |
| `qwen2.5-coder:7b` / equivalent small coder | mechanical low-cost tasks | optional for simple transformations, fixtures, commit text, bounded repairs |

Specific model names are replaceable operational choices, not permanent TrajectoryOS architecture.
Model novelty alone is not a reason to add a new project dependency or permanently change routing.

`qwen3.8-dev3090` is a local Ollama alias of the validated Qwen3.8 27B Q4_K_M model, configured with `draft_num_predict=1` after RTX 3090 A/B benchmarking. The alias changes runtime speculative-decoding configuration only; it does not change the underlying model family or the validated developer role.

### Reproducible local setup and preflight

The canonical Ollama definition for this alias is versioned at:

```text
config/ollama/qwen3.8-dev3090.Modelfile
```

It intentionally fixes:

```text
FROM qwen3.8:27b
PARAMETER num_ctx 65536
PARAMETER draft_num_predict 1
```

These values are benchmark-backed rather than arbitrary. On the validated RTX 3090 setup at
64K context, `draft_num_predict=1` produced approximately 57.25 tok/s, versus 45.20 tok/s for
`draft_num_predict=2` and 29.62 tok/s for the previous depth of 4.

Before running Pi workloads, the local configuration can be checked without modifying anything:

```bash
scripts/check-local-ai
```

The preflight verifies:

- Ollama is available;
- `qwen3.8-dev3090` exists;
- Ollama reports `num_ctx=65536`;
- Ollama reports `draft_num_predict=1`;
- the Pi Ollama registry contains `qwen3.8-dev3090`;
- Pi declares a 65536-token context window and 32768 maximum output tokens;
- reasoning, text+image input, and reasoning-effort support match the validated profile.

A non-zero exit code means the local developer stack is not ready.

Repair is always explicit:

```bash
scripts/setup-local-ai --apply
```

The setup command does not download the base model automatically. If `qwen3.8:27b` is absent,
it fails and leaves installation to the human operator. When the Pi registry must be changed,
the existing registry is backed up before writing, unrelated model entries are preserved, and
a second invocation is idempotent when the configuration is already correct.

Running `scripts/setup-local-ai` without `--apply` performs no modification and exits with a refusal.

---

## Model routing

**VALIDATED baseline from V1.11 / V1.12 real TrajectoryOS work**

Core operational conclusion:

> **Pi is the preferred autonomous developer; Aider is the preferred precision editor.**

Optimization priority: **correctness and low human-intervention burden first**; minimum
first-draft elapsed time is a secondary concern. Agents are not meant to hand back a failing
first draft and wait — substantial agents self-repair through focused tests and
`bash scripts/quality.sh` until green, and human intervention is reserved for genuine ambiguity,
architecture decisions, destructive/irreversible operations, and merge authority.

The routing below is the current validated baseline. Specific model names remain replaceable
operational choices, not permanent TrajectoryOS architecture.

### Cloud architect / arbitrator — GPT-5.6 Sol / high-value cloud reasoning

**VALIDATED** — Use cloud reasoning primarily for:

- architecture and issue / acceptance-contract design;
- current external research;
- difficult debugging arbitration;
- comparison and arbitration of competing local-model findings;
- review of consequential or ambiguous changes.

Do not default to cloud capacity for repetitive edits, test reruns, simple lint repairs, or
mechanical repository work when local capacity is adequate.

### Primary local autonomous developer — Pi + `qwen3.8-dev3090` (medium thinking)

**VALIDATED (V1.12)** — the preferred primary local autonomous developer for:

- substantive single- and multi-file feature implementation from an Issue contract;
- complex debugging;
- repository comprehension and local implementation planning;
- self-repair loops through focused tests and the canonical quality gate;
- remediation of valid BLOCKER/MAJOR reviewer findings.

Reasoning level stays task-dependent rather than globally maximized:

- LOW — simple/mechanical tasks;
- MEDIUM — default engineering work (this is the validated default for feature work);
- HIGH/XHIGH — only for genuinely difficult reasoning where additional thinking improves outcomes.

Higher reasoning effort must not be treated as a substitute for sufficient context/output budgets.

### Bounded precision editor — Aider + `qwen3-coder:30b`

**VALIDATED for bounded scope only** — Use for:

- explicit small, file-scoped edits;
- mechanical typing/lint/format repairs;
- bounded refactors with a tightly specified scope.

**Not the default substantive-feature developer.** V1.11 showed Aider/Qwen3.8 sessions making
repeated fixture/model-level mistakes even when the core semantics were correct; substantive,
multi-file work is routed to Pi + `qwen3.8` instead. It must still follow `AGENTS.md`, the linked
Issue, the quality gate, and human merge authority.

### Read-only adversarial reviewer — `qwen3.6:35b` via Ollama

**VALIDATED (V1.12)** — Use as the independent read-only adversarial reviewer:

```text
Issue / contract
+ changed diff (including untracked new files)
+ relevant tests
+ quality-gate evidence
→ BLOCKER / MAJOR / MINOR findings
```

The reviewer must not simultaneously act as an uncontrolled second editor of the same branch.
Only valid BLOCKER/MAJOR findings are repaired (by the implementation agent, e.g. Pi) before a
final quality gate.

### Small local model

Use a smaller coder model for low-risk, bounded tasks when doing so improves throughput without reducing correctness.

Examples:

- commit-message drafts;
- small fixture generation;
- simple renames;
- basic documentation transformations;
- isolated formatting or lint repairs.

---

## Agent / harness strategy

### Ollama

**CURRENT** — local inference runtime.

Ollama configuration must be validated from actual runtime state rather than inferred from model metadata.

### Pi

**VALIDATED — preferred primary local autonomous coding-agent harness (V1.12).**

- The **Pi CLI is the canonical automation interface**. The VS Code Pi extension is optional
  convenience, never a workflow dependency.
- Non-interactive (automated) runs use Pi **print mode** (`pi -p`) in Pi 0.84.x.
- Large task contracts are passed through Pi's native **`@file`** context mechanism rather than
  relying on inherited standard input.
- V1.12 completed substantive feature work with Pi + `qwen3.8` (medium thinking) end-to-end:
  implementation, test/repair loops, autonomous remediation of valid reviewer findings, and a
green canonical quality gate.

V1.10 incidents still stand as evidence that model capability can be masked by harness
limitations; Pi must be judged separately from the model it hosts. Later V1.11/V1.12 evidence,
however, supersedes the earlier hypothesis that Pi was experimental/secondary.

### Aider

**VALIDATED for bounded scope** — precision/repository-map editor for explicit small, file-scoped
edits and mechanical repairs.

Aider is effective when a compact repository map and explicit file scope reduce unnecessary context
consumption. It is **not** the default substantive-feature agent (see Model routing and the V1.11
lesson).

### OpenCode

**OBSERVATION ONLY — superseded as a primary-harness candidate.**

OpenCode remains a capable general-purpose agent with planning/build roles, model configuration,
and local Ollama integration, but the V1.11/V1.12 real-work evidence validated Pi + `qwen3.8`
as the primary local autonomous developer. OpenCode is retained here only as an observation item;
no re-evaluation campaign is planned unless routing evidence changes.

### Git worktrees

**VALIDATED pattern conceptually; operational use remains task-dependent**

Use isolated Git worktrees when a coding agent should be allowed to edit freely without contaminating the authoritative feature branch.

Pattern:

```text
feature branch / known clean state
        |
        +--> disposable agent worktree
                 |
                 +--> edits
                 +--> tests
                 +--> diff
        |
        +--> reviewed patch / selected changes
```

A worktree is isolation, not approval. Changes still require review and the canonical quality gate.

---

## Runtime audit checklist

Before changing Ollama or agent configuration, record the actual state locally.

### 1. Loaded model and effective context

Run while the target model is active:

```bash
ollama ps
```

Record locally:

- model name;
- processor placement;
- effective context;
- whether CPU offload is occurring.

Do not commit machine-identifying information.

### 2. Ollama service environment

Inspect how Ollama is started and which environment variables are effective.

Candidate variables to audit include:

```text
OLLAMA_FLASH_ATTENTION
OLLAMA_KV_CACHE_TYPE
OLLAMA_CONTEXT_LENGTH
OLLAMA_MAX_LOADED_MODELS
OLLAMA_NUM_PARALLEL
OLLAMA_KEEP_ALIVE
```

### 3. Pi / agent model configuration

Inspect the actual local configuration, including where applicable:

```text
contextWindow
maxTokens
reasoning support
reasoning level/budget
compaction reserve
recent-token retention
provider/tool-message behavior
```

A UI display of a model's theoretical context is not proof that the runtime is using that context.

### 4. Aider / (optionally) OpenCode configuration

If installed for evaluation, record:

- model binding;
- context/output limits;
- tool permissions;
- compaction behavior;
- repository scoping;
- observed completion behavior on real TrajectoryOS tasks.

---

## Ollama optimization experiments

The following are **PROPOSED hypotheses**, not validated defaults:

- target approximately 48–64k effective context for large coding-agent sessions when GPU performance remains acceptable;
- enable Flash Attention where supported;
- evaluate `q8_0` KV cache to reduce KV-memory pressure;
- keep one large model loaded when simultaneous large-model residency causes VRAM pressure;
- use one parallel large-model request on a single 24 GiB GPU unless evidence supports more;
- prefer a responsive mostly-GPU configuration over a theoretically huge context that forces damaging CPU offload.

Candidate environment baseline to test, not blindly apply:

```text
OLLAMA_FLASH_ATTENTION=1
OLLAMA_KV_CACHE_TYPE=q8_0
OLLAMA_MAX_LOADED_MODELS=1
OLLAMA_NUM_PARALLEL=1
```

Context size must be selected from measured VRAM use, throughput, and task success.

Do not encode a global 64k context merely because it is desirable; validate it on the actual workstation.

### Runtime / GPU utilization note

Agentic coding alternates model inference with filesystem inspection, grep, tests, Ruff, mypy,
and tool calls. GPU utilization is therefore **naturally bursty**, and instantaneous utilization
below 100% is normal even when a model remains mostly or fully GPU-resident.

- Do **not** require constant 98–100% GPU utilization as a health or routing criterion.
- Inspect residency and context with `ollama ps`. Runtime placement/context is the meaningful
  signal; GPU utilization alone is not a routing criterion.

---

## Output-budget and flow policy

Large-context models can still fail agentic work if the harness caps generated output too aggressively.

Therefore model configuration must distinguish:

```text
context window
!=
maximum output tokens
!=
reasoning budget
!=
agent compaction reserve
```

For long implementation tasks:

1. avoid spending the whole output budget narrating repository exploration;
2. instruct coding agents to edit after inspecting only necessary files;
3. prefer tool actions and file edits over repeated prose summaries;
4. preserve enough output/compaction budget to reach tests and handoff;
5. split very large tasks at Issue boundaries rather than relying on unlimited agent continuation.

---

## V1.10 incident lessons (historical evidence)

**Historical observed evidence (V1.10)**

During the V1.10 planned-effort-estimate milestone, local Qwen sessions demonstrated correct issue/repository comprehension but encountered two distinct workflow failures before implementation completion:

### Incident A — maximum output token limit

The agent inspected the repository and contract correctly, then stopped because its maximum output budget was exhausted before code completion.

Lesson:

> Model quality and harness output budget are separate dimensions. A capable model can fail operationally when the harness consumes or caps output poorly.

### Incident B — tool/message integration failure

A local agent flow also encountered a `no user query found in messages`-class integration error after tool-oriented interaction.

Lesson:

> Provider chat templates, agent message history, and tool-call orchestration must be validated independently from model reasoning quality.

These incidents motivate Issue #36. They are not evidence that Qwen3.8 is intrinsically unsuitable for coding.

**Superseded as routing basis.** These V1.10 incidents predate the V1.11/V1.12 evidence in the next
section and must not be read as disqualifying: the output-flow and tool-message failures were
harness/budget-era issues under earlier setups, while later validated runs (V1.12, Pi + `qwen3.8`
medium) completed substantive multi-file work end-to-end. The incidents remain valid as historical
evidence for the output-budget and tool-message design rules, but they **do not supersede** the
later validated routing.

---

## V1.11 / V1.12 validated evidence and pipeline lessons

### V1.11

Aider/Qwen3.8 sessions during V1.11 repeatedly produced fixture and model-level mistakes despite
correct core semantics. Lesson: a precision editor with correct intent can still
emit incorrect concrete values; substantive multi-file work with self-check capability belongs to
the autonomous developer (Pi + `qwen3.8` medium), while Aider stays bounded to precise, file-scoped
edits with strong deterministic verification.

### V1.12

V1.12 completed successfully with the validated stack:

- **Pi + `qwen3.8` (medium thinking)** as the autonomous implementation and self-repair agent;
- **`qwen3.6` (35B via Ollama)** as the independent read-only adversarial reviewer;
- deterministic quality gates (`bash scripts/quality.sh`) and GitHub CI as executable truth;
- **human merge authority as the only merge path**.

The V1.12 workflow also exposed and fixed two reusable harness/process hazards: inherited `stdin`
in heredoc-based shell pipelines, and false-positive success when no repository change existed.

### Reusable Pi CLI / pipeline safeguards

1. The **Pi CLI is the canonical automation surface**; VS Code Pi extensions are optional
   convenience, not a dependency of any workflow.
2. For Pi 0.84.x non-interactive operation, use **print mode** (`pi -p`).
3. For large task contracts, use Pi's native **`@file`** context mechanism rather than relying on
   inherited standard input.
4. **Never allow a Pi process in a shell pipeline to inherit the script's own `stdin`** when the
   script itself is supplied through a heredoc (`bash <<HEREDOC` containing a Pi call). Prefer
   writing a real executable `.sh` file and running it.
5. **Redirect Pi stdin explicitly** (`</dev/null`) where appropriate so the data flow is unambiguous.
6. A pipeline must not declare feature success merely because the existing quality gate is green;
green tests on the old baseline do **not** prove a feature was implemented. Require a real intended
change: a non-empty scoped diff and/or a commit ahead of the known baseline.
7. Review payloads must include **untracked new files** as well as tracked/staged diffs; reject an
effectively empty review payload.
8. A successful local-agent **exit code alone is not success evidence**; success requires the
   intended repository change plus deterministic validation.
9. Do not put `set -euo pipefail` directly into the user's long-lived interactive shell. Contain
   strict shell options inside a child script or subshell so a failure cannot close the parent
   terminal.

---

## ChatGPT Plus quota-preservation strategy

ChatGPT Plus should be treated as **high-value cloud reasoning capacity**, not as the default execution engine for every repository action.

Default escalation ladder:

```text
1. deterministic/local mechanical work
2. local implementation model
3. local independent reviewer
4. ChatGPT architecture/arbitration when useful
5. cloud coding agent only when escalation value justifies quota/cost
```

### Prefer local execution for

- repository inspection already available locally;
- repetitive edits;
- test generation;
- test/fix cycles;
- Ruff/mypy repair;
- bounded refactoring;
- routine adversarial review.

### Prefer ChatGPT for

- architecture and acceptance-contract design;
- current external research;
- cross-model arbitration;
- difficult semantic bugs;
- consequential design changes;
- final synthesis where broader reasoning materially reduces risk.

### Cloud coding-agent escalation

Use a cloud coding agent when local attempts are failing for capability reasons rather than preventable harness/configuration reasons.

Do not use cloud escalation merely because the local agent needs one configuration correction.

---

## Canonical engineering loop (validated feature loop)

For substantial features, the validated multi-model process is:

```text
Issue / acceptance contract
   |
   v
Pi + qwen3.8 (medium thinking) — primary local autonomous developer
   |
   v
Inspect existing models / enums / conventions before editing
   |
   v
Implementation
   |
   v
Focused tests
   |
   v
Autonomous self-repair (do not stop at the first failure)
   |
   v
bash scripts/quality.sh
   |
   v
Autonomous repair until pytest / Ruff / mypy are green
   |
   v
git diff --check
   |
   v
qwen3.6 read-only adversarial review (diff incl. untracked files)
   |
   v
Pi repairs valid BLOCKER / MAJOR findings
   |
   v
Final quality gate
   |
   v
GitHub PR + CI
   |
   v
One human merge decision
   |
   v
Post-merge main revalidation
```

Agents should not stop after the first test failure; self-repair through focused tests and the
canonical gate is part of the job. Human intervention is reserved for genuine ambiguity,
architecture decisions, destructive/irreversible operations, and merge authority. Cloud reasoning
(GPT-5.6 Sol / high-value cloud inference) is invoked at the architecture, specification, current
research, arbitration, and consequential-review points rather than for mechanical repair loops.

The exact models are replaceable. The authority boundaries are not.

---

## Fine-tuning policy

**VALIDATED decision for current phase: defer fine-tuning.**

Do not fine-tune a model to compensate for:

- insufficient context;
- insufficient output budget;
- defective tool-message handling;
- poor compaction;
- weak prompts;
- incorrect model routing;
- missing deterministic tests.

Optimization order:

1. runtime;
2. agent/harness;
3. context/output budgets;
4. prompts and repository instructions;
5. workflow/routing;
6. benchmark and evidence;
7. only then evaluate fine-tuning.

A future fine-tuning dataset should come from accepted project evidence such as:

```text
Issue
→ candidate patch
→ review findings
→ corrected patch
→ human acceptance
→ CI outcome
```

Fine-tuning should address recurring, measurable model deficiencies, not novelty.

---

## Benchmark policy

**VALIDATED policy (V1.12 onward):** real TrajectoryOS product issues are the primary evidence.

- **Do not restart synthetic benchmark campaigns by default.**
- Use real product Issues (issue → implementation → review → quality gate → CI → human merge)
  as the primary evidence for routing decisions.
- Run a controlled comparison **only when its result can materially change a routing decision**.
- Preserve accepted real-world evidence (V1.10/V1.11/V1.12 records) instead of optimizing for
  benchmark volume.
- Do not rely only on public model benchmarks either.

### Evidence recording (when a comparison is warranted)

For each meaningful comparison:

```text
Date
Task / Issue
Model
Harness
Reasoning level
Effective context
Max output budget if known
GPU/CPU placement if relevant
Completion status
Failure/interruption class
Elapsed time if practical
Substantive correction rounds
Quality-gate result
Human corrections required
Notes
```

### Evaluation principle

The winning combination is not simply the model with the best prose or benchmark score.

Prefer the combination that minimizes:

```text
human intervention burden   (primary)
+ failed iterations
+ corrective round-trips
+ cloud quota consumption
```

while preserving correctness and architectural discipline. Minimum first-draft elapsed time is
explicitly **not** the primary optimization target.

---

## Promotion rule for a primary local coding stack

A model/harness pair becomes **VALIDATED primary local implementation stack** only after it
demonstrates, on real repository work:

1. successful completion without avoidable output/context interruption;
2. correct scoped edits;
3. successful quality gate after reasonable correction rounds;
4. no recurrent authority-boundary violations;
5. acceptable interactive throughput on the reference workstation;
6. lower or equal human correction burden than the alternative being replaced.

**Current state:** Pi + `qwen3.8` (medium thinking) satisfies this rule as of V1.12 and is the
VALIDATED primary local autonomous developer. Aider + `qwen3-coder` is VALIDATED as a bounded
precision editor only. All other harness/model roles remain experimental/proposed where marked,
and model names remain replaceable operational choices.

---

## Security and repository hygiene

Never commit:

- API keys;
- access tokens;
- private model-provider credentials;
- private machine hostnames when unnecessary;
- personal/client data;
- raw local logs containing sensitive paths or content.

Commit summarized technical findings instead of raw diagnostic dumps when those dumps may reveal local information.

---

## Change discipline

When an observed failure reveals a reusable lesson, apply:

```text
incident
→ evidence
→ rule
→ configuration/process change
→ evidence capture / validation
→ documentation
```

Do not silently rewrite history when a tool/model decision changes. Update this document with the new evidence and preserve the decision through normal Git history.

---

## Status after Issue #43

The routing baseline is now documented from real V1.11/V1.12 evidence:

1. Pi + `qwen3.8` (medium thinking) — VALIDATED primary local autonomous developer.
2. Aider + `qwen3-coder` — VALIDATED bounded precision editor only.
3. `qwen3.6` — VALIDATED independent read-only adversarial reviewer.
4. GPT-5.6 Sol / high-value cloud reasoning — architecture, specification, current research,
   difficult arbitration, consequential review.
5. `bash scripts/quality.sh` + `git diff --check` + GitHub CI — deterministic validation.
6. Human merge authority — unchanged; agents never merge.

Remaining open work is narrow: keep validating only when routing could materially change, and
carry forward the pipeline safeguards above into automation. Do not restart synthetic benchmark
campaigns by default (see Benchmark policy).
