# M056–M063 — Adaptive intelligence & practical workflows

This bundle builds the first complete adaptive-intelligence loop on the
trusted Trajectory_OS operator platform:

    canonical execution history
      -> learning dataset (M056)
      -> predictive ML baseline (M057)
      -> advisory routing (M058) / adaptive scheduling (M059)
      -> grounded knowledge workspace (M060)
      -> practical workflows (M061)
      -> decision workspace (M062)
      -> actual outcomes -> future learning data

It is additive and advisory: no layer performs a release Git write, mutates
policy, or presents an inferred value as a measurement. The canonical mission,
release, readiness, semantic-patch-identity and human GO COMMIT / GO MERGE
gates are untouched (ADR-027).

## Artifacts

| Artifact | Contents |
| --- | --- |
| `m056-m063-bundle-evidence.json` / `.md` | consolidated machine + human bundle evidence |
| `m056-m063-acceptance.json` | the 75-case M063 acceptance matrix |
| `m056-m063-dataset-quality.json` | real-history field availability, provenance counts and target trainability |
| `m056-m063-dogfood.json` | real-history dogfood: dataset quality, ML evaluation, routing, scheduler, workflows, decision snapshot, limitations |

Regenerate locally:

```bash
python scripts/m056_m063_bundle_evidence.py
```

The script runs the acceptance matrix (deterministic fixtures, runs in CI) and
the dogfood. The dogfood reads real canonical history from
`.trajectory-pi/runs` when it exists; when it does not, it reports
`REAL_HISTORY_UNAVAILABLE` and claims no ML score.

## Real-history dogfood — actual sample size

At the time of writing, `.trajectory-pi/runs` contains **152 canonical run
directories** ("REAL" rows). Field availability (rows with a measured value):

| Metric | Available rows |
| --- | --- |
| duration (`elapsed_seconds`) | 148 |
| readiness / success / blocked | 148 |
| changed files / patch bytes | 148 |
| repairs (`repair_attempts_used`) | 48 |
| local GPU sample (`status.log`) | 25 |
| cost, tokens, TTFT, cache, retries, protocol failures, CI result | **0** |

The token/cost metrics are genuinely absent from the canonical run evidence
(provider telemetry was not persisted for these runs). They are reported as
`UNAVAILABLE` with a reason and **never imputed**.

## ML evaluation — what is and is not supportable

Feature set `PLANNING` (only information available before a run executes;
task-size/resource features are kept in a separate `TASK_SIZE` set and never
silently mixed in). Split: group-aware train/validation/test (whole
branch/workspace groups), leakage-free.

| Target | Status | Selected | Test | Naive baseline |
| --- | --- | --- | --- | --- |
| `duration_s` | TRAINED | gradient boosting | MAE 734.7, R² −0.30 | MAE 773.8, R² −0.39 |
| `success` | TRAINED | random forest | log loss 0.671, AUC 0.522 | log loss 0.663, AUC 0.500 |
| `blocked` | TRAINED | logistic | log loss 0.546, AUC 0.727 | log loss 0.643, AUC 0.500 |
| `repairs` | TRAINED | ridge | MAE 0.700, R² 0.079 | MAE 0.879, R² −0.046 |
| `cost_usd` | **INSUFFICIENT_DATA** | — | — | no labelled rows |

Honest reading of this table:

* The held-out **test split is small (10–14 rows)**. These are point estimates,
  not statistically significant results; no p-values are produced.
* `duration_s` improves MAE over the naive mean but has a **negative R²**,
  i.e. it does not explain variance better than the mean on this split.
* `success` **does not beat the naive baseline** on log loss; the dogfood
  records this explicitly. Only `blocked` and `repairs` show a modest
  improvement on this split.
* `cost_usd` cannot be trained at all and returns `INSUFFICIENT_DATA`.

No synthetic history is used to obtain an ML score. The M063 acceptance matrix
uses clearly-labelled `FIXTURE` runs only to prove the machinery; those results
are never presented as real-world performance.

## Advisory routing

With 152 real rows, two routes are comparable (`>= 5` runs): the actual
published recommendation is:

> `pi/deepseek/deepseek-flash` has the highest transparent advisory score
> (success − blocked = 0.323; success 35.5% of 31 labelled runs, blocked 3.2%,
> 95% Wilson interval 21.1%–53.1%) versus runner-up `qwen3.8-dev3090` (0.229).

This is observational evidence, not a controlled benchmark. The final
independent reviewer remains `qwen3.8:27b-q4_K_M`; the routing policy and all
human gates remain authoritative. When fewer than two routes have comparable
evidence the result is `NO_RECOMMENDATION`.

## Adaptive scheduling

The M050 scheduler remains canonical. The M059 overlay produces a transparent,
component-scored advisory ordering and a rule-vs-ML comparison. It never
claims an improvement without measured outcomes (`claimed_improvement: false`),
never mutates the canonical scheduler and never performs a Git write.

## Practical workflows (sample inputs)

Three families are run on safe, user-neutral `SAMPLE` inputs:

* **Career / consulting** — requirement extraction, heuristic business-problem
  signals, experience mapping and an evidence-based value proposition that is
  empty when no experience assets are supplied (no invented personal facts);
* **Life sciences / pharma** — deterministic theme classification, multi-period
  trend detection, recurring problems and AI/Data opportunity candidates with
  mini business cases (value statements explicitly labelled `HYPOTHESIS`);
* **Research / strategy** — question decomposition, BM25 evidence retrieval
  with citations, numeric-contradiction surfacing and uncertainty.

Each produces a durable `deliverable.md`, a machine `artifact.json` and a
LifeOS-compatible markdown note.

## Decision workspace

The decision workspace ranks candidate next actions against explicit criteria
and classifies every contribution as `FACT`, `PREDICTION`, `HEURISTIC` or
`HUMAN_PRIORITY`. It reports prediction uncertainty, persists an auditable
snapshot and can compare a later actual outcome against the prior
recommendation. Humans remain the final decision-maker; no irreversible
high-trust action is autonomous. A dogfood decision snapshot is persisted; its
outcome comparison is `null` because no post-decision outcome has occurred yet
(the comparison mechanism is proven by the acceptance matrix).

## Acceptance matrix

`m056-m063-acceptance.json` contains **75** executable cases across eight
categories: DATASET (10), ML (16), ROUTING (10), SCHEDULING (7), RAG (10),
WORKFLOWS (7), DECISION (9), TRUST (6). All 75 pass in CI using deterministic
fixtures.

## Trust boundaries preserved

* No intelligence module contains a release Git verb; the bundle evidence
  trust-boundary scan is clean.
* No intelligence layer mutates policy or the canonical scheduler.
* Every concrete value requires a provenance source; `UNAVAILABLE` requires a
  reason.
* No success is inferred from prose: every claim is a durable document.
* M017–M055 invariants (semantic patch identity, fresh final review,
  GO COMMIT, exact-head CI, GO MERGE) are unchanged.
