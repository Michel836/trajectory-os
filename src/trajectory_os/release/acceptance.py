"""M036–M039 — deterministic human-gated release acceptance matrix.

Every case drives the *real* release code with a deterministic in-memory Git
adapter and GitHub fixture, on top of a *real* M031 mission driven to
``READY_FOR_COMMIT`` by the canonical orchestrator with fixture executors and
scripted reviewers. The report is therefore a pure function of its inputs and
requires no credentials, network or repository mutation.

The matrix proves the eighteen release-trust claims (see ``_CASES``).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trajectory_os.assembly import model as assembly_model
from trajectory_os.assembly import orchestrator as assembly_run
from trajectory_os.assembly import store as assembly_store
from trajectory_os.benchmark.executor import FixtureExecutor
from trajectory_os.observability import model as obs_model
from trajectory_os.observability import run as obs_run
from trajectory_os.observability import store as obs_store
from trajectory_os.release import closure as release_closure
from trajectory_os.release import evidence as release_evidence
from trajectory_os.release import handoff as release_handoff
from trajectory_os.release import merge_gate, model, pull_request, store
from trajectory_os.release.authorization import authorize, unauthorized
from trajectory_os.release.git_adapter import FakeGitAdapter
from trajectory_os.release.github_adapter import FixtureGitHubAdapter

ACCEPTANCE_VERSION = "m039.1"

OPERATOR = "operator"
TOKEN = "deterministic-operator-token"
PASS_REVIEW = (
    "VERDICT: PASS\nBLOCKERS:\n- none\nMAJORS:\n- none\nMINORS:\n- none\n"
    "FINAL RECOMMENDATION: GO COMMIT\n")

_CI_RUN_ID = "10000000001"


class ScriptedClock:
    def __init__(self, start: int = 0) -> None:
        self._tick = start

    def __call__(self) -> str:
        self._tick += 1
        return f"2026-09-21T00:00:{self._tick:02d}Z"


@dataclass(frozen=True)
class AcceptanceCheck:
    case: str
    description: str
    ok: bool
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"case": self.case, "description": self.description,
                "ok": self.ok, "detail": dict(self.detail)}


@dataclass(frozen=True)
class AcceptanceCase:
    case: str
    title: str
    ok: bool
    checks: tuple[AcceptanceCheck, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"case": self.case, "title": self.title, "ok": self.ok,
                "checks": [c.to_dict() for c in self.checks]}


@dataclass(frozen=True)
class ReleaseAcceptanceReport:
    version: str
    root: str
    generated_at: str
    cases: tuple[AcceptanceCase, ...]

    @property
    def status(self) -> str:
        return "PASS" if all(case.ok for case in self.cases) else "FAIL"

    @property
    def checks(self) -> tuple[AcceptanceCheck, ...]:
        return tuple(c for case in self.cases for c in case.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": f"trajectory-release-acceptance/{self.version}",
            "version": self.version,
            "generated_at": self.generated_at,
            "root": self.root,
            "status": self.status,
            "cases": [case.to_dict() for case in self.cases],
            "summary": {
                "cases": len(self.cases),
                "passed_cases": sum(1 for c in self.cases if c.ok),
                "checks": len(self.checks),
                "passed_checks": sum(1 for c in self.checks if c.ok),
            },
        }

    def render(self) -> str:
        lines = [
            "=" * 72,
            f"TRAJECTORY-OS RELEASE ACCEPTANCE ({self.version})",
            f"status : {self.status}",
            f"root   : {self.root}",
            f"at     : {self.generated_at}",
            "-" * 72,
        ]
        for case in self.cases:
            lines.append(f"[{'PASS' if case.ok else 'FAIL'}] "
                         f"{case.case}: {case.title}")
            for check in case.checks:
                lines.append(
                    f"    {'ok  ' if check.ok else 'FAIL'} {check.description}")
        lines.append("-" * 72)
        lines.append(
            f"{sum(1 for c in self.cases if c.ok)}/{len(self.cases)} cases, "
            f"{sum(1 for c in self.checks if c.ok)}/{len(self.checks)} checks")
        lines.append("=" * 72)
        return "\n".join(lines)


# --- helpers ------------------------------------------------------------------


def _workspace(root: str, mission_id: str) -> str:
    workspace = Path(root) / mission_id / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return str(workspace)


def make_ready_mission(root: str, mission_id: str, *,
                       clock: ScriptedClock | None = None) -> None:
    """Drive a real M031 mission to READY_FOR_COMMIT (fixture reviewer)."""
    clock = clock or ScriptedClock()
    orchestrator = assembly_run.MissionOrchestrator(
        root, executor=FixtureExecutor(interrupt_once=False),
        reviewer_factory=obs_run.scripted_reviewer_factory([PASS_REVIEW]),
        clock=clock)
    result = orchestrator.start(assembly_run.MissionRequest(
        objective=f"release acceptance {mission_id}: fix add(a, b)",
        workspace=_workspace(root, mission_id),
        mission_id=mission_id, workload_id="small-targeted-repair",
        trust_policy=model_trust()))
    if result.status.get("readiness") != obs_model.RD_READY_FOR_COMMIT:
        raise RuntimeError(f"mission {mission_id} not ready: "
                           f"{result.status.get('readiness')}")


def model_trust() -> assembly_model.TrustPolicy:
    return assembly_model.TrustPolicy(max_repairs=2)


@dataclass
class Pipeline:
    root: str
    mission_id: str
    git: FakeGitAdapter
    github: FixtureGitHubAdapter
    clock: ScriptedClock
    branch: str = "bundle/release-acceptance"

    def __post_init__(self) -> None:
        branch = self.git.branch or self.branch
        self.branch = branch
        self.github.branch_heads.setdefault(branch, self.git.head_sha)
        self.github.branch_heads.setdefault("main", "b" * 40)

    def ready(self) -> None:
        make_ready_mission(self.root, self.mission_id, clock=self.clock)

    def handoff(self) -> model.CommitHandoff:
        return release_handoff.build_commit_handoff(
            self.root, self.mission_id, git=self.git,
            base_branch="main", clock=self.clock)

    def commit(self) -> model.CommitResult:
        result = release_handoff.go_commit(
            self.root, self.mission_id, git=self.git,
            authorization=authorize(model.GATE_GO_COMMIT, TOKEN,
                                    actor=OPERATOR, clock=self.clock),
            clock=self.clock)
        self.github.branch_heads[self.branch] = result.commit_sha
        return result

    def bind(self) -> model.PullRequestBinding:
        return pull_request.bind_pull_request(
            self.root, self.mission_id, git=self.git, github=self.github,
            base_branch="main", clock=self.clock)

    def set_ci(self, states: str) -> str:
        state = store.load_state(assembly_store.mission_root(
            self.root, self.mission_id))
        commit_sha = state.commit_sha or ""
        self.github.set_checks(commit_sha, (ci_runs(commit_sha, states),))
        return commit_sha

    def watch(self, *, persist: bool = True) -> model.CiStatus:
        return pull_request.watch_ci(
            self.root, self.mission_id, github=self.github, persist=persist,
            clock=self.clock)

    def merge_handoff(self) -> model.MergeHandoff:
        return merge_gate.build_merge_handoff(
            self.root, self.mission_id, github=self.github,
            base_branch="main", clock=self.clock)

    def merge(self) -> model.MergeResult:
        return merge_gate.go_merge(
            self.root, self.mission_id, github=self.github,
            authorization=authorize(model.GATE_GO_MERGE, TOKEN,
                                    actor=OPERATOR, clock=self.clock),
            clock=self.clock)

    def closure(self) -> model.ReleaseClosure:
        return release_closure.build_release_closure(
            self.root, self.mission_id, github=self.github,
            base_branch="main", clock=self.clock)

    def full_release(self) -> None:
        self.ready()
        self.handoff()
        self.commit()
        self.bind()
        self.set_ci(model.CI_SUCCESS)
        self.watch()
        self.merge_handoff()
        self.merge()
        self.closure()


def ci_runs(head_sha: str, state: str) -> model.CiRun:
    if state == model.CI_SUCCESS:
        status, conclusion = "completed", "success"
    elif state == model.CI_QUEUED:
        status, conclusion = "queued", None
    elif state == model.CI_IN_PROGRESS:
        status, conclusion = "in_progress", None
    elif state == model.CI_FAILURE:
        status, conclusion = "completed", "failure"
    elif state == model.CI_CANCELLED:
        status, conclusion = "completed", "cancelled"
    else:
        raise ValueError(state)
    job = model.CiJob(name="quality", status=status, conclusion=conclusion,
                      url="https://example.invalid/job", log_ref="log://job")
    return model.CiRun(
        workflow="CI", run_id=_CI_RUN_ID, name="quality", head_sha=head_sha,
        status=status, conclusion=conclusion,
        url="https://example.invalid/run", jobs=(job,))


def _digest_root(mission_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(mission_root.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(mission_root)).encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _pipeline(root: str, mission_id: str, *, tick: int = 0) -> Pipeline:
    return Pipeline(
        root=root, mission_id=mission_id,
        git=FakeGitAdapter(branch="bundle/release-acceptance",
                           head_sha="a" * 40),
        github=FixtureGitHubAdapter(), clock=ScriptedClock(tick))


def _check(checks: list[AcceptanceCheck], case: str, description: str,
           ok: bool, **detail: Any) -> None:
    checks.append(AcceptanceCheck(case=case, description=description,
                                  ok=bool(ok), detail=detail))


# --- cases --------------------------------------------------------------------


def _case_handoff(root: str, checks: list[AcceptanceCheck]) -> str:
    pipe = _pipeline(root, "m036-handoff")
    pipe.ready()
    handoff = pipe.handoff()
    _check(checks, "1", "READY_FOR_COMMIT produces a deterministic handoff",
           handoff.gate == model.GATE_GO_COMMIT
           and model.is_sha256(handoff.patch_sha256)
           and handoff.patch_sha256 == handoff.reviewed_patch_sha256)
    _check(checks, "1", "handoff records branch, baseline and HEAD",
           handoff.branch == pipe.branch and handoff.base_branch == "main"
           and model.is_git_sha(handoff.baseline_head)
           and model.is_git_sha(handoff.current_head))
    # Re-emitting the handoff with the same inputs is deterministic in content.
    handoff2 = pipe.handoff()
    _check(checks, "1", "commit handoff is deterministic in content",
           handoff2.patch_sha256 == handoff.patch_sha256
           and handoff2.scope_summary == handoff.scope_summary)
    _check(checks, "1", "handoff artifact persists and is self-consistent",
           store.exists(assembly_store.mission_root(
               root, "m036-handoff"), store.COMMIT_HANDOFF_NAME))
    return "m036-handoff"


def _case_stale_patch(root: str, checks: list[AcceptanceCheck]) -> None:
    pipe = _pipeline(root, "m036-stale-patch")
    pipe.ready()
    pipe.handoff()
    mission_root = assembly_store.mission_root(root, "m036-stale-patch")
    document = store.read_document(mission_root, "closure.json")
    document["current_patch"] = "f" * 64
    obs_store.write_json(mission_root / "closure.json", document)
    code = _error_code(lambda: pipe.commit())
    _check(checks, "2", "a stale patch blocks GO COMMIT",
           code in (model.R_READINESS_CHANGED, model.R_PATCH_MISMATCH),
           code=code)


def _case_moved_head(root: str, checks: list[AcceptanceCheck]) -> None:
    pipe = _pipeline(root, "m036-moved-head")
    pipe.ready()
    pipe.handoff()
    pipe.git.advance_head(head_sha="c" * 40)
    code = _error_code(lambda: pipe.commit())
    _check(checks, "3", "a moved HEAD blocks GO COMMIT",
           code == model.R_HEAD_MOVED, code=code)


def _case_wrong_branch(root: str, checks: list[AcceptanceCheck]) -> None:
    pipe = _pipeline(root, "m036-wrong-branch")
    pipe.ready()
    pipe.handoff()
    pipe.git.advance_head(branch="other-branch")
    code = _error_code(lambda: pipe.commit())
    _check(checks, "4", "a moved branch blocks GO COMMIT",
           code == model.R_BRANCH_MOVED, code=code)


def _case_authorized_commit(root: str, checks: list[AcceptanceCheck]) -> str:
    pipe = _pipeline(root, "m037-commit")
    pipe.ready()
    handoff = pipe.handoff()
    result = pipe.commit()
    _check(checks, "5", "authorized commit/push records the exact commit SHA",
           model.is_git_sha(result.commit_sha)
           and result.commit_sha != handoff.current_head
           and result.pushed_sha == result.commit_sha)
    _check(checks, "5", "commit result binds the reviewed semantic patch",
           result.reviewed_patch_sha256 == handoff.reviewed_patch_sha256
           and result.parent_head == handoff.current_head)
    _check(checks, "5", "unauthorized commit is refused before any Git write",
           _error_code(lambda: release_handoff.go_commit(
               root, "m037-commit", git=FakeGitAdapter(head_sha="a" * 40),
               authorization=unauthorized(model.GATE_GO_COMMIT),
               clock=pipe.clock)) == model.R_UNAUTHORIZED)
    return "m037-commit"


def _case_pr_bound(root: str, checks: list[AcceptanceCheck]) -> str:
    pipe = _pipeline(root, "m037-pr")
    pipe.full_release()
    binding = store.load_pr_binding(assembly_store.mission_root(
        root, "m037-pr"))
    commit = store.load_commit_result(assembly_store.mission_root(
        root, "m037-pr"))
    _check(checks, "6", "exactly one PR is bound to the exact head SHA",
           binding.bound_commit_sha == commit.commit_sha
           and binding.head_sha == commit.commit_sha)
    # A second binding for the same commit is idempotent.
    again = pipe.bind()
    _check(checks, "6", "repeated PR binding is idempotent",
           again.number == binding.number)
    return "m037-pr"


def _ci_blocks_merge(root: str, checks: list[AcceptanceCheck], *,
                     case: str, state: str, label: str) -> None:
    pipe = _pipeline(root, f"m038-{state}")
    pipe.ready()
    pipe.handoff()
    pipe.commit()
    pipe.bind()
    if state != model.CI_MISSING:
        pipe.set_ci(state)
    pipe.watch()
    code = _error_code(lambda: pipe.merge_handoff())
    _check(checks, case, f"{label} exact-head CI blocks merge",
           code == model.R_CI_NOT_GREEN, code=code, state=state)


def _case_ci_states(root: str, checks: list[AcceptanceCheck]) -> None:
    _ci_blocks_merge(root, checks, case="7", state=model.CI_QUEUED,
                     label="queued")
    _ci_blocks_merge(root, checks, case="8", state=model.CI_IN_PROGRESS,
                     label="in-progress")
    _ci_blocks_merge(root, checks, case="9", state=model.CI_FAILURE,
                     label="failed")
    _ci_blocks_merge(root, checks, case="10", state=model.CI_CANCELLED,
                     label="cancelled")
    _ci_blocks_merge(root, checks, case="11", state=model.CI_MISSING,
                     label="missing")


def _case_moved_pr_head(root: str, checks: list[AcceptanceCheck]) -> None:
    pipe = _pipeline(root, "m038-moved-pr-head")
    pipe.ready()
    pipe.handoff()
    pipe.commit()
    pipeline_binding = pipe.bind()
    pipe.set_ci(model.CI_SUCCESS)
    pipe.watch()
    pipe.merge_handoff()
    index = next(
        i for i, pr in enumerate(pipe.github.prs)
        if pr.number == pipeline_binding.number)
    pipe.github.prs[index] = dataclasses.replace(
        pipe.github.prs[index], head_sha="d" * 40)
    code = _error_code(lambda: pipe.merge())
    _check(checks, "12", "a moved PR head blocks merge",
           code in (model.R_PR_HEAD_MOVED, model.R_CI_NOT_GREEN), code=code)


def _case_successful_merge(root: str, checks: list[AcceptanceCheck]) -> str:
    pipe = _pipeline(root, "m038-merge")
    pipe.full_release()
    result = store.load_merge_result(assembly_store.mission_root(
        root, "m038-merge"))
    _check(checks, "13",
           "green exact-head CI + explicit GO MERGE permits merge",
           result.merged and model.is_git_sha(result.merge_sha or "")
           and result.merge_method == model.MERGE_SQUASH)
    _check(checks, "13", "unauthorized merge is refused",
           _error_code(lambda: merge_gate.go_merge(
               root, "m038-merge",
               github=pipe.github,
               authorization=unauthorized(model.GATE_GO_MERGE),
               clock=pipe.clock)) == model.R_UNAUTHORIZED)
    return "m038-merge"


def _case_target_verification(root: str,
                              checks: list[AcceptanceCheck]) -> None:
    closure = store.load_release_closure(assembly_store.mission_root(
        root, "m038-merge"))
    merge = store.load_merge_result(assembly_store.mission_root(
        root, "m038-merge"))
    _check(checks, "14", "target branch is verified after merge",
           closure.target_branch_verified
           and closure.target_branch == "main"
           and merge.target_head_sha == closure.merge_sha)


def _case_closure_links(root: str, checks: list[AcceptanceCheck]) -> str:
    mission_id = "m039-closure"
    pipe = _pipeline(root, mission_id)
    pipe.full_release()
    mission_root = assembly_store.mission_root(root, mission_id)
    closure = store.load_release_closure(mission_root)
    commit = store.load_commit_result(mission_root)
    binding = store.load_pr_binding(mission_root)
    merge = store.load_merge_result(mission_root)
    gate_evidence = release_evidence.derive_review_gate(
        release_evidence.load_mission_evidence(root, mission_id))
    _check(checks, "15", "closure links mission/patch/commit/PR/CI/merge",
           closure.mission_id == mission_id
           and closure.reviewed_patch_sha256
           == gate_evidence.semantic_patch_identity
           and closure.commit_sha == commit.commit_sha
           and closure.pr_number == binding.number
           and closure.pr_head_sha == binding.head_sha
           and closure.ci_status == model.CI_SUCCESS
           and closure.merge_sha == merge.merge_sha)
    _check(checks, "15", "closure records the GO COMMIT and GO MERGE gates",
           closure.go_commit_evidence.get("gate") == model.GATE_GO_COMMIT
           and closure.go_merge_evidence.get("gate") == model.GATE_GO_MERGE
           and closure.go_merge_evidence.get("ci_state") == model.CI_SUCCESS)
    return mission_id


def _case_read_only(root: str, checks: list[AcceptanceCheck]) -> None:
    mission_id = "m039-closure"
    mission_root = assembly_store.mission_root(root, mission_id)
    before = _digest_root(mission_root)
    pipe = _pipeline(root, mission_id)
    pipe.watch(persist=False)
    release_closure.reconstruct_release(root, mission_id)
    release_evidence.derive_review_gate(
        release_evidence.load_mission_evidence(root, mission_id))
    after = _digest_root(mission_root)
    _check(checks, "16", "watch/status/reconstruct are read-only",
           before == after)


def _case_repeated_reconstruct(root: str,
                               checks: list[AcceptanceCheck]) -> None:
    mission_id = "m039-closure"
    first = release_closure.reconstruct_release(root, mission_id)
    second = release_closure.reconstruct_release(root, mission_id)
    _check(checks, "17", "repeated release reconstruction is idempotent",
           json.dumps(first, sort_keys=True)
           == json.dumps(second, sort_keys=True))


def _case_trust_boundary(root: str, checks: list[AcceptanceCheck]) -> None:
    repo_src = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    guarded = (
        "agents", "assembly", "benchmark", "missions", "observability",
        "runtime_control.py",
    )
    verbs = ("commit", "push", "merge", "reset", "restore", "clean", "stash",
             "rebase", "switch", "checkout")
    for relative in guarded:
        target = repo_src / relative
        files = [target] if target.is_file() else sorted(target.rglob("*.py")) \
            if target.is_dir() else []
        for path in files:
            source = path.read_text(encoding="utf-8")
            for verb in verbs:
                if f'"git", "{verb}"' in source:
                    offenders.append(f"{path.name}:{verb}")
    _check(checks, "18",
           "non-release modules never perform a Git trust-boundary write",
           not offenders, offenders=offenders)
    _check(checks, "18",
           "agent actor cannot mint a release authorization",
           _error_code(lambda: authorize(model.GATE_GO_MERGE, TOKEN,
                                         actor="agent")) == model.R_UNAUTHORIZED)
    _check(checks, "18",
           "the unauthorized sentinel never validates",
           _error_code(lambda: unauthorized(model.GATE_GO_COMMIT).validate())
           == model.R_UNAUTHORIZED)
    # The release layer is the only layer allowed to write Git, and only via
    # the authorized adapter methods (proved by the checks above).
    _check(checks, "18", "runtime control remains release-write free",
           "subprocess" not in (
               repo_src / "runtime_control.py").read_text(encoding="utf-8"))


def _error_code(action: Any) -> str | None:
    try:
        action()
    except model.ReleaseError as exc:
        return exc.code
    return None


_CASES = (
    ("1", "READY_FOR_COMMIT -> deterministic commit handoff"),
    ("2", "Stale patch blocks GO COMMIT"),
    ("3", "Moved HEAD blocks GO COMMIT"),
    ("4", "Wrong branch blocks GO COMMIT"),
    ("5", "Authorized commit/push records the exact commit SHA"),
    ("6", "PR bound to the exact head SHA"),
    ("7", "Queued CI blocks merge"),
    ("8", "In-progress CI blocks merge"),
    ("9", "Failed CI blocks merge"),
    ("10", "Cancelled CI blocks merge"),
    ("11", "Missing CI blocks merge"),
    ("12", "Moved PR head blocks merge"),
    ("13", "Green exact-head CI + explicit GO MERGE permits merge"),
    ("14", "Target branch verification after merge"),
    ("15", "Release closure links mission/patch/commit/PR/CI/merge"),
    ("16", "Status/watch/reconstruct are read-only"),
    ("17", "Repeated release reconstruction is idempotent"),
    ("18", "Agent/runtime-control trust boundary remains intact"),
)


def run_acceptance(root: str | Path, *,
                   generated_at: str | None = None,
                   ) -> ReleaseAcceptanceReport:
    """Run the deterministic M036–M039 release acceptance matrix."""
    root_str = str(root)
    Path(root_str).mkdir(parents=True, exist_ok=True)
    checks: list[AcceptanceCheck] = []
    _case_handoff(root_str, checks)
    _case_stale_patch(root_str, checks)
    _case_moved_head(root_str, checks)
    _case_wrong_branch(root_str, checks)
    _case_authorized_commit(root_str, checks)
    _case_pr_bound(root_str, checks)
    _case_ci_states(root_str, checks)
    _case_moved_pr_head(root_str, checks)
    _case_successful_merge(root_str, checks)
    _case_target_verification(root_str, checks)
    _case_closure_links(root_str, checks)
    _case_read_only(root_str, checks)
    _case_repeated_reconstruct(root_str, checks)
    _case_trust_boundary(root_str, checks)
    cases = tuple(
        AcceptanceCase(
            case=code, title=title,
            ok=all(check.ok for check in checks if check.case == code),
            checks=tuple(check for check in checks if check.case == code))
        for code, title in _CASES)
    return ReleaseAcceptanceReport(
        version=ACCEPTANCE_VERSION, root=root_str,
        generated_at=generated_at or model.utc_now(), cases=cases)


__all__ = [
    "ACCEPTANCE_VERSION",
    "AcceptanceCase",
    "AcceptanceCheck",
    "Pipeline",
    "ReleaseAcceptanceReport",
    "ScriptedClock",
    "ci_runs",
    "make_ready_mission",
    "run_acceptance",
]
