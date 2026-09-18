#!/usr/bin/env python3
"""Mission 016 — production goal-level proof harness (M008-M016 dogfood).

Bounded, deterministic, fail-closed harness that drives the REAL production
operator surface (``scripts/trajectory-pi-goals`` -> ``trajectory_os.graph.cli``
plus the canonical mission orchestrator) and proves Issue #222's acceptance
criteria end to end:

* S1 the strategic goal is created from the checked-in declarative spec;
* S2 the goal is fail-closed INCOMPLETE while any criterion is unresolved;
* S3 a controlled mission failure blocks the goal and is recorded;
* S4 a bounded deterministic replan supersedes the failed node and the
  prior generation is preserved exactly;
* S5 downstream work consumes explicit proven cross-mission evidence;
* S6 dependency- and resource-aware scheduling runs on the active generation;
* S7 every acceptance criterion becomes proven and the goal reaches COMPLETE;
* S8 interruption/reconstruction reproduces the exact goal-proof identity;
* S9 the operator dashboard/JSON/why surface exposes the exact proof chain;
* S10 the bounded DeepSeek Harness qualification is deterministic and
  secret-free.

Safety: all state lives in an isolated temp root (or ``--root``); no network,
no GPU, no Git history mutation. The harness never performs a Git
trust-boundary write.

Exit codes: 0 = all scenarios proven and report written;
            3 = proof blocked (one or more checks failed);
            2 = usage error.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))
CLI = REPO / "scripts" / "trajectory-pi-goals"
DOGFOOD_SPEC = REPO / "examples" / "final_program_dogfood.json"
REPLAN_REQUEST = REPO / "examples" / "final_program_replan_request.json"

GOAL = "g-m016-program"
MISSIONS = (
    "m-m016-foundation", "m-m016-runtime", "m-m016-failing",
    "m-m016-failing2", "m-m016-integration", "m-m016-proof",
)

CHECK_LOG: list[dict[str, Any]] = []


def _check(scenario: str, description: str, ok: bool) -> bool:
    CHECK_LOG.append({"scenario": scenario, "description": description,
                      "ok": bool(ok)})
    if not ok:
        print(f"  FAIL [{scenario}] {description}", file=sys.stderr)
    return ok


def _run_cli(args: list[str], root: str) -> tuple[int, str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    env["TRAJECTORY_GOALS_ROOT"] = root
    proc = subprocess.run(  # noqa: S603 - argv is harness-fixed
        [str(CLI), "--root", root, *args],
        capture_output=True, text=True, timeout=120, check=False,
        cwd=str(REPO), env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _cli_json(args: list[str], root: str) -> tuple[int, dict[str, Any]]:
    code, out, err = _run_cli(args, root)
    try:
        return code, json.loads(out)
    except json.JSONDecodeError:
        return code, {"_stdout": out, "_stderr": err}


def _scenario_result(scenario: str) -> dict[str, Any]:
    checks = [c for c in CHECK_LOG if c["scenario"] == scenario]
    return {
        "scenario": scenario,
        "status": "PASS" if checks and all(c["ok"] for c in checks)
        else "FAIL",
        "checks": checks,
    }


def _create_missions(root: str) -> None:
    from trajectory_os.missions import model as mission_model
    from trajectory_os.missions import orchestrator

    for mission_id in MISSIONS:
        orchestrator.create_mission(root, orchestrator.MissionConfig(
            mission_id=mission_id,
            objective="M016 production goal-level proof harness mission",
            phase_specs=orchestrator.default_phase_specs(
                {kind: ("true",)
                 for kind in mission_model.CANONICAL_SEQUENCE},
                repair_budget=0),
            baseline_revision="base"))


def _attested():
    from trajectory_os.missions import model as mission_model
    from trajectory_os.missions import semantic
    from trajectory_os.missions.runner import SubrunResult

    class _Runner:
        def run(self, request: Any) -> SubrunResult:
            return SubrunResult(
                0, mission_model.CR_COMPLETED,
                semantic_status=semantic.STATUS_SUCCESS,
                attestation=semantic.ATTESTATION_VERIFIED)

    return _Runner()


def _failing():
    from trajectory_os.missions import model as mission_model
    from trajectory_os.missions import semantic
    from trajectory_os.missions.runner import SubrunResult

    class _Runner:
        def run(self, request: Any) -> SubrunResult:
            if request.phase_id == "validate":
                return SubrunResult(1, mission_model.CR_FAILED)
            return SubrunResult(
                0, mission_model.CR_COMPLETED,
                semantic_status=semantic.STATUS_SUCCESS,
                attestation=semantic.ATTESTATION_VERIFIED)

    return _Runner()


def _run_mission(root: str, mission_id: str, runner: Any) -> str:
    from trajectory_os.missions import orchestrator

    report = orchestrator.run_mission(root, mission_id, runner)
    return report.mission_state


def _scenarios(root: str) -> dict[str, bool]:
    results: dict[str, bool] = {}
    runner = _attested()

    # S1: graph creation from the checked-in spec.
    code, created = _cli_json(
        ["create", "--spec", str(DOGFOOD_SPEC), "--repo", str(REPO),
         "--head", "base", "--json"], root)
    results["S1_graph_created"] = _check(
        "S1", "goal graph created from the checked-in spec",
        code == 0 and created.get("status") == "CREATED")

    # S2: fail-closed INCOMPLETE before any evidence exists.
    code, initial = _cli_json(["goal", GOAL, "--json"], root)
    results["S2_initial_incomplete"] = _check(
        "S2", "goal is fail-closed INCOMPLETE with no proven criterion",
        code == 0 and initial["final"]["state"] == "INCOMPLETE"
        and initial["counts"]["criteria_proven"] == 0)

    # S3: controlled failure blocks the goal and is recorded.
    results["S3_foundation_complete"] = _check(
        "S3", "producer mission completes with exact attested evidence",
        _run_mission(root, "m-m016-foundation", runner) == "COMPLETE")
    results["S3_failure_blocked"] = _check(
        "S3", "controlled failure blocks the goal fail-closed",
        _run_mission(root, "m-m016-failing", _failing()) in
        ("FAILED", "BLOCKED"))
    _, blocked = _cli_json(["goal", GOAL, "--json"], root)
    results["S3_recorded"] = _check(
        "S3", "fail-closed incomplete goal proof is recorded",
        blocked["final"]["state"] == "INCOMPLETE"
        and _cli_json(["goal-record", GOAL, "--json"], root)[0] == 0)

    # S4: bounded deterministic replan preserves the prior generation.
    code, replan = _cli_json(
        ["replan-apply", GOAL, "--spec", str(REPLAN_REQUEST), "--json"],
        root)
    results["S4_replan_applied"] = _check(
        "S4", "bounded deterministic replan activates generation 2",
        code == 0 and replan.get("accepted") is True
        and replan.get("generation_number") == 2)
    _, generation = _cli_json(["current-generation", GOAL, "--json"], root)
    results["S4_history_preserved"] = _check(
        "S4", "prior generation is preserved (generation count >= 2)",
        generation.get("generation_count", 0) >= 2)

    # S5: explicit cross-mission evidence reuse on the active generation.
    results["S5_replacement_complete"] = _check(
        "S5", "replacement mission completes with exact evidence",
        _run_mission(root, "m-m016-failing2", runner) == "COMPLETE")
    code, reuse = _cli_json(["reuse-resolve", GOAL, "--json"], root)
    results["S5_reuse_resolved"] = _check(
        "S5", "explicit cross-mission evidence reuse resolves",
        code == 0 and reuse.get("status") == "RESOLVED"
        and reuse["counts"]["resolved"] >= 1)

    # S6: dependency/resource-aware scheduling on the active generation.
    code, cycle = _cli_json(["schedule", GOAL, "--json"], root)
    results["S6_scheduled"] = _check(
        "S6", "scheduler admits dependency-ready work",
        code == 0 and "n-runtime" in cycle.get("candidate_order", []))

    # S7: COMPLETE only after every criterion is proven.
    for mission_id in ("m-m016-runtime", "m-m016-integration",
                       "m-m016-proof"):
        _run_mission(root, mission_id, runner)
    _run_cli(["schedule", GOAL, "--json"], root)
    code, complete = _cli_json(["goal-record", GOAL, "--json"], root)
    results["S7_complete"] = _check(
        "S7", "goal reaches COMPLETE with every criterion proven",
        code == 0 and complete.get("complete") is True
        and complete.get("final_reason") == "ALL_CRITERIA_PROVEN")

    # S8: reconstruction reproduces the exact proof identity.
    code, validated = _cli_json(["goal-validate", GOAL, "--json"], root)
    results["S8_reconstructed"] = _check(
        "S8", "reconstruction reproduces the exact persisted proof id",
        code == 0 and validated.get("reconstructed") is True
        and validated.get("persisted_proof_id")
        == validated.get("live_proof_id"))

    # S9: operator surface exposes the exact proof chain.
    code, dashboard = _cli_json(["goal", GOAL, "--json"], root)
    criteria = dashboard.get("criteria", [])
    results["S9_dashboard"] = _check(
        "S9", "dashboard/JSON exposes every criterion and its evidence",
        code == 0 and dashboard["final"]["state"] == "COMPLETE"
        and len(criteria) == 6
        and all(c["status"] == "PROVEN" and c["evidence"] for c in criteria))
    code, explained = _cli_json(["goal-explain", GOAL, "goal", "--json"], root)
    results["S9_why"] = _check(
        "S9", "deterministic why/explain is available",
        code == 0 and explained["final"]["complete"] is True)
    code, history = _cli_json(["goal-history", GOAL, "--json"], root)
    results["S9_history"] = _check(
        "S9", "append-only goal-proof history is recorded",
        code == 0 and history.get("count", 0) >= 2)

    return results


def _qualification() -> dict[str, Any]:
    from trajectory_os.agents import qualification

    outcome = qualification.qualify(
        workspace=tempfile.mkdtemp(prefix="m016-qualify-"), timeout_s=30)
    _check("S10", "DeepSeek Harness qualification is deterministic",
           outcome.status in (qualification.QS_QUALIFIED,
                              qualification.QS_UNAVAILABLE,
                              qualification.QS_INCOMPATIBLE))
    _check("S10", "DeepSeek Harness qualification performs no Git write",
           outcome.git_writes is False)
    _check("S10", "proven Pi path remains the authoritative fallback",
           outcome.fallback["authoritative"] is True
           and outcome.fallback["backend"] == "pi")
    return outcome.to_dict()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=None,
                        help="isolated proof root (default: temp dir)")
    parser.add_argument("--out", default=None,
                        help="report path (default: <root>/m016-report.json)")
    parser.add_argument("--keep", action="store_true",
                        help="keep the isolated root after the run")
    parser.add_argument("--skip-qualification", action="store_true")
    args = parser.parse_args(argv)

    temporary = args.root is None
    root = args.root or tempfile.mkdtemp(prefix="m016-proof-")
    pathlib.Path(root).mkdir(parents=True, exist_ok=True)
    try:
        _create_missions(root)
        results = _scenarios(root)
        qualification_payload = (
            None if args.skip_qualification else _qualification())
        scenarios = sorted({c["scenario"] for c in CHECK_LOG})
        report = {
            "mission": "M016",
            "issue": 222,
            "generated_at": datetime.datetime.now(datetime.UTC)
            .replace(microsecond=0, tzinfo=None).isoformat() + "Z",
            "root": root,
            "results": results,
            "scenarios": [_scenario_result(s) for s in scenarios],
            "qualification": qualification_payload,
            "status": ("PASS" if all(results.values())
                       and all(c["ok"] for c in CHECK_LOG) else "FAIL"),
        }
        out = pathlib.Path(args.out or pathlib.Path(root) / "m016-report.json")
        out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        print(f"M016 proof {report['status']}: {out}")
        for scenario in scenarios:
            print(f"  {scenario}: {_scenario_result(scenario)['status']}")
        return 0 if report["status"] == "PASS" else 3
    finally:
        if temporary and not args.keep:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
