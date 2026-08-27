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
| J | Pi 0.84.3 | Qwen3.8 27B | medium | **736.39 s** | **PASS** |

Token accounting differs by harness and must not be read as raw model throughput. It is retained only as operational context.

## OpenCode + Qwen3-Coder

### Tool-call configuration incident

The initial OpenCode attempt did not enter an agent loop. Qwen3-Coder emitted a textual representation of a tool call and OpenCode terminated the turn as text.

Direct non-streaming and streaming calls against Ollama's OpenAI-compatible endpoint both returned correct structured `tool_calls`, proving that Qwen3-Coder and Ollama were capable of standard tool calling.

Adding the model metadata:

```json
"tool_call": true
```

to the OpenCode custom-model configuration repaired the tool loop. A read-only smoke test then executed `pwd` as a real tool call and continued correctly.

This configuration requirement is reusable operational knowledge for the tested OpenCode 1.18.23 custom-model setup.

### G1 feature result

After repairing the tool loop, OpenCode genuinely attempted the feature:

- elapsed: **1084.74 s**;
- 131 model steps;
- 128 tool calls;
- ~4.165M cumulative step tokens;
- all five requested surfaces were created or modified;
- `git diff --check` was clean.

The generated domain module nevertheless contained an invalid Pydantic annotation equivalent to:

```python
Annotated[StrictInt] | None
```

which failed import/collection. The hidden acceptance failed and the canonical quality gate failed with collection errors.

The final OpenCode message nevertheless claimed that all tests passed. That handoff was contradicted by deterministic evidence.

**Conclusion:** OpenCode + Qwen3-Coder is not validated for this additive feature. The false-success handoff is itself a material reliability signal.

## Aider + Qwen3-Coder

Run H used the same frozen contract with the five editable surfaces explicitly scoped and relevant existing implementation/tests supplied read-only.

Observed result:

- elapsed: **554.04 s**;
- approximately **205.8k** reported sent+received tokens across the displayed interactions;
- all five requested surfaces were created or modified;
- `git diff --check` was clean;
- hidden acceptance failed;
- canonical quality gate failed with **16 failed, 699 passed**.

The production model constrained `variance_seconds >= 0`, directly contradicting the contract requirement that negative variance be valid when actual effort is below planned effort.

Aider also generated invalid test fixtures using ad-hoc objects where concrete Pydantic `WorkBreakdownEffortPlanItem` instances were required. It recognized this problem late, but recovery then failed because the model did not conform to Aider's SEARCH/REPLACE edit format.

**Conclusion:** explicit file scoping remains valuable, but Aider + Qwen3-Coder is not validated for this substantive additive feature.

## Pi + Qwen3-Coder

Run I held the harness fixed at Pi 0.84.3 and used Qwen3-Coder with thinking disabled.

Observed result:

- elapsed: **117.06 s**;
- 13 assistant messages;
- 9 tool calls;
- 75,233 cumulative input tokens;
- 2,856 output tokens;
- 78,089 cumulative total tokens;
- 3 retry starts and only 1 retry end;
- zero compactions;
- only the two new implementation modules were created;
- public exports and both required test files were never completed.

The last assistant text was effectively a continuation marker stating that `application/__init__.py` would be updated next. The run then ended.

External hidden acceptance failed immediately because the public durable function was not exported. The quality gate also failed on Ruff violations in the partial implementation.

**Conclusion:** Pi + Qwen3-Coder was fast only because the task was incomplete. It remains validated for prior bounded repair/navigation benchmarks, not for this substantive additive feature.

## Pi + Qwen3.8 — PASS

Run J changed one major variable from Run I: the Pi harness, commit, frozen feature prompt, hidden acceptance, machine, runtime family, 64k context target, and deterministic gates remained the same, while the model changed from Qwen3-Coder 30B to Qwen3.8 27B with `--thinking medium`.

Observed result:

- elapsed: **736.39 s**;
- user CPU: 5.17 s;
- system CPU: 0.71 s;
- maximum harness RSS: 119,136 KB;
- 34 assistant messages;
- 42 tool calls:
  - 23 `bash`;
  - 11 `edit`;
  - 4 `read`;
  - 4 `write`;
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

The feature added **35 tests** above the 691-test baseline.

The final handoff accurately described the task, files, design, tests, quality-gate result, Git state, and limitations.

### Frozen Run J evidence

The final local evidence was SHA-256 frozen as follows:

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

The stderr hash is the SHA-256 of an empty file.

## Interpretation

The strongest controlled comparison is Pi Run I versus Pi Run J because the harness and task contract were held constant.

Under Pi:

```text
Qwen3-Coder 30B → incomplete FAIL
Qwen3.8 27B     → deterministic PASS
```

This does not prove that Qwen3.8 universally dominates Qwen3-Coder. Earlier controlled benchmarks show the opposite latency result for tiny bounded fixes, where Qwen3-Coder was materially faster and fully correct.

The evidence therefore supports **task-dependent routing** rather than one universal coding model:

- Qwen3-Coder remains the preferred fast model for small bounded repairs and explicitly scoped multi-file fixes;
- Qwen3.8 is now validated under Pi for at least one substantive additive feature requiring deeper semantic reasoning, test design, repeated validation, and long-session endurance;
- successful Pi compaction during Run J is positive evidence for long-session continuity;
- deterministic gates remain authoritative because OpenCode G1 demonstrated that a fluent final handoff can contradict actual test state.

## Provisional routing after Run J

```text
architecture / arbitration / current external research
    → cloud reasoning when marginal value justifies it

small bounded edit
    → Aider + Qwen3-Coder

bounded multi-file repair with known target files
    → explicitly file-scoped Aider + Qwen3-Coder

broader bounded repair/navigation
    → Pi + Qwen3-Coder, where prior benchmark evidence applies

substantive additive feature / deeper local reasoning
    → Pi + Qwen3.8, validated by Run J

bounded read-only adversarial review
    → Qwen3.6 35B

all accepted implementation work
    → deterministic quality gate + CI + human merge authority
```

This routing is evidence-based but still subject to further controlled comparison.

## Next discriminating experiment

The highest-value remaining comparison is to hold **Qwen3.8 27B** and the feature contract constant while changing only the harness:

```text
Pi + Qwen3.8
vs
Aider + Qwen3.8
```

If explicitly scoped Aider + Qwen3.8 also passes, latency/context efficiency can decide the preferred feature harness. If it fails while Pi continues to pass, the evidence for the Pi + Qwen3.8 pairing becomes materially stronger.

Do not repeat additional Qwen3-Coder runs on this exact feature merely to accumulate samples without changing a meaningful variable.
