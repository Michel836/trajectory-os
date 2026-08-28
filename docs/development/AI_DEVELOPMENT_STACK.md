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

| Model | Intended evaluation role | Notes |
|---|---|---|
| `qwen3.8:27b` | local reasoning, architecture, debugging, candidate implementation | modern general/reasoning model; demonstrated strong repository understanding but encountered agent output-flow limits |
| `qwen3-coder:30b` | implementation/editor | coding-specialized candidate for repository edits and repeated test/fix loops |
| `qwen3.6:35b` | independent adversarial review | use issue + diff + tests rather than unrestricted competing edits |
| `qwen2.5-coder:7b` / equivalent small coder | mechanical low-cost tasks | candidate for simple transformations, fixtures, commit text, bounded repairs |

Other installed models may be tested, but model novelty alone is not a reason to add a new project dependency or permanently change routing.

---

## Proposed model routing

**PROPOSED — must be benchmarked before being marked VALIDATED**

### Cloud architect / arbitrator

Use ChatGPT / GPT-5.6 Sol primarily for:

- issue and acceptance-contract design;
- architecture reasoning;
- current external research when needed;
- difficult debugging arbitration;
- comparison of competing local-model findings;
- final review of consequential or ambiguous changes.

Do not default to ChatGPT for repetitive edits, test reruns, simple lint repairs, or mechanical repository work when local capacity is adequate.

### Local reasoning model

Use `qwen3.8:27b` primarily for:

- repository comprehension;
- local architecture analysis;
- difficult debugging;
- implementation planning;
- candidate implementation where the harness supports long agentic flow reliably;
- secondary review.

Reasoning level should be task-dependent rather than globally maximized:

- LOW — simple/mechanical tasks;
- MEDIUM — default engineering work;
- HIGH/XHIGH — only for genuinely difficult reasoning where additional thinking improves outcomes.

Higher reasoning effort must not be treated as a substitute for sufficient context/output budgets.

### Local implementation model

Use `qwen3-coder:30b` as the leading implementation/editor candidate for:

- multi-file repository changes;
- repeated edit/test/fix loops;
- test generation;
- bounded refactors;
- mechanical typing/lint repairs;
- implementation from a well-defined Issue contract.

It must still follow `AGENTS.md`, the linked Issue, the quality gate, and human merge authority.

### Local adversarial reviewer

Use `qwen3.6:35b` or another independent model family/configuration primarily for read-only adversarial review:

```text
Issue / contract
+ changed diff
+ relevant tests
+ quality-gate evidence
→ BLOCKER / MAJOR / MINOR findings
```

The reviewer should not simultaneously act as an uncontrolled second editor of the same branch.

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

### OpenCode

**PROPOSED** — candidate primary local coding-agent harness.

Evaluate because it supports explicit planning/build roles, agent iteration, model configuration, local Ollama integration, and context management suitable for repository-scale work.

Do not promote OpenCode to primary harness until it completes controlled TrajectoryOS tasks more reliably than the current baseline.

### Pi

**CURRENT** — existing local agent/harness used in development experiments.

**PROPOSED role** — experimental/secondary harness until context, output-token, compaction, and tool-message behavior are audited.

Observed V1.10 incidents show that model capability can be masked by harness limitations. Pi must therefore be judged separately from the model it hosts.

### Aider

**PROPOSED** — precision/repository-map editor and fallback for bounded changes.

Aider is particularly interesting when a compact repository map and explicit file scope can reduce unnecessary context consumption.

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

### 4. OpenCode / Aider configuration

If installed for evaluation, record:

- model binding;
- context/output limits;
- tool permissions;
- compaction behavior;
- repository scoping;
- observed completion behavior on benchmark tasks.

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

## V1.10 incident lessons

**CURRENT observed evidence**

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

## Canonical engineering loop

For substantial features, the target multi-model process is:

```text
Human goal
   |
   v
Issue / acceptance contract
   |
   +--> cloud or local architecture reasoning as appropriate
   |
   v
Local implementation agent
   |
   v
pytest / Ruff / mypy / quality.sh
   |
   v
Independent local adversarial review
   |
   +--> fix/retest if needed
   |
   v
Cloud arbitration/final review when marginal value is high
   |
   v
GitHub PR + CI
   |
   v
Human merge decision
```

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

## Benchmark plan

Do not rely only on public model benchmarks. Maintain a lightweight TrajectoryOS-specific comparison using real tasks.

### Initial task categories

1. repository comprehension;
2. bounded bug fix;
3. multi-file feature from an Issue;
4. hostile-input test design;
5. refactor without regressions;
6. Ruff/mypy repair;
7. adversarial diff review;
8. longer issue-to-quality-gate implementation.

### Minimum fields to record

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
human correction effort
+ failed iterations
+ latency/friction
+ cloud quota consumption
```

while preserving correctness and architectural discipline.

---

## Promotion rule for a primary local coding stack

A model/harness pair becomes **VALIDATED primary local implementation stack** only after it demonstrates, on real repository work:

1. successful completion without avoidable output/context interruption;
2. correct scoped edits;
3. successful quality gate after reasonable correction rounds;
4. no recurrent authority-boundary violations;
5. acceptable interactive throughput on the reference workstation;
6. lower or equal human correction burden than the alternative being replaced.

Until then, OpenCode, Pi, Aider, Qwen3.8, and Qwen3-Coder roles remain experimental/proposed where marked.

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
→ benchmark
→ validation
→ documentation
```

Do not silently rewrite history when a tool/model decision changes. Update this document with the new evidence and preserve the decision through normal Git history.

---

## Immediate next actions for Issue #36

1. audit the actual Ollama runtime with the principal model loaded;
2. audit Pi model/context/output/compaction configuration;
3. test proposed Ollama memory/context settings incrementally;
4. install/evaluate OpenCode only if not already available;
5. evaluate Aider for a bounded edit task;
6. compare `qwen3.8:27b` and `qwen3-coder:30b` on at least one controlled TrajectoryOS implementation task;
7. use `qwen3.6:35b` as read-only adversarial reviewer on the resulting diff;
8. update `CURRENT` / `PROPOSED` / `VALIDATED` markers with measured evidence;
9. keep V1.10 implementation separate from this tooling/process branch.
