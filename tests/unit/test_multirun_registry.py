"""Deterministic read-only run registry (V1.85) + unified state model (V1.86).

Evidence model under test (mirrors the producer contract):
  meta.txt                KV (run_id/pid/workspace/started_at/ended_at/...)
  validation.json         {"overall_status": "PASS"|...}
  review-meta.txt         KV review_status=...
  final-verify-meta.txt   KV final_verify_status=... (+canonical patch sha256)
  worktree.patch.sha256   first token = patch sha256
  worktree-metadata.txt   KV patch_sha256 / snapshot_status=COMPLETE
  diff-check.txt          "DIFF CHECK: PASS"
  repair-summary.txt      KV convergence="N of M attempts ..."

Covers: newest-first deterministic ordering; stable filters + limit;
fail-closed (missing / malformed / contradictory evidence never yields
ready or unknown-ok); repair evidence (attempts + convergence + used
attempts); CLI exit codes (0 ok, 2 usage, 4 unknown run).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from trajectory_os.runs import cli, query, registry
from trajectory_os.runs.model import (
    FILTER_ACTIVE,
    FILTER_FAILED,
    FILTER_INCOMPLETE,
    FILTER_READY,
    FILTER_RECENT,
    FILTER_REPAIRED,
)

SHA_A = hashlib.sha256(b"canonical-v1").hexdigest()
SHA_B = hashlib.sha256(b"canonical-v1-different").hexdigest()


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _meta(ran_id: str, *, started: str | None = "2026-09-12T12:00:00+02:00",
          ended: str | None = None, pid: str | None = None) -> str:
    lines = [
        "trajectory_pi_version=0.4.0",
        f"run_id={ran_id}",
    ]
    if started is not None:
        lines.append(f"started_at={started}")
    if ended is not None:
        lines.append(f"ended_at={ended}")
    if pid is not None:
        lines.append(f"pid={pid}")
        lines.append("workspace=/tmp/irrelevant")
    lines += ["run_class=repair", "branch=feat/test", "model=test-model"]
    return "\n".join(lines) + "\n"


def _full_positive(run_id: str, root: Path) -> None:
    """A complete, consistent, positive evidence chain (=> ready)."""
    _write(root, f"{run_id}/meta.txt", _meta(run_id, ended="2026-09-12T12:07:00+02:00"))
    _write(root, f"{run_id}/validation.json",
           '{"schema_version": 1, "overall_status": "PASS", "checks": []}\n')
    _write(root, f"{run_id}/review-meta.txt", "review_status=PASS\nreview_summary=ok\n")
    _write(root, f"{run_id}/final-verify-meta.txt",
           f"final_verify_status=PASS\ncanonical_patch_sha256={SHA_A}\n")
    _write(root, f"{run_id}/worktree.patch.sha256", f"{SHA_A}\n")
    _write(root, f"{run_id}/worktree-metadata.txt",
           f"snapshot_status=COMPLETE\npatch_sha256={SHA_A}\n")
    _write(root, f"{run_id}/diff-check.txt", "DIFF CHECK: PASS\nfiles=3\n")


def _build_tree(root: Path) -> None:
    # ready-run: complete positive chain.
    _full_positive("20260912-120000", root)

    # failed-run: complete, *consistent* evidence with definitive gate fails.
    d = "20260912-110000"
    _write(root, f"{d}/meta.txt", _meta(d, started="2026-09-12T11:00:00+02:00",
                                        ended="2026-09-12T11:07:00+02:00"))
    _write(
        root, f"{d}/validation.json",
        '{"schema_version": 1, "overall_status": "FAIL", "checks": []}\n',
    )
    _write(root, f"{d}/review-meta.txt", "review_status=FAIL\n")
    _write(root, f"{d}/final-verify-meta.txt", "final_verify_status=FAIL\n")
    _write(root, f"{d}/worktree.patch.sha256", f"{SHA_A}\n")
    _write(
        root, f"{d}/worktree-metadata.txt",
        "snapshot_status=COMPLETE\npatch_sha256=" + SHA_A + "\n",
    )
    _write(root, f"{d}/diff-check.txt", "DIFF CHECK: FAIL\nfiles=9\n")

    # stale-run: started, never ended, dead/unrecorded owner => unknown lifecycle.
    d = "20260912-100000"
    _write(root, f"{d}/meta.txt", _meta(d, pid="99999999"))

    # repair-run: positive chain + repair convergence evidence.
    d = "20260912-130000"
    _full_positive(d, root)
    _write(root, f"{d}/repair-summary.txt",
           "convergence=green on first pass\nlast_error=flake\n")

    # invalid-run: contradictory patch identity (sha mismatch).
    d = "20260912-140000"
    _full_positive(d, root)
    _write(root, f"{d}/worktree-metadata.txt",
           "snapshot_status=COMPLETE\npatch_sha256=" + SHA_B + "\n")

    # malformed-run: unparseable evidence document.
    d = "20260912-150000"
    _write(root, f"{d}/meta.txt", _meta(d, ended="2026-09-12T15:10:00+02:00"))
    _write(root, f"{d}/validation.json", "{this is not json")


class TestRegistry:
    def test_newest_first_deterministic_order(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        _build_tree(runs_root)
        runs_a = registry.load_runs(runs_root)
        runs_b = registry.load_runs(runs_root)  # second load: deterministic
        assert [r.run_id for r in runs_a] == [r.run_id for r in runs_b]
        order = [r.run_id for r in runs_a]
        assert order == sorted(order, reverse=True)
        assert len(order) == 6

    def test_filter_active_finds_only_proven_live(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        _build_tree(runs_root)
        active = query.apply_filter(registry.load_runs(runs_root), FILTER_ACTIVE, None)
        # no run has a live owner in the fixture => none are active
        assert active == []
        assert "20260912-100000" not in [r.run_id for r in active]
        # it must be visible as incomplete (unknown lifecycle) instead
        view = query.find_run(runs_root, "20260912-100000")
        assert view is not None and view.state == "incomplete"

    def test_filter_recent_window_and_limit(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        _build_tree(runs_root)
        runs = registry.load_runs(runs_root)
        recent = query.apply_filter(runs, FILTER_RECENT, None)
        assert len(recent) == 6  # default recent window
        limited = query.apply_filter(runs, FILTER_RECENT, 3)
        assert len(limited) == 3
        assert [r.run_id for r in limited] == [r.run_id for r in recent[:3]]

    def test_limit_bounds_results(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        _build_tree(runs_root)
        runs = registry.load_runs(runs_root)
        assert len(query.apply_filter(runs, "all", 2)) == 2
        assert query.apply_filter(runs, "all", 0) == []

    def test_ready_requires_full_positive_chain(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        _build_tree(runs_root)
        by_id = {r.run_id: r for r in registry.load_runs(runs_root)}
        ready = by_id["20260912-120000"]
        assert ready.state == "ready"
        assert ready.patch_consistent is True
        assert ready.patch_sha256 == SHA_A
        # failed: complete evidence with fails; never ready
        failed = by_id["20260912-110000"]
        assert failed.state == "failed"
        assert "GATE_FAIL_REVIEW" in failed.state_reasons
        # contradictory identity: malformed => invalid, never ready
        invalid = by_id["20260912-140000"]
        assert invalid.state == "invalid"
        assert "patch_identity_mismatch" in invalid.evidence_problems
        # malformed document: invalid, never fabricated
        malformed = by_id["20260912-150000"]
        assert malformed.state == "invalid"

    def test_repair_evidence_exposed(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        _build_tree(runs_root)
        repaired = query.find_run(runs_root, "20260912-130000")
        assert repaired is not None
        assert repaired.repaired.requested is True
        assert repaired.repaired.used_attempts == 0
        assert "first pass" in (repaired.repaired.convergence or "")
        assert repaired.state == "ready"
        rep = query.apply_filter(registry.load_runs(runs_root), FILTER_REPAIRED, None)
        assert [r.run_id for r in rep] == ["20260912-130000"]

    def test_repair_attempts_from_attempt_dirs(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        d = "20260912-160000"
        _full_positive(d, runs_root)
        (runs_root / d / "repair" / "attempt-01").mkdir(parents=True)
        (runs_root / d / "repair" / "attempt-02").mkdir(parents=True)
        (runs_root / d / "repair" / "attempt-03").mkdir(parents=True)
        view = query.find_run(runs_root, d)
        assert view is not None
        assert view.repaired.used_attempts == 3

    def test_evidence_conflict_blocks_ready(self, tmp_path: Path) -> None:
        """sha A vs sha B mismatch must never yield `ready` (stale evidence)."""
        runs_root = tmp_path / "runs"
        d = "20260912-170000"
        _full_positive(d, runs_root)
        _write(runs_root, f"{d}/worktree.patch.sha256", "abc" + "1" * 31 + "\n")
        view = query.find_run(runs_root, d)
        assert view is not None
        assert view.state != "ready"
        assert view.readiness in (None, "unavailable") or view.state != "ready"

    def test_missing_evidence_never_fabricated(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        d = "20260912-180000"
        (runs_root / d).mkdir(parents=True)
        _write(runs_root, f"{d}/meta.txt",
               f"run_id={d}\nstarted_at=2026-09-12T18:00:00+02:00\n"
               "ended_at=2026-09-12T18:10:00+02:00\n")
        view = query.find_run(runs_root, d)
        assert view is not None
        assert view.state == "incomplete"
        assert "MISSING_VALIDATION" in view.state_reasons
        assert "validation" in view.evidence_missing
        assert "UNKNOWN" not in view.state_reasons

    def test_registry_payload_deterministic(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        _build_tree(runs_root)
        p1 = registry.registry_payload(runs_root)
        p2 = registry.registry_payload(runs_root)
        assert p1 == p2  # deterministic: no now() timestamps leak
        assert p1["schema_version"] == 1
        assert p1["run_count"] == 6
        assert p1["order"] == "newest_first"

    def test_run_id_conflict_reported_not_invented(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        d = "20260912-190000"
        _write(runs_root, f"{d}/meta.txt",
               "run_id=19990101-000000\nstarted_at=2026-09-12T19:00:00+02:00\n"
               "ended_at=2026-09-12T19:10:00+02:00\n")
        view = query.find_run(runs_root, d)
        assert view is not None
        assert view.run_id == d  # directory name remains authoritative (not invented)
        assert "meta_run_id_conflicts" in view.evidence_problems

    def test_non_run_directories_skipped(self, tmp_path: Path) -> None:
        root = tmp_path / "runs"
        (root / "notes").mkdir(parents=True)
        (root / "notes" / "readme.md").write_text("not a run", encoding="utf-8")
        (root / "2026091-10000").mkdir(parents=True)  # bad id shape
        assert registry.load_runs(root) == []


class TestQuery:
    def test_unknown_filter_raises_deterministic_code(self, tmp_path: Path) -> None:
        try:
            query.apply_filter([], "bogus-filter", None)
        except query.UnknownFilterError as exc:
            assert exc.filter_name == "bogus-filter"
        else:  # pragma: no cover - defensive
            raise AssertionError("expected UnknownFilterError")

    def test_find_run_by_id_and_miss(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        _build_tree(runs_root)
        found = query.find_run(runs_root, "20260912-130000")
        assert found is not None and found.repaired.requested
        assert query.find_run(runs_root, "00000000-000000") is None

    def test_filter_sets_are_consistent(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        _build_tree(runs_root)
        runs = registry.load_runs(runs_root)
        all_ids = {r.run_id for r in query.apply_filter(runs, "all", None)}
        ready = {r.run_id for r in query.apply_filter(runs, FILTER_READY, None)}
        failed = {r.run_id for r in query.apply_filter(runs, FILTER_FAILED, None)}
        invalid = {r.run_id for r in runs if r.state == "invalid"}
        incomplete = {r.run_id for r in query.apply_filter(runs, FILTER_INCOMPLETE, None)}
        assert all_ids == ready | failed | invalid | incomplete
        assert not ready & failed
        assert not ready & invalid


class TestCliSurface:
    def test_list_and_status_exit_codes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runs_root = tmp_path / "runs"
        _build_tree(runs_root)
        code = cli.main(["list", "--runs-root", str(runs_root), "--limit", "2"])
        out = capsys.readouterr().out
        assert code == 0
        assert ("run_id" in out.lower()) or ("run_id" in out) or ("RUN_ID" in out)
        code = cli.main(["status", "20260912-130000", "--runs-root", str(runs_root)])
        assert code == 0
        code = cli.main(["status", "00000000-000000", "--runs-root", str(runs_root)])
        assert code == cli.EXIT_UNKNOWN

    def test_json_output_reports_state(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runs_root = tmp_path / "runs"
        _build_tree(runs_root)
        code = cli.main(["list", "--runs-root", str(runs_root), "--json", "--filter", "ready"])
        out = capsys.readouterr().out
        assert code == 0
        assert "20260912-120000" in out
        assert '"ready"' in out

    def test_bad_limit_is_usage_error(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        runs_root.mkdir(parents=True, exist_ok=True)
        code = cli.main(["list", "--runs-root", str(runs_root), "--limit", "abc"])
        assert code == cli.EXIT_USAGE

    def test_bad_filter_is_usage_error(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        runs_root.mkdir(parents=True, exist_ok=True)
        try:
            cli.main(["list", "--runs-root", str(runs_root), "--filter", "bogus"])
        except SystemExit as exc:  # argparse choices => exit 2
            assert exc.code == 2
        else:  # pragma: no cover - defensive
            raise AssertionError("expected SystemExit(2)")


def test_stale_live_pid_never_active_without_workspace(tmp_path: Path) -> None:
    """No workspace evidence + dead pid => unknown lifecycle (fail closed)."""
    runs_root = tmp_path / "runs"
    d = "20260912-210000"
    _write(runs_root, f"{d}/meta.txt", f"run_id={d}\nstarted_at=2026-09-12T21:00:00+02:00\n")
    view = query.find_run(runs_root, d)
    assert view is not None
    assert view.lifecycle == "unknown"
    assert view.state == "incomplete"
