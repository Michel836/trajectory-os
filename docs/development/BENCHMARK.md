# Pi vs DeepSeek Harness benchmark (M029)

`trajectory_os.benchmark` is a decision-grade, repeatable A/B benchmark
between the proven `Pi -> DeepSeek Flash` runtime and a qualified
`DeepSeek Harness -> DeepSeek Flash` runtime. It exists so the long-term
backend choice is made from measured evidence, not intuition.

The design and invariants are documented in
`docs/adr/ADR-020-pi-vs-harness-benchmark.md`.

## Quick start

```bash
# Live benchmark (both backends, canonical workloads, real reviewer)
scripts/trajectory-benchmark run \
  --root .trajectory-benchmark \
  --run-id m029-live --mode live --backend both \
  --repetitions 2 --timeout 600 \
  --provider deepseek --model deepseek-flash \
  --reviewer-model qwen3.8:27b-q4_K_M

# Watch until a terminal state
scripts/trajectory-benchmark follow --root .trajectory-benchmark --run-id m029-live

# Read the decision report
scripts/trajectory-benchmark report --root .trajectory-benchmark --run-id m029-live
```

A run is resumable. If a process is interrupted, or an interruptible trial
cancels a pass, continue with `run --resume`:

```bash
scripts/trajectory-benchmark run --root .trajectory-benchmark \
  --run-id m029-live --mode live --resume
```

## Commands

| command | purpose |
| --- | --- |
| `run` | run or resume the benchmark and persist all artifacts |
| `status` | canonical machine/human status of one run |
| `follow` | poll until `READY_FOR_COMMIT` / `BLOCKED` / `FAILED` / `CANCELLED` / `COMPLETE` |
| `report` | print the human-readable decision report |
| `reconstruct` | strictly reconstruct one run from durable artifacts (JSON) |
| `version` | CLI version |

## Workloads

The same five workloads run for every backend and repetition:

1. `small-targeted-repair` — a one-line bug fix;
2. `medium-feature-implementation` — implement a documented function;
3. `multi-file-integration-change` — add a method across two modules;
4. `interruption-resume` — an interruptible trial completed on resume; in
   fixture mode every backend traverses the same interrupt-then-resume
   lifecycle;
5. `intentional-blocked-fail-closed` — a request that must be refused
   (the protected file must remain untouched).

Each trial runs in a fresh isolated workspace under
`<root>/<run_id>/workspaces/<trial_id>`.

## Artifacts

```
<root>/<run_id>/
  manifest.json     exact inputs, configuration and environment
  state.json        canonical run state
  events.jsonl      append-only operator timeline
  trials/<id>.json  one authoritative record per trial
  summary.json      deterministic aggregate
  report.md         human-readable decision report
```

`manifest.json` records the exact workloads, backends, target provider/model,
final reviewer model, repetitions and the read-only repository baseline.
Every trial persists the backend/provider/model identity, validation result,
independent-review outcome, the exact semantic patch SHA-256, and the full
telemetry block with per-metric provenance.

## Metric provenance

Every metric is labelled:

* `PROVIDER` — read verbatim from a structured provider response;
* `DERIVED` — computed from provider/local values (for example
  `total = prompt + completion`);
* `LOCAL` — measured by the benchmark runtime (for example wall time);
* `UNAVAILABLE` — not exposed by the backend/provider; the value is `null`
  and an explicit reason is preserved.

A `UNAVAILABLE` metric is never displayed as a number. For example a Pi text
run that does not expose token usage yields
`prompt_tokens=unavailable reason="backend/provider does not expose this
metric"` rather than an estimate.

## Decision, not promotion

The report lists all failed/blocked/unavailable trials and presents the three
operator options:

* Harness primary / Pi fallback;
* Pi primary / Harness fallback;
* hybrid by workload class.

A single lower metric never promotes a backend. Harness can only be promoted
when the runtime is `QUALIFIED` and its live trials pass the same validation
and review gates as Pi.

## Fixture mode

`--mode fixture` runs the same pipeline with a deterministic executor and is
used for tests, dogfood and pipeline validation only. Fixture evidence is
labelled `PIPELINE_FIXTURE` and the report explicitly states it must not be
used to promote a runtime.

## Trust and safety

* No command performs a Git trust-boundary write.
* Every trial uses its own isolated workspace.
* A Harness runtime that cannot be proven yields `UNAVAILABLE` evidence; no
  successful measurement is fabricated.
* Reviewer identity is explicit; an inactive reviewer is never shown as
  active, and the legacy `qwen3.6` reviewer is never presented for M029.

## Dogfood

```bash
# Deterministic pipeline proof plus real backend qualification evidence
python3 scripts/mission029_dogfood.py --live
```
