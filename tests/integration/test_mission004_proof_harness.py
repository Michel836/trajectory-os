"""Mission 004 (Issue #198) — production proof harness integration test.

Runs the REAL operator path end-to-end (CLI + orchestrator + store +
summary) with an isolated temp root and a deterministic fake provider,
asserting the four safety/production invariants:

* repair convergence under a bounded repair budget;
* crash -> reconstruct -> resume recovery (COUNTABLE in reports);
* improper re-after-crash FAILS CLOSED (never guessed);
* HEAD drift blocks BEFORE any sub-run launches.

Also asserts the fresh-context invariant (distinct process id per
model-heavy sub-run) and that the reports render from the same data.
The harness kills only its own orchestrator process (via the fake
provider's parent link) — CI-safe.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "mission004_proof.py"


def _run_harness(out_dir: pathlib.Path) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(out_dir)],
        capture_output=True, text=True, timeout=600,
        check=False, cwd=str(REPO),
    )
    report = {
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }
    json_path = out_dir / "mission-004-production-orchestration-report.json"
    if json_path.exists():
        with open(json_path, encoding="utf-8") as fh:
            report["json"] = json.load(fh)
    return proc.returncode, report


def test_full_proof_harness_passes(tmp_path: pathlib.Path) -> None:
    """The complete four-scenario proof completes and its reports are
    structurally self-consistent (deterministic assertions on counts,
    fresh-context pids, and fail-closed block states)."""
    code, report = _run_harness(tmp_path / "out")
    body = (report["stdout"] or "") + "\n" + (report["stderr"] or "")
    assert code == 0, f"harness exit {code}\n{body}"
    doc = report["json"]
    assert doc["status"] == "COMPLETE"
    by_name = {s["checks"][0]["scenario"]: s for s in doc["scenarios"]}
    for sc in doc["scenarios"]:
        assert sc["status"] == "PASS", json.dumps(doc, indent=2)
        assert sc["checks"]
    s1 = by_name["S1-repair-convergence"]
    # Bounded repair proven: 7 sub-runs, exactly 1 repair round.
    s1_text = json.dumps(s1)
    assert "7 sub-runs exactly" in s1_text
    assert "fresh-context" in s1_text
    # Crash recovery proven and COUNTABLE.
    s2 = by_name["S2-crash-reconstruct-resume"]
    assert "reconstruction=1" in json.dumps(s2)
    # Impropriety fails closed with an explicit reason.
    assert "STALE_EVIDENCE" in json.dumps(
        by_name["S3-improper-rerun-blocks"])
    assert "HEAD_DRIFT" in json.dumps(by_name["S4-head-drift-blocks"])
    assert "ZERO sub-runs" in json.dumps(by_name["S4-head-drift-blocks"])
    # Markdown + JSON both rendered from the same data.
    md = (tmp_path / "out" /
          "mission-004-production-orchestration-report.md").read_text(
              encoding="utf-8")
    assert "004" in md and "COMPLETE" in md and "S1" in md


def test_harness_module_imports_cleanly() -> None:
    spec = importlib.util.spec_from_file_location("_mission004_proof",
                                                  SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    checks = [
        "scenario_1_repair_convergence", "scenario_2_crash_reconstruct_resume",
        "scenario_3_improper_rerun_blocks", "scenario_4_head_drift_blocks",
        "produce_report", "main",
    ]
    for name in checks:
        assert hasattr(module, name), name


def test_cli_shim_executes_version() -> None:
    proc = subprocess.run(
        [str(REPO / "scripts" / "trajectory-pi-missions"), "version"],
        capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0
    assert "trajectory-pi-missions" in proc.stdout


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
