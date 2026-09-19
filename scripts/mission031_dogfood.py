#!/usr/bin/env python3
"""M031 — end-to-end mission assembly dogfood (authoritative evidence).

Drives the REAL assembly orchestrator end to end and records honest,
machine-readable evidence:

* S1 — objective intake -> mission creation -> preflight -> plan ->
  implementation -> deterministic validation -> independent review ->
  bounded repair -> fresh review -> human gate -> closure, producing the
  canonical mission root (`mission.json`, `plan.json`, `events.jsonl`,
  `status.json`, `telemetry.json`, `summary.json`, `closure.json`);
* S2 — preflight reject never invokes a downstream expensive gate;
* S3 — validation reject can never become `READY_FOR_COMMIT`;
* S4 — a stale review can never become `READY_FOR_COMMIT`;
* S5 — repair budget exhaustion yields `BLOCKED`;
* S6 — lifecycle `COMPLETE` + non-ready readiness never renders as success;
* S7 — interruption/resume keeps the same `mission_id` and evidence history;
* S8 — the closure artifact reconstructs the mission without prose logs;
* S9 — no Git trust-boundary write anywhere in the assembly sources.

Exit codes: 0 = all checks proven; 3 = proof blocked; 2 = usage error.
"""

# ruff: noqa: E402
# (the repository src/ layout is added to sys.path below)

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import shutil
import sys
import tempfile
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from trajectory_os.agents import model as agent_model
from trajectory_os.assembly import closure as assembly_closure
from trajectory_os.assembly import model, store
from trajectory_os.assembly import orchestrator as assembly_run
from trajectory_os.benchmark.executor import (
    ExecutionOutcome,
    ExecutionRequest,
    FixtureExecutor,
    TrialExecutor,
)
from trajectory_os.observability import model as obs_model
from trajectory_os.observability import projection
from trajectory_os.observability import run as obs_run
from trajectory_os.observability import store as obs_store

PASS_REVIEW = (
    "VERDICT: PASS\nBLOCKERS:\n- none\nMAJORS:\n- none\nMINORS:\n- none\n"
    "FINAL RECOMMENDATION: GO COMMIT\n")
REJECT_REVIEW = (
    "VERDICT: REJECT\nBLOCKERS:\n- missing edge-case handling\n"
    "MAJORS:\n- none\nMINORS:\n- none\nFINAL RECOMMENDATION: REPAIR\n")

CHECK_LOG: list[dict[str, Any]] = []


def _check(scenario: str, description: str, ok: bool) -> bool:
    CHECK_LOG.append({"scenario": scenario, "description": description,
                      "ok": bool(ok)})
    return bool(ok)


def _workspace(root: str, mission_id: str) -> str:
    workspace = pathlib.Path(root) / mission_id / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return str(workspace)


def _request(root: str, mission_id: str, **overrides: Any,
             ) -> assembly_run.MissionRequest:
    base: dict[str, Any] = {
        "objective": "fix add(a, b) so it adds instead of subtracting",
        "workspace": _workspace(root, mission_id),
        "mission_id": mission_id,
        "workload_id": "small-targeted-repair",
        "trust_policy": model.TrustPolicy(max_repairs=2),
    }
    base.update(overrides)
    return assembly_run.MissionRequest(**base)


class _CountingExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        self.calls += 1
        raise AssertionError("downstream execution must not run")


class _WrongSolutionExecutor:
    def __init__(self, inner: FixtureExecutor) -> None:
        self._inner = inner

    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        outcome = self._inner.execute(request)
        (pathlib.Path(request.workspace) / "calc.py").write_text(
            "def add(a, b):\n    return a * b\n", encoding="utf-8")
        return outcome


def _orchestrator(root: str, executor: TrialExecutor,
                  reviews: list[str]) -> assembly_run.MissionOrchestrator:
    return assembly_run.MissionOrchestrator(
        root, executor=executor,
        reviewer_factory=obs_run.scripted_reviewer_factory(reviews))


def _scenario_canonical(root: str) -> dict[str, Any]:
    mission_id = "m031-dogfood-repair"
    orchestrator = _orchestrator(
        root, FixtureExecutor(interrupt_once=False),
        [REJECT_REVIEW, PASS_REVIEW])
    result = orchestrator.start(_request(
        root, mission_id, telemetry_mode=obs_model.TELEMETRY_BENCHMARK))
    mission_root = store.mission_root(root, mission_id)
    artifacts = {
        name: (mission_root / name).is_file()
        for name in (store.MISSION_NAME, store.PLAN_NAME,
                     obs_store.EVENTS_NAME, obs_store.STATUS_NAME,
                     obs_store.TELEMETRY_NAME, obs_store.SUMMARY_NAME,
                     store.CLOSURE_NAME)}
    _check("S1", "canonical mission root artifacts are persisted",
           all(artifacts.values()))
    _check("S1", "objective intake creates a durable mission identity",
           result.mission.mission_id == mission_id
           and result.mission.objective
           and (mission_root / store.MISSION_NAME).is_file())
    _check("S1", "preflight passes before expensive phases",
           result.status["state"] == obs_model.LC_COMPLETE)
    _check("S1", "plan is persisted and bounded",
           result.plan is not None and result.plan.retry_budget == 2
           and (mission_root / store.PLAN_NAME).is_file())
    _check("S1", "bounded repair preserved REJECT -> PASS review history",
           result.closure is not None
           and [entry["result"] for entry in result.closure.review_results]
           == [obs_model.RESULT_REJECT, obs_model.RESULT_PASS]
           and result.closure.attempts == 2
           and result.closure.repairs == 1)
    _check("S1", "post-repair fresh review is on the exact current patch",
           result.status["readiness"] == obs_model.RD_READY_FOR_COMMIT
           and result.status["reviewed_patch"] == result.status["current_patch"]
           and result.status["final_reviewer"]["active"] is True
           and result.status["final_reviewer"]["display_model"]
           == obs_model.FINAL_REVIEWER_MODEL)
    _check("S1", "deterministic validation ran",
           result.closure is not None
           and len(result.closure.validation_results) == 2
           and all(entry["result"] == obs_model.RESULT_PASS
                   for entry in result.closure.validation_results))
    return {
        "mission_id": mission_id, "artifacts": artifacts,
        "readiness": result.status["readiness"],
        "attempts": result.closure.attempts if result.closure else None,
        "repairs": result.closure.repairs if result.closure else None,
    }


def _scenario_preflight(root: str) -> dict[str, Any]:
    executor = _CountingExecutor()
    orchestrator = assembly_run.MissionOrchestrator(
        root, executor=executor)
    result = orchestrator.start(_request(
        root, "m031-dogfood-preflight", provider="ollama",
        model="deepseek-flash"))
    mission_root = store.mission_root(root, "m031-dogfood-preflight")
    events = obs_store.load_events(mission_root)
    _check("S2", "preflight reject invokes no downstream execution",
           executor.calls == 0
           and result.status["readiness"] == obs_model.RD_BLOCKED)
    _check("S2", "preflight reject stops before planning",
           not store.plan_exists(root, "m031-dogfood-preflight"))
    _check("S2", "preflight reject preserves checks and a terminal reason",
           [event["kind"] for event in events]
           == ["MISSION_CREATED", "PREFLIGHT_COMPLETED", "MISSION_CLOSED"]
           and result.status["terminal_reason_code"]
           == obs_model.R_INVALID_MODEL_PROVIDER)
    return {"calls": executor.calls, "events": [e["kind"] for e in events]}


def _scenario_validation_reject(root: str) -> dict[str, Any]:
    executor = _WrongSolutionExecutor(FixtureExecutor(interrupt_once=False))
    orchestrator = _orchestrator(root, executor, [PASS_REVIEW])
    result = orchestrator.start(_request(root, "m031-dogfood-validation"))
    _check("S3", "validation reject cannot become READY_FOR_COMMIT",
           result.status["readiness"] == obs_model.RD_FAILED
           and result.status["readiness"] != obs_model.RD_READY_FOR_COMMIT
           and result.status["previous_gate"] == obs_model.GATE_VALIDATION)
    _check("S3", "no review runs after a validation reject",
           result.closure is not None
           and result.closure.review_results == ())
    return {"readiness": result.status["readiness"]}


def _scenario_stale_review(root: str) -> dict[str, Any]:
    mission = _orchestrator(
        root, FixtureExecutor(interrupt_once=False), [PASS_REVIEW])
    mission.start(_request(root, "m031-dogfood-stale"))
    mission_root = store.mission_root(root, "m031-dogfood-stale")
    loaded = store.load_mission(root, "m031-dogfood-stale")
    plan = store.load_plan(root, "m031-dogfood-stale")
    stale = obs_model.CanonicalStatus.build(
        run_id="m031-dogfood-stale", state=obs_model.LC_COMPLETE,
        stage=obs_model.STAGE_DONE, phase="DONE", attempt=1,
        current_backend=agent_model.BACKEND_PI, current_provider="deepseek",
        current_model="deepseek-flash", inline_review_enabled=False,
        inline_reviewer=obs_model.ReviewerStatus.disabled(
            obs_model.ROLE_INLINE_REVIEWER),
        final_review_enabled=True,
        final_reviewer=obs_model.ReviewerStatus(
            role=obs_model.ROLE_FINAL_INDEPENDENT_REVIEWER, enabled=True,
            active=True, backend="ollama", provider="ollama",
            model=obs_model.FINAL_REVIEWER_MODEL, reason=obs_model.R_OK),
        previous_gate=obs_model.GATE_REVIEW,
        previous_result=obs_model.RESULT_PASS,
        reviewed_patch="a" * 64, current_patch="b" * 64,
        next_action="operator: commit the reviewed patch",
        last_meaningful_event_at=None, heartbeat_at=None,
        terminal_reason="all gates passed",
        readiness=obs_model.RD_READY_FOR_COMMIT)
    blocked, gate = mission._human_gate(  # noqa: SLF001 - dogfood proof
        mission_root, loaded, plan, stale)
    _check("S4", "a stale reviewed patch blocks the human gate",
           gate == "blocked"
           and blocked.readiness == obs_model.RD_BLOCKED
           and blocked.terminal_reason_code
           == model.R_HUMAN_GATE_VIOLATION)
    return {"gate": gate, "readiness": blocked.readiness}


def _scenario_repair_budget(root: str) -> dict[str, Any]:
    orchestrator = _orchestrator(
        root, FixtureExecutor(interrupt_once=False), [REJECT_REVIEW])
    result = orchestrator.start(_request(
        root, "m031-dogfood-budget",
        trust_policy=model.TrustPolicy(max_repairs=0)))
    _check("S5", "repair budget exhaustion yields BLOCKED",
           result.status["readiness"] == obs_model.RD_BLOCKED
           and result.status["state"] == obs_model.LC_COMPLETE
           and result.closure is not None
           and result.closure.attempts == 1
           and result.closure.repairs == 0)
    return {"readiness": result.status["readiness"],
            "attempts": result.closure.attempts if result.closure else None}


def _scenario_complete_not_ready(root: str) -> dict[str, Any]:
    blocked = obs_model.CanonicalStatus.build(
        run_id="m031-dogfood-blocked", state=obs_model.LC_COMPLETE,
        stage=obs_model.STAGE_DONE, phase="DONE", attempt=1,
        current_backend=agent_model.BACKEND_PI, current_provider="deepseek",
        current_model="deepseek-flash", inline_review_enabled=False,
        inline_reviewer=obs_model.ReviewerStatus.disabled(
            obs_model.ROLE_INLINE_REVIEWER),
        final_review_enabled=True,
        final_reviewer=obs_model.ReviewerStatus(
            role=obs_model.ROLE_FINAL_INDEPENDENT_REVIEWER, enabled=True,
            active=True, backend="ollama", provider="ollama",
            model=obs_model.FINAL_REVIEWER_MODEL, reason=obs_model.R_OK),
        previous_gate=obs_model.GATE_REVIEW,
        previous_result=obs_model.RESULT_REJECT,
        reviewed_patch="a" * 64, current_patch="a" * 64,
        next_action="operator: resolve blocking findings",
        last_meaningful_event_at=None, heartbeat_at=None,
        terminal_reason="blocking review findings",
        readiness=obs_model.RD_BLOCKED).to_dict()
    web = projection.render_projection(
        blocked, target=projection.PROJECTION_WEB)
    cli = projection.render_projection(
        blocked, target=projection.PROJECTION_CLI)
    _check("S6", "COMPLETE + non-ready never renders as success",
           blocked["state"] == obs_model.LC_COMPLETE
           and blocked["success"] is False
           and 'data-ready-for-commit="false"' in web
           and "READY_FOR_COMMIT —" not in cli)
    return {"readiness": blocked["readiness"], "success": blocked["success"]}


def _scenario_interruption_resume(root: str) -> dict[str, Any]:
    orchestrator = _orchestrator(
        root, FixtureExecutor(interrupt_once=False), [PASS_REVIEW])
    mission_id = "m031-dogfood-resume"
    first = orchestrator.start(
        _request(root, mission_id),
        interrupt_after_phase=model.MP_EXECUTION)
    mission_root = store.mission_root(root, mission_id)
    events_before = obs_store.load_events(mission_root)
    resumed = orchestrator.resume(mission_id)
    events_after = obs_store.load_events(mission_root)
    _check("S7", "interruption leaves no closure and keeps the mission id",
           first.interrupted is True and first.closure is None
           and first.mission.mission_id == mission_id)
    _check("S7", "resume keeps the same mission_id and evidence history",
           resumed.mission.mission_id == mission_id
           and events_after[:len(events_before)] == events_before
           and resumed.closure is not None
           and resumed.status["readiness"] == obs_model.RD_READY_FOR_COMMIT)
    _check("S7", "a second resume is idempotent",
           orchestrator.resume(mission_id).closure is not None)
    return {"mission_id": mission_id,
            "events_before": len(events_before),
            "events_after": len(events_after),
            "readiness": resumed.status["readiness"]}


def _scenario_reconstruction(root: str) -> dict[str, Any]:
    document = assembly_closure.reconstruct_mission(
        root, "m031-dogfood-repair")
    closure = document["closure"]
    _check("S8", "closure reconstructs the mission without prose logs",
           document["mission"]["mission_id"] == "m031-dogfood-repair"
           and document["mission"]["objective"]
           and document["mission"]["definition_of_done"]
           and document["mission"]["baseline"]["workspace_digest"]
           and document["plan"]["steps"]
           and closure["readiness"] == obs_model.RD_READY_FOR_COMMIT
           and closure["next_action"]
           and closure["artifacts"][store.CLOSURE_NAME]
           and closure["steps_executed"][0] == "intake"
           and closure["steps_executed"][-1] == "closure")
    _check("S8", "authoritative artifact paths are recorded",
           all(pathlib.Path(path).exists()
               for path in closure["artifacts"].values()
               if path.endswith(".json") or path.endswith(".jsonl")))
    return {"steps_executed": closure["steps_executed"],
            "readiness": closure["readiness"]}


def _scenario_trust_boundary() -> dict[str, Any]:
    import trajectory_os.assembly.baseline as base
    import trajectory_os.assembly.cli as cli_mod
    import trajectory_os.assembly.closure as clo
    import trajectory_os.assembly.model as mdl
    import trajectory_os.assembly.orchestrator as orch
    import trajectory_os.assembly.store as st

    verbs = ("commit", "push", "merge", "reset", "restore", "clean", "stash",
             "rebase", "switch", "checkout", "pull", "fetch", "cherry-pick")
    offenders: list[str] = []
    for module in (base, cli_mod, clo, mdl, orch, st):
        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        for verb in verbs:
            if f'"git", "{verb}"' in source or f"'git', '{verb}'" in source:
                offenders.append(f"{module.__name__}:{verb}")
    _check("S9", "no assembly module performs a Git trust-boundary write",
           not offenders)
    reader = (REPO / "scripts" / "trajectory-mission").read_text(
        encoding="utf-8")
    _check("S9", "the operator CLI is a thin read/write-bounded shim",
           "exec python3 -m trajectory_os.assembly.cli" in reader
           and "git commit" not in reader)
    return {"offenders": offenders}


def _scenario_result(name: str) -> dict[str, Any]:
    checks = [c for c in CHECK_LOG if c["scenario"] == name]
    return {"scenario": name, "checks": len(checks),
            "passed": sum(1 for c in checks if c["ok"]),
            "status": ("PASS" if checks and all(c["ok"] for c in checks)
                       else "FAIL")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mission031_dogfood",
        description="M031 end-to-end mission assembly dogfood.")
    parser.add_argument("--root", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--keep", action="store_true", default=False)
    args = parser.parse_args(argv)

    temporary = args.root is None
    root = args.root or tempfile.mkdtemp(prefix="trajectory-m031-")
    pathlib.Path(root).mkdir(parents=True, exist_ok=True)
    try:
        canonical = _scenario_canonical(root)
        preflight_result = _scenario_preflight(root)
        validation_result = _scenario_validation_reject(root)
        stale_result = _scenario_stale_review(root)
        budget_result = _scenario_repair_budget(root)
        lifecycle_result = _scenario_complete_not_ready(root)
        resume_result = _scenario_interruption_resume(root)
        reconstruction = _scenario_reconstruction(root)
        trust = _scenario_trust_boundary()
        scenarios = sorted({c["scenario"] for c in CHECK_LOG})
        report = {
            "mission": "M031",
            "issue": 234,
            "generated_at": datetime.datetime.now(datetime.UTC)
            .replace(microsecond=0, tzinfo=None).isoformat() + "Z",
            "root": root,
            "canonical": canonical,
            "preflight": preflight_result,
            "validation_reject": validation_result,
            "stale_review": stale_result,
            "repair_budget": budget_result,
            "lifecycle_readiness": lifecycle_result,
            "interruption_resume": resume_result,
            "reconstruction": reconstruction,
            "trust_boundary": trust,
            "scenarios": [_scenario_result(code) for code in scenarios],
            "checks": CHECK_LOG,
            "status": ("PASS" if all(c["ok"] for c in CHECK_LOG)
                       else "FAIL"),
        }
        out = pathlib.Path(
            args.out or pathlib.Path(root) / "m031-dogfood.json")
        out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        print(f"M031 dogfood {report['status']}: {out}")
        for code in scenarios:
            print(f"  {code}: {_scenario_result(code)['status']}")
        return 0 if report["status"] == "PASS" else 3
    finally:
        if temporary and not args.keep:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
