# TrajectoryOS bounded-edit harness benchmark — Aider + Qwen3-Coder — 2026-08-27

Tracked by Issue #36 and draft PR #37.

## Purpose

Measure whether changing the editing harness materially changes the efficiency of the already-validated Qwen3-Coder 30B bounded-repair workflow.

This experiment reuses the same deliberate one-character semantic regression, starting Git ref, prompt, model alias, 64k Ollama runtime, deterministic focused test, and canonical quality gate used in the prior Pi model A/B benchmark.

Only summarized non-sensitive evidence is committed. Raw local stdout, LLM history, timing logs, and caches remain local.

---

## Controlled starting state

Starting commit:

```text
474c607a3c707c3e4c6ed3e23b0f62ef069279f5
feat(v1.10): add durable planned-effort estimates (#38)
```

Injected regression:

```diff
-    if duration_seconds < 0:
+    if duration_seconds <= 0:
```

The mutated source file SHA-256 matched the earlier Pi benchmark exactly:

```text
243489c0a716f39a65280bb80e789cebcb2495fec1d67ebd7e641bff2920ee49
```

The focused test failed before the run with exit code `1` and the same expected exception:

```text
duration_seconds must be >= 0; got 0
```

The benchmark prompt was unchanged from the Pi comparison:

```text
df3295ef7f972a682d0f8a1dd93520122bc052b38ae718a678cccf1de920e5b0
```

No human code edit was made during the run.

---

## Harness and runtime

Harness:

```text
Aider 0.86.2
```

Model path:

```text
openai/qwen3-coder-dev64
```

Aider connected through Ollama's local OpenAI-compatible API.

Observed Ollama runtime after warm-up:

- effective context: `65536`;
- processor placement: approximately `92% GPU / 8% CPU`;
- sampled GPU memory after completion: about 23.1 GiB of 24 GiB.

Aider emitted a model-metadata warning because the custom local alias is not known to its model registry:

```text
Unknown context window size and costs, using sane defaults.
```

This warning does not contradict the measured Ollama runtime context of 65536, but it means Aider's own token-budget heuristics were not fully metadata-aware in this run.

Aider repo-map budget:

```text
1024 tokens
```

Configuration intentionally disabled Aider auto-commits and dirty commits.

---

## Result

Aider diagnosed the exact semantic defect and applied the minimal production-code repair:

```diff
-    if duration_seconds <= 0:
+    if duration_seconds < 0:
```

Deterministic evidence:

- focused test: `1 passed in 0.07s`;
- canonical quality gate: `691 passed`;
- Ruff: all checks passed;
- mypy: no issues in 36 source files;
- `git diff --check`: clean;
- production source returned exactly to HEAD;
- tests modified: none;
- human correction: none.

Aider left one untracked harness cache directory:

```text
.aider.tags.cache.v4/
```

Therefore the correct conclusion is:

> the code state returned exactly to HEAD, but the worktree was not completely clean because of an Aider-generated cache artifact.

This artifact is harness-local and does not affect product correctness, but it matters for scope-discipline accounting and should be excluded or redirected in future controlled runs.

---

## Timing and token evidence

Measured wall time:

```text
26.93 s
```

Aider reported three model interactions:

```text
Tokens: 4.5k sent, 131 received.
Tokens: 6.8k sent, 193 received.
Tokens: 8.8k sent, 385 received.
```

Because the sent-token values are rounded to one decimal `k`, only an approximate cumulative input can be reported:

- approximate sent tokens: ~20,100;
- exact reported received tokens: 709;
- approximate sent + received total: ~20,809.

These Aider token figures are not perfectly equivalent to Pi's cumulative JSONL accounting. They are therefore suitable for order-of-magnitude and practical harness-cost comparison, not for claiming sub-percent precision.

---

## Comparison with the two Pi baselines

Prior controlled results:

| Stack | Wall time | Cumulative / reported token total | Quality |
|---|---:|---:|---|
| Pi + Qwen3.8 27B | 103.18 s | 57,568 | PASS |
| Pi + Qwen3-Coder 30B | 53.75 s | 156,495 | PASS |
| Aider + Qwen3-Coder 30B | **26.93 s** | **~20,809** | PASS |

Relative timing:

- Aider + Qwen3-Coder was approximately **49.9% faster** than Pi + Qwen3-Coder;
- Aider + Qwen3-Coder was approximately **73.9% faster** than Pi + Qwen3.8;
- Pi + Qwen3-Coder took about **2.00x** Aider's wall time;
- Pi + Qwen3.8 took about **3.83x** Aider's wall time.

Approximate token comparison, with the accounting caveat above:

- Aider's ~20.8k reported total is about **13.3%** of Pi + Qwen3-Coder's 156.5k cumulative total;
- Aider's ~20.8k reported total is about **36.1%** of Pi + Qwen3.8's 57.6k cumulative total.

The practical evidence is strong enough to conclude that the Pi + Qwen3-Coder session overhead observed in the previous benchmark was not an unavoidable property of Qwen3-Coder itself.

---

## Interpretation

### VALIDATED — Aider + Qwen3-Coder for small bounded repairs

For this task shape, Aider + Qwen3-Coder achieved:

- perfect deterministic correctness;
- minimal one-line production repair;
- no test edits;
- no human repair;
- full 691-test/Ruff/mypy validation;
- roughly half the wall time of Pi + Qwen3-Coder;
- dramatically lower reported context/token traffic.

This is sufficient to promote Aider + Qwen3-Coder to a **validated candidate for small bounded repository repairs** on the reference workstation.

### Pi is a major efficiency variable for this task shape

Holding Qwen3-Coder constant while changing the harness from Pi to Aider materially reduced both elapsed time and observed context traffic.

Therefore the earlier 156k-token Pi result should not be interpreted as Qwen3-Coder inherently requiring extreme context churn.

### Remaining caveats

1. Aider token reporting is rounded and not identical to Pi JSONL cumulative accounting.
2. Aider did not know the custom model alias's context/cost metadata even though Ollama itself ran at 65536.
3. Aider created `.aider.tags.cache.v4/`, so cache handling should be made explicit in future benchmark hygiene.
4. One tiny repair does not establish Aider as the best harness for multi-file or long-horizon implementation.

---

## Updated routing implication

Recommended working interpretation after this run:

```text
Cloud reasoning / architecture / arbitration
        +
Qwen3.8 27B for substantial reasoning and long implementation
        +
Aider + Qwen3-Coder 30B for small bounded edits / repairs
        +
Qwen3.6 35B direct Ollama for bounded read-only adversarial review
        +
deterministic quality gates and CI
        +
human merge authority
```

Pi remains validated for long-session endurance and capable implementation, but it should not be the default harness for tiny bounded corrections when Aider can satisfy the same deterministic contract with much lower friction.

---

## Next experiment

The next highest-value question is whether Aider's advantage persists beyond a one-line repair.

Use a small but non-trivial controlled task requiring two or more files and at least one test change or test addition, while keeping Qwen3-Coder constant.

Compare:

```text
Aider + Qwen3-Coder
vs
Pi + Qwen3-Coder
```

Then evaluate OpenCode only if it is likely to add a distinct benefit such as stronger agentic planning, broader multi-file orchestration, or better long-session context management.
