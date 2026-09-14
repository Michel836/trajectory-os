"""Mission 001-D proof-harness repairs (PR #193 findings).

Covers the two harness fixes:

* the final best-effort child reap must be **bounded and non-busy-looping**:
  with a still-alive child it must give up after a small bounded budget
  (previously ``waitpid(-1, WNOHANG)`` in an unbounded loop spun forever);
* ``evidence-tree.txt`` must be generated **only after** all final artifacts
  exist, so the tree lists the final reports it and the report mutually
  reference (internally consistent evidence references).
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Any, cast

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "mission001_closure_proof.py"


def _load_harness_module() -> Any:
    spec = importlib.util.spec_from_file_location("_mission001_closure_proof", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(Any, module)


def test_final_reap_is_bounded_while_a_child_is_alive() -> None:
    """A still-alive child must not busy-spin the final cleanup forever."""
    harness = _load_harness_module()
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        start = time.monotonic()
        harness._reap_harness_children(budget_secs=0.3)
        elapsed = time.monotonic() - start
        # Bounded: it gave up after the small budget, it did not spin on the
        # still-alive child. And the live child is untouched (reap-only pass).
        assert elapsed < 5.0
        os.kill(child.pid, 0)  # raises if the child is no longer alive
    finally:
        child.kill()
        child.wait()


def test_final_reap_collects_exited_children_immediately() -> None:
    """Exited (unreaped -> zombie) children are still collected best-effort,
    and the collection happens promptly (no long wait)."""
    harness = _load_harness_module()
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.kill()  # dies; deliberately NOT .wait()ed -> zombie until reaped
    time.sleep(0.3)  # let it terminate
    start = time.monotonic()
    harness._reap_harness_children(budget_secs=1.0)
    elapsed = time.monotonic() - start
    try:
        os.waitpid(child.pid, os.WNOHANG)
        reaped_by_harness = False  # we could still reap it -> harness left it
    except ChildProcessError:
        reaped_by_harness = True
    assert reaped_by_harness
    assert elapsed < 5.0


def test_harness_run_produces_reports_and_consistent_evidence_tree(tmp_path: pathlib.Path) -> None:
    """End-to-end: the proof completes, and every evidence reference it and
    the tree list is a file that actually exists (tree generated last)."""
    root = tmp_path / "proof"
    out = root / "report"
    proc = subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--root", str(root),
            "--jobs", "2",
            "--deadline", "5",
            "--out", str(out),
        ],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr

    tree_path = root / "raw" / "evidence-tree.txt"
    assert tree_path.is_file()
    tree = tree_path.read_text(encoding="utf-8")
    # The tree is generated after the final reports, so it lists them ...
    assert "report/mission-001-closure-report.json" in tree
    assert "report/mission-001-closure-report.md" in tree

    # ... and every evidence path the report references exists on disk.
    report = json.loads((out / "mission-001-closure-report.json").read_text(encoding="utf-8"))
    references = list(report.get("evidence", [])) + list(
        report.get("production_path_proof", {}).get("evidence", [])
    )
    assert references
    for ref in references:
        assert pathlib.Path(ref).is_file(), ref
