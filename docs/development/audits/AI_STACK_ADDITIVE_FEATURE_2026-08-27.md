# AI Stack Additive-Feature Benchmark — 2026-08-27

## Purpose

This audit records the first controlled TrajectoryOS benchmark that moved beyond synthetic repairs into a small but real additive feature requiring domain design, application orchestration, public exports, new tests, and the canonical deterministic quality gate.

The experiment was tracked by Issue #36 and run only in detached disposable worktrees. No benchmark implementation was committed, pushed, merged, or applied to `main`.

## Frozen task contract

All feature runs started from the exact same TrajectoryOS commit:

```text
474c607a3c707c3e4c6ed3e23b0f62ef069279f5
```

The frozen feature prompt SHA-256 was:

```text
2b8d06a9ca803050333d7a5d1138392663137e5a95a491b8ff921397fd37a6da
```

The independent hidden-acceptance script SHA-256 was:

```text
b0c39e0bf3842c579a9619ff224de754845ad3fab41654579be5963cee398c91
```

The requested feature added a deterministic planned-vs-actual effort comparison layer over the existing V1.10 planned-effort output and V1.9 actual-effort measurement output.

The contract required five repository surfaces:

1. `src/trajectory_os/domain/execution_effort_comparison.py`;
2. `src/trajectory_os/application/execution_effort_comparison.py`;
3. public application exports in `src/trajectory_os/application/__init__.py`;
4. `tests/unit/test_execution_effort_comparison.py`;
5. `tests/unit/test_durable_execution_effort_comparison.py`.

The semantics included signed variance, preservation of planning uncertainty, exact WBS structural alignment, immutable models, strict type handling, load-once durable orchestration, reader ordering, no writes, and deterministic validation.

## Pass criteria

A run counted as PASS only if all of the following held without human code repair:

- the requested feature was materially complete;
- the external hidden acceptance passed;
- the canonical `bash scripts/quality.sh` gate passed;
- `git diff --check` passed;
- no commit, push, merge, staging, or branch mutation occurred.

Harness self-reports never overrode deterministic evidence.

## Results

| Run | Harness | Model | Reasoning | Wall time | Outcome |
|---|---|---|---|---:|---|
| G1 | OpenCode 1.18.23 | Qwen3-Coder 30B | non-reasoning | 1084.74 s | FAIL |
| H | Aider 0.86.2 | Qwen3-Coder 30B | non-reasoning | 554.04 s | FAIL |
| I | Pi 0.84.3 | Qwen3-Coder 30B | off | 117.06 s | FAIL |
| J | Pi 0.84.3 | Qwen3.8 27B | medium | 736.39 s | PASS |
| K | Aider 0.86.2 | Qwen3.8 27B | medium | **534.29 s** | **PASS** |

Token accounting differs by harness and must not be read as raw model throughput. It is retained only as operational context.

## OpenCode + Qwen3-Coder

The initial OpenCode attempt did not enter an agent loop because the custom-model metadata lacked `tool_call: true`. Direct non-streaming and streaming calls against Ollama's OpenAI-compatible endpoint both returned correct structured `tool_calls`. Adding `tool_call: true` repaired the tool loop.

The subsequent feature run genuinely edited all five surfaces but generated invalid Pydantic typing that broke collection. Hidden acceptance and the canonical quality gate failed, while the final handoff incorrectly claimed all tests passed.

**Conclusion:** OpenCode + Qwen3-Coder is not validated for this additive feature. Deterministic evidence overrode the fluent but false-success handoff.

## Aider + Qwen3-Coder

Run H created all five surfaces but violated the signed-variance contract and generated invalid Pydantic test fixtures. Hidden acceptance failed and the canonical quality gate ended with 16 failed, 699 passed. Recovery then hit SEARCH/REPLACE edit-format failures.

**Conclusion:** explicit file scoping remains valuable, but Aider + Qwen3-Coder is not validated for this substantive additive feature.

## Pi + Qwen3-Coder

Run I created only the two new implementation modules, then stopped before public exports or tests. The final text was a continuation marker. Hidden acceptance failed immediately because the public durable function was not exported, and the quality gate also failed on Ruff violations in the partial implementation.

Observed metrics:

- elapsed: 117.06 s;
- 13 assistant messages;
- 9 tool calls;
- 78,089 cumulative tokens;
- 3 retry starts, 1 retry end;
- zero compactions.

**Conclusion:** Pi + Qwen3-Coder remains validated for prior bounded repair/navigation benchmarks, not for this substantive additive feature.

## Pi + Qwen3.8 — Run J PASS

Run J changed the model while holding Pi, the commit, frozen feature prompt, hidden acceptance, runtime family, 64k context target, and deterministic gates constant.

Observed result:

- elapsed: **736.39 s**;
- 34 assistant messages;
- 42 tool calls;
- 1 successful compaction pair;
- 0 retries;
- 1,130,070 cumulative input tokens;
- 27,277 output tokens;
- 1,157,347 cumulative total tokens;
- effective Ollama context: 65,536;
- all five requested surfaces created or modified.

Deterministic result:

```text
HIDDEN_ACCEPTANCE: PASS
726 tests passed
Ruff: all checks passed
mypy: success in 38 source files
git diff --check: clean
```

The feature added 35 tests above the 691-test baseline. The final handoff accurately described the task, files, design, tests, quality-gate result, Git state, and limitations.

### Frozen Run J evidence

```text
8769d299c89fabed0b0cb2bf65569964b1cbc77f2ca5799b6db164e5a2172dd2  feature-pi-qwen38.jsonl
e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  feature-pi-qwen38.stderr
1dd74da02984a9d3a4f2d90e579c86b94e6507fdd1d9cdccca285d9e04e0b363  feature-pi-qwen38.time
becc79fac05dbb11773506965e5678cb5881061d3fba66cae3e03a9a4518c494  feature-pi-qwen38-hidden.txt
2226a1af04f483b4c0777c2217cfd89e88bfc24acdf2db0aa07e789770916a91  feature-pi-qwen38-quality.txt
40a502db275407804343bef7056ea0e516608090c947ca709e2ea221b4e3398b  feature-pi-qwen38-final-status.txt
d3e83ad2579c3d5069845d1b451a4b680584a433054d27deac31a3b240bd11b7  feature-pi-qwen38-tracked.diff
2396fcfacdea2fb2754dae1404d24322ae95554912a7841c071ead858e2d76c3  feature-pi-qwen38-generated-files.tar.gz
```

## Aider + Qwen3.8 — Run K PASS

Run K held the successful Qwen3.8 model, 64k Ollama alias, baseline, feature contract, and hidden acceptance constant while changing to Aider 0.86.2 configured for `--reasoning-effort medium`.

The five feature surfaces were explicitly supplied as editable, relevant existing implementation/tests were supplied read-only, and the shared deterministic public-validation helper was used as Aider's `--test-cmd`.

Observed result:

- elapsed: **534.29 s**;
- user CPU: 10.76 s;
- system CPU: 3.65 s;
- maximum harness RSS: 274,224 KB;
- 2 Aider-reported interactions;
- approximately 89,000 sent tokens + 16,500 received tokens = 105,500 reported total;
- effective Ollama context: 65,536;
- all five requested surfaces created or modified;
- stderr empty;
- known `.aider.tags.cache.v4/` artifact remained.

Deterministic result:

```text
HIDDEN_ACCEPTANCE: PASS
724 tests passed
Ruff: all checks passed
mypy: success in 38 source files
git diff --check: clean
```

The feature added 33 tests above baseline. The exact count differs from Run J, but both satisfy the independent hidden acceptance and canonical quality gate.

Run K was approximately **27.4% faster** than Run J (534.29 s vs 736.39 s). Pi took approximately **1.38x** as long on this known-surface feature.

Token accounting is not directly comparable: Pi's cumulative JSONL accounting and Aider's displayed sent/received totals use different semantics. The lower repeated-context overhead observed under Aider is nevertheless consistent with the earlier bounded-edit results when file scope is known explicitly.

## Interpretation

The additive-feature campaign now supports two distinct conclusions.

### Model selection is task-dependent

Across the tested substantive feature:

```text
Qwen3-Coder + OpenCode -> FAIL
Qwen3-Coder + Aider    -> FAIL
Qwen3-Coder + Pi       -> incomplete FAIL
Qwen3.8 + Pi           -> PASS
Qwen3.8 + Aider        -> PASS
```

This is strong evidence that Qwen3.8 provides materially better end-to-end reliability for this deeper semantic feature class, while earlier bounded-edit evidence still favors Qwen3-Coder for narrow repairs.

### Harness selection depends on scope knowledge

With Qwen3.8 held constant, both Pi and Aider pass. Aider is faster for the tested feature when the editable surfaces are known and can be explicitly scoped. Pi retains advantages when the agent must discover or navigate broader scope autonomously, and Pi demonstrated successful long-session compaction continuity with no Aider-style tag-cache residue.

The comparison is operational rather than a claim that every harness-internal variable was identical: explicit file scoping and test-command integration are native parts of the Aider configuration being evaluated.

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

broader bounded repair/navigation where prior benchmark evidence applies
    -> Pi + Qwen3-Coder

bounded read-only adversarial review
    -> Qwen3.6 35B

all accepted implementation work
    -> deterministic pytest/Ruff/mypy/CI + human merge authority
```

No universal single model/harness is promoted.

## Next validation

Repeating the same additive feature now has diminishing value. The next benchmark should change task class, preferably one where file scope is less obvious so that Aider's scoping advantage and Pi's autonomous-navigation advantage are meaningfully stressed.

High-value candidates include:

1. a second substantive feature with less obvious file scope;
2. a refactor-without-regressions benchmark;
3. a hostile-input / adversarial-test-design benchmark;
4. a small-model mechanical-work benchmark.

Fine-tuning remains deferred.