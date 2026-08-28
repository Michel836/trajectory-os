# AI Stack Scope-Discovery Benchmark — 2026-08-28

## Purpose

Test whether the local substantive-feature routing rule should depend on whether editable file scope is known in advance.

The benchmark intentionally withheld all target file paths. Each agent had to discover the relevant domain/application/test surfaces from the repository itself.

## Frozen benchmark contract

Baseline commit:

`474c607a3c707c3e4c6ed3e23b0f62ef069279f5`

Task: add an atomic batch entity-status-transition capability over the existing single-entity transition behavior, with strict pure-domain semantics, durable orchestration, public exports, tests, and the canonical quality gate.

Frozen local benchmark artifacts:

- prompt SHA-256: `ae7b9ef589cc30c6180c17f14b9acfcf42873b5b6e46f121cbe624f68cf95c62`;
- hidden acceptance SHA-256: `6b928b1459d83ebe84ecc42bd764cc664208ae203e963bc4504beb56b3e71657`;
- public validator SHA-256: `f7a43be194cb4754254fd17ecab557562b64ba32081459f7f4cb6120efac0252`.

The hidden acceptance was never supplied to the agents.

## Runs

| Run | Harness | Git mode | Wall time | Hidden acceptance | Canonical gate | Real Git index | Classification |
|---|---|---|---:|---|---|---|---|
| L | Pi 0.84.3 + Qwen3.8 medium | normal | 799.70 s | PASS | 725 tests + Ruff + mypy PASS | clean | PASS |
| M | Aider 0.86.2 + Qwen3.8 medium | native Git | 454.62 s | PASS | 721 tests + Ruff + mypy PASS | modified by empty-file staging | functional PASS / workflow-contract FAIL |
| M1 | Aider 0.86.2 + Qwen3.8 medium | disposable `GIT_INDEX_FILE` | 796.33 s | PASS | 716 tests + Ruff + mypy PASS | byte-for-byte unchanged | PASS |
| M2 | Aider 0.86.2 + Qwen3.8 medium | disposable `GIT_INDEX_FILE` | 662.73 s | FAIL | 8 failed / 708 passed | byte-for-byte unchanged | feature FAIL / failure-aware termination |
| M3 | Aider 0.86.2 + Qwen3.8 medium | disposable `GIT_INDEX_FILE` | 531.22 s | PASS | 19 failed / 701 passed | byte-for-byte unchanged | hidden semantics PASS / self-test generation FAIL / overall FAIL |
| H1 | Pi read-only discovery → scoped Aider + Qwen3.8 | disposable `GIT_INDEX_FILE` for Aider | 69.17 s + 597.59 s = 666.76 s | PASS | 14 failed / 704 passed | byte-for-byte unchanged | hybrid FAIL / self-test fixture failure |

## Scope-discovery findings

### Pi

Run L discovered a correct six-file implementation/testing scope without any target paths in the prompt and completed the full deterministic gate successfully.

This validates Pi + Qwen3.8 medium for at least one substantive feature requiring autonomous repository navigation and scope discovery.

### Aider

Aider also demonstrated real scope-discovery capability. Runs M and M1 found coherent public domain/application/test surfaces without explicit file scoping and passed hidden acceptance plus the canonical gate.

Therefore the earlier hypothesis that Aider intrinsically requires pre-known file scope for substantive work is too strong.

However, reproducibility was materially weaker in free-scope mode:

- M1: PASS;
- M2: overall FAIL;
- M3: overall FAIL.

Among the three disposable-index replications, only 1/3 passed the full canonical gate. The sample is small and must not be interpreted as a population failure rate, but it is sufficient to reject automatic promotion of unscoped Aider as the default substantive-feature route.

## Hybrid H1 finding

H1 tested whether scope discovery and implementation should be split between harnesses rather than asking Aider to discover and implement in one run.

Pi performed read-only discovery in **69.17 s**, exited cleanly, made no repository changes, and selected exactly these six editable surfaces:

- `src/trajectory_os/domain/__init__.py`;
- `src/trajectory_os/domain/entity_status_transition_batch.py`;
- `src/trajectory_os/application/__init__.py`;
- `src/trajectory_os/application/entity_status_transition_batch.py`;
- `tests/unit/test_entity_status_transition_batch.py`;
- `tests/unit/test_durable_entity_status_transition_batch.py`.

The scope was therefore not the problem. Scoped Aider then completed in **597.59 s**, preserved the real Git index through the disposable-index wrapper, passed the hidden acceptance, and passed `git diff --check`, but the independent canonical gate failed with **14 failed / 704 passed**.

The failures came from Aider-authored tests constructing `Portfolio` fixtures without the required `name` field. The underlying implementation still satisfied the hidden semantics.

Nominal combined H1 wall time was **666.76 s**, approximately **16.6% faster** than Pi-only Run L at 799.70 s. That latency advantage is not operationally useful because the canonical gate rejected the result.

H1 therefore shows that separating discovery from implementation does not solve the recurring Aider self-test-generation reliability problem for this task class. The additional orchestration is not justified as a default workflow.

Decision: **do not run H2**. Close this synthetic benchmark campaign and return to real TrajectoryOS work.

## Git-index finding

Native Aider creation of new files staged empty blobs in the real index before filling the working-tree files. The staged blob was the canonical empty-file blob:

`e69de29bb2d1d6434b8b29ae775ad8c2e48c5391`

A disposable Git index initialized from `HEAD` successfully isolated that harness behavior:

```text
GIT_INDEX_FILE=<disposable-index>
git read-tree HEAD
```

M1, M2, M3, and H1 all left the real worktree index byte-for-byte unchanged while Aider continued to operate with Git/repo-map behavior isolated from the authoritative index.

This validates disposable-index isolation as an effective Git-hygiene control when Aider repository mapping/discovery is desired.

## Failure analysis

### M2

M2 chose an incomplete scope, failed hidden acceptance because the required public application export was absent, and generated tests using nonexistent `EntityStatus.BLOCKED` values.

Aider observed the failing quality gate, corrected an earlier fixture error, then stopped after its configured reflection limit. It did not falsely claim success.

Classification: **feature FAIL / failure-aware termination**.

### M3

M3 satisfied the independent hidden acceptance, which indicates that the requested public behavior was materially present, but its own generated tests used nonexistent `SourceKind.HUMAN` values and caused 19 canonical-gate failures.

Classification: **hidden semantics PASS / self-test generation FAIL / overall FAIL**.

### H1

H1 also satisfied hidden acceptance but failed because self-authored fixtures omitted the required `Portfolio.name` field. Since Pi had already discovered the correct editable scope, this failure cannot reasonably be attributed to scope discovery.

Classification: **hidden semantics PASS / self-test fixture FAIL / overall FAIL**.

These failures reinforce that agent-authored tests are not authoritative merely because the implementation itself appears semantically correct.

## Performance observations

Aider-reported token accounting is approximate and is not directly comparable to Pi JSONL accounting.

For the Aider free-scope runs:

| Run | Interactions | Reported total tokens | Wall time | Reported tokens/s proxy |
|---|---:|---:|---:|---:|
| M | 3 | 94,800 | 454.62 s | 208.53 |
| M1 | 4 | 116,841 | 796.33 s | 146.72 |
| M2 | 4 | 142,779 | 662.73 s | 215.44 |
| M3 | 3 | 88,868 | 531.22 s | 167.29 |

The disposable index is not intrinsically responsible for M1's slower run: M2 used the same isolation mechanism while achieving the best reported-tokens/s proxy of the four Aider runs.

Observed latency variance is therefore dominated by agent/model trajectory and runtime variability rather than by a demonstrated deterministic cost of `GIT_INDEX_FILE`.

H1 adds a separate **69.17 s** discovery stage and still fails the canonical gate. Its nominal total of **666.76 s** is faster than Pi L, but a failed quality gate makes that speed advantage irrelevant for production routing.

## Routing conclusion

Validated working routing after S1 and H1:

```text
substantive feature + editable surfaces known
    → Aider + Qwen3.8 medium

substantive feature + scope must be discovered autonomously
    → Pi + Qwen3.8 medium (conservative default)

Aider + Qwen3.8 + repo-map + disposable Git index
    → experimental only for unknown-scope work;
      discovery and Git hygiene work,
      but end-to-end reproducibility is insufficient for default routing

Pi read-only discovery → scoped Aider
    → not adopted as default;
      correct discovery did not eliminate Aider self-test failures
```

Deterministic pytest/Ruff/mypy/CI remains mandatory in every path. Human merge authority remains unchanged.

## What this benchmark does not prove

- It does not prove Pi is universally more accurate than Aider for unknown-scope work.
- It does not establish a statistically meaningful success probability from three Aider replications.
- It does not show that disposable-index isolation causes a latency penalty.
- It does not prove that every future hybrid discovery/implementation pipeline would fail.
- It does not validate autonomous merge authority.

The result is a routing decision for the current TrajectoryOS stack based on the measured evidence available today.

## Benchmark stop rule

This synthetic task class is now closed. No H2 or further repetition is justified by the routing uncertainty it would resolve.

Future model/harness measurements should be collected during real TrajectoryOS Issues unless a concrete new uncertainty could materially change routing.