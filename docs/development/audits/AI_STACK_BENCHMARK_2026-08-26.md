# TrajectoryOS AI Stack Benchmark — 2026-08-26

Tracked by Issue #36 and draft PR #37.

## Purpose

Record the controlled post-audit evidence gathered while implementing and reviewing TrajectoryOS V1.10 after correcting the original Pi/Ollama context mismatch.

This report complements `AI_STACK_AUDIT_2026-08-26.md`. The audit remains the baseline-before-change record; this file captures measured results after runtime and harness corrections.

Only summarized non-sensitive evidence is committed. Raw local logs remain local.

---

## Executive result

The V1.10 experiment demonstrated that the earlier failures were not evidence that Qwen3.8 was intrinsically incapable of repository-scale implementation.

After runtime alignment, Qwen3.8 27B completed the V1.10 implementation, targeted repair, and canonical quality gate. The final candidate reached 691 passing tests, Ruff clean, mypy clean on 36 source files, and `git diff --check` clean. An independent Qwen3.6 35B read-only review returned `REVIEW_RESULT: PASS`. PR #38 then passed GitHub CI before merge and `main` passed post-merge CI again.

At the same time, the experiment exposed a separate limitation: Pi can sustain long agentic work at a truthful 64k context, but it remains context- and token-inefficient for small bounded corrections. Model capability and harness efficiency must therefore be scored separately.

---

## VALIDATED — Qwen3.8 64k runtime profile for long coding work

A derived Qwen3.8 development profile was run with an effective context of `65536` tokens.

Measured runtime facts:

- effective Ollama context: `65536`;
- one parallel slot;
- Flash Attention: enabled;
- K/V KV cache: `q8_0`;
- processor placement: approximately `91% GPU / 9% CPU`;
- model remained viable on a 24 GiB GPU with 64 GiB system RAM;
- sampled total GPU memory use was roughly 22.8–23.1 GiB of 24 GiB during the long run.

The 64k profile is therefore **VALIDATED for this reference workstation and this Qwen3.8 development workload**. This is not a claim that 64k should be forced globally for every model.

### Thermal envelope

During the unusually long V1.10 agentic run, sampled board power reached roughly 300–308 W and GPU core temperature briefly peaked around 89°C with the case closed. Subsequent samples fell into the low/mid-80s and then high-70s while work continued; opening the case materially improved airflow.

Interpretation:

- 64k itself is operationally viable;
- sustained large-model sessions can expose chassis-airflow limits;
- thermal monitoring remains appropriate for unusually long runs;
- no permanent GPU power cap was required by this experiment.

---

## VALIDATED — reasoning-effort support exists in the Ollama endpoint

Direct OpenAI-compatible Ollama API testing demonstrated distinct behavior for `reasoning_effort=none` and `reasoning_effort=medium` on the installed runtime/model path.

Therefore the original Pi provider declaration `supportsReasoningEffort: false` was an integration/configuration mismatch, not an Ollama capability limit.

Model and harness configuration must expose reasoning controls truthfully before reasoning-level benchmarks are compared.

---

## VALIDATED — Pi can recover from real 64k context overflow and continue

During the real V1.10 implementation session, Pi reached the actual ~65.5k runtime ceiling multiple times. It emitted truncation/overflow signals, compacted, and resumed the task autonomously rather than terminating.

This recovery survived multiple large-context cycles while the model continued implementation and test repair.

Important limitation:

> These events validate overflow recovery, not the intended proactive compaction threshold.

Observed compactions occurred near provider overflow. The configured reserve was therefore not proven to trigger early enough to avoid reaching the runtime ceiling.

Pi 0.84.3 is consequently **VALIDATED for long-session endurance/recovery under this profile**, but proactive context-management behavior remains unresolved.

---

## VALIDATED — Qwen3.8 can complete a complex TrajectoryOS feature

The tuned Qwen3.8 27B + Pi/Ollama stack completed the V1.10 implementation candidate across domain, application, SQLite persistence, durable orchestration, and focused tests.

Final deterministic evidence after targeted correction:

- focused planned-effort tests: 43 passed;
- canonical `bash scripts/quality.sh`: 691 tests passed;
- Ruff: clean;
- mypy: clean on 36 source files;
- `git diff --check`: clean.

The implementation subsequently passed independent semantic review, independent Qwen3.6 adversarial review, GitHub pull-request CI, squash merge, and post-merge `main` CI.

Conclusion:

> Qwen3.8 27B is a demonstrated capable local implementation/reasoning model for substantial TrajectoryOS work when runtime context and harness configuration are coherent.

This does not yet make Pi + Qwen3.8 the primary stack for every task class.

---

## BENCHMARK FINDING — Pi/Qwen3.8 is inefficient for small bounded corrections

Two correction runs exposed strong working-directory and exploration sensitivity.

### Wrong-root / weakly anchored correction run

A bounded follow-up task consumed roughly:

- cumulative input: ~676k tokens;
- cumulative output: ~24k tokens;
- active context: ~88.4% of the 64k window;

without efficiently applying the trivial intended correction.

### Correct-root / strictly anchored correction run

After forcing the exact worktree and requiring an initial `pwd` check, the same class of bounded work consumed roughly:

- cumulative input: ~285k tokens;
- cumulative output: ~10k tokens;
- active context: ~45.6% of the 64k window;

and completed the targeted evidence/corrections successfully.

This is roughly a 58% reduction in cumulative input compared with the wrong-root run.

Interpretation:

1. explicit working-directory anchoring materially improves agent efficiency;
2. repository exploration must be bounded by prompt and harness design;
3. even the corrected run remained expensive for a four-item follow-up task;
4. Pi/Qwen3.8 should not automatically receive small mechanical fixes merely because it succeeded on the large feature.

Routing hypothesis retained for benchmark:

- long/complex reasoning and implementation: Pi + Qwen3.8 remains viable;
- small bounded edits: compare Aider, OpenCode, Qwen3-Coder, and/or smaller models before selecting a default.

---

## VALIDATED — Qwen3.6 35B direct Ollama 64k for occasional read-only review

The final V1.10 adversarial review intentionally bypassed Pi and supplied only:

- the authoritative Issue #35 contract;
- the complete final candidate patch;
- deterministic quality evidence.

Runtime facts:

- effective context: `65536`;
- processor placement: approximately `83% GPU / 17% CPU`;
- sampled GPU memory: about 23.3 GiB of 24 GiB;
- low idle/post-prompt GPU load after completion.

The model reviewed the complete candidate against twenty explicit checkpoints and returned:

```text
REVIEW_RESULT: PASS
```

The candidate later passed GitHub CI and post-merge CI.

Conclusion:

> Direct Ollama inference is the preferred validated path for bounded read-only adversarial review when no editing/tools are required. An agent harness would add unnecessary context/tool overhead for this task shape.

Qwen3.6 35B at 64k is feasible on the reference workstation for occasional review despite moderate CPU offload and very limited VRAM headroom. It is not established as the best daily interactive coding model.

---

## VALIDATED — worktree isolation plus explicit path checks

V1.10 also validated two workflow rules:

1. use a disposable worktree for high-autonomy implementation runs;
2. for every bounded follow-up session, make the intended working directory explicit and verify it before exploration/editing.

A worktree isolates writes, but the harness/footer must not be treated as proof that commands are operating in the intended directory. Actual command `pwd`, Git status, and deterministic diff inspection remain authoritative.

---

## Routing status after V1.10

### VALIDATED

- Ollama as the local inference runtime;
- Qwen3.8 27B as a capable substantial-feature reasoning/implementation model;
- Qwen3.8 64k + Flash Attention + q8_0 KV on the reference workstation for long coding sessions;
- Pi 0.84.3 overflow recovery/endurance at truthful 64k;
- strict worktree + working-directory anchoring;
- Qwen3.6 35B direct Ollama 64k for bounded read-only adversarial review;
- deterministic tests/Ruff/mypy/CI and explicit human merge authority as the non-negotiable acceptance path;
- defer fine-tuning until runtime/harness/routing comparisons are complete.

### PARTIALLY VALIDATED / RESTRICTED ROLE

- Pi as an implementation harness: capable for long work, but inefficient on small bounded corrections and proactive compaction behavior remains unresolved.

### PROPOSED / NOT YET BENCHMARKED

- OpenCode as primary local coding harness;
- Qwen3-Coder 30B as primary implementation/editor model;
- Aider as precision bounded editor;
- small-model routing for low-risk mechanical work;
- any claim that one harness/model pair should be the universal primary stack.

---

## Next controlled benchmark

The next comparison should focus on the task class where Pi/Qwen3.8 showed the clearest weakness: a **small bounded repository edit**.

Use the same acceptance contract and compare at least two of:

- Pi + Qwen3.8 64k;
- Qwen3-Coder 30B through a suitable harness;
- Aider;
- OpenCode with Qwen3.8 or Qwen3-Coder.

Record:

- exact starting ref/worktree;
- files inspected;
- tool calls / iterations where available;
- cumulative token/context use where available;
- elapsed time where practical;
- deterministic test result;
- human corrections required;
- whether the agent stayed within scope.

The objective is not to prove one tool universally superior. It is to establish task-shape routing with the lowest correction burden and friction while preserving deterministic correctness.

---

## Current decision

Do not fine-tune and do not replace Qwen3.8 based on the original V1.10 failures.

The validated working interpretation is now:

```text
cloud reasoning / architecture / arbitration
        +
local Qwen3.8 for substantial high-volume implementation
        +
direct Qwen3.6 for bounded adversarial review
        +
deterministic quality gate and CI
        +
human merge authority
```

Pi remains useful but task-shape sensitive. The primary bounded-editor/harness decision remains open pending OpenCode/Qwen3-Coder/Aider comparison.
