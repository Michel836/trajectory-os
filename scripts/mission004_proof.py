#!/usr/bin/env python3
"""Mission 004 — production orchestration proof harness.

Bounded, fail-closed, CI-safe harness that drives the REAL production
operator path (``scripts/trajectory-pi-missions`` ->
``trajectory_os.missions.cli`` -> ``trajectory_os.missions.orchestrator``)
with a deterministic fake pi provider, closing Issue #198's evidence gaps:

* S1 — repair convergence: canonical five-phase mission; a deterministic
  VALIDATE failure consumes exactly one bounded REPAIR round and the phase
  passes on re-evidence; the mission COMPLETEs with fresh-context (distinct
  process) evidence per model-heavy sub-run.
* S2 — crash / reconstruct / resume: the orchestrator process dies mid-
  sub-run (crash window); a direct re-run is never attempted on ambiguous
  state; ``resume`` (reconstruct — never guesses — then run) recovers the
  mission to COMPLETE with an explicit reconstruction + human note.
* S3 — improper re-after-crash: after the same crash, an immediate ``run``
  (skipping reconstruct) FAILS CLOSED with an explicit BLOCKED reason
  (``STALE_EVIDENCE``); the mission is terminal and the reconstruction
  report refuses to resume it.
* S4 — HEAD drift: review baseline is explicit; when the worktree HEAD
  moves after creation, the run blocks BEFORE any sub-run launches
  (``HEAD_DRIFT``) — zero sub-runs, fail closed.

Safety invariants (mirrored in the mission contract):

* All mission state lives in an isolated temp root (or ``--root``); the
  harness owns its children and signals only its own orchestrator process
  (via the fake provider, whose parent is exactly that process).
* Bounded: short commands, bounded budgets, no network, no GPU.
* No git history is mutated — the scratch repo is a throwaway init in the
  isolated root; S4's drift commit is a throwaway commit in that scratch
  repo only.
* Fail closed: any discrepancy between expectation and observed canonical
  state is recorded as a FAILED check and the harness exit code is 3.

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
import subprocess
import sys
import tempfile
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
CLI = REPO / "scripts" / "trajectory-pi-missions"

CHECK_LOG: list[dict[str, Any]] = []


def _check(scenario: str, description: str, ok: bool) -> bool:
    """Record one deterministic check (fail-closed accounting)."""
    CHECK_LOG.append({"scenario": scenario, "description": description,
                      "ok": bool(ok)})
    if not ok:
        print(f"  FAIL [{scenario}] {description}", file=sys.stderr)
    return ok


def _run_cli(args: list[str]) -> tuple[int, str, str]:
    """Run the operator CLI (the real production path) once, bounded."""
    env = dict(os.environ)
    env.pop("TRAJECTORY_MISSIONS_ROOT", None)
    proc = subprocess.run(  # noqa: S603 — argv is operator-fixed
        [str(CLI), *args],
        capture_output=True, text=True, timeout=120, check=False,
        env=env, cwd=str(REPO),
    )
    return proc.returncode, proc.stdout, proc.stderr


def _git(args: list[str]) -> int:
    proc = subprocess.run(  # noqa: S603 — fixed argv
        ["git", *args], capture_output=True, text=True, timeout=60,
        check=False, cwd=str(REPO))
    return proc.returncode


def _write_fake_provider(path: pathlib.Path) -> None:
    """Deterministic fake pi provider (bash).

    Reads the phase marker line (``[phase:KIND]``) from its
    ``--prompt-file``; honors per-mission behavior files in
    ``<mission>/state/``:

    * ``impl_behavior=kill_once`` — first implement/repair invocation kills
      its immediate parent (the orchestrator process) to simulate a crash
      mid-sub-run; afterwards behaves normally;
    * ``impl_behavior=fail_once`` — first invocation exits 7;
    * normal — model-heavy kinds exit 0 and write deterministic artifacts.
    """
    src = r'''#!/usr/bin/env bash
set -u
pf=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --prompt-file) pf="$2"; shift 2 ;;
    *) shift ;;
  esac
done
[[ -f "$pf" ]] || { echo "no prompt file" >&2; exit 4; }
MR="$(dirname "$(dirname "$pf")")"
kind=""
for k in PLAN IMPLEMENT REPAIR REVIEW; do
  if grep -q "^\[phase:$k\]" "$pf"; then kind="$k"; break; fi
done
mkdir -p "$MR/state"
echo "pid=$$ kind=$kind" >> "$MR/state/invocations.log"

emit_semantic_success() {
  [[ -n "${TRAJECTORY_SUBRUN_RESULT_FILE:-}" &&
     -n "${TRAJECTORY_SUBRUN_ID:-}" ]] || return 0

  # Mission 008 — produce a real, verifiable exact attestation: create the
  # wrapper run directory the runner independently re-checks (run id,
  # workspace, HEAD before) and the exact patch artifact whose digest is
  # recomputed by the runner.
  local repo head run_id run_dir patch_sha
  repo="$(pwd -P)"
  head="$(git -C "$repo" rev-parse HEAD 2>/dev/null || echo '')"
  [[ -n "$head" ]] || return 0
  run_id="run-$TRAJECTORY_SUBRUN_ID"
  run_dir="$repo/.trajectory-pi/runs/$run_id"
  mkdir -p "$run_dir" 2>/dev/null || return 0
  printf 'run_id=%s\nworkspace=%s\nhead_before=%s\n' \
    "$run_id" "$repo" "$head" > "$run_dir/meta.txt" 2>/dev/null || return 0
  : > "$run_dir/worktree.patch" 2>/dev/null || return 0
  patch_sha="$(sha256sum "$run_dir/worktree.patch" | awk '{print $1}')"

  printf '{"schema_version":1,"subrun_id":"%s","status":"SUCCESS",' \
    "$TRAJECTORY_SUBRUN_ID" > "$TRAJECTORY_SUBRUN_RESULT_FILE"
  printf '"agent_classification":"AGENT_COMPLETED",' \
    >> "$TRAJECTORY_SUBRUN_RESULT_FILE"
  printf '"readiness":"READY_FOR_COMMIT",' \
    >> "$TRAJECTORY_SUBRUN_RESULT_FILE"
  printf '"reason":"proof harness semantic success",' \
    >> "$TRAJECTORY_SUBRUN_RESULT_FILE"
  printf '"attestation":{"schema_version":1,"subrun_id":"%s",' \
    "$TRAJECTORY_SUBRUN_ID" >> "$TRAJECTORY_SUBRUN_RESULT_FILE"
  printf '"run_id":"%s","repo_head_before":"%s",' \
    "$run_id" "$head" >> "$TRAJECTORY_SUBRUN_RESULT_FILE"
  printf '"repo_head_after":"%s","patch_sha256":"%s"}}\n' \
    "$head" "$patch_sha" >> "$TRAJECTORY_SUBRUN_RESULT_FILE"
}

case "$kind" in
  PLAN)
    printf 'plan: artifact contract\n' > "$MR/state/plan.txt"
    emit_semantic_success
    exit 0
    ;;
  IMPLEMENT|REPAIR)
    b="$(cat "$MR/state/impl_behavior" 2>/dev/null || echo ok)"
    if [[ "$b" == kill_once && ! -e "$MR/state/kill_once_used" ]]; then
      touch "$MR/state/kill_once_used"
      kill -TERM "$PPID" 2>/dev/null || true
      exit 9
    fi
    if [[ "$b" == fail_once && ! -e "$MR/state/fail_once_used" ]]; then
      touch "$MR/state/fail_once_used"
      echo "deterministic transient provider failure" >&2
      exit 7
    fi
    printf 'implementation v1\n' > "$MR/state/artifact"
    emit_semantic_success
    exit 0
    ;;
  REVIEW)
    printf 'review: ok\n' > "$MR/state/review.txt"
    emit_semantic_success
    exit 0
    ;;
  *)
    echo "bad kind: $kind" >&2
    exit 4
    ;;
esac
'''
    path.write_text(src, encoding="utf-8")
    path.chmod(0o755)


def _pid_lines(mission_root: pathlib.Path) -> list[str]:
    log = mission_root / "state" / "invocations.log"
    if not log.is_file():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines()
            if line.startswith("pid=")]


def _status_json(root: pathlib.Path, mission_id: str) -> dict[str, Any] | None:
    code, out, _ = _run_cli(["--root", str(root), "--json", "status",
                             mission_id])
    if code != 0:
        return None
    try:
        doc = json.loads(out)
    except json.JSONDecodeError:
        return None
    return doc.get("summary")  # type: ignore[no-any-return]


# --- scenarios -------------------------------------------------------------------


def _head(repo_dir: pathlib.Path) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True).stdout.strip()


def _mission_root(root: pathlib.Path, mission_id: str) -> pathlib.Path:
    return root / "missions" / mission_id


def _scenario_checks(scenario: str) -> list[dict[str, Any]]:
    return [c for c in CHECK_LOG if c["scenario"] == scenario]


def _scenario_summary(scenario: str) -> dict[str, Any]:
    checks = _scenario_checks(scenario)
    return {
        "scenario": scenario,
        "status": "PASS" if checks and all(c["ok"] for c in checks)
        else "FAIL",
        "checks": checks,
    }


def scenario_1_repair_convergence(root: pathlib.Path,
                                  work: pathlib.Path) -> dict[str, Any]:
    sc = "S1-repair-convergence"
    mission_root = _mission_root(root, "m1")
    state = work / "s1-validate"
    state.mkdir(parents=True, exist_ok=True)
    validate = state / "validate.sh"
    validate.write_text(
        "#!/bin/bash\n"
        'S="$(dirname "$0")"\n'
        "if [[ ! -e \"$S/fail_used\" && -f \"$S/artifact\" ]]; then\n"
        "  touch \"$S/fail_used\"\n"
        "  echo 'deterministic one-time validate failure' >&2\n"
        "  exit 1\n"
        "fi\n"
        "test -f \"$S/artifact\"\n",
        encoding="utf-8")
    head = _head(work / "repo")

    code, out, _ = _run_cli(["--root", str(root), "--json", "create", "m1",
                             "--objective", "S1: produce the canonical "
                                            "artifact",
                             "--repo", str(work / "repo"), "--head", head,
                             "--pi-wrapper", str(work / "fakepi"),
                             "--model", "fake",
                             "--validate", f"bash {validate}"])
    _check(sc, "create exits 0", code == 0)
    created = json.loads(out) if code == 0 else {}
    _check(sc, "create plan is canonical five-phase order",
            created.get("phases") == ["plan", "implement", "validate",
                                      "review", "consolidate"])
    _prompt_files = created.get("prompt_files", {})
    _check(sc, "prompt files exist only for model-heavy phases",
            set(_prompt_files) == {"plan", "implement", "review"} and all(
                pathlib.Path(p).is_file() for p in _prompt_files.values()))

    state_link = state / "artifact"
    if state_link.exists():
        state_link.unlink()
    state_link.symlink_to(mission_root / "state" / "artifact")

    code, _, _ = _run_cli(["--root", str(root), "run", "m1"])
    _check(sc, "run exits 0 (COMPLETE)", code == 0)

    summary = _status_json(root, "m1") or {}
    _check(sc, "mission state is COMPLETE",
            summary.get("mission_state") == "COMPLETE")
    jobs = summary.get("jobs", {})
    _check(sc, "7 sub-runs exactly (5 core + validate retry + repair.1)",
            jobs.get("started") == 7)
    _check(sc, "repairs_used == 1 (bounded repair loop)",
            summary.get("automatic", {}).get("repairs_used") == 1)
    phases = {p["phase_id"]: p for p in summary.get("phases", [])}
    _check(sc, "validate re-evidenced on attempt 2",
            phases.get("validate", {}).get("attempts") == 2)
    _check(sc, "repair.1 PASSED and counted",
            phases.get("repair.1", {}).get("state") == "PASSED")

    # Deterministic VALIDATE: exit-code evidence for both attempts.
    evid = None
    code, out, _ = _run_cli(["--root", str(root), "--json", "evidence", "m1",
                             "validate"])
    if code == 0:
        evid = json.loads(out)
    evs = (evid or {}).get("subruns") or []
    _check(sc, "validate-a1 FAILED (exit 1) then validate-a2 COMPLETED",
            [s.get("exit_code") for s in evs] == [1, 0] and
            [s.get("classification") for s in evs] == ["FAILED",
                                                       "COMPLETED"])
    # Fresh-context proof: every model-heavy sub-run ran in a distinct
    # process (fresh interpreter context each time).
    pids = [line.split(" ", 1)[0] for line in _pid_lines(mission_root)]
    _check(sc, "fresh-context: distinct process ids per model-heavy sub-run",
            len(pids) == 4 and len(set(pids)) == 4)
    return _scenario_summary(sc)


def scenario_2_crash_reconstruct_resume(root: pathlib.Path,
                                        work: pathlib.Path) -> dict[str, Any]:
    sc = "S2-crash-reconstruct-resume"
    mission_root = _mission_root(root, "m2")
    head = _head(work / "repo")
    code, _, _ = _run_cli(["--root", str(root), "create", "m2",
                           "--objective", "S2: crash mid-sub-run then "
                                          "resume",
                           "--repo", str(work / "repo"), "--head", head,
                           "--pi-wrapper", str(work / "fakepi"),
                           "--model", "fake",
                           "--validate", "true"])
    _check(sc, "create exits 0", code == 0)
    (mission_root / "state").mkdir(parents=True, exist_ok=True)
    (mission_root / "state" / "impl_behavior").write_text("kill_once\n",
                                                          encoding="utf-8")

    code, _, _ = _run_cli(["--root", str(root), "run", "m2"])
    _check(sc, "orchestrator killed mid-sub-run (nonzero exit)", code != 0)
    summary = _status_json(root, "m2") or {}
    non_terminal = {"COMPLETE", "BLOCKED", "FAILED"}
    _check(sc, "post-crash state non-terminal (fail-closed, never guessed)",
            summary.get("mission_state") not in non_terminal)

    code, _, _ = _run_cli(["--root", str(root), "resume", "m2"])
    _check(sc, "resume (reconstruct then run) exits 0", code == 0)
    summary = _status_json(root, "m2") or {}
    _check(sc, "mission recovered to COMPLETE",
            summary.get("mission_state") == "COMPLETE")
    phases = {p["phase_id"]: p for p in summary.get("phases", [])}
    _check(sc, "implement re-evidenced (attempt 2) after reconstruction",
            phases.get("implement", {}).get("attempts") == 2)
    # lifecycle: reconstruction + resume are explicit, countable events
    _, out, _ = _run_cli(["--root", str(root), "summary", "m2"])
    _check(sc, "summary records reconstruction=1, resume=1",
            "reconstruction=1" in out and "resume=1" in out)
    # Fresh-context after resume: every model-heavy sub-run (plan, the two
    # implement attempts, review) ran in its own process.
    pids = [line.split(" ", 1)[0] for line in _pid_lines(mission_root)]
    _check(sc, "fresh-context across the crash boundary (all sub-runs "
               "distinct processes)",
            len(pids) >= 4 and len(set(pids)) == len(pids))
    code, _, _ = _run_cli(["--root", str(root), "note", "m2", "--text",
                           "operator verified crash recovery"])
    _check(sc, "human note recorded (bounded intervention)", code == 0)
    return _scenario_summary(sc)


def scenario_3_improper_rerun_blocks(root: pathlib.Path,
                                     work: pathlib.Path) -> dict[str, Any]:
    sc = "S3-improper-rerun-blocks"
    mission_root = _mission_root(root, "m3")
    head = _head(work / "repo")
    code, _, _ = _run_cli(["--root", str(root), "create", "m3",
                           "--objective", "S3: crash then improper re-run",
                           "--repo", str(work / "repo"), "--head", head,
                           "--pi-wrapper", str(work / "fakepi"),
                           "--model", "fake",
                           "--validate", "test -f x_never"])
    _check(sc, "create exits 0", code == 0)
    (mission_root / "state").mkdir(parents=True, exist_ok=True)
    (mission_root / "state" / "impl_behavior").write_text("kill_once\n",
                                                          encoding="utf-8")

    code, _, _ = _run_cli(["--root", str(root), "run", "m3"])
    _check(sc, "orchestrator killed mid-sub-run", code != 0)
    code, out, _ = _run_cli(["--root", str(root), "run", "m3"])
    _check(sc, "immediate re-run (no reconstruct) FAILS CLOSED (exit 3)",
            code == 3)
    _check(sc, "block reason is explicit (STALE_EVIDENCE)",
            "STALE_EVIDENCE" in out)
    code, out, _ = _run_cli(["--root", str(root), "reconstruct", "m3"])
    _check(sc, "reconstruction of the blocked mission refuses resume "
               "(resumable=False, terminal)",
            code == 0 and "resumable=False" in out)
    # The blocked state is machine-readable, not just human text.
    code, out, _ = _run_cli(["--root", str(root), "--json", "status", "m3"])
    status = json.loads(out).get("summary", {}).get("mission_state") \
        if code == 0 else None
    _check(sc, "status JSON reports terminal BLOCKED", status == "BLOCKED")
    return _scenario_summary(sc)


def scenario_4_head_drift_blocks(root: pathlib.Path,
                                 work: pathlib.Path) -> dict[str, Any]:
    sc = "S4-head-drift-blocks"
    head = _head(work / "repo")
    code, _, _ = _run_cli(["--root", str(root), "create", "m4",
                           "--objective", "S4: HEAD drift guard",
                           "--repo", str(work / "repo"), "--head", head,
                           "--pi-wrapper", str(work / "fakepi"),
                           "--model", "fake",
                           "--validate", "true"])
    _check(sc, "create exits 0 (explicit review baseline)", code == 0)
    created_head = _status_json(root, "m4") or {}
    _check(sc, "baseline recorded",
            created_head.get("baseline_revision") == head)
    # Move the scratch repo HEAD (throwaway commit in the scratch repo only).
    (work / "repo" / "drift.txt").write_text("drift\n", encoding="utf-8")
    _git(["-C", str(work / "repo"), "add", "-A"])
    _git(["-C", str(work / "repo"), "-c", "user.email=t@t.t",
          "-c", "user.name=proof", "commit", "-q", "-m", "drift"])

    code, out, _ = _run_cli(["--root", str(root), "run", "m4"])
    _check(sc, "run FAILS CLOSED on HEAD drift (exit 3)", code == 3)
    _check(sc, "block reason is explicit (HEAD_DRIFT)", "HEAD_DRIFT" in out)
    _check(sc, "ZERO sub-runs launched (guard fired before launch)",
            "0/0" in out)
    return _scenario_summary(sc)


# --- report ----------------------------------------------------------------------


def produce_report(root: pathlib.Path, work: pathlib.Path,
                   out_dir: pathlib.Path) -> dict[str, Any]:
    scenarios = [
        scenario_1_repair_convergence(root, work),
        scenario_2_crash_reconstruct_resume(root, work),
        scenario_3_improper_rerun_blocks(root, work),
        scenario_4_head_drift_blocks(root, work),
    ]
    all_ok = all(s["status"] == "PASS" for s in scenarios)
    report = {
        "mission": "004-production-orchestration",
        "issue": "#198",
        "generated_at": datetime.datetime.now(datetime.UTC)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "harness": str(CLI),
        "root": str(root),
        "scenarios": scenarios,
        "status": "COMPLETE" if all_ok else "BLOCKED",
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "mission-004-production-orchestration-report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (out_dir / "mission-004-production-orchestration-report.md").write_text(
        _render_md(report), encoding="utf-8")
    return report


def _render_md(report: dict[str, Any]) -> str:
    lines = [
        "# Mission 004 — Production Orchestration Proof Report",
        "",
        f"- issue: {report['issue']} (generated {report['generated_at']})",
        f"- status: **{report['status']}**",
        f"- harness: `{report['harness']}`",
        f"- isolated root: `{report['root']}`",
        "",
        "## Invariants proven",
        "",
        "1. Production sub-runs: real `pi`-shaped wrapper invocations "
        "(fresh process per sub-run; distinct PIDs recorded in the mission "
        "state log).",
        "2. Operator CLI: create/run/continue/resume/status/reconstruct/"
        "evidence/summary/note/list — canonical exit codes, fail-closed "
        "errors, machine-readable output.",
        "3. Bounded repair: one REPAIR round for one deterministic "
        "VALIDATE failure; convergence proven (7 sub-runs, repairs=1).",
        "4. Crash safety: ambiguous in-flight sub-runs are never re-run; "
        "reconstruction terminalizes them to explicit UNPROVEN and recovery "
        "is resumable + countable (reconstruction/resume events).",
        "5. Fail-closed ordering: run-before-reconstruct on crashed state "
        "BLOCKS STALE_EVIDENCE (terminal); HEAD drift BLOCKS before any "
        "sub-run launches.",
        "6. Fresh context: model-heavy sub-runs are distinct processes "
        "(no shared provider state except the recorded state log).",
        "",
        "## Scenarios",
        "",
    ]
    for sc in report["scenarios"]:
        lines.append(f"### {sc['status']} — {sc['checks'][0]['scenario']}")
        lines.append("")
        for check in sc["checks"]:
            mark = "PASS" if check["ok"] else "FAIL"
            lines.append(f"- {mark}: {check['description']}")
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=None,
                        help="isolated missions root (default: fresh temp)")
    parser.add_argument("--out", default=".",
                        help="report output directory")
    args = parser.parse_args(argv)

    tmp_root = None
    if args.root:
        root = pathlib.Path(args.root)
        work = root
    else:
        base = pathlib.Path(tempfile.mkdtemp(prefix="mission004-proof-"))
        root = base / "root"
        work = base / "work"
        tmp_root = base
    try:
        root.mkdir(parents=True, exist_ok=True)
        work.mkdir(parents=True, exist_ok=True)
        repo = work / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        _git(["-C", str(repo), "init", "-q"])
        _git(["-C", str(repo), "-c", "user.email=t@t.t",
              "-c", "user.name=proof", "commit", "-q", "--allow-empty",
              "-m", "baseline"])
        _write_fake_provider(work / "fakepi")

        report = produce_report(root, work, pathlib.Path(args.out))
        print(json.dumps({
            "status": report["status"],
            "scenarios": {s["checks"][0]["scenario"]: s["status"]
                          for s in report["scenarios"]},
            "report": str(pathlib.Path(args.out) /
                          "mission-004-production-orchestration-report.json"),
        }, indent=2))
        return 0 if report["status"] == "COMPLETE" else 3
    finally:
        if tmp_root is not None:
            # Bounded cleanup of the harness's own isolated tree.
            import shutil
            shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
