# M056–M063 — Adaptive Intelligence & Practical Workflows
## Bundle Evidence

- baseline: `fc8de41c9b743fc5356ec827211987c91bb00b41`
- generated: 2026-09-19T20:02:54Z
- intelligence version: `m056-m063.1`
- marker: `M056_M063_ADAPTIVE_INTELLIGENCE_PRACTICAL_WORKFLOWS_COMPLETE`

## Acceptance matrix

- status: **PASS**
- cases: 75/75 passed
- artifact: `docs/missions/m056-m063/m056-m063-acceptance.json`

| category | passed | failed |
| --- | --- | --- |
| DATASET | 10 | 0 |
| DECISION | 9 | 0 |
| ML | 16 | 0 |
| RAG | 10 | 0 |
| ROUTING | 10 | 0 |
| SCHEDULING | 7 | 0 |
| TRUST | 6 | 0 |
| WORKFLOWS | 7 | 0 |

## Dogfood evidence

- history source: **REAL**
- real rows: 152 / fixture rows: 0
- routing decision: RECOMMEND
- scheduler rank agreement: 0.0 (improvement claimed: False)
- workflow families: CAREER_CONSULTING, LIFE_SCIENCES_PHARMA, RESEARCH_STRATEGY
- artifact: `docs/missions/m056-m063/m056-m063-dogfood.json`

## Trust boundary

- clean: **True** (14 files scanned)
- offenders: none
- git release writes from intelligence layers: 0
- M017–M055 trust gates unchanged: True

## Semantic patch identity

The digest below is the complete tracked+untracked working-tree patch
at generation time (isolated temporary Git index), excluding only the
generated evidence under this directory.

- complete working-tree SHA-256: `5f517cff103b6fe0956380ab3072489f12d9bea90feb59fbc18c0bf7bc426875`
- patch bytes: 412154
- changed files: 34
- temporary index isolated: True

## Limitations (real history)

- target duration_s: gradient_boosting selected on validation; test metrics are point estimates on a small held-out split and are not statistically significant
- target success: random_forest selected on validation; test metrics are point estimates on a small held-out split and are not statistically significant
- target success: the selected model does not beat the naive baseline on the held-out split (log_loss 0.6660 vs baseline 0.6637); no superiority is claimed
- target blocked: logistic selected on validation; test metrics are point estimates on a small held-out split and are not statistically significant
- target repairs: ridge selected on validation; test metrics are point estimates on a small held-out split and are not statistically significant
- target cost_usd: no labelled rows for this target
- metric 'cost_usd' is unavailable for every observed run; it is reported as UNAVAILABLE with a reason
- metric 'prompt_tokens' is unavailable for every observed run; it is reported as UNAVAILABLE with a reason
- metric 'completion_tokens' is unavailable for every observed run; it is reported as UNAVAILABLE with a reason
- metric 'total_tokens' is unavailable for every observed run; it is reported as UNAVAILABLE with a reason
- metric 'ttft_ms' is unavailable for every observed run; it is reported as UNAVAILABLE with a reason
- metric 'retries' is unavailable for every observed run; it is reported as UNAVAILABLE with a reason
- metric 'protocol_failures' is unavailable for every observed run; it is reported as UNAVAILABLE with a reason
- metric 'ci_result' is unavailable for every observed run; it is reported as UNAVAILABLE with a reason

## Changed files

- `README.md`
- `docs/adr/ADR-027-adaptive-intelligence-practical-workflows.md`
- `docs/missions/m056-m063/README.md`
- `docs/missions/m056-m063/m056-m063-dataset-quality.json`
- `docs/missions/m056-m063/m056-m063-final-review.json`
- `docs/missions/m056-m063/m056-m063-final-review.txt`
- `scripts/m056_m063_bundle_evidence.py`
- `scripts/m056_m063_final_review.py`
- `scripts/trajectory-pi`
- `src/trajectory_os/intelligence/__init__.py`
- `src/trajectory_os/intelligence/acceptance.py`
- `src/trajectory_os/intelligence/adaptive.py`
- `src/trajectory_os/intelligence/dataset.py`
- `src/trajectory_os/intelligence/decision.py`
- `src/trajectory_os/intelligence/dogfood.py`
- `src/trajectory_os/intelligence/features.py`
- `src/trajectory_os/intelligence/fixtures.py`
- `src/trajectory_os/intelligence/knowledge.py`
- `src/trajectory_os/intelligence/ml.py`
- `src/trajectory_os/intelligence/model.py`
- `src/trajectory_os/intelligence/projection.py`
- `src/trajectory_os/intelligence/routing.py`
- `src/trajectory_os/intelligence/workflows.py`
- `src/trajectory_os/platform/api.py`
- `tests/integration/test_intelligence_bundle_m056_m063.py`
- `tests/unit/test_intelligence_acceptance_m063.py`
- `tests/unit/test_intelligence_adaptive_m059.py`
- `tests/unit/test_intelligence_dataset_m056.py`
- `tests/unit/test_intelligence_decision_m062.py`
- `tests/unit/test_intelligence_knowledge_m060.py`
- `tests/unit/test_intelligence_ml_m057.py`
- `tests/unit/test_intelligence_routing_m058.py`
- `tests/unit/test_intelligence_workflows_m061.py`
- `tests/unit/test_trajectory_pi_wrapper.py`
