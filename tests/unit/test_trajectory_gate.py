"""Tests for scripts/trajectory_gate.py (Process V2 decision-gated automation).

All tests run against disposable temporary git repositories — never against the
developer's actual repository state. Verification phases (tests/ruff/mypy) are
injected as dummies to stay focused and hermetic.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module() -> Any:
    path = REPO_ROOT / "scripts" / "trajectory_gate.py"
    spec = importlib.util.spec_from_file_location("trajectory_gate_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tg = _load_module()


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Gate Test")
    _git(repo, "config", "user.email", "gate@test.local")
    (repo / "README.md").write_text("# repo\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "baseline")
    return repo


def _new_branch(repo: Path, branch: str) -> None:
    _git(repo, "checkout", "-q", "-b", branch)


def _dummy(
    name: str,
    ok: bool = True,
    summary: str = "ok",
    calls: list[str] | None = None,
) -> tg.Phase:
    def run() -> tg.PhaseResult:
        if calls is not None:
            calls.append(name)
        return tg.PhaseResult(
            name=name,
            outcome=tg.PhaseOutcome.PASS if ok else tg.PhaseOutcome.FAIL,
            summary=summary,
            suggested_decision=tg.FIX,
        )

    return tg.Phase(name, run)


def _options(repo: Path, allow: tuple[str, ...] = ()) -> tg.GateOptions:
    return tg.GateOptions(repo=repo, allow=allow)


@pytest.mark.parametrize(
    ("command", "state", "decision"),
    [
        ("implement", "READY_FOR_COMMIT", "GO COMMIT"),
        ("commit-check", "READY_FOR_COMMIT", "GO COMMIT"),
        ("push-check", "READY_FOR_PUSH", "GO PUSH"),
        ("pr-check", "READY_FOR_PR", "GO PR"),
        ("merge-check", "READY_FOR_MERGE", "GO MERGE (human)"),
    ],
)
def test_command_contract(command: str, state: str, decision: str, tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _new_branch(repo, "feat/x")
    report = tg.run_gate(command, _options(repo), phases=[])
    body = tg.render_report(report)
    assert f"STATE: {state}" in body
    assert f"DECISION REQUIRED:\n{decision}" in body
    assert "BLOCKERS:\n- None" in body


def test_implement_ready_with_scoped_dirty_file(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _new_branch(repo, "feat/x")
    (repo / "app" / "new.py").parent.mkdir(parents=True)
    (repo / "app" / "new.py").write_text("x = 1\n", encoding="utf-8")
    calls: list[str] = []
    report = tg.run_gate(
        "implement",
        _options(repo, allow=("app",)),
        phases=[
            tg.branch_phase(_options(repo)),
            tg.scope_phase(_options(repo, allow=("app",))),
            _dummy("focused_tests", calls=calls),
        ],
    )
    assert not report.blocked
    assert calls == ["focused_tests"]
    body = tg.render_report(report)
    assert "READY_FOR_COMMIT" in body


def test_implement_blocks_unexpected_dirty_file_and_stops(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _new_branch(repo, "feat/x")
    (repo / "unrelated.txt").write_text("nope\n", encoding="utf-8")
    calls: list[str] = []
    options = _options(repo, allow=("docs",))
    report = tg.run_gate(
        "implement",
        options,
        phases=[
            tg.branch_phase(options),
            tg.scope_phase(options),
            _dummy("focused_tests", calls=calls),
        ],
    )
    assert report.blocked
    assert calls == [], "fail-fast: no later phase may run after a guard failure"
    body = tg.render_report(report)
    assert "STATE: BLOCKED" in body
    assert "scope: FAIL" in body
    assert "unrelated.txt" in body
    assert body.rstrip().endswith("STOP")


def test_implement_unscoped_dirty_work_blocks_until_explicit_allow(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _new_branch(repo, "feat/x")
    (repo / "app" / "new.py").parent.mkdir(parents=True)
    (repo / "app" / "new.py").write_text("x = 1\n", encoding="utf-8")
    report = tg.run_gate("implement", _options(repo), phases=[tg.scope_phase(_options(repo))])
    assert report.blocked
    assert tg.render_report(report).rstrip().endswith("STOP")


def test_branch_guard_rejects_main(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    options = _options(repo)
    report = tg.run_gate("implement", options, phases=[tg.branch_phase(options)])
    assert report.blocked
    body = tg.render_report(report)
    assert "wrong branch: main" in body
    assert body.rstrip().endswith("STOP")


def test_expected_branch_mismatch_blocks(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _new_branch(repo, "feat/x")
    options = tg.GateOptions(repo=repo, expected_branch="feat/other")
    report = tg.run_gate("commit-check", options, phases=[tg.branch_phase(options)])
    assert report.blocked


def test_commit_check_requires_main_ancestor(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _new_branch(repo, "feat/x")
    _git(repo, "checkout", "-q", "main")
    (repo / "later.md").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "later.md")
    _git(repo, "commit", "-q", "-m", "main moved on")
    _git(repo, "checkout", "-q", "feat/x")
    options = _options(repo)
    report = tg.run_gate("commit-check", options, phases=[tg.ancestry_phase(options)])
    assert report.blocked
    assert "main is not an ancestor of HEAD" in tg.render_report(report)


def test_commit_check_requires_nonempty_diff(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _new_branch(repo, "feat/x")
    options = _options(repo)
    report = tg.run_gate("commit-check", options, phases=[tg.nonempty_diff_phase(options)])
    assert report.blocked
    assert "no intended diff against main" in tg.render_report(report)


def test_push_check_blocks_without_origin_remote(tmp_path: Path) -> None:
    """Hostile: an unpushed branch with no configured origin cannot be verified."""
    repo = _make_repo(tmp_path)
    _new_branch(repo, "feat/x")
    options = _options(repo)
    report = tg.run_gate("push-check", options, phases=[tg.upstream_phase(options)])
    assert report.blocked
    body = tg.render_report(report)
    assert "no origin remote configured" in body
    assert body.rstrip().endswith("STOP")


def test_push_check_initial_push_ready_when_remote_lacks_branch(tmp_path: Path) -> None:
    """Hostile: new branch, remote exists but never had this branch → initial push READY."""
    repo = _make_repo(tmp_path)
    _make_remote(repo, tmp_path, "remote")  # registers `origin`, no feat/x on it
    _new_branch(repo, "feat/x")
    options = _options(repo)
    report = tg.run_gate("push-check", options, phases=[tg.upstream_phase(options)])
    assert not report.blocked
    body = tg.render_report(report)
    assert "READY_FOR_PUSH" in body
    assert "initial push: origin/feat/x absent" in body
    assert "no remote history to reconcile" in body


def test_push_check_ready_with_upstream(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    remote = _make_remote(repo, tmp_path, "remote")
    _new_branch(repo, "feat/x")
    # remote knows the branch base (0 commits to push so far)
    _git(repo, "push", "-q", str(remote), "HEAD:feat/x")
    _git(repo, "fetch", str(remote), "+refs/heads/feat/x:refs/remotes/origin/feat/x")
    # then one local commit ahead of the remote head
    (repo / "work.md").write_text("work\n", encoding="utf-8")
    _git(repo, "add", "work.md")
    _git(repo, "commit", "-q", "-m", "work on branch")

    options = _options(repo)
    report = tg.run_gate("push-check", options, phases=[tg.upstream_phase(options)])
    assert not report.blocked
    body = tg.render_report(report)
    assert "READY_FOR_PUSH" in body
    assert "1 commit(s) to push" in body


def test_push_check_rejects_force_push_scenario(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    remote = _make_remote(repo, tmp_path, "remote")
    _new_branch(repo, "feat/x")
    base_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "-q", str(remote), "HEAD:feat/x")
    _git(repo, "fetch", str(remote), "+refs/heads/feat/x:refs/remotes/origin/feat/x")

    (repo / "work.md").write_text("work\n", encoding="utf-8")
    _git(repo, "add", "work.md")
    _git(repo, "commit", "-q", "-m", "work on branch")

    # independent work advances the remote ref on a divergent line
    tree = _git(repo, "rev-parse", f"{base_sha}^{{tree}}")
    advanced = _git(remote, "commit-tree", tree, "-p", base_sha, "-m", "advanced")
    _git(remote, "update-ref", "refs/heads/feat/x", advanced)
    _git(repo, "fetch", str(remote), "+refs/heads/feat/x:refs/remotes/origin/feat/x")

    assert advanced != _git(repo, "rev-parse", "HEAD")
    options = _options(repo)
    report = tg.run_gate("push-check", options, phases=[tg.upstream_phase(options)])
    assert report.blocked
    body = tg.render_report(report)
    assert "advanced beyond local HEAD" in body
    assert body.rstrip().endswith("STOP")


def test_push_check_no_tracking_ref_not_initial_push(tmp_path: Path) -> None:
    """Hostile: remote has feat/x but local refs/remotes/origin/feat/x is absent.

    The gate must NOT treat this as an initial push and must NOT declare
    READY when the remote commit is missing from local history.
    """
    repo = _make_repo(tmp_path)
    remote = _make_remote(repo, tmp_path, "remote")
    _new_branch(repo, "feat/x")
    _git(repo, "push", "-q", str(remote), "HEAD:feat/x")
    # independent remote commit: its SHA is NOT in local history, and no local
    # refs/remotes/origin/feat/x tracking ref exists
    base_sha = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", f"{base_sha}^{{tree}}")
    advanced = _git(remote, "commit-tree", tree, "-p", base_sha, "-m", "remote-only work")
    _git(remote, "update-ref", "refs/heads/feat/x", advanced)
    # hard requirement: local tracking ref is absent (the old buggy signal)
    assert _no_local_tracking_ref(repo, "feat/x")

    options = _options(repo)
    report = tg.run_gate("push-check", options, phases=[tg.upstream_phase(options)])
    assert report.blocked
    body = tg.render_report(report)
    assert "not in local history" in body
    assert "initial push" not in body
    assert body.rstrip().endswith("STOP")


def test_push_check_no_tracking_ref_ready_when_fast_forward(tmp_path: Path) -> None:
    """Remote branch exists (queryable) and is an ancestor of local HEAD.

    No local tracking ref is required to declare a safe plain push READY.
    """
    repo = _make_repo(tmp_path)
    remote = _make_remote(repo, tmp_path, "remote")
    _new_branch(repo, "feat/x")
    _git(repo, "push", "-q", str(remote), "HEAD:feat/x")
    (repo / "work.md").write_text("work\n", encoding="utf-8")
    _git(repo, "add", "work.md")
    _git(repo, "commit", "-q", "-m", "local ahead")
    # local tracking ref is deliberately absent (the old buggy signal)
    assert _no_local_tracking_ref(repo, "feat/x")

    options = _options(repo)
    report = tg.run_gate("push-check", options, phases=[tg.upstream_phase(options)])
    assert not report.blocked
    body = tg.render_report(report)
    assert "READY_FOR_PUSH" in body
    assert "initial push" not in body
    assert "1 commit(s) to push" in body


def test_push_check_blocks_when_ls_remote_fails(tmp_path: Path) -> None:
    """Hostile: an origin URL that cannot be reached must block, not guess."""
    repo = _make_repo(tmp_path)
    remote = _make_remote(repo, tmp_path, "remote")
    _new_branch(repo, "feat/x")
    # origin is still configured, but points at a path that is not a repository
    _git(repo, "remote", "set-url", "origin", str(tmp_path / "ghost-remote"))
    assert str(remote) != str(tmp_path / "ghost-remote")

    options = _options(repo)
    report = tg.run_gate("push-check", options, phases=[tg.upstream_phase(options)])
    assert report.blocked
    body = tg.render_report(report)
    assert "git ls-remote failed" in body
    assert body.rstrip().endswith("STOP")


def test_pr_check_reuses_quality_evidence_at_matching_head(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    options = tg.GateOptions(repo=repo, quality_evidence="0" * 40)  # wrong full SHA must block
    report = tg.run_gate("pr-check", options, phases=[tg.quality_phase(options)])
    assert report.blocked

    head = _git(repo, "rev-parse", "HEAD")
    options_ok = tg.GateOptions(repo=repo, quality_evidence=head)
    report_ok = tg.run_gate("pr-check", options_ok, phases=[tg.quality_phase(options_ok)])
    assert not report_ok.blocked
    assert "reused full quality evidence" in tg.render_report(report_ok)


@pytest.mark.parametrize(
    "candidate",
    [
        "3" * 39,   # short SHA prefix of 39 chars: must NOT validate
        "a",         # one-character
        "zzzz",      # malformed (not hex)
        "" ,         # empty
        "HEAD",      # non-SHA token
    ],
)
def test_quality_evidence_rejects_short_malformed_sha(candidate: str, tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    if candidate == "3" * 39:
        candidate = head[:39]  # a real prefix of HEAD: still must NOT validate
    options = tg.GateOptions(repo=repo, quality_evidence=candidate)
    report = tg.run_gate("pr-check", options, phases=[tg.quality_phase(options)])
    assert report.blocked
    body = tg.render_report(report)
    assert "does not exactly match" in body
    assert body.rstrip().endswith("STOP")


def test_quality_evidence_rejects_wrong_full_sha(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    other = "9" * 40  # well-formed full SHA, but not HEAD
    options = tg.GateOptions(repo=repo, quality_evidence=other)
    report = tg.run_gate("pr-check", options, phases=[tg.quality_phase(options)])
    assert report.blocked


@pytest.mark.parametrize("prefix_length", [1, 12, 20, 39])
def test_merge_evidence_rejects_short_sha_head(
    prefix_length: int, tmp_path: Path
) -> None:
    repo = _make_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    evidence_path = tmp_path / "short.json"
    evidence_path.write_text(
        json.dumps(
            {
                "head": head[:prefix_length],
                "ci": "green",
                "reviews_resolved": True,
                "mergeable": True,
            }
        ),
        encoding="utf-8",
    )
    options = tg.GateOptions(repo=repo, evidence_file=evidence_path)
    report = tg.run_gate(
        "merge-check", options, phases=[tg.external_evidence_phase(options)]
    )
    assert report.blocked
    body = tg.render_report(report)
    assert "does not exactly match current HEAD" in body
    assert body.rstrip().endswith("STOP")


def test_merge_evidence_rejects_malformed_head(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    evidence_path = tmp_path / "bad.json"
    evidence_path.write_text(
        json.dumps(
            {"head": "not-a-sha", "ci": "green", "reviews_resolved": True, "mergeable": True}
        ),
        encoding="utf-8",
    )
    options = tg.GateOptions(repo=repo, evidence_file=evidence_path)
    report = tg.run_gate(
        "merge-check", options, phases=[tg.external_evidence_phase(options)]
    )
    assert report.blocked


def test_pr_check_quality_failure_blocks_before_later_phases(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    calls: list[str] = []
    phases = [
        _dummy("quality_gate", ok=False, summary="1 failed"),
        _dummy("diff_summary", calls=calls),
    ]
    report = tg.run_gate("pr-check", _options(repo), phases=phases)
    assert report.blocked
    assert calls == []
    body = tg.render_report(report)
    assert "quality_gate: FAIL — 1 failed" in body
    assert body.rstrip().endswith("FIX")


def test_merge_ready_with_full_external_evidence(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(
        json.dumps(
            {"head": head, "ci": "green", "reviews_resolved": True, "mergeable": True}
        ),
        encoding="utf-8",
    )
    options = tg.GateOptions(repo=repo, evidence_file=evidence_path)
    report = tg.run_gate("merge-check", options, phases=[tg.external_evidence_phase(options)])
    assert not report.blocked
    body = tg.render_report(report)
    assert "STATE: READY_FOR_MERGE" in body
    assert "GO MERGE (human)" in body


def test_merge_check_phase_order_includes_ancestry() -> None:
    options = tg.GateOptions(repo=Path("/unused"))
    names = [phase.name for phase in tg.default_phases("merge-check", options)]
    # ancestry must be enforced before READY_FOR_MERGE and before external evidence
    assert names == ["branch", "ancestry", "worktree", "external_evidence"]


def test_merge_check_blocks_when_main_not_ancestor(tmp_path: Path) -> None:
    """Hostile: main advanced past feat/x merge-base → merge-check BLOCKED/STOP."""
    repo = _make_repo(tmp_path)
    _new_branch(repo, "feat/x")
    # main moves on independently: feat/x now does NOT contain main
    _git(repo, "checkout", "-q", "main")
    (repo / "later.md").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "later.md")
    _git(repo, "commit", "-q", "-m", "main moved on")
    _git(repo, "checkout", "-q", "feat/x")

    options = _options(repo)
    report = tg.run_gate("merge-check", options)  # full default phase list
    assert report.blocked
    assert report.failed is not None and report.failed.name == "ancestry"
    body = tg.render_report(report)
    assert "main is not an ancestor of HEAD" in body
    assert body.rstrip().endswith("STOP")
    # fail-fast: external evidence phase must not have run
    assert not any(r.name == "external_evidence" for r in report.results)


def test_merge_blocks_without_evidence_and_names_human_decision(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _new_branch(repo, "feat/x")
    options = _options(repo)
    report = tg.run_gate("merge-check", options, phases=[tg.external_evidence_phase(options)])
    assert report.blocked
    body = tg.render_report(report)
    assert "external_evidence: FAIL" in body
    assert "GO MERGE (human)" in body


def test_merge_blocks_on_stale_evidence_head(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _new_branch(repo, "feat/x")
    evidence_path = tmp_path / "stale.json"
    evidence_path.write_text(
        json.dumps(
            {"head": "f" * 40, "ci": "green", "reviews_resolved": True, "mergeable": True}
        ),
        encoding="utf-8",
    )
    options = tg.GateOptions(repo=repo, evidence_file=evidence_path)
    report = tg.run_gate("merge-check", options, phases=[tg.external_evidence_phase(options)])
    assert report.blocked
    assert "does not exactly match current HEAD" in tg.render_report(report)


def test_render_report_is_stable_for_known_state(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _new_branch(repo, "feat/x")
    options = _options(repo)
    phases = [tg.branch_phase(options), _dummy("focused_tests")]
    body_a = tg.render_report(tg.run_gate("implement", options, phases=phases))
    body_b = tg.render_report(tg.run_gate("implement", options, phases=phases))
    assert body_a == body_b
    for section in ("STATE:", "PHASE:", "BRANCH:", "HEAD:", "BASE:", "EVIDENCE:", "BLOCKERS:"):
        assert section in body_a


def test_no_git_side_effects_during_read_only_gates(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    remote = _make_remote(repo, tmp_path, "remote")
    _new_branch(repo, "feat/x")
    (repo / "wip.txt").write_text("wip\n", encoding="utf-8")
    before_head = _git(repo, "rev-parse", "HEAD")
    before_status = _git(repo, "status", "--porcelain")
    before_refs = _git(repo, "for-each-ref")
    before_remote_sha = _git(remote, "rev-parse", "HEAD")

    for command in ("implement", "commit-check", "push-check", "pr-check", "merge-check"):
        options = tg.GateOptions(
            repo=repo,
            allow=("wip.txt",),
            tests_cmd="true",
            ruff_cmd="true",
            mypy_cmd="true",
            quality_cmd="true",
        )
        tg.run_gate(command, options)

    assert _git(repo, "rev-parse", "HEAD") == before_head
    assert _git(repo, "status", "--porcelain") == before_status
    assert _git(repo, "for-each-ref") == before_refs
    assert _git(remote, "rev-parse", "HEAD") == before_remote_sha, "no push/pull side effects"


def test_dirty_records_parse_spaces_rename_and_modification(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _new_branch(repo, "feat/x")
    # ordinary modified tracked file
    (repo / "README.md").write_text("# repo v2\n", encoding="utf-8")
    # untracked file in a directory containing spaces
    (repo / "dir with space").mkdir()
    (repo / "dir with space" / "a file.txt").write_text("x\n", encoding="utf-8")
    records = tg.dirty_records(repo)
    assert (" M", "README.md") in records  # status is the two-char XY field
    # git reports untracked directories as the directory (trailing slash)
    assert ("??", "dir with space/") in records
    assert set(tg.dirty_files(repo)) == {"README.md", "dir with space/"}

    # rename: porcelain -z reports the destination as the tracked path
    _git(repo, "add", "dir with space")
    _git(repo, "commit", "-q", "-m", "add spaced dir")
    _git(repo, "mv", "dir with space/a file.txt", "renamed target.txt")
    records = tg.dirty_records(repo)
    assert any(
        status.startswith("R") and path == "renamed target.txt" for status, path in records
    ), f"rename destination must be tracked, got: {records!r}"


def test_scope_phase_allows_spaced_paths(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _new_branch(repo, "feat/x")
    (repo / "dir with space").mkdir()
    (repo / "dir with space" / "a file.txt").write_text("x\n", encoding="utf-8")
    report = tg.run_gate(
        "implement", _options(repo, allow=("dir with space",)),
        phases=[tg.scope_phase(_options(repo, allow=("dir with space",)))],
    )
    assert not report.blocked


def test_diff_summary_flags_untracked_files_not_in_committed_diff(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _new_branch(repo, "feat/x")
    (repo / "committed.md").write_text("c\n", encoding="utf-8")
    _git(repo, "add", "committed.md")
    _git(repo, "commit", "-q", "-m", "committed work")
    # uncommitted untracked file (name with spaces) after the commit
    (repo / "draft notes.txt").write_text("wip\n", encoding="utf-8")
    options = _options(repo)
    result = tg.diff_summary_phase(options).run()
    assert result.outcome is tg.PhaseOutcome.PASS
    # the summary must not silently present only the committed (partial) stat:
    assert "INCOMPLETE WITH WORKTREE" in result.summary
    assert "+1 untracked" in result.summary
    # committed file still listed:
    assert "committed.md" in result.detail
    # untracked file explicitly flagged and named:
    assert "not in committed diff above" in result.detail
    assert "draft notes.txt" in result.detail


def test_diff_summary_clean_worktree_has_no_incomplete_marker(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _new_branch(repo, "feat/x")
    (repo / "committed.md").write_text("c\n", encoding="utf-8")
    _git(repo, "add", "committed.md")
    _git(repo, "commit", "-q", "-m", "committed work")
    options = _options(repo)
    result = tg.diff_summary_phase(options).run()
    assert result.outcome is tg.PhaseOutcome.PASS
    assert "INCOMPLETE" not in result.summary
    assert "1 file changed" in result.summary



def test_write_artifact_creates_evidence_only_file(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = tg.run_gate("implement", _options(repo), phases=[])
    path = tg.write_artifact(repo, tg.render_report(report))
    text = path.read_text(encoding="utf-8")
    assert path == repo / ".artifacts" / "handoff" / "latest.md"
    assert "NOT semantic authority over Git/repository state" in text
    assert "STATE: READY_FOR_COMMIT" in text


def test_cli_unknown_command_is_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        tg.main(["definitely-not-a-command"])
    assert excinfo.value.code == tg.EXIT_USAGE


def test_cli_non_git_path_is_usage_error(tmp_path: Path) -> None:
    assert tg.main(["implement", "--repo", str(tmp_path / "not-a-repo")]) == tg.EXIT_USAGE


def _no_local_tracking_ref(repo: Path, branch: str) -> bool:
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
        cwd=repo, capture_output=True,
    )
    return proc.returncode != 0


def _make_remote(repo: Path, tmp_path: Path, name: str) -> Path:
    """Simulate `origin` with a bare repository (no worktree, no checkout rules)."""
    path = tmp_path / name
    _git(repo, "clone", "--bare", "-q", str(repo), str(path))
    _git(path, "config", "user.name", "Gate Test")
    _git(path, "config", "user.email", "gate@test.local")
    _git(repo, "remote", "add", "origin", str(path))
    return path
