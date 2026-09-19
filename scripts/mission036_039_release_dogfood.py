#!/usr/bin/env python3
"""M036–M039 — dry-run local release handoff dogfood (authoritative evidence).

Proves the complete human-gated release chain with:

* a **real** local Git repository and a real bare remote, so the authorized
  ``go_commit`` path genuinely stages, commits and pushes (no fixture Git);
* a **real** M031 mission driven to ``READY_FOR_COMMIT`` by the canonical
  orchestrator with fixture executors and a scripted reviewer;
* the deterministic GitHub fixture for PR binding, exact-head CI and merge
  (so no GitHub credentials are required and nothing is mutated remotely);
* a separate, read-only real-GitHub adapter liveness probe that never blocks
  the deterministic proof and never writes anything.

Exit codes: 0 = the deterministic chain closed; 3 = the chain failed closed;
2 = usage error.
"""

# ruff: noqa: E402

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

from trajectory_os.assembly import model as assembly_model
from trajectory_os.assembly import orchestrator as assembly_run
from trajectory_os.assembly import store as assembly_store
from trajectory_os.benchmark.executor import FixtureExecutor
from trajectory_os.observability import model as obs_model
from trajectory_os.observability import run as obs_run
from trajectory_os.release import closure as release_closure
from trajectory_os.release import evidence as release_evidence
from trajectory_os.release import handoff as release_handoff
from trajectory_os.release import merge_gate, model, pull_request, store
from trajectory_os.release.authorization import authorize
from trajectory_os.release.git_adapter import LocalGitAdapter
from trajectory_os.release.github_adapter import (
    FixtureGitHubAdapter,
    GhCliGitHubAdapter,
)

PASS_REVIEW = (
    "VERDICT: PASS\nBLOCKERS:\n- none\nMAJORS:\n- none\nMINORS:\n- none\n"
    "FINAL RECOMMENDATION: GO COMMIT\n")
OPERATOR = "operator"
TOKEN = "dogfood-operator-token"
BRANCH = "release/m036-m039-dogfood"
BASE_BRANCH = "main"


def _run_git(args: list[str], *, cwd: pathlib.Path) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def prepare_repo(repo: pathlib.Path, remote: pathlib.Path) -> None:
    """Create a real repo + bare remote at a clean baseline."""
    if repo.exists():
        shutil.rmtree(repo)
    if remote.exists():
        shutil.rmtree(remote)
    remote.mkdir(parents=True)
    repo.mkdir(parents=True)
    _run_git(["init", "-b", BASE_BRANCH], cwd=repo)
    _run_git(["config", "user.email", "release@example.invalid"], cwd=repo)
    _run_git(["config", "user.name", "Release Dogfood"], cwd=repo)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def add(a, b):\n    return a - b\n",
                                         encoding="utf-8")
    _run_git(["add", "-A"], cwd=repo)
    _run_git(["commit", "-m", "baseline"], cwd=repo)
    _run_git(["init", "--bare"], cwd=remote)
    _run_git(["remote", "add", "origin", str(remote)], cwd=repo)
    _run_git(["push", "-u", "origin", BASE_BRANCH], cwd=repo)
    _run_git(["checkout", "-b", BRANCH], cwd=repo)
    # The reviewed patch is already applied to the worktree (operator step).
    (repo / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n",
                                         encoding="utf-8")


def _workspace(root: pathlib.Path, mission_id: str) -> str:
    workspace = root / mission_id / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return str(workspace)


def _ready_mission(root: pathlib.Path, mission_id: str) -> None:
    orchestrator = assembly_run.MissionOrchestrator(
        str(root), executor=FixtureExecutor(interrupt_once=False),
        reviewer_factory=obs_run.scripted_reviewer_factory([PASS_REVIEW]))
    result = orchestrator.start(assembly_run.MissionRequest(
        objective="release dogfood: fix add(a, b)",
        workspace=_workspace(root, mission_id), mission_id=mission_id,
        workload_id="small-targeted-repair",
        trust_policy=assembly_model.TrustPolicy(max_repairs=2)))
    if result.status.get("readiness") != obs_model.RD_READY_FOR_COMMIT:
        raise RuntimeError(f"mission not ready: {result.status.get('readiness')}")


def _ci_runs(head_sha: str) -> tuple[model.CiRun, ...]:
    job = model.CiJob(name="quality", status="completed", conclusion="success",
                      url="https://example.invalid/job", log_ref="log://job")
    return (model.CiRun(
        workflow="CI", run_id="10000000002", name="quality", head_sha=head_sha,
        status="completed", conclusion="success",
        url="https://example.invalid/run", jobs=(job,)),)


def run_dogfood(root: pathlib.Path) -> dict[str, Any]:
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    repo = root / "repo"
    remote = root / "remote.git"
    prepare_repo(repo, remote)
    mission_id = "m036-m039-dogfood"
    _ready_mission(root, mission_id)

    git = LocalGitAdapter(repo=str(repo))
    github = FixtureGitHubAdapter()
    state = git.read_state()
    assert state.branch is not None and state.head_sha is not None

    handoff = release_handoff.build_commit_handoff(
        str(root), mission_id, git=git, base_branch=BASE_BRANCH)
    committed = release_handoff.go_commit(
        str(root), mission_id, git=git,
        authorization=authorize(model.GATE_GO_COMMIT, TOKEN, actor=OPERATOR))
    remote_head = _run_git(["ls-remote", "origin",
                            f"refs/heads/{BRANCH}"], cwd=repo).split()[0]

    github.branch_heads[BRANCH] = committed.commit_sha
    github.branch_heads[BASE_BRANCH] = remote_head
    binding = pull_request.bind_pull_request(
        str(root), mission_id, git=git, github=github,
        base_branch=BASE_BRANCH)
    github.set_checks(committed.commit_sha, _ci_runs(committed.commit_sha))
    ci = pull_request.watch_ci(str(root), mission_id, github=github,
                               persist=True)
    merge_handoff = merge_gate.build_merge_handoff(
        str(root), mission_id, github=github, base_branch=BASE_BRANCH)
    merged = merge_gate.go_merge(
        str(root), mission_id, github=github,
        authorization=authorize(model.GATE_GO_MERGE, TOKEN, actor=OPERATOR))
    closure = release_closure.build_release_closure(
        str(root), mission_id, github=github, base_branch=BASE_BRANCH,
        issue="238")

    evidence = release_evidence.derive_review_gate(
        release_evidence.load_mission_evidence(str(root), mission_id))
    mission_root = assembly_store.mission_root(str(root), mission_id)
    return {
        "mission_id": mission_id,
        "release_root": str(root),
        "repo": str(repo),
        "remote": str(remote),
        "remote_branch": BRANCH,
        "base_branch": BASE_BRANCH,
        "authorized_commit_sha": committed.commit_sha,
        "remote_branch_head": remote_head,
        "remote_push_verified": remote_head == committed.commit_sha,
        "handoff": {
            "branch": handoff.branch,
            "baseline_head": handoff.baseline_head,
            "current_head": handoff.current_head,
            "patch_sha256": handoff.patch_sha256,
            "gate": handoff.gate,
        },
        "commit": {
            "commit_sha": committed.commit_sha,
            "pushed_sha": committed.pushed_sha,
            "authorization": committed.authorization,
        },
        "pr": {
            "number": binding.number,
            "head_sha": binding.head_sha,
            "base_branch": binding.base_branch,
            "base_sha": binding.base_sha,
        },
        "ci": {
            "state": ci.state,
            "head_sha": ci.head_sha,
            "green": ci.green,
        },
        "merge": {
            "merge_sha": merged.merge_sha,
            "target_branch": merged.target_branch,
            "verified": merged.verified,
            "method": merged.merge_method,
        },
        "merge_handoff": {
            "gate": merge_handoff.gate,
            "pr_number": merge_handoff.pr_number,
            "release_commit_sha": merge_handoff.release_commit_sha,
            "ci_state": merge_handoff.ci_state,
            "merge_method": merge_handoff.merge_method,
        },
        "closure": {
            "status": closure.status,
            "reviewed_patch_sha256": closure.reviewed_patch_sha256,
            "commit_sha": closure.commit_sha,
            "pr_number": closure.pr_number,
            "ci_status": closure.ci_status,
            "merge_sha": closure.merge_sha,
            "target_branch_verified": closure.target_branch_verified,
            "issue_closure": dict(closure.issue_closure),
        },
        "review_gate": evidence.to_dict(),
        "release_state": store.load_state(mission_root).to_dict(),
        "release_events": store.load_release_events(mission_root),
    }


def github_probe() -> dict[str, Any]:
    """Read-only real-GitHub adapter liveness (never blocks the evidence)."""
    probe: dict[str, Any] = {"attempted": True, "available": False}
    try:
        gh = GhCliGitHubAdapter(repo=str(REPO))
        available = gh.available()
        probe["available"] = available
        if not available:
            probe["reason"] = "gh CLI unavailable or unauthenticated"
            return probe
        probe["slug"] = gh.slug()
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(REPO), capture_output=True,
            text=True, check=False).stdout.strip()
        probe["head_sha"] = head
        if head:
            query = gh.checks_for_head(head)
            probe["exact_head_ci"] = query.state
            probe["exact_head_ci_runs"] = len(query.runs)
        probe["write_operations"] = "none (read-only liveness probe)"
    except Exception as exc:  # noqa: BLE001 - dogfood probe fails soft
        probe["available"] = False
        probe["reason"] = f"{type(exc).__name__}: {exc}"
    return probe


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mission036_039_release_dogfood")
    parser.add_argument("--root", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    root = pathlib.Path(args.root or REPO / ".artifacts" / "m036-m039"
                        / "dogfood")
    try:
        evidence = run_dogfood(root)
    except Exception as exc:  # noqa: BLE001 - the dogfood fails closed
        print(f"dogfood failed closed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 3
    evidence["github_read_only_probe"] = github_probe()
    evidence["generated_at"] = (
        datetime.datetime.now(datetime.UTC)
        .replace(microsecond=0, tzinfo=None).isoformat() + "Z")
    out = pathlib.Path(args.out or REPO / "docs" / "missions" / "m036-m039"
                       / "m036-m039-dogfood-evidence.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    print(f"release dogfood: {evidence['closure']['status']} "
          f"commit={evidence['commit']['commit_sha']} "
          f"merge={evidence['merge']['merge_sha']}")
    print(f"evidence: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
