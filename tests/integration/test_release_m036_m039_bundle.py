"""M036–M039 integration — full human-gated release bundle on a real repo."""

from __future__ import annotations

import subprocess
from pathlib import Path

from trajectory_os.assembly import model as assembly_model
from trajectory_os.assembly import orchestrator as assembly_run
from trajectory_os.assembly import store as assembly_store
from trajectory_os.benchmark.executor import FixtureExecutor
from trajectory_os.observability import model as obs_model
from trajectory_os.observability import run as obs_run
from trajectory_os.release import (
    acceptance,
    evidence,
    handoff,
    merge_gate,
    model,
    pull_request,
    store,
)
from trajectory_os.release.authorization import authorize
from trajectory_os.release.git_adapter import LocalGitAdapter
from trajectory_os.release.github_adapter import FixtureGitHubAdapter

PASS_REVIEW = (
    "VERDICT: PASS\nBLOCKERS:\n- none\nMAJORS:\n- none\nMINORS:\n- none\n"
    "FINAL RECOMMENDATION: GO COMMIT\n")
OPERATOR = "operator"
TOKEN = "integration-operator-token"


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _prepare_repo(repo: Path, remote: Path) -> None:
    remote.mkdir(parents=True)
    repo.mkdir(parents=True)
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "t@example.invalid"], repo)
    _git(["config", "user.name", "Test"], repo)
    (repo / "app.py").write_text("def add(a, b):\n    return a - b\n",
                                 encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "baseline"], repo)
    _git(["init", "--bare"], remote)
    _git(["remote", "add", "origin", str(remote)], repo)
    _git(["push", "-u", "origin", "main"], repo)
    _git(["checkout", "-b", "feature"], repo)
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n",
                                 encoding="utf-8")


def _ready_mission(root: Path, mission_id: str) -> None:
    workspace = root / mission_id / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    orchestrator = assembly_run.MissionOrchestrator(
        str(root), executor=FixtureExecutor(interrupt_once=False),
        reviewer_factory=obs_run.scripted_reviewer_factory([PASS_REVIEW]))
    result = orchestrator.start(assembly_run.MissionRequest(
        objective="integration release: fix add(a, b)",
        workspace=str(workspace), mission_id=mission_id,
        workload_id="small-targeted-repair",
        trust_policy=assembly_model.TrustPolicy(max_repairs=2)))
    assert result.status["readiness"] == obs_model.RD_READY_FOR_COMMIT


def test_full_release_bundle_on_real_git_repo(tmp_path: Path) -> None:
    root = tmp_path / "missions"
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    root.mkdir()
    _prepare_repo(repo, remote)
    mission_id = "m036-m039-integration"
    _ready_mission(root, mission_id)

    git = LocalGitAdapter(repo=str(repo))
    github = FixtureGitHubAdapter()
    head_before = git.read_state().head_sha
    assert head_before is not None

    handoff_obj = handoff.build_commit_handoff(
        str(root), mission_id, git=git, base_branch="main")
    assert handoff_obj.patch_sha256 == handoff_obj.reviewed_patch_sha256

    committed = handoff.go_commit(
        str(root), mission_id, git=git,
        authorization=authorize(model.GATE_GO_COMMIT, TOKEN, actor=OPERATOR))
    remote_head = _git(["ls-remote", "origin", "refs/heads/feature"],
                       repo).split()[0]
    assert remote_head == committed.commit_sha
    assert committed.commit_sha != head_before

    github.branch_heads["feature"] = committed.commit_sha
    binding = pull_request.bind_pull_request(
        str(root), mission_id, git=git, github=github, base_branch="main")
    assert binding.head_sha == committed.commit_sha

    github.set_checks(
        committed.commit_sha,
        (acceptance.ci_runs(committed.commit_sha, model.CI_SUCCESS),))
    ci = pull_request.watch_ci(str(root), mission_id, github=github,
                               persist=True)
    assert ci.green

    merge_gate.build_merge_handoff(
        str(root), mission_id, github=github, base_branch="main")
    merged = merge_gate.go_merge(
        str(root), mission_id, github=github,
        authorization=authorize(model.GATE_GO_MERGE, TOKEN, actor=OPERATOR))
    assert merged.merged
    assert merged.merge_method == model.MERGE_SQUASH
    assert merged.verified

    from trajectory_os.release import closure as release_closure

    result = release_closure.build_release_closure(
        str(root), mission_id, github=github, base_branch="main", issue="238")
    assert result.status == "CLOSED"
    assert result.target_branch_verified
    assert result.issue_closure["issue"] == "238"
    assert result.commit_sha == committed.commit_sha
    assert result.merge_sha == merged.merge_sha

    gate = evidence.derive_review_gate(
        evidence.load_mission_evidence(str(root), mission_id))
    assert gate.ready
    assert result.reviewed_patch_sha256 == gate.semantic_patch_identity

    state = store.load_state(assembly_store.mission_root(str(root),
                                                         mission_id))
    assert state.stage == model.RST_RELEASED


def test_release_acceptance_matrix_passes(tmp_path: Path) -> None:
    report = acceptance.run_acceptance(tmp_path)
    assert report.status == "PASS", report.render()
    assert len(report.cases) == 18
    assert all(case.ok for case in report.cases)
