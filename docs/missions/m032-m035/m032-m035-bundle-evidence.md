# M032–M035 Real Operator Bundle — Evidence

Baseline: `39fc9b0029142abdf6d8e42759bd4f4679a3ac96`
Marker: `M032_M035_REAL_OPERATOR_BUNDLE_COMPLETE`

## M032 — real non-fixture mission
- objective: Harden `trajectory-mission status` and `follow` against stale mission identity. In `src/trajectory_os/assembly/cli.py`, status is loaded with `obs_store.load_status(store.mission_root(root, mission_id))` without checking that the loaded document's `run_id` equals the requested `mission_id`. A stale or mismatched `status.json` (for example from a copied or corrupt mission root) can therefore be rendered as the requested mission. Fail closed when the loaded status `run_id` does not match the requested mission id, and reject an unknown mission id. Do not change the canonical status schema, the observability model, or any Git behaviour.
- mission_id / run_id: `m032-real-status-identity-b` / `m032-real-status-identity-b`
- backend: `pi` / `deepseek` / `deepseek-flash`; reviewer `qwen3.8:27b-q4_K_M`
- terminal readiness: **READY_FOR_COMMIT** (COMPLETE) — all gates passed
- current patch: `98a4587a5326def0cd38bf4a80f613e11672680a007362549535ec814dfab2ff`
- reviewed patch: `98a4587a5326def0cd38bf4a80f613e11672680a007362549535ec814dfab2ff`
- attempts / repairs: 1 / 0
- phase transitions: MISSION_CREATED, PREFLIGHT_COMPLETED, PLAN_PERSISTED, PREFLIGHT_COMPLETED, PATCH_CAPTURED, VALIDATION_COMPLETED, REVIEW_COMPLETED, RUN_COMPLETED, HUMAN_GATE_REACHED, MISSION_CLOSED
- earlier fail-closed attempt: `m032-real-status-identity` -> BLOCKED (reviewer protocol invalid after 2 repairs)

## M033 — real interruption / recovery
- status: **PASS** (SIGKILL and SIGTERM scenarios)
- SIGKILL: identity preserved=True, evidence preserved=True, readiness=READY_FOR_COMMIT, idempotent repeated resume=True
- SIGTERM: identity preserved=True, evidence preserved=True, readiness=READY_FOR_COMMIT, idempotent repeated resume=True

## M034 — operator control
- request-stop=ACCEPTED, clear-stop=CLEARED, cancel=ACCEPTED (CANCELLED)
- unknown mission fails closed: True

## M035 — production acceptance
- status: **PASS** (14/14 cases, 29/29 checks)

## Trust boundary
- Git trust-boundary offenders: []

## Final independent review
- reviewer: `qwen3.8:27b-q4_K_M`
- outcome: **VALID_PASS** (PASS_GO_COMMIT_NO_BLOCKING_FINDINGS)
- reviewed patch: `a7699825fd047be084dc6b9b05d82454941ada86479e813caf6174a43fe1b322` (still current: True)
- blocking findings: 0

## Authoritative artifacts
- m032_live_mission: `docs/missions/m032-m035/m032-live-mission-evidence.json`
- m032_blocked_attempt: `docs/missions/m032-m035/m032-live-mission-blocked-evidence.json`
- m033_interruption: `docs/missions/m032-m035/m033-interruption-evidence.json`
- m035_acceptance_json: `docs/missions/m032-m035/m035-acceptance-report.json`
- m035_acceptance_txt: `docs/missions/m032-m035/m035-acceptance-report.txt`
- final_review: `docs/missions/m032-m035/m032-m035-final-review.json`
