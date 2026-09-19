# M064–M071 — Real-World Operating System & Portfolio Proof
## Bundle Evidence

- baseline: `77de25be7b04c27d4a66de206a8fc911f7da4017`
- generated: 2026-09-19T21:42:31Z
- realworld version: `m064-m071.1`
- marker: `M064_M071_REAL_WORLD_OS_PORTFOLIO_PROOF_COMPLETE`

## Acceptance matrix

- status: **PASS**
- cases: 98/98 passed
- artifact: `docs/missions/m064-m071/m064-m071-acceptance.json`

| category | passed | failed |
| --- | --- | --- |
| ACTIVE_LEARNING | 10 | 0 |
| CAREER | 12 | 0 |
| COCKPIT | 8 | 0 |
| INPUTS | 16 | 0 |
| LIFEOS | 12 | 0 |
| LIFE_SCIENCES | 10 | 0 |
| OUTCOMES | 10 | 0 |
| PORTFOLIO | 10 | 0 |
| TRUST | 10 | 0 |

## Dogfood evidence

- history source: **REAL**
- real rows: 153 / fixture rows: 0
- career input: SAMPLE
- life-sciences input: SAMPLE
- outcome links: 20 (20 reconciled)
- prediction error (MAE): 720.65
- model refresh state: NO_PROMOTION
- artifact: `docs/missions/m064-m071/m064-m071-dogfood.json`

## Value metrics

| metric | value | unit | source |
| --- | --- | --- | --- |
| time_to_usable_deliverable_s | 0.2253 | seconds | dogfood timing |
| evidence_coverage | 1.0 | ratio | acceptance matrix |
| source_coverage | 16 | sources | ingestion manifest |
| output_completeness | 1.0 | ratio | workflow artifacts |
| prediction_error | 720.65 | mean_absolute_error | outcome ledger |
| outcome_capture_completeness | 1.0 | ratio | outcome ledger |
| unresolved_unknowns | 5 | count | workflow unknowns |
| corrections_required | 0 | count | review findings |
| reuse_across_workflow_families | 3 | families | workflow families |

## Privacy-safe demo

- command: `scripts/trajectory demo portfolio`
- history source: FIXTURE (real rows: 0)
- documents: 8

## Trust boundary

- clean: **True** (13 files scanned)
- offenders: none
- git release writes from realworld layers: 0
- M017–M063 trust gates unchanged: True

## Semantic patch identity

The digest below is the complete tracked+untracked working-tree patch
at generation time (isolated temporary Git index), excluding only the
generated evidence under this directory.

- complete working-tree SHA-256: `dba1bb533e9ff6f4c53e64896d0fbc1f49351924b67fe7440139c467a34b075e`
- patch bytes: 325391
- changed files: 30
- temporary index isolated: True

## Limitations (real history)

- prediction error is the canonical run ETA-range midpoint vs measured elapsed_seconds; it is a point estimate

## Changed files

- `README.md`
- `docs/adr/ADR-028-real-world-os-portfolio-proof.md`
- `docs/missions/m064-m071/README.md`
- `docs/missions/m064-m071/m064-m071-final-review.json`
- `docs/missions/m064-m071/m064-m071-final-review.txt`
- `scripts/m064_m071_bundle_evidence.py`
- `scripts/m064_m071_final_review.py`
- `scripts/trajectory`
- `src/trajectory_os/operator/cli.py`
- `src/trajectory_os/realworld/__init__.py`
- `src/trajectory_os/realworld/acceptance.py`
- `src/trajectory_os/realworld/career.py`
- `src/trajectory_os/realworld/cli.py`
- `src/trajectory_os/realworld/cockpit.py`
- `src/trajectory_os/realworld/dogfood.py`
- `src/trajectory_os/realworld/ingest.py`
- `src/trajectory_os/realworld/learning.py`
- `src/trajectory_os/realworld/lifeos_ops.py`
- `src/trajectory_os/realworld/lifesci.py`
- `src/trajectory_os/realworld/model.py`
- `src/trajectory_os/realworld/outcomes.py`
- `src/trajectory_os/realworld/portfolio.py`
- `tests/integration/test_realworld_bundle_m064_m071.py`
- `tests/unit/test_realworld_acceptance_m064_m071.py`
- `tests/unit/test_realworld_career_lifesci_m065_m066.py`
- `tests/unit/test_realworld_cockpit_portfolio_m070_m071.py`
- `tests/unit/test_realworld_ingest_m064.py`
- `tests/unit/test_realworld_learning_m069.py`
- `tests/unit/test_realworld_lifeos_ops_m067.py`
- `tests/unit/test_realworld_outcomes_m068.py`
