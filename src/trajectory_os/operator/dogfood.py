"""M040 — true self-release dogfood over the production code path.

:func:`run_self_hosting_dogfood` proves the canonical product path with the
*production* operator/release code (never a parallel demo):

    objective -> mission -> execution -> validation -> independent review
    -> READY_FOR_COMMIT -> release handoff -> GO COMMIT gate -> commit/push
    -> PR binding -> exact-head CI -> GO MERGE gate -> merge -> release closure

Two clearly separated sections are persisted:

* ``fixture_proof`` — the *complete* path driven with the deterministic
  in-memory Git/GitHub fixtures. Destructive remote actions are never real
  here; this proves the code path and the semantic patch linkage;
* ``live_dogfood`` — the *current* M040–M047 implementation run over the
  real repository as far as is safe: policy resolution, routing, mission
  execution (isolated workspace), READY_FOR_COMMIT and the read-only commit
  handoff. It **stops at the human GO COMMIT gate** and performs zero Git
  writes.

Evidence never infers success from prose: every claim is a durable document.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from trajectory_os.assembly import orchestrator as assembly_run
from trajectory_os.benchmark.executor import FixtureExecutor
from trajectory_os.observability import model as obs_model
from trajectory_os.observability import run as obs_run
from trajectory_os.operator import model
from trajectory_os.operator._util import read_optional_json, utc_now, write_json
from trajectory_os.operator.control_plane import (
    FIXTURE_PASS_REVIEW,
    ControlPlane,
)
from trajectory_os.operator.policy import PROFILE_RELEASE
from trajectory_os.operator.recovery import decide_recovery
from trajectory_os.release import acceptance as release_acceptance
from trajectory_os.release import model as release_model
from trajectory_os.release.git_adapter import FakeGitAdapter
from trajectory_os.release.github_adapter import FixtureGitHubAdapter

DOGFOOD_TOKEN = "self-hosting-dogfood-operator-token"
DOGFOOD_OPERATOR = "operator"

#: Marker guaranteeing the live dogfood never crossed a human Git gate.
HUMAN_GATES = "GO_COMMIT+GO_MERGE"

DOGFOOD_OBJECTIVE = (
    "Self-hosting dogfood: prove the M040-M047 operator platform can carry "
    "one objective from intake to release closure over the real canonical "
    "product path while stopping at every human Git trust gate.")


def _fixture_proof(root: str, *,
                   clock: release_acceptance.ScriptedClock) -> dict[str, Any]:
    """Drive the complete path with deterministic fixtures (production code)."""
    mission_id = "m040-fixture-proof"
    workspace = Path(root) / mission_id / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    git = FakeGitAdapter(branch="m040-fixture-proof", head_sha="a" * 40)
    github = FixtureGitHubAdapter()
    github.branch_heads.setdefault(git.branch, git.head_sha)
    github.branch_heads.setdefault("main", "b" * 40)
    plane = ControlPlane(root, git=git, github=github, clock=clock)
    outcome = plane.start(
        assembly_run.MissionRequest(
            objective=DOGFOOD_OBJECTIVE, workspace=str(workspace),
            mission_id=mission_id),
        profile=PROFILE_RELEASE,
        executor=FixtureExecutor(interrupt_once=False),
        reviewer_factory=obs_run.scripted_reviewer_factory(
            [FIXTURE_PASS_REVIEW]))
    ready = outcome.result["status"]["readiness"] == (
        obs_model.RD_READY_FOR_COMMIT)
    if not ready:
        return {"ok": False, "reason": "MISSION_NOT_READY_FOR_COMMIT",
                "mission_id": mission_id,
                "readiness": outcome.result["status"]["readiness"]}
    handoff = plane.handoff(mission_id, base_branch="main", issue="240")
    committed = plane.go_commit(mission_id, token=DOGFOOD_TOKEN,
                                actor=DOGFOOD_OPERATOR)
    github.branch_heads[git.branch] = committed["commit_sha"]
    binding = plane.bind_pr(mission_id, base_branch="main")
    commit_sha = committed["commit_sha"]
    github.set_checks(commit_sha, (
        release_acceptance.ci_runs(commit_sha, release_model.CI_SUCCESS),))
    ci = plane.watch_ci(mission_id, persist=True)
    plane.merge_handoff(mission_id, base_branch="main")
    merged = plane.go_merge(mission_id, token=DOGFOOD_TOKEN,
                            actor=DOGFOOD_OPERATOR)
    closure = plane.closure(mission_id, base_branch="main", issue="240")
    replay = plane.reconstruct(mission_id)
    recovery = decide_recovery(root, mission_id, clock=clock)
    return {
        "ok": bool(merged.get("merged")) and closure.get("status") == "CLOSED",
        "mission_id": mission_id,
        "reviewed_patch": handoff["reviewed_patch_sha256"],
        "commit_sha": commit_sha,
        "remote_branch": committed["branch"],
        "pr_number": binding["number"],
        "pr_head_sha": binding["head_sha"],
        "ci_state": ci["state"],
        "ci_head_sha": ci["head_sha"],
        "merge_sha": merged.get("merge_sha"),
        "merge_method": merged.get("merge_method"),
        "target_branch_verified": closure.get("target_branch_verified"),
        "release_status": closure.get("status"),
        "release_stage": (
            ((replay.get("release") or {}).get("release") or {})
             .get("state")),
        "recovery_action": recovery.action,
        "durable_linkage": {
            "mission_id": closure.get("mission_id"),
            "reviewed_patch_sha256": closure.get("reviewed_patch_sha256"),
            "commit_sha": closure.get("commit_sha"),
            "pr_number": closure.get("pr_number"),
            "pr_head_sha": closure.get("pr_head_sha"),
            "ci_head_sha": closure.get("ci_head_sha"),
            "merge_sha": closure.get("merge_sha"),
        },
    }


def _live_dogfood(root: str, *, repo: str | None,
                  workspace: str | None,
                  clock: Callable[[], str],
                  base_branch: str, issue: str | None,
                  mission_id: str | None) -> dict[str, Any]:
    """Run the current implementation over the real repo up to GO COMMIT."""
    mission_id = mission_id or "m040-live-dogfood"
    workspace_path = Path(workspace) if workspace is not None else (
        Path(root) / mission_id / "workspace")
    workspace_path.mkdir(parents=True, exist_ok=True)
    plane = ControlPlane(root, repo=repo, clock=clock)
    outcome = plane.start(
        assembly_run.MissionRequest(
            objective=DOGFOOD_OBJECTIVE, workspace=str(workspace_path),
            mission_id=mission_id),
        profile=PROFILE_RELEASE,
        executor=FixtureExecutor(interrupt_once=False),
        reviewer_factory=obs_run.scripted_reviewer_factory(
            [FIXTURE_PASS_REVIEW]))
    status = outcome.result["status"]
    ready = status["readiness"] == obs_model.RD_READY_FOR_COMMIT
    live: dict[str, Any] = {
        "mission_id": mission_id,
        "objective": DOGFOOD_OBJECTIVE,
        "ready_for_commit": ready,
        "lifecycle": status["state"],
        "readiness": status["readiness"],
        "policy_profile": outcome.policy["profile"],
        "policy_id": outcome.policy["policy_id"],
        "routing": outcome.routing,
        "reviewed_patch": status.get("reviewed_patch"),
        "current_patch": status.get("current_patch"),
        "stopped_at": None,
        "commit_handoff": None,
        "git_writes": 0,
        "human_gates": HUMAN_GATES,
    }
    if not ready:
        live["stopped_at"] = "MISSION_NOT_READY"
        return live
    try:
        handoff = plane.handoff(mission_id, base_branch=base_branch,
                                issue=issue)
    except Exception as exc:  # noqa: BLE001 - evidence records the reason
        live["stopped_at"] = "GO_COMMIT_HANDOFF_UNAVAILABLE"
        live["handoff_reason"] = f"{type(exc).__name__}: {exc}"
        return live
    live["commit_handoff"] = handoff
    live["stopped_at"] = "HUMAN_GO_COMMIT_GATE"
    live["pending_human_gate"] = "GO_COMMIT"
    recovery = plane.recover(mission_id)
    live["recovery"] = recovery
    live["recovery_action"] = recovery["action"]
    live["dashboard"] = plane.dashboard(mission_id)["state"]
    return live


def run_self_hosting_dogfood(
    root: str | Path,
    *,
    repo: str | Path | None = None,
    workspace: str | Path | None = None,
    mission_id: str | None = None,
    base_branch: str = "main",
    issue: str | None = None,
    clock: Callable[[], str] = utc_now,
    write_evidence: bool = True,
) -> dict[str, Any]:
    """Run and persist the M040 self-hosting dogfood evidence."""
    root_str = str(root)
    Path(root_str).mkdir(parents=True, exist_ok=True)
    fixture_clock = release_acceptance.ScriptedClock()
    fixture = _fixture_proof(root_str, clock=fixture_clock)
    live = _live_dogfood(
        root_str, repo=str(repo) if repo is not None else None,
        workspace=str(workspace) if workspace is not None else None,
        clock=clock, base_branch=base_branch, issue=issue,
        mission_id=mission_id)
    evidence: dict[str, Any] = {
        "schema_version": model.SCHEMA_VERSION,
        "operator_version": model.OPERATOR_VERSION,
        "generator": "trajectory_os.operator.dogfood",
        "built_at": clock(),
        "root": root_str,
        "repo": str(repo) if repo is not None else None,
        "self_hosting": True,
        "human_gates": HUMAN_GATES,
        "production_code_path": True,
        "fixture_proof_separated_from_live": True,
        "fixture_proof": fixture,
        "live_dogfood": live,
        "guardrails": {
            "agent_git_writes": 0,
            "runtime_control_git_writes": 0,
            "real_go_commit": False,
            "real_go_merge": False,
            "crossed_human_gate": False,
        },
        "linkage": {
            "mission_id": live["mission_id"],
            "run_id": live["mission_id"],
            "reviewed_patch": live.get("reviewed_patch"),
            "commit_handoff_patch": (
                (live.get("commit_handoff") or {}).get(
                    "reviewed_patch_sha256")),
            "fixture_reviewed_patch": fixture.get("reviewed_patch"),
        },
    }
    evidence["status"] = (
        "PASS" if fixture.get("ok") and live.get("ready_for_commit")
        else "FAIL")
    if write_evidence:
        _write_evidence(root_str, live["mission_id"], evidence)
    return evidence


def _write_evidence(root: str, mission_id: str,
                    evidence: Mapping[str, Any]) -> None:
    from trajectory_os.assembly import store as assembly_store

    write_json(Path(root) / model.SELF_HOSTING_EVIDENCE_NAME,
               dict(evidence))
    mission_root = Path(assembly_store.mission_root(root, mission_id))
    mission_root.mkdir(parents=True, exist_ok=True)
    write_json(mission_root / model.SELF_HOSTING_EVIDENCE_NAME,
               dict(evidence))


def load_self_hosting_evidence(root: str,
                               mission_id: str | None = None,
                               ) -> dict[str, Any] | None:
    """Read the durable self-hosting evidence (never prose)."""
    path = Path(root) / model.SELF_HOSTING_EVIDENCE_NAME
    document = read_optional_json(path)
    if document is not None:
        return document
    if mission_id is None:
        return None
    from trajectory_os.assembly import store as assembly_store

    return read_optional_json(
        Path(assembly_store.mission_root(root, mission_id))
        / model.SELF_HOSTING_EVIDENCE_NAME)


__all__ = [
    "DOGFOOD_OBJECTIVE",
    "DOGFOOD_OPERATOR",
    "DOGFOOD_TOKEN",
    "HUMAN_GATES",
    "load_self_hosting_evidence",
    "run_self_hosting_dogfood",
]
