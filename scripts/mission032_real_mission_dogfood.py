#!/usr/bin/env python3
"""M032 — real non-fixture mission dogfood (authoritative evidence).

Runs one *real* Trajectory_OS repository hardening objective through the
M031/M034 assembled mission path using the normal production backend
(``pi`` + ``deepseek-flash`` for implementation, ``qwen3.8:27b-q4_K_M`` for
the fresh independent final review).

Selected objective (a real defect discovered while hardening the M034
operator surface): ``trajectory-mission status`` / ``follow`` loaded the
canonical ``status.json`` for the requested mission *without* verifying that
the document's ``run_id`` equals the requested ``mission_id``. A stale or
mismatched status document left in a mission root was therefore rendered as
the requested mission. The fix must fail closed with an explicit identity
error for a mismatched ``run_id`` and reject an unknown mission id.

The isolated workspace is a real copy of the Trajectory_OS source tree with
the pre-fix (baseline) ``assembly/cli.py`` restored, so the mission must
produce a real code change. No Git trust-boundary write is ever performed by
the mission; the mission stops at the human ``GO COMMIT`` gate.

Exit codes: 0 = READY_FOR_COMMIT (or a clean fail-closed terminal state,
which is recorded honestly and still yields exit 0 for the dogfood), 3 = the
dogfood itself could not run, 2 = usage error.
"""

# ruff: noqa: E402
# (the repository src/ layout is added to sys.path below)

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import shutil
import subprocess
import sys
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from trajectory_os.agents import model as agent_model
from trajectory_os.assembly import orchestrator as assembly_run
from trajectory_os.assembly import recovery, store
from trajectory_os.benchmark import model as bench_model
from trajectory_os.benchmark import review as bench_review
from trajectory_os.benchmark.executor import LiveExecutor
from trajectory_os.observability import model as obs_model
from trajectory_os.observability import store as obs_store

BASELINE = "39fc9b0029142abdf6d8e42759bd4f4679a3ac96"
CLI_RELATIVE = "src/trajectory_os/assembly/cli.py"
SOURCE_RELATIVE = "src/trajectory_os"

REAL_OBJECTIVE = (
    "Harden `trajectory-mission status` and `follow` against stale mission "
    "identity. In `src/trajectory_os/assembly/cli.py`, status is loaded with "
    "`obs_store.load_status(store.mission_root(root, mission_id))` without "
    "checking that the loaded document's `run_id` equals the requested "
    "`mission_id`. A stale or mismatched `status.json` (for example from a "
    "copied or corrupt mission root) can therefore be rendered as the "
    "requested mission. Fail closed when the loaded status `run_id` does not "
    "match the requested mission id, and reject an unknown mission id. Do not "
    "change the canonical status schema, the observability model, or any Git "
    "behaviour."
)

RATIONALE = (
    "Discovered while implementing the M034 mission-level operator control "
    "requirements: controls must target an explicit mission/run identity and "
    "stale/unknown mission ids must fail closed. The observation surface had "
    "the same identity gap, so a mismatched status could be displayed as the "
    "requested mission. It is a small, real, auditable repository hardening "
    "defect with a deterministic validation gate and no architectural change."
)

VALIDATION_SCRIPT = '''\
"""Deterministic validation for the M032 real mission (status identity)."""
import io
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from trajectory_os.assembly import cli
from trajectory_os.assembly import orchestrator as run
from trajectory_os.benchmark.executor import FixtureExecutor
from trajectory_os.observability import run as obs_run
from trajectory_os.observability import store as obs_store

PASS = (
    "VERDICT: PASS\\nBLOCKERS:\\n- none\\nMAJORS:\\n- none\\nMINORS:\\n- none\\n"
    "FINAL RECOMMENDATION: GO COMMIT\\n"
)
FAILURES = []


def check(description, ok):
    if not ok:
        FAILURES.append(description)


def invoke(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def main():
    root = tempfile.mkdtemp(prefix="m032-validate-")
    workspace = str(Path(root) / "workspace")
    Path(workspace).mkdir(parents=True, exist_ok=True)
    orchestrator = run.MissionOrchestrator(
        root, executor=FixtureExecutor(interrupt_once=False),
        reviewer_factory=obs_run.scripted_reviewer_factory([PASS]))
    orchestrator.start(run.MissionRequest(
        objective="validation seed", workspace=workspace,
        mission_id="m032-validate", workload_id="small-targeted-repair"))
    correct = root + "/m032-validate/status.json"

    code, _, _ = invoke(["status", "--root", root,
                         "--mission-id", "m032-validate", "--json"])
    check("a correct identity status is accepted", code == 0)

    # Tamper the durable status with a foreign run_id.
    document = obs_store.read_json(Path(correct))
    document["run_id"] = "some-other-mission"
    obs_store.write_json(Path(correct), document)
    code, _, err = invoke(["status", "--root", root,
                           "--mission-id", "m032-validate", "--json"])
    check("a mismatched status run_id fails closed", code != 0)
    check("the identity failure is explicit",
          "IDENTITY" in err.upper() or "MISSION" in err.upper())

    code, _, _ = invoke(["status", "--root", root,
                         "--mission-id", "does-not-exist", "--json"])
    check("an unknown mission id fails closed", code != 0)

    if FAILURES:
        print("VALIDATION FAILED")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("VALIDATION PASSED: status identity is fail-closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _ignore(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names
            if name in ("__pycache__", ".pytest_cache", ".ruff_cache",
                        ".mypy_cache")
            or name.endswith(".pyc")}


def prepare_workspace(workspace: pathlib.Path) -> None:
    """Materialize a real source workspace at the pre-fix baseline."""
    if workspace.exists():
        shutil.rmtree(workspace)
    target = workspace / SOURCE_RELATIVE
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO / SOURCE_RELATIVE, target, ignore=_ignore)
    baseline = subprocess.run(
        ["git", "show", f"{BASELINE}:{CLI_RELATIVE}"],
        cwd=str(REPO), capture_output=True, text=True, check=False)
    if baseline.returncode != 0 or not baseline.stdout:
        raise SystemExit(
            f"could not restore baseline cli.py: {baseline.stderr.strip()}")
    (workspace / CLI_RELATIVE).write_text(baseline.stdout, encoding="utf-8")
    (workspace / "validate_real_mission.py").write_text(
        VALIDATION_SCRIPT, encoding="utf-8")


def _workload() -> bench_model.WorkloadSpec:
    return bench_model.WorkloadSpec(
        workload_id="real-repo-status-identity-hardening",
        workload_class=bench_model.WC_SMALL_REPAIR,
        title="Real repository status-identity hardening",
        objective=REAL_OBJECTIVE,
        validation_command=("{python}", "validate_real_mission.py"),
        expect_pass=True,
        fixture={},
        descriptive_files=(CLI_RELATIVE, "validate_real_mission.py"),
    ).validate()


def run(root: pathlib.Path, *, live: bool, telemetry_mode: str,
        timeout_s: int, mission_id: str = "m032-real-status-identity",
        ) -> dict[str, Any]:
    workspace = root / mission_id / "workspace"
    prepare_workspace(workspace)
    reviewer_model = obs_model.FINAL_REVIEWER_MODEL
    executor: Any = LiveExecutor() if live else _NoopExecutor()
    reviewer_factory = (
        (lambda: bench_review.OllamaReviewerClient(model_name=reviewer_model))
        if live else None)
    orchestrator = assembly_run.MissionOrchestrator(
        root, executor=executor, reviewer_factory=reviewer_factory)
    request = assembly_run.MissionRequest(
        objective=REAL_OBJECTIVE,
        workspace=str(workspace),
        constraints=(
            "only modify files inside the mission workspace",
            "no git commit/push/merge/reset/restore/clean/stash/rebase/switch",
            "keep the canonical status schema and observability model intact",
        ),
        definition_of_done=(
            "a status with a mismatched run_id is rejected (fail closed)",
            "an unknown mission id is rejected",
            "the deterministic validation script passes on the exact patch",
        ),
        backend=agent_model.BACKEND_PI,
        provider="deepseek",
        model="deepseek-flash",
        workload_id="real-repo-status-identity-hardening",
        workload=_workload(),
        mission_id=mission_id,
        mode=bench_model.MODE_LIVE if live else bench_model.MODE_FIXTURE,
        telemetry_mode=telemetry_mode,
        timeout_s=timeout_s,
    )
    result = orchestrator.start(request)
    mission_root = store.mission_root(root, mission_id)
    decision = recovery.detect_resume(root, mission_id)
    closure = (store.load_closure(root, mission_id)
               if store.closure_exists(root, mission_id) else result.closure)
    events = obs_store.load_events(mission_root)
    telemetry = None
    try:
        telemetry = obs_store.load_telemetry(mission_root)
    except obs_store.CanonicalStoreError:
        telemetry = None
    return {
        "selected_objective": REAL_OBJECTIVE,
        "why_chosen": RATIONALE,
        "baseline": BASELINE,
        "mission_id": mission_id,
        "run_id": result.status.get("run_id"),
        "backend": result.status.get("current_backend"),
        "provider": result.status.get("current_provider"),
        "model": result.status.get("current_model"),
        "reviewer_model": reviewer_model,
        "mode": "LIVE" if live else "FIXTURE",
        "readiness": result.status.get("readiness"),
        "lifecycle": result.status.get("state"),
        "terminal_reason": result.status.get("terminal_reason"),
        "terminal_reason_code": result.status.get("terminal_reason_code"),
        "current_patch": result.status.get("current_patch"),
        "reviewed_patch": result.status.get("reviewed_patch"),
        "recovery": decision.to_dict(),
        "phase_transitions": [event.get("kind") for event in events],
        "event_count": len(events),
        "attempts": closure.attempts if closure else None,
        "repairs": closure.repairs if closure else None,
        "review_results": ([dict(entry)
                            for entry in closure.review_results]
                           if closure else []),
        "validation_results": ([dict(entry)
                                for entry in closure.validation_results]
                               if closure else []),
        "telemetry_summary": (closure.telemetry_summary
                             if closure else None),
        "telemetry": telemetry,
        "artifacts": _artifacts(mission_root),
        "workspace": str(workspace),
        "objective_implemented": _implementation_present(workspace),
    }


class _NoopExecutor:
    """Fixture mode is never used for the real mission; fails loudly."""

    def execute(self, request: Any) -> Any:
        raise AssertionError("M032 real mission requires the live backend")


def _implementation_present(workspace: pathlib.Path) -> bool:
    cli_path = workspace / CLI_RELATIVE
    if not cli_path.is_file():
        return False
    source = cli_path.read_text(encoding="utf-8")
    return "run_id" in source and "IDENTITY_MISMATCH" in source


def _artifacts(mission_root: pathlib.Path) -> dict[str, str]:
    return {
        name: str(mission_root / name)
        for name in ("mission.json", "plan.json", "events.jsonl",
                     "status.json", "telemetry.json", "summary.json",
                     "closure.json", "recovery.json", "control.jsonl")
        if (mission_root / name).exists()
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mission032_real_mission_dogfood",
        description="M032 real non-fixture mission dogfood.")
    parser.add_argument("--root", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--live", action="store_true", default=True)
    parser.add_argument("--no-live", dest="live", action="store_false")
    parser.add_argument("--telemetry", default=obs_model.TELEMETRY_BENCHMARK)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--mission-id", dest="mission_id",
                        default="m032-real-status-identity")
    args = parser.parse_args(argv)

    root = pathlib.Path(args.root or pathlib.Path.cwd()
                        / ".trajectory-missions" / "m032")
    root.mkdir(parents=True, exist_ok=True)
    evidence = run(root, live=args.live, telemetry_mode=args.telemetry,
                   timeout_s=args.timeout, mission_id=args.mission_id)
    evidence["generated_at"] = (
        datetime.datetime.now(datetime.UTC)
        .replace(microsecond=0, tzinfo=None).isoformat() + "Z")
    out = pathlib.Path(args.out or root / "m032-evidence.json")
    out.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    print(f"M032 real mission: {evidence['readiness']} "
          f"(patch={evidence['current_patch']})")
    print(f"evidence: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
