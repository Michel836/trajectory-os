#!/usr/bin/env python3
"""M033 — real local process interruption / recovery dogfood harness.

This harness proves recovery beyond a fixture phase boundary by killing a
*real* mission process:

* a child process runs the M031 mission orchestrator with a deliberately slow
  executor that signals it has entered the implementation phase and then
  blocks;
* the parent delivers a real ``SIGKILL`` (or ``SIGTERM``) mid-implementation;
* the parent verifies the canonical durable evidence is intact and that no
  closure was written;
* a fresh child process resumes the *same* mission (identical
  ``mission_id == run_id``) with the normal deterministic executor;
* the parent verifies prior evidence was appended (never rewritten), the
  closure is recorded, and a second resume is byte-idempotent.

No Git trust-boundary write is performed. Exit 0 = all checks proven.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from trajectory_os.assembly import recovery, store
from trajectory_os.observability import model as obs_model
from trajectory_os.observability import store as obs_store

RUNNER = '''\
import argparse
import pathlib
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("command", choices=("start", "resume"))
parser.add_argument("--src", required=True)
parser.add_argument("--root", required=True)
parser.add_argument("--mission-id", required=True)
parser.add_argument("--marker", required=True)
parser.add_argument("--sleep", type=float, default=60.0)
args = parser.parse_args()
sys.path.insert(0, args.src)

from trajectory_os.assembly import orchestrator as run
from trajectory_os.benchmark.executor import FixtureExecutor
from trajectory_os.observability import run as obs_run

PASS = ("VERDICT: PASS\\nBLOCKERS:\\n- none\\nMAJORS:\\n- none\\n"
        "MINORS:\\n- none\\nFINAL RECOMMENDATION: GO COMMIT\\n")


class SlowExecutor:
    """Signals entry into implementation, then blocks to be killed."""

    def __init__(self, marker, sleep_s):
        self._marker = marker
        self._sleep = sleep_s

    def execute(self, request):
        pathlib.Path(self._marker).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(self._marker).write_text("implementing\\n",
                                              encoding="utf-8")
        time.sleep(self._sleep)
        return FixtureExecutor(interrupt_once=False).execute(request)


def main():
    root = args.root
    mission_id = args.mission_id
    workspace = str(pathlib.Path(root) / mission_id / "workspace")
    pathlib.Path(workspace).mkdir(parents=True, exist_ok=True)
    if args.command == "start":
        executor = SlowExecutor(args.marker, args.sleep)
        orchestrator = run.MissionOrchestrator(
            root, executor=executor,
            reviewer_factory=obs_run.scripted_reviewer_factory([PASS]))
        orchestrator.start(run.MissionRequest(
            objective="interruption-resume dogfood fixture",
            workspace=workspace, mission_id=mission_id,
            workload_id="small-targeted-repair"))
    else:
        orchestrator = run.MissionOrchestrator(
            root, executor=FixtureExecutor(interrupt_once=False),
            reviewer_factory=obs_run.scripted_reviewer_factory([PASS]))
        orchestrator.resume(mission_id)
    return 0


raise SystemExit(main())
'''


def _digest(mission_root: pathlib.Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(mission_root.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(mission_root)).encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _loadable(mission_root: pathlib.Path) -> bool:
    try:
        store.load_mission(mission_root.parent, mission_root.name)
        obs_store.load_status(mission_root)
        obs_store.load_events(mission_root)
    except Exception:  # noqa: BLE001 - any failure is a corrupt-evidence proof
        return False
    return True


def run_once(root: pathlib.Path, *, sig: int, mission_id: str,
             keep: bool) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    runner_path = root / "runner.py"
    runner_path.write_text(RUNNER, encoding="utf-8")
    marker = root / f"{mission_id}.marker"
    mission_root = store.mission_root(root, mission_id)
    proc = subprocess.Popen(
        [sys.executable, str(runner_path), "start", "--src", str(SRC),
         "--root", str(root), "--mission-id", mission_id,
         "--marker", str(marker), "--sleep", "60"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 30.0
    while not marker.exists() and time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    observed_implementing = marker.exists()
    signal_name = signal.Signals(sig).name
    if proc.poll() is None:
        os.kill(proc.pid, sig)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=15)
    exit_code = proc.returncode

    evidence: dict[str, Any] = {
        "signal": signal_name,
        "observed_implementing_marker": observed_implementing,
        "child_exit_code": exit_code,
        "evidence_loadable_after_kill": _loadable(mission_root),
        "closure_absent_after_kill": not store.closure_exists(root,
                                                              mission_id),
    }

    events_before = obs_store.load_events(mission_root)
    status_before = None
    try:
        status_before = obs_store.load_status(mission_root)
    except obs_store.CanonicalStoreError:
        status_before = None
    evidence["events_before_resume"] = len(events_before)
    evidence["status_before_resume"] = (
        None if status_before is None else status_before.get("state"))
    decision = recovery.detect_resume(root, mission_id)
    evidence["resume_point"] = decision.to_dict()

    resume = subprocess.run(
        [sys.executable, str(runner_path), "resume", "--src", str(SRC),
         "--root", str(root), "--mission-id", mission_id,
         "--marker", str(marker)],
        capture_output=True, text=True, check=False)
    evidence["resume_exit_code"] = resume.returncode
    evidence["resume_stderr"] = resume.stderr[-1000:]

    events_after = obs_store.load_events(mission_root)
    closure = store.load_closure(root, mission_id)
    status_after = obs_store.load_status(mission_root)
    evidence["events_after_resume"] = len(events_after)
    evidence["prior_evidence_preserved"] = (
        events_after[:len(events_before)] == events_before)
    evidence["same_mission_id"] = (
        status_after.get("run_id") == mission_id
        and closure.mission_id == mission_id)
    evidence["readiness_after_resume"] = status_after.get("readiness")
    evidence["lifecycle_after_resume"] = status_after.get("state")
    evidence["current_patch"] = status_after.get("current_patch")
    evidence["reviewed_patch"] = status_after.get("reviewed_patch")

    # Repeated resume after terminal must be byte-idempotent.
    digest_before = _digest(mission_root)
    events_terminal = obs_store.load_events(mission_root)
    second = subprocess.run(
        [sys.executable, str(runner_path), "resume", "--src", str(SRC),
         "--root", str(root), "--mission-id", mission_id,
         "--marker", str(marker)],
        capture_output=True, text=True, check=False)
    evidence["second_resume_exit_code"] = second.returncode
    evidence["repeated_resume_byte_idempotent"] = (
        _digest(mission_root) == digest_before
        and obs_store.load_events(mission_root) == events_terminal)

    ready_transitions = [
        event for event in obs_store.load_events(mission_root)
        if event.get("kind") == "HUMAN_GATE_REACHED"
        and event.get("result") == obs_model.RESULT_PASS]
    evidence["ready_for_commit_transitions"] = len(ready_transitions)
    evidence["no_duplicate_ready_transition"] = len(ready_transitions) <= 1
    if not keep:
        pass
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mission033_interrupt_dogfood",
        description="M033 real interruption/recovery dogfood.")
    parser.add_argument("--root", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--keep", action="store_true", default=False)
    args = parser.parse_args(argv)
    root = pathlib.Path(args.root or pathlib.Path.cwd()
                        / ".trajectory-missions" / "m033")
    root.mkdir(parents=True, exist_ok=True)

    results = {}
    checks: list[dict[str, Any]] = []

    def record(scenario: str, description: str, ok: bool) -> None:
        checks.append({"scenario": scenario, "description": description,
                       "ok": bool(ok)})

    for label, sig in (("SIGKILL", signal.SIGKILL),
                       ("SIGTERM", signal.SIGTERM)):
        mission_id = f"m033-{label.lower()}"
        evidence = run_once(root, sig=sig, mission_id=mission_id,
                            keep=args.keep)
        results[label] = evidence
        record(label, "implementation phase observed before the kill",
               evidence["observed_implementing_marker"])
        record(label, "canonical evidence loads after the kill",
               evidence["evidence_loadable_after_kill"])
        record(label, "no closure exists after the kill",
               evidence["closure_absent_after_kill"])
        record(label, "resume keeps the identical mission_id",
               evidence["same_mission_id"])
        record(label, "append-only prior evidence survives",
               evidence["prior_evidence_preserved"])
        record(label, "resume reaches READY_FOR_COMMIT",
               evidence["readiness_after_resume"]
               == obs_model.RD_READY_FOR_COMMIT)
        record(label, "no duplicate READY_FOR_COMMIT transition",
               evidence["no_duplicate_ready_transition"])
        record(label, "repeated resume after terminal is idempotent",
               evidence["repeated_resume_byte_idempotent"])

    report = {
        "mission": "M033",
        "generated_at": (datetime.datetime.now(datetime.UTC)
                         .replace(microsecond=0, tzinfo=None).isoformat()
                         + "Z"),
        "root": str(root),
        "results": results,
        "checks": checks,
        "status": "PASS" if all(c["ok"] for c in checks) else "FAIL",
    }
    out = pathlib.Path(args.out or root / "m033-interruption-evidence.json")
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    print(f"M033 interruption/recovery dogfood {report['status']}: {out}")
    for check in checks:
        print(f"  {'ok  ' if check['ok'] else 'FAIL'} "
              f"[{check['scenario']}] {check['description']}")
    return 0 if report["status"] == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
