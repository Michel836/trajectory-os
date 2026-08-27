# TrajectoryOS bounded-edit harness benchmark — Aider + Qwen3.8 — 2026-08-27

Tracked by Issue #36 and draft PR #37.

## Purpose

Complete the controlled 2x2 harness/model matrix for the same one-character TrajectoryOS regression by measuring Aider 0.86.2 with the already-validated Qwen3.8 27B 64k runtime.

The starting Git ref, injected mutation, prompt, focused test, canonical quality gate, Aider configuration, and absence of human code repair were held constant with the prior bounded-edit runs.

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

Evidence before the run:

- mutated source SHA-256: `243489c0a716f39a65280bb80e789cebcb2495fec1d67ebd7e641bff2920ee49`;
- prompt SHA-256: `df3295ef7f972a682d0f8a1dd93520122bc052b38ae718a678cccf1de920e5b0`;
- focused baseline exit code: `1`;
- baseline exception: `duration_seconds must be >= 0; got 0`;
- no human code edit during the run.

---

## Harness and runtime

Harness:

```text
Aider 0.86.2
```

Model path:

```text
openai/qwen3.8-dev64
```

Aider connected through Ollama's local OpenAI-compatible API.

Observed Ollama runtime:

- effective context: `65536`;
- processor placement: approximately `89% GPU / 11% CPU`;
- sampled GPU memory after completion: about 22.6 GiB of 24 GiB.

As with the Qwen3-Coder Aider run, Aider warned that the custom alias had unknown context/cost metadata and used internal sane defaults. Ollama's measured runtime remained 65536.

Aider repo-map budget: `1024 tokens`.

---

## Result

Qwen3.8 diagnosed the same defect correctly and Aider applied the minimal repair:

```diff
-    if duration_seconds <= 0:
+    if duration_seconds < 0:
```

Deterministic evidence:

- focused test: `1 passed in 0.05s`;
- canonical quality gate: `691 passed`;
- Ruff: all checks passed;
- mypy: no issues in 36 source files;
- `git diff --check`: clean;
- production source returned exactly to HEAD;
- tests modified: none;
- human correction: none.

Aider again left an untracked cache directory:

```text
.aider.tags.cache.v4/
```

Therefore the code state was clean while the worktree retained a harness-local cache artifact.

After the deterministic work had completed, Aider also emitted:

```text
Summarization failed for model openai/qwen3.8-dev64: cannot schedule new futures after shutdown
summarizer unexpectedly failed for all models
```

This did not affect the edit or quality-gate result, but it is a harness/model stability signal and should be retained as evidence rather than hidden.

---

## Timing and token evidence

Measured wall time:

```text
85.73 s
```

Aider reported two model interactions:

```text
Tokens: 4.8k sent, 450 received.
Tokens: 9.3k sent, 970 received.
```

Approximate totals:

- sent: ~14,100 tokens;
- received: 1,420 tokens;
- sent + received: ~15,520 tokens.

The sent values are rounded by Aider, and Aider's accounting is not identical to Pi JSONL cumulative-session accounting. Use these figures for practical order-of-magnitude comparison only.

---

## Completed 2x2 matrix

All four runs repaired the same regression and passed the same deterministic acceptance path without human code repair.

| Harness | Qwen3.8 27B | Qwen3-Coder 30B |
|---|---:|---:|
| Pi 0.84.3 | 103.18 s | 53.75 s |
| Aider 0.86.2 | 85.73 s | **26.93 s** |

### Harness effect at constant model

Qwen3.8:

- Pi: 103.18 s;
- Aider: 85.73 s;
- Aider reduced wall time by 17.45 s, approximately **16.9%**.

Qwen3-Coder:

- Pi: 53.75 s;
- Aider: 26.93 s;
- Aider reduced wall time by 26.82 s, approximately **49.9%**.

### Model effect at constant harness

Under Pi:

- Qwen3-Coder was approximately **47.9% faster** than Qwen3.8.

Under Aider:

- Qwen3-Coder was approximately **68.6% faster** than Qwen3.8;
- Aider + Qwen3-Coder used about `0.31x` the wall time of Aider + Qwen3.8.

### Aider-reported context traffic

- Aider + Qwen3.8: ~15.5k sent+received;
- Aider + Qwen3-Coder: ~20.8k sent+received.

Under Aider, Qwen3.8 therefore used roughly **25% less reported token traffic** than Qwen3-Coder while taking much longer to finish.

This separates two dimensions clearly: Qwen3.8 is more context-frugal in this bounded task, while Qwen3-Coder is dramatically faster.

---

## Interpretation

### VALIDATED — Qwen3-Coder is the stronger bounded-edit model on latency

The complete matrix now shows the Qwen3-Coder latency advantage under two different harnesses, not only under Pi.

That makes it much less likely that the original speed advantage was a Pi-specific artifact.

For this task shape, Qwen3-Coder is the preferred model when fast deterministic repair is the primary goal.

### VALIDATED — Aider is the stronger bounded-edit harness in this experiment

Aider improved wall time for both models, although the gain was much larger for Qwen3-Coder.

The Aider + Qwen3-Coder combination remains the best observed bounded-edit stack in this experiment: 26.93 s with perfect deterministic quality.

### Qwen3.8 remains context-efficient but slower

Qwen3.8 used less Aider-reported context traffic than Qwen3-Coder, but required 85.73 s and encountered a post-task summarizer failure.

This supports keeping Qwen3.8 in reasoning-heavy, repository-comprehension, debugging, and substantial-implementation roles rather than routing tiny deterministic repairs to it by default.

### Caveats

1. The task is intentionally tiny and cannot establish multi-file or long-horizon superiority.
2. Aider and Pi token accounting methods differ.
3. Aider did not know the custom aliases' context metadata even though Ollama measured 65536.
4. Both Aider runs created `.aider.tags.cache.v4/`; future hygiene should redirect or ignore the cache explicitly.
5. Qwen3.8's post-task summarizer failure did not compromise deterministic correctness but is a real stability observation.

---

## Updated routing implication

```text
Cloud reasoning / architecture / arbitration
        +
Qwen3.8 27B for substantial reasoning, repository comprehension,
complex debugging, and long implementation
        +
Aider + Qwen3-Coder 30B for small bounded edits / repairs
        +
Qwen3.6 35B direct Ollama for bounded read-only adversarial review
        +
deterministic quality gates and CI
        +
human merge authority
```

The next meaningful benchmark should move beyond a one-line repair to a small controlled multi-file task before promoting Aider + Qwen3-Coder to a broader implementation role.