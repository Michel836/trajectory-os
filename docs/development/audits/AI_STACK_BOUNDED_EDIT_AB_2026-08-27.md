# TrajectoryOS bounded-edit A/B benchmark — 2026-08-27

Tracked by Issue #36 and draft PR #37.

## Purpose

Compare two local models under the same coding-agent harness on a deliberately tiny, deterministic repository regression, while holding the task contract, starting Git ref, repository mutation, context window, validation path, and human intervention constant.

The goal is task-shape routing, not a universal model ranking.

Only summarized non-sensitive evidence is committed. Raw JSONL and timing logs remain local.

---

## Experimental design

Authoritative starting commit:

```text
474c607a3c707c3e4c6ed3e23b0f62ef069279f5
feat(v1.10): add durable planned-effort estimates (#38)
```

Two detached worktrees were created from the exact same commit.

The same one-character semantic regression was injected into both:

```diff
-    if duration_seconds < 0:
+    if duration_seconds <= 0:
```

This deliberately made an explicit zero planned-effort estimate invalid, contradicting the V1.10 contract.

Baseline evidence before either model ran:

- both mutated source files had the same SHA-256: `243489c0a716f39a65280bb80e789cebcb2495fec1d67ebd7e641bff2920ee49`;
- both worktree diffs were byte-identical;
- both focused tests failed with exit code `1`;
- both failed with the same exception: `duration_seconds must be >= 0; got 0`;
- the task prompt was identical for both runs, SHA-256 `df3295ef7f972a682d0f8a1dd93520122bc052b38ae718a678cccf1de920e5b0`;
- no human edit was made during either run.

The fixed prompt required each agent to:

1. work only in the current worktree;
2. read and obey `AGENTS.md`;
3. diagnose before editing;
4. inspect only necessary files;
5. leave tests unchanged;
6. make the smallest correct production-code change;
7. run the focused failing test;
8. run `bash scripts/quality.sh`;
9. run `git diff --check` and `git status --short`;
10. avoid commit, push, merge, and branch changes.

Harness for both runs:

```text
Pi 0.84.3
--mode json
--no-session
Ollama local provider
truthful 65536-token model context
```

---

## Run A — Pi + Qwen3.8 27B 64k, reasoning MEDIUM

Runtime profile:

- model: `qwen3.8-dev64`;
- effective context: `65536`;
- sampled placement after warm-up: approximately `89% GPU / 11% CPU`;
- explicit reasoning: `medium`.

Result:

- diagnosis: correct;
- production edit: exactly `<= 0` back to `< 0`;
- tests modified: none;
- focused test: passed;
- complete planned-effort unit file sanity: 31 passed, as reported by the agent;
- canonical quality gate: 691 tests passed, Ruff clean, mypy clean on 36 source files;
- `git diff --check`: clean;
- final worktree: clean and identical to HEAD;
- human correction: none;
- compaction: none.

Measured Pi/JSONL evidence:

| Metric | Qwen3.8 |
|---|---:|
| Wall time | 103.18 s |
| Assistant turns | 11 |
| Tool calls | 13 |
| Cumulative input tokens | 54,748 |
| Output tokens | 2,820 |
| Total cumulative tokens | 57,568 |
| Compactions | 0 |
| Auto-retries | 0 observed |

Interpretation:

Qwen3.8 was fully correct, scope-disciplined, and stable. For a one-character regression, however, 57.6k cumulative tokens and 103 seconds remain substantial overhead.

---

## Run B — Pi + Qwen3-Coder 30B 64k, reasoning OFF

Runtime profile:

- model: `qwen3-coder-dev64`;
- effective context: `65536`;
- sampled placement: approximately `92% GPU / 8% CPU`;
- explicit reasoning: off / model declared non-reasoning.

Result:

- diagnosis: correct;
- production edit: exactly `<= 0` back to `< 0`;
- tests modified: none;
- focused test: passed;
- canonical quality gate: 691 tests passed, Ruff clean, mypy clean;
- `git diff --check`: clean;
- final worktree: clean and identical to HEAD;
- human correction: none;
- compaction: none.

Measured Pi/JSONL evidence:

| Metric | Qwen3-Coder |
|---|---:|
| Wall time | 53.75 s |
| Assistant turns | 20 |
| Tool calls | 17 |
| Cumulative input tokens | 154,797 |
| Output tokens | 1,698 |
| Total cumulative tokens | 156,495 |
| Compactions | 0 |
| `agent_start` events | 3 |
| `auto_retry_start` events | 2 |

Interpretation:

Qwen3-Coder completed the same task correctly in substantially less wall time, but with much greater cumulative context churn and more agent/tool iterations. The JSONL also recorded retries that did not appear in the Qwen3.8 run.

---

## Direct comparison

Both models achieved the same deterministic quality outcome with no human repair.

Relative to Qwen3.8, Qwen3-Coder:

- reduced wall time by **49.43 seconds**, or approximately **47.9%**;
- used **100,049 more cumulative input tokens**, approximately **182.7% more**;
- used **98,927 more total cumulative tokens**, approximately **171.8% more**;
- produced **1,122 fewer output tokens**, approximately **39.8% less**;
- used **9 more assistant turns**, approximately **81.8% more**;
- used **4 more tool calls**, approximately **30.8% more**;
- triggered two recorded auto-retry starts, versus none observed for Qwen3.8.

Equivalently:

```text
Qwen3-Coder wall time ≈ 0.52 × Qwen3.8
Qwen3-Coder cumulative tokens ≈ 2.72 × Qwen3.8
Qwen3-Coder cumulative input ≈ 2.83 × Qwen3.8
```

Because Pi's cumulative token accounting includes context repeatedly supplied across turns, these numbers must not be misread as raw model-generation throughput. They measure practical agent-session context cost under the tested harness.

---

## Benchmark conclusion

There is no single winner across all dimensions.

### Qwen3-Coder wins this task on latency

For this bounded one-line repair, Qwen3-Coder completed the full deterministic workflow in roughly half the wall time while preserving perfect correctness.

This validates Qwen3-Coder 30B as a **capable bounded code-repair model** on the reference workstation.

### Qwen3.8 wins this task on context efficiency and run stability

Qwen3.8 required far fewer cumulative tokens, fewer turns, fewer tool calls, and no recorded retry cycle.

This reinforces its value when context efficiency, predictable tool flow, or reduced risk of long-session context pressure matters.

### Pi remains the unresolved efficiency variable

The comparison deliberately held Pi constant. The result therefore does not prove that Qwen3-Coder itself inherently requires 2.7× more context for bounded edits.

The excess cumulative input may arise from model behavior, Pi's interaction pattern with this model, retry behavior, or a combination of those factors.

Therefore:

> Qwen3-Coder is validated for bounded repair quality and latency under Pi, but Pi + Qwen3-Coder is not validated as the most context-efficient bounded-edit stack.

A harness comparison remains necessary before selecting a default bounded editor.

---

## Updated routing interpretation

### VALIDATED

- Qwen3.8 27B + truthful 64k runtime for substantial reasoning/implementation work;
- Qwen3.8 can also perform bounded repairs correctly with strong context discipline, though not with minimum latency;
- Qwen3-Coder 30B + truthful 64k runtime is viable on the reference workstation;
- Qwen3-Coder 30B can perform a bounded repository repair with perfect deterministic quality and approximately half the wall time of Qwen3.8 in this controlled test;
- Qwen3.6 35B direct Ollama remains validated for bounded read-only adversarial review;
- deterministic tests, Ruff, mypy, diff checks, worktree isolation, and human merge authority remain mandatory.

### PARTIALLY VALIDATED / RESTRICTED

- Pi 0.84.3 as a bounded-edit harness: functionally capable, but task/model combinations can incur large cumulative context overhead;
- Pi + Qwen3-Coder: latency-strong in this test, but context-inefficient and observed to retry;
- Pi + Qwen3.8: stable and more context-efficient here, but slower than Qwen3-Coder.

### STILL PROPOSED / NOT YET BENCHMARKED

- OpenCode as a primary local coding harness;
- Aider as a precision bounded editor;
- Qwen3-Coder under a lower-overhead harness;
- small-model routing for low-risk mechanical work;
- any universal single-model or single-harness default.

---

## Next experiment

The highest-value next experiment is now **harness isolation**, not another model-only comparison.

Use the same bounded regression and acceptance contract with Qwen3-Coder while changing only the editing harness, for example:

```text
Pi + Qwen3-Coder
        vs
Aider + Qwen3-Coder
        and/or
OpenCode + Qwen3-Coder
```

Measure the same evidence:

- wall time;
- cumulative context/tokens where observable;
- tool/iteration count;
- deterministic test and quality-gate result;
- scope discipline;
- retries/errors;
- human correction burden.

This will determine whether Qwen3-Coder's 2.7× cumulative-token cost in this A/B test is primarily model behavior or Pi harness overhead.
