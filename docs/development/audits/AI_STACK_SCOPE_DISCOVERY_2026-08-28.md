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

## Git-index finding

Native Aider creation of new files staged empty blobs in the real index before filling the working-tree files. The staged blob was the canonical empty-file blob:

`e69de29bb2d1d6434b8b29ae775ad8c2e48c5391`

A disposable Git index initialized from `HEAD` successfully isolated that harness behavior:

```text
GIT_INDEX_FILE=<disposable-index>
git read-tree HEAD
```

M1, M2, and M3 all left the real worktree index byte-for-byte unchanged while Aider continued to maintain its own staged empty-file placeholders in the disposable index.

This validates disposable-index isolation as an effective Git-hygiene control when Aider repository mapping/discovery is desired.

## Failure analysis

### M2

M2 chose an incomplete scope, failed hidden acceptance because the required public application export was absent, and generated tests using nonexistent `EntityStatus.BLOCKED` values.

Aider observed the failing quality gate, corrected an earlier fixture error, then stopped after its configured reflection limit. It did not falsely claim success.

Classification: **feature FAIL / failure-aware termination**.

### M3

M3 satisfied the independent hidden acceptance, which indicates that the requested public behavior was materially present, but its own generated tests used nonexistent `SourceKind.HUMAN` values and caused 19 canonical-gate failures.

Classification: **hidden semantics PASS / self-test generation FAIL / overall FAIL**.

This is a particularly important reminder that agent-authored tests are not authoritative merely because the implementation itself appears semantically correct.

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

## Routing conclusion

Validated working routing after S1:

```text
substantive feature + editable surfaces known
    → Aider + Qwen3.8 medium

substantive feature + scope must be discovered autonomously
    → Pi + Qwen3.8 medium (conservative default)

Aider + Qwen3.8 + repo-map + disposable Git index
    → validated experimental alternative for unknown-scope work;
      discovery works and Git hygiene works,
      but end-to-end reproducibility is not yet sufficient for default routing
```

Deterministic pytest/Ruff/mypy/CI remains mandatory in every path. Human merge authority remains unchanged.

## What this benchmark does not prove

- It does not prove Pi is universally more accurate than Aider for unknown-scope work.
- It does not establish a statistically meaningful success probability from three Aider replications.
- It does not show that disposable-index isolation causes a latency penalty.
- It does not validate autonomous merge authority.

The result is a routing decision for the current TrajectoryOS stack based on the measured evidence available today.
