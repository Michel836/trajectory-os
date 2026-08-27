# TrajectoryOS multi-file harness benchmark — Aider vs Pi with Qwen3-Coder — 2026-08-27

Tracked by Issue #36 and draft PR #37.

## Purpose

Test whether the bounded-edit advantage previously observed for Aider + Qwen3-Coder extends beyond a one-line repair to a small controlled two-file repair, while keeping the model and effective Ollama context constant.

This benchmark deliberately separates two Aider configurations:

- **E0 — unanchored Aider**: repo-map plus prompt-level instructions only;
- **E1 — explicitly file-scoped Aider**: the two production files are passed as editable files and the relevant tests/rules are passed read-only.

The final harness comparison uses E1 against Pi because both receive the same anchored task contract.

Only summarized non-sensitive evidence is committed. Raw JSONL, LLM history, timing files, hashes, and caches remain local.

---

## Controlled starting state

Starting commit:

```text
474c607a3c707c3e4c6ed3e23b0f62ef069279f5
feat(v1.10): add durable planned-effort estimates (#38)
```

Two deliberate semantic regressions were injected into two production files.

### Regression 1 — zero planned effort rejected

File:

```text
src/trajectory_os/domain/execution_effort_estimates.py
```

Mutation:

```diff
-    if duration_seconds < 0:
+    if duration_seconds <= 0:
```

This incorrectly rejects zero even though zero is explicitly valid and meaningful.

### Regression 2 — observation count doubled

File:

```text
src/trajectory_os/domain/execution_effort_measurement.py
```

Mutation:

```diff
-        self.observation_count += 1
+        self.observation_count += 2
```

This preserves duration arithmetic while doubling the observation count.

The Aider and Pi starting diffs were byte-identical.

Before either successful run, the two focused tests failed deterministically:

```text
test_zero_direct_effort_is_valid_and_meaningful
test_multiple_direct_observations_sum_exact_integer_seconds
```

Observed baseline result:

```text
2 failed
```

---

## Anchored task contract

The successful E1 and Pi runs used the same prompt with SHA-256:

```text
d77d658beb93269c4c13c857e23ed98b68461be0258e55b327c75aa3fe5e600a
```

The contract explicitly stated that the repository uses a `src/` layout and that only these two production files were authorized for editing:

```text
src/trajectory_os/domain/execution_effort_estimates.py
src/trajectory_os/domain/execution_effort_measurement.py
```

The relevant unit tests and repository rules were read-only evidence. No file creation, test modification, staging, commit, push, merge, or branch change was allowed.

---

## E0 — Aider + Qwen3-Coder without explicit file anchoring — FAIL

Harness:

```text
Aider 0.86.2
```

Model:

```text
openai/qwen3-coder-dev64
```

Observed result:

- elapsed time: **265.87 s**;
- approximate reported sent tokens: ~63.5k;
- approximate reported received tokens: ~13.6k;
- approximate reported total: **~77.1k**;
- both focused tests still failed after the run;
- the two injected regressions remained present in the real `src/` files;
- Aider/Qwen3-Coder created two unintended files under `trajectory_os/domain/` outside the repository's `src/` package layout;
- the model attempted SEARCH/REPLACE blocks against tests despite the prompt forbidding test modification;
- Aider reported edit-format mismatches;
- `.aider.tags.cache.v4/` was also generated;
- post-run deterministic result remained `2 failed`.

This is a real harness/configuration failure and is preserved as evidence rather than discarded.

Interpretation:

> Aider + Qwen3-Coder must not be treated as a reliable multi-file repository agent when only repo-map discovery and natural-language scope instructions are provided.

---

## E1 — explicitly file-scoped Aider + Qwen3-Coder — PASS

The E0 worktree was reset to the exact starting commit and the same two regressions were re-injected. The resulting baseline diff was byte-identical to E0.

Aider was then given:

- both authorized production files explicitly as editable files;
- `AGENTS.md`, `scripts/quality.sh`, and both focused test files explicitly as read-only context;
- the anchored prompt above;
- the same `qwen3-coder-dev64` model and 65536-context Ollama runtime.

Observed result:

```text
elapsed_seconds=50.31
```

Aider-reported model interactions:

```text
Tokens: 15k sent, 948 received.
Tokens: 23k sent, 488 received.
```

Approximate totals:

- sent: ~38,000;
- received: 1,436;
- sent + received: **~39,436**.

Correctness:

- both independent regressions were diagnosed;
- both minimal production fixes were applied;
- focused tests: `2 passed`;
- canonical quality gate: `691 passed`;
- Ruff: all checks passed;
- mypy: no issues in 36 source files;
- `git diff --check`: clean;
- production code returned exactly to committed HEAD because the injected regressions were repaired;
- no unintended files outside `src/` were created;
- no human code repair was required.

Aider again left the known harness-local cache:

```text
.aider.tags.cache.v4/
```

Therefore the code state was exact, while the worktree was not byte-empty solely because of the known Aider cache artifact.

Compared with E0, explicit file anchoring reduced elapsed time from 265.87 s to 50.31 s — about **81.1% lower wall time** — and reduced approximate reported token traffic from ~77.1k to ~39.4k.

Interpretation:

> For small multi-file repairs, explicit file scoping is not merely an optimization for Aider + Qwen3-Coder; on this task it was the difference between failure and deterministic success.

---

## F — Pi + Qwen3-Coder with the same anchored contract — PASS

Harness:

```text
Pi 0.84.3
```

Model:

```text
qwen3-coder-dev64
```

Reasoning mode:

```text
off
```

Observed runtime:

- effective context: `65536`;
- processor placement after warm-up: approximately `93% GPU / 7% CPU`;
- sampled GPU memory after completion: about 23.1 GiB of 24 GiB.

Measured result:

```text
elapsed_seconds=64.65
user_cpu_seconds=5.66
system_cpu_seconds=0.49
max_rss_kb=243492
```

Pi JSONL metrics:

```text
events             = 1433
assistant_messages = 27
tool_calls         = 26
compaction_start   = 0
compaction_end     = 0
auto_retry_start   = 0
auto_retry_end     = 0
input              = 416136
output             = 3002
cacheRead          = 0
cacheWrite         = 0
totalTokens        = 419138
```

Correctness and discipline:

- both regressions were diagnosed correctly;
- both minimal production fixes were applied;
- both focused tests passed;
- canonical quality gate passed with all 691 tests, Ruff, and mypy;
- no human repair was required;
- final worktree was completely clean;
- final diff was empty;
- no cache or unintended files were left;
- no compaction occurred;
- no automatic retry occurred.

---

## Direct E1 vs Pi comparison

Both successful runs used the same model, the same initial two-file regression state, the same anchored prompt, the same deterministic tests, and the same quality gate.

| Stack | Result | Wall time | Token/context evidence | Final hygiene |
|---|---|---:|---:|---|
| Aider unanchored + Qwen3-Coder (E0) | FAIL | 265.87 s | ~77.1k reported | out-of-scope files + cache |
| **Aider file-scoped + Qwen3-Coder (E1)** | **PASS** | **50.31 s** | **~39.4k reported** | code exact; Aider cache remains |
| **Pi + Qwen3-Coder (F)** | **PASS** | **64.65 s** | **419,138 cumulative JSONL** | completely clean |

### Timing

Relative to Pi, file-scoped Aider was approximately **22.2% faster**:

```text
64.65 s -> 50.31 s
```

Equivalently, Pi took about **1.29x** Aider's wall time on this task.

### Context / token traffic

Aider reported ~39.4k sent+received tokens, while Pi's cumulative JSONL usage was 419,138 tokens.

These accounting systems are not equivalent: Aider's displayed sent-token figures are rounded, while Pi sums repeated-context usage across turns. Therefore this must not be presented as a raw model-throughput comparison.

Even with that caveat, the practical harness difference is very large: Pi's cumulative session accounting is about **10.6x** Aider's reported total. This reinforces the earlier finding that Pi can impose substantial repeated-context overhead on bounded work.

### Workflow discipline

Pi had the stronger final filesystem hygiene:

- completely clean worktree;
- no harness cache;
- no unexpected files;
- no retries;
- no compaction.

Aider had the stronger latency/context profile only when explicitly file-scoped.

---

## Updated operational interpretation

### VALIDATED

**Aider + Qwen3-Coder** is validated for:

- one-file bounded repairs;
- explicitly file-scoped small multi-file repairs;
- edit -> deterministic tests -> full quality gate workflows.

**Pi + Qwen3-Coder** is validated for:

- the same bounded two-file repair contract;
- autonomous repository navigation with correct scope under the anchored prompt;
- completely clean final worktree;
- deterministic success without retries or compaction.

### NOT VALIDATED

**Unanchored Aider + Qwen3-Coder** is not validated for general multi-file repository repair. E0 failed materially.

### Routing implication

```text
Cloud reasoning / architecture / arbitration
        +
Qwen3.8 27B for substantial reasoning, repository understanding,
complex debugging, and long implementation
        +
Aider + Qwen3-Coder for small bounded edits when the target files can be scoped explicitly
        +
Pi + Qwen3-Coder when broader repository navigation/autonomy is required and
higher context overhead is acceptable
        +
Qwen3.6 35B direct Ollama for bounded read-only adversarial review
        +
deterministic quality gates and CI
        +
human merge authority
```

This is a task-specific routing policy, not a universal model or harness ranking.

---

## Next experiment

The next useful experiment should not repeat another tiny synthetic regression.

Two high-value options remain:

1. benchmark a **small additive feature** requiring a new or modified test and two to three production files, comparing file-scoped Aider vs Pi with Qwen3-Coder; or
2. install and benchmark **OpenCode + Qwen3-Coder** on the same small additive feature to test whether it can combine Aider-like efficiency with stronger autonomous repository navigation.

Fine-tuning remains deferred.
