# AI Stack Feature Harness A/B — 2026-08-27

## Purpose

This audit records the controlled follow-up to the first substantive additive-feature benchmark tracked by Issue #36. The purpose was to hold the successful local model and feature contract constant while comparing two harness configurations on the same TrajectoryOS feature.

No benchmark implementation was committed to the product branch. All feature code remained in detached disposable worktrees.

## Frozen contract

Both successful Qwen3.8 feature runs started from:

```text
474c607a3c707c3e4c6ed3e23b0f62ef069279f5
```

Frozen feature prompt SHA-256:

```text
2b8d06a9ca803050333d7a5d1138392663137e5a95a491b8ff921397fd37a6da
```

Independent hidden-acceptance SHA-256:

```text
b0c39e0bf3842c579a9619ff224de754845ad3fab41654579be5963cee398c91
```

Shared public-validation helper SHA-256 used by the Aider configuration:

```text
5bfd0f4548532eea06862acfd59aab81f3a99f0e7c183e5576ae2f876b3aad5f
```

The feature required five repository surfaces: two implementation modules, application exports, and two unit-test modules. Required semantics included signed planned-vs-actual variance, preservation of incomplete planning uncertainty, exact WBS structural identity, strict/frozen models, load-once durable orchestration, ordered reads, and no persistence writes.

## Configurations

### Run J — Pi + Qwen3.8

- Pi 0.84.3;
- `qwen3.8-dev64`;
- 65,536 effective Ollama context;
- `--thinking medium`;
- repository navigation and validation driven autonomously from the frozen feature prompt.

### Run K — Aider + Qwen3.8

- Aider 0.86.2;
- same `qwen3.8-dev64` alias;
- 65,536 effective Ollama context;
- `--reasoning-effort medium`;
- five authorized feature surfaces explicitly supplied as editable;
- relevant existing implementation/tests supplied read-only;
- deterministic public validation supplied as Aider `--test-cmd`;
- no auto commits or dirty commits.

This is therefore an operational comparison of two best-practice harness configurations with model/task/baseline held constant. It is not a claim that every harness-internal variable was identical: explicit file scoping and test-command integration are native parts of the Aider configuration being evaluated.

## Results

| Run | Harness | Model | Wall time | Hidden acceptance | Canonical gate | Outcome |
|---|---|---|---:|---|---|---|
| J | Pi 0.84.3 | Qwen3.8 27B, medium | 736.39 s | PASS | 726 tests + Ruff + mypy PASS | PASS |
| K | Aider 0.86.2 | Qwen3.8 27B, medium | **534.29 s** | PASS | 724 tests + Ruff + mypy PASS | **PASS** |

Run K was approximately **27.4% faster** in wall time than Run J (534.29 s vs 736.39 s), or Pi took about **1.38x** as long for this feature.

Both runs satisfied the independent acceptance contract without human code repair.

## Run K details

Observed harness metrics:

- elapsed: **534.29 s**;
- user CPU: 10.76 s;
- system CPU: 3.65 s;
- maximum harness RSS: 274,224 KB;
- Aider-reported interactions: 2;
- approximate Aider-reported traffic:
  - 89,000 sent tokens;
  - 16,500 received tokens;
  - 105,500 sent+received total;
- stdout: 112,822 bytes;
- stderr: empty;
- llm-history: 533,929 bytes;
- effective Ollama context: 65,536;
- all five requested feature surfaces created or modified;
- `.aider.tags.cache.v4/` remained as the known harness-local cache artifact.

Deterministic result:

```text
HIDDEN_ACCEPTANCE: PASS
724 tests passed
Ruff: all checks passed
mypy: success in 38 source files
git diff --check: clean
```

The feature added 33 tests above the 691-test baseline. The exact test count differs from Pi Run J (35 added tests), but both independently satisfy the frozen acceptance contract and canonical repository gate.

## Token-accounting caution

Pi Run J accumulated 1,157,347 tokens in its JSONL accounting, while Aider Run K reported approximately 105,500 sent+received tokens across two interactions.

These numbers are **not raw-throughput equivalents** because the harnesses account for context and repeated turns differently. Operationally, however, the result is consistent with the earlier bounded-edit evidence: explicit Aider scoping can materially reduce repeated repository-context overhead when the edit surfaces are known in advance.

## Interpretation

The additive-feature campaign now establishes two separate conclusions.

First, the model matters strongly for substantive work:

```text
Qwen3-Coder under OpenCode  -> FAIL
Qwen3-Coder under Aider     -> FAIL
Qwen3-Coder under Pi        -> incomplete FAIL
Qwen3.8 under Pi            -> PASS
Qwen3.8 under Aider         -> PASS
```

Second, once Qwen3.8 is held constant, the harness configuration matters for efficiency:

```text
known feature surfaces + explicit scope
    -> Aider + Qwen3.8: PASS in 534.29 s

broader autonomous repository navigation
    -> Pi + Qwen3.8: PASS in 736.39 s
```

For this feature class, Aider's explicit scoping and deterministic test-command integration produced the lower measured wall time while preserving correctness.

Pi retains advantages that this A/B does not remove:

- broader autonomous repository navigation;
- clean final filesystem without an Aider tag-cache artifact;
- validated long-session compaction continuity;
- less dependence on knowing the complete editable surface before the run.

## Evidence-based routing after Run K

```text
architecture / arbitration / current external research
    -> cloud reasoning when marginal value justifies it

small bounded edit
    -> Aider + Qwen3-Coder

bounded multi-file repair with known target files
    -> explicitly file-scoped Aider + Qwen3-Coder

substantive additive feature with known editable surfaces
    -> explicitly file-scoped Aider + Qwen3.8, medium reasoning

substantive feature requiring broader autonomous scope discovery/navigation
    -> Pi + Qwen3.8, medium reasoning

broader bounded repair/navigation where the prior repair benchmark applies
    -> Pi + Qwen3-Coder

bounded read-only adversarial review
    -> Qwen3.6 35B

all accepted implementation work
    -> deterministic pytest/Ruff/mypy/CI + human merge authority
```

This routing is task- and scope-dependent. No universal single model/harness is promoted.

## Remaining high-value validation

The first feature benchmark is now sufficiently discriminating that repeating the same feature offers diminishing value. The next useful evidence should change the task class rather than merely repeat a winning configuration.

High-value candidates include:

1. a second substantive feature with less obvious file scope, to test whether Pi's navigation advantage becomes decisive;
2. a refactor-without-regressions benchmark;
3. a hostile-input / adversarial-test-design benchmark;
4. a small-model mechanical-work benchmark.

Fine-tuning remains deferred.