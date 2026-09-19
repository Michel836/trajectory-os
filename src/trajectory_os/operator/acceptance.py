"""M047 — production acceptance matrix and product boundary.

A deterministic, machine-checkable matrix over the *real* operator/release
code with fixture adapters, scripted reviewers and an explicit clock. The
report is a pure function of those inputs: no credentials, network or real
repository mutation.

The matrix proves the thirty-five platform claims listed in ``_CASES``,
covering the full happy path, self-hosting dogfood, stale-review/patch/head
fail-closed behaviour, backend fallback, phantom-reviewer prevention,
crash/recovery idempotence, exact-head CI, human merge authorization, release
reconstruction, event replay, read-only observation, trust boundaries, policy
determinism, legacy compatibility, terminal-vs-readiness and semantic
identity.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trajectory_os.assembly import control as assembly_control
from trajectory_os.assembly import model as assembly_model
from trajectory_os.assembly import orchestrator as assembly_run
from trajectory_os.assembly import store as assembly_store
from trajectory_os.benchmark.executor import FixtureExecutor
from trajectory_os.observability import model as obs_model
from trajectory_os.observability import run as obs_run
from trajectory_os.observability import store as obs_store
from trajectory_os.operator import events as operator_events
from trajectory_os.operator import model
from trajectory_os.operator import recovery as operator_recovery
from trajectory_os.operator import state as operator_state
from trajectory_os.operator._util import utc_now
from trajectory_os.operator.control_plane import ControlPlane
from trajectory_os.operator.dogfood import run_self_hosting_dogfood
from trajectory_os.operator.policy import (
    LEGACY_PHANTOM_REVIEWER,
    PROFILE_BENCHMARK,
    PROFILE_RELEASE,
    PROFILE_SAFE,
    release_policy,
    resolve_policy,
)
from trajectory_os.operator.routing import (
    BackendCapability,
    load_routing,
    persist_routing,
    resolve_routing,
    routing_history,
)
from trajectory_os.release import acceptance as release_acceptance
from trajectory_os.release import closure as release_closure
from trajectory_os.release import evidence as release_evidence
from trajectory_os.release import model as release_model
from trajectory_os.release import store as release_store
from trajectory_os.release.authorization import authorize, unauthorized
from trajectory_os.release.git_adapter import FakeGitAdapter
from trajectory_os.release.github_adapter import FixtureGitHubAdapter

ACCEPTANCE_VERSION = "m047.1"

OPERATOR = "operator"
TOKEN = "m047-deterministic-operator-token"

ScriptedClock = release_acceptance.ScriptedClock


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
class OperatorAcceptanceReport:
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
            "schema": f"trajectory-operator-acceptance/{self.version}",
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
            f"TRAJECTORY-OS OPERATOR ACCEPTANCE ({self.version})",
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


# --- deterministic operator pipeline ------------------------------------------


class OperatorPipeline:
    """The full production path driven through the unified control plane."""

    def __init__(self, root: str, mission_id: str, *,
                 tick: int = 0, branch: str = "m047-acceptance") -> None:
        self.root = root
        self.mission_id = mission_id
        self.clock = ScriptedClock(tick)
        self.git = FakeGitAdapter(branch=branch, head_sha="a" * 40)
        self.branch = branch
        self.github = FixtureGitHubAdapter()
        self.github.branch_heads.setdefault(branch, self.git.head_sha or "")
        self.github.branch_heads.setdefault("main", "b" * 40)
        self.plane = ControlPlane(root, git=self.git, github=self.github,
                                  clock=self.clock)

    def workspace(self) -> str:
        path = Path(self.root) / self.mission_id / "workspace"
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def start(self, *, interrupt_after_phase: str | None = None,
              overrides: Mapping[str, object] | None = None,
              executor: FixtureExecutor | None = None,
              reviewer_factory: Any | None = None,
              ) -> None:
        self.plane.start(
            assembly_run.MissionRequest(
                objective=f"M047 acceptance {self.mission_id}",
                workspace=self.workspace(), mission_id=self.mission_id),
            profile=PROFILE_RELEASE, overrides=overrides,
            executor=executor or FixtureExecutor(interrupt_once=False),
            reviewer_factory=reviewer_factory,
            interrupt_after_phase=interrupt_after_phase)

    def ready(self) -> None:
        self.start()

    @property
    def mission_root(self) -> Path:
        return Path(assembly_store.mission_root(self.root, self.mission_id))

    def handoff(self) -> dict[str, Any]:
        return self.plane.handoff(self.mission_id, base_branch="main")

    def commit(self) -> dict[str, Any]:
        result = self.plane.go_commit(self.mission_id, token=TOKEN,
                                      actor=OPERATOR)
        self.github.branch_heads[self.branch] = result["commit_sha"]
        return result

    def bind(self) -> dict[str, Any]:
        return self.plane.bind_pr(self.mission_id, base_branch="main")

    def set_ci(self, state: str, *, head_sha: str | None = None) -> str:
        commit_sha = head_sha or self.commit_sha()
        self.github.set_checks(
            commit_sha,
            (release_acceptance.ci_runs(commit_sha, state),))
        return commit_sha

    def commit_sha(self) -> str:
        state = release_store.load_state(self.mission_root)
        return state.commit_sha or ""

    def watch(self, *, persist: bool = True) -> dict[str, Any]:
        return self.plane.watch_ci(self.mission_id, persist=persist)

    def merge_handoff(self) -> dict[str, Any]:
        return self.plane.merge_handoff(self.mission_id, base_branch="main")

    def merge(self) -> dict[str, Any]:
        return self.plane.go_merge(self.mission_id, token=TOKEN,
                                   actor=OPERATOR)

    def closure(self) -> dict[str, Any]:
        return self.plane.closure(self.mission_id, base_branch="main",
                                  issue="240")

    def full_release(self) -> None:
        self.ready()
        self.handoff()
        self.commit()
        self.bind()
        self.set_ci(release_model.CI_SUCCESS)
        self.watch()
        self.merge_handoff()
        self.merge()
        self.closure()

    def review_gate(self) -> release_model.ReviewGateEvidence:
        return release_evidence.derive_review_gate(
            release_evidence.load_mission_evidence(self.root,
                                                   self.mission_id))


def _pipeline(root: str, mission_id: str, *, tick: int = 0,
              branch: str = "m047-acceptance") -> OperatorPipeline:
    return OperatorPipeline(root, mission_id, tick=tick, branch=branch)


# --- check helpers ------------------------------------------------------------


def _check(checks: list[AcceptanceCheck], case: str, description: str,
           ok: bool, **detail: Any) -> None:
    checks.append(AcceptanceCheck(case=case, description=description,
                                  ok=bool(ok), detail=detail))


def _error_code(action: Any) -> str | None:
    try:
        action()
    except (model.OperatorError, release_model.ReleaseError) as exc:
        return str(exc.code)
    except (RuntimeError, ValueError) as exc:
        return f"{type(exc).__name__}"
    return None


def _digest_root(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_file():
            digest.update(str(item.relative_to(path)).encode("utf-8"))
            digest.update(item.read_bytes())
    return digest.hexdigest()


# --- cases --------------------------------------------------------------------


def _case_happy_path(root: str, checks: list[AcceptanceCheck]) -> str:
    mission_id = "m047-happy"
    pipe = _pipeline(root, mission_id)
    pipe.full_release()
    closure = release_store.load_release_closure(pipe.mission_root)
    state = release_store.load_state(pipe.mission_root)
    gate = pipe.review_gate()
    _check(checks, "1", "complete mission + release happy path closes",
           closure.status == "CLOSED"
           and state.stage == release_model.RST_RELEASED
           and gate.ready)
    _check(checks, "1", "release closure carries the exact reviewed patch",
           closure.reviewed_patch_sha256 == gate.semantic_patch_identity)
    return mission_id


def _case_self_hosting(root: str, checks: list[AcceptanceCheck]) -> None:
    dogfood_root = str(Path(root) / "dogfood")
    evidence = run_self_hosting_dogfood(
        dogfood_root, repo=None, clock=ScriptedClock(),
        write_evidence=True)
    _check(checks, "2", "self-hosting fixture proof completes the whole path",
           bool(evidence["fixture_proof"].get("ok")),
           fixture=evidence["fixture_proof"])
    _check(checks, "2", "live dogfood reaches READY_FOR_COMMIT safely",
           bool(evidence["live_dogfood"].get("ready_for_commit"))
           and evidence["guardrails"]["real_go_commit"] is False
           and evidence["guardrails"]["crossed_human_gate"] is False)
    _check(checks, "2", "durable self-hosting evidence is persisted",
           (Path(dogfood_root)
            / model.SELF_HOSTING_EVIDENCE_NAME).is_file())


def _case_stale_review(root: str, checks: list[AcceptanceCheck]) -> None:
    pipe = _pipeline(root, "m047-stale-review")
    pipe.ready()
    pipe.handoff()
    mission_root = pipe.mission_root
    sequence = obs_store.event_count(mission_root) + 1
    current_patch = pipe.review_gate().current_patch
    obs_store.append_event(mission_root, obs_model.CanonicalEvent.build(
        run_id=pipe.mission_id, sequence=sequence, kind="PATCH_CAPTURED",
        at=pipe.clock(), stage=obs_model.STAGE_EXECUTE, phase="IMPLEMENT",
        attempt=0, patch=current_patch))
    code = _error_code(lambda: pipe.commit())
    _check(checks, "3", "stale review blocks GO COMMIT",
           code in (release_model.R_READINESS_CHANGED,
                    release_model.R_STALE_REVIEW), code=code)


def _case_changed_patch(root: str, checks: list[AcceptanceCheck]) -> None:
    pipe = _pipeline(root, "m047-changed-patch")
    pipe.ready()
    pipe.handoff()
    document = release_store.read_document(pipe.mission_root, "closure.json")
    document["current_patch"] = "f" * 64
    obs_store.write_json(pipe.mission_root / "closure.json", document)
    code = _error_code(lambda: pipe.commit())
    _check(checks, "4", "changed semantic patch blocks GO COMMIT",
           code in (release_model.R_READINESS_CHANGED,
                    release_model.R_PATCH_MISMATCH), code=code)


def _case_moved_head(root: str, checks: list[AcceptanceCheck]) -> None:
    pipe = _pipeline(root, "m047-moved-head")
    pipe.ready()
    pipe.handoff()
    pipe.git.advance_head(head_sha="c" * 40)
    _check(checks, "6", "moved HEAD blocks GO COMMIT",
           _error_code(lambda: pipe.commit()) == release_model.R_HEAD_MOVED)


def _case_moved_branch(root: str, checks: list[AcceptanceCheck]) -> None:
    pipe = _pipeline(root, "m047-moved-branch")
    pipe.ready()
    pipe.handoff()
    pipe.git.advance_head(branch="other-branch")
    _check(checks, "5", "moved branch blocks GO COMMIT",
           _error_code(lambda: pipe.commit())
           == release_model.R_BRANCH_MOVED)


def _case_unavailable_backend(root: str, checks: list[AcceptanceCheck]) -> None:
    resolved = release_policy(clock=ScriptedClock())
    unavailable = BackendCapability(
        backend=resolved.policy.implementation_backend, provider="deepseek",
        model="deepseek-flash", available=False,
        supports_implementation=False, supports_review=False,
        reason="probe failed")
    code = _error_code(lambda: resolve_routing(
        "m047-unavailable", resolved, capabilities={"pi": unavailable}))
    _check(checks, "7", "unavailable backend fails closed",
           code == model.E_ROUTING_UNAVAILABLE, code=code)


def _case_explicit_fallback(root: str, checks: list[AcceptanceCheck]) -> None:
    resolved = resolve_policy(PROFILE_BENCHMARK, clock=ScriptedClock())
    unavailable = BackendCapability(
        backend="pi", provider="deepseek", model="deepseek-flash",
        available=False, supports_implementation=False, supports_review=False,
        reason="probe failed")
    available = BackendCapability(
        backend="deepseek-harness", provider="deepseek-official",
        model="deepseek-flash", available=True,
        supports_implementation=True, supports_review=False,
        reason="available")
    decision = resolve_routing(
        "m047-fallback", resolved,
        capabilities={"pi": unavailable,
                      "deepseek-harness": available})
    _check(checks, "8", "explicit deterministic fallback is used",
           decision.fallback_used
           and decision.fallback_to == "deepseek-harness"
           and bool(decision.fallback_reason))
    ensure = (Path(root) / "m047-fallback")
    ensure.mkdir(parents=True, exist_ok=True)
    persist_routing(root, "m047-fallback", decision)
    loaded = load_routing(root, "m047-fallback")
    _check(checks, "8", "fallback decision is persisted with its reason",
           loaded is not None
           and loaded.fallback_reason == decision.fallback_reason
           and bool(routing_history(root, "m047-fallback")))


def _case_phantom_reviewer(root: str, checks: list[AcceptanceCheck]) -> None:
    resolved = resolve_policy(PROFILE_SAFE, clock=ScriptedClock(),
                              overrides={"final_review_required": False})
    decision = resolve_routing("m047-phantom", resolved)
    _check(checks, "9", "a disabled reviewer never carries a phantom model",
           decision.final_review.enabled is False
           and decision.final_review.display_model is None)
    code = _error_code(lambda: resolve_policy(
        PROFILE_RELEASE, clock=ScriptedClock(),
        overrides={"final_reviewer_model": LEGACY_PHANTOM_REVIEWER}))
    _check(checks, "9", "the legacy phantom reviewer is refused",
           code in (model.E_ROUTING_PHANTOM_REVIEWER,
                    model.E_POLICY_INVALID), code=code)


def _case_routing_identity(root: str, checks: list[AcceptanceCheck]) -> None:
    resolved = release_policy(clock=ScriptedClock())
    decision = resolve_routing("m047-identity", resolved)
    _check(checks, "10", "reviewer routing identity is explicit and exact",
           decision.final_review.enabled
           and decision.final_review.model == "qwen3.8:27b-q4_K_M"
           and bool(decision.final_review.route_id)
           and decision.inline_review.enabled is False)


def _case_execution_resume(root: str, checks: list[AcceptanceCheck]) -> None:
    pipe = _pipeline(root, "m047-exec-resume")
    pipe.start(interrupt_after_phase=assembly_model.MP_PLAN)
    decision = operator_recovery.decide_recovery(
        root, pipe.mission_id, clock=pipe.clock)
    _check(checks, "11", "execution interruption resumes at execution",
           decision.action == operator_recovery.RA_RESUME_EXECUTION
           and not decision.already_done)
    pipe.plane.resume(pipe.mission_id,
                      executor=FixtureExecutor(interrupt_once=False),
                      reviewer_factory=obs_run.scripted_reviewer_factory(
                          [release_acceptance.PASS_REVIEW]))
    status = obs_store.load_status(pipe.mission_root)
    _check(checks, "11", "resumed execution reaches READY_FOR_COMMIT",
           status["readiness"] == obs_model.RD_READY_FOR_COMMIT
           and status["run_id"] == pipe.mission_id)


def _case_review_resume(root: str, checks: list[AcceptanceCheck]) -> None:
    pipe = _pipeline(root, "m047-review-resume")
    crashed = False
    try:
        pipe.start(executor=FixtureExecutor(interrupt_once=False),
                   reviewer_factory=_crashing_reviewer_factory())
    except RuntimeError:
        crashed = True
    decision = operator_recovery.decide_recovery(
        root, pipe.mission_id, clock=pipe.clock)
    _check(checks, "12", "review interruption resumes safely",
           crashed
           and decision.action in (operator_recovery.RA_RESUME_EXECUTION,
                                   operator_recovery.RA_RESUME_REVIEW),
           action=decision.action)
    _remove_fixture_solution(pipe.workspace())
    pipe.plane.resume(pipe.mission_id,
                      executor=FixtureExecutor(interrupt_once=False),
                      reviewer_factory=obs_run.scripted_reviewer_factory(
                          [release_acceptance.PASS_REVIEW]))
    status = obs_store.load_status(pipe.mission_root)
    _check(checks, "12", "review resume reaches READY_FOR_COMMIT",
           status["readiness"] == obs_model.RD_READY_FOR_COMMIT)


def _case_handoff_resume(root: str, checks: list[AcceptanceCheck]) -> None:
    pipe = _pipeline(root, "m047-handoff-resume")
    pipe.ready()
    first = pipe.handoff()
    decision = operator_recovery.decide_recovery(
        root, pipe.mission_id, clock=pipe.clock)
    _check(checks, "13", "handoff interruption is discovered, not repeated",
           decision.action == operator_recovery.RA_AWAIT_GO_COMMIT
           and operator_recovery.IRREVERSIBLE_COMMIT not in decision.already_done)
    second = pipe.handoff()
    _check(checks, "13", "rebuilt handoff carries the identical patch",
           first["patch_sha256"] == second["patch_sha256"]
           and not release_store.exists(pipe.mission_root,
                                        release_store.COMMIT_RESULT_NAME))


def _case_commit_no_duplicate(root: str,
                              checks: list[AcceptanceCheck]) -> None:
    pipe = _pipeline(root, "m047-commit-once")
    pipe.ready()
    pipe.handoff()
    pipe.commit()
    code = _error_code(lambda: pipe.commit())
    decision = operator_recovery.decide_recovery(
        root, pipe.mission_id, clock=pipe.clock)
    _check(checks, "14", "a commit-result crash never duplicates the commit",
           code == release_model.R_ALREADY_COMMITTED
           and len(pipe.git.commits) == 1
           and decision.action == operator_recovery.RA_BIND_PR
           and operator_recovery.IRREVERSIBLE_COMMIT in decision.already_done)


def _case_pr_no_duplicate(root: str, checks: list[AcceptanceCheck]) -> None:
    pipe = _pipeline(root, "m047-pr-once")
    pipe.ready()
    pipe.handoff()
    pipe.commit()
    first = pipe.bind()
    second = pipe.bind()
    decision = operator_recovery.decide_recovery(
        root, pipe.mission_id, clock=pipe.clock)
    _check(checks, "15", "a PR-bind crash never duplicates the pull request",
           first["number"] == second["number"]
           and len(pipe.github.prs) == 1
           and decision.action == operator_recovery.RA_WATCH_CI)


def _case_ci_exact_head(root: str, checks: list[AcceptanceCheck]) -> None:
    pipe = _pipeline(root, "m047-ci-exact")
    pipe.ready()
    pipe.handoff()
    committed = pipe.commit()
    pipe.bind()
    commit_sha = committed["commit_sha"]
    pipe.set_ci(release_model.CI_SUCCESS, head_sha="e" * 40)
    wrong = pipe.watch(persist=False)
    pipe.set_ci(release_model.CI_SUCCESS, head_sha=commit_sha)
    exact = pipe.watch(persist=False)
    _check(checks, "16", "CI watch is exact-head only",
           wrong["head_sha"] == commit_sha
           and wrong["state"] == release_model.CI_MISSING
           and exact["head_sha"] == commit_sha
           and exact["state"] == release_model.CI_SUCCESS
           and exact["green"])


def _case_ci_blocks(root: str, checks: list[AcceptanceCheck], *,
                    case: str, state: str, label: str) -> None:
    pipe = _pipeline(root, f"m047-ci-{state}")
    pipe.ready()
    pipe.handoff()
    pipe.commit()
    pipe.bind()
    if state != release_model.CI_MISSING:
        pipe.set_ci(state)
    pipe.watch(persist=True)
    code = _error_code(lambda: pipe.merge_handoff())
    _check(checks, case, f"{label} exact-head CI blocks merge",
           code == release_model.R_CI_NOT_GREEN, code=code, state=state)


def _case_moved_pr_head(root: str, checks: list[AcceptanceCheck]) -> None:
    pipe = _pipeline(root, "m047-moved-pr")
    pipe.ready()
    pipe.handoff()
    pipe.commit()
    binding = pipe.bind()
    pipe.set_ci(release_model.CI_SUCCESS)
    pipe.watch()
    pipe.merge_handoff()
    import dataclasses

    index = next(i for i, pr in enumerate(pipe.github.prs)
                 if pr.number == binding["number"])
    pipe.github.prs[index] = dataclasses.replace(
        pipe.github.prs[index], head_sha="d" * 40)
    code = _error_code(lambda: pipe.merge())
    _check(checks, "19", "moved PR head blocks merge",
           code in (release_model.R_PR_HEAD_MOVED,
                    release_model.R_CI_NOT_GREEN), code=code)


def _case_merge_authorization(root: str,
                              checks: list[AcceptanceCheck]) -> None:
    pipe = _pipeline(root, "m047-merge-auth")
    pipe.ready()
    pipe.handoff()
    pipe.commit()
    pipe.bind()
    pipe.set_ci(release_model.CI_SUCCESS)
    pipe.watch()
    pipe.merge_handoff()
    code = _error_code(lambda: pipe.plane.go_merge(
        pipe.mission_id, token=None, actor=OPERATOR))
    _check(checks, "20", "merge requires explicit human authorization",
           code == release_model.R_UNAUTHORIZED
           and len(pipe.github.merged) == 0, code=code)


def _case_merge_no_duplicate(root: str,
                             checks: list[AcceptanceCheck]) -> None:
    pipe = _pipeline(root, "m047-merge-once")
    pipe.full_release()
    again = pipe.merge()
    _check(checks, "21", "a merge is never duplicated after recovery",
           bool(again.get("merged"))
           and len(pipe.github.merged) == 1
           and operator_recovery.decide_recovery(
               root, pipe.mission_id, clock=pipe.clock).action
           == operator_recovery.RA_ALREADY_COMPLETE)


def _case_closure_identity(root: str, checks: list[AcceptanceCheck],
                           mission_id: str) -> None:
    pipe = _pipeline(root, mission_id, tick=1)
    mission_root = pipe.mission_root
    closure = release_store.load_release_closure(mission_root)
    commit = release_store.load_commit_result(mission_root)
    binding = release_store.load_pr_binding(mission_root)
    merge = release_store.load_merge_result(mission_root)
    reconstructed = release_closure.reconstruct_release(root, mission_id)
    _check(checks, "22", "release closure reconstructs exact identities",
           closure.commit_sha == commit.commit_sha
           and closure.pr_head_sha == binding.head_sha
           and closure.pr_number == binding.number
           and closure.merge_sha == merge.merge_sha
           and reconstructed["release"]["release_closure"]["commit_sha"]
           == closure.commit_sha)


def _case_event_replay(root: str, checks: list[AcceptanceCheck],
                       mission_id: str) -> None:
    replay = operator_events.replay(root, mission_id)
    projection = replay["projection"]
    _check(checks, "23", "event replay reconstructs the unified projection",
           replay["event_count"] > 0
           and projection["mission_id"] == mission_id
           and projection["commit_sha"] is not None
           and projection["pr_number"] is not None
           and projection["merge_sha"] is not None
           and projection["release_status"] == "CLOSED")
    first = json.dumps(replay["events"], sort_keys=True)
    second = json.dumps(operator_events.replay(root, mission_id)["events"],
                        sort_keys=True)
    _check(checks, "24", "repeated event replay is idempotent",
           first == second and replay["replay_id"] == (
               operator_events.replay(root, mission_id)["replay_id"]))


def _case_read_only(root: str, checks: list[AcceptanceCheck],
                    mission_id: str) -> None:
    pipe = _pipeline(root, mission_id, tick=1)
    before = _digest_root(pipe.mission_root)
    pipe.plane.status(mission_id)
    pipe.plane.dashboard(mission_id)
    pipe.plane.follow(mission_id, iterations=2)
    pipe.plane.pr_status(mission_id)
    pipe.plane.reconstruct(mission_id)
    operator_events.replay(root, mission_id)
    after = _digest_root(pipe.mission_root)
    _check(checks, "25", "observation commands are read-only",
           before == after)


def _case_control_isolation(root: str, checks: list[AcceptanceCheck]) -> None:
    pipe = _pipeline(root, "m047-control")
    pipe.start(interrupt_after_phase=assembly_model.MP_PLAN)
    outcome = assembly_control.pause(root, pipe.mission_id, clock=pipe.clock)
    mission_root = pipe.mission_root
    release_artifacts = [
        mission_root / release_store.COMMIT_HANDOFF_NAME,
        mission_root / release_store.COMMIT_RESULT_NAME,
        mission_root / release_store.MERGE_RESULT_NAME,
        mission_root / release_store.RELEASE_CLOSURE_NAME,
    ]
    _check(checks, "26", "operator controls remain isolated from release "
           "evidence",
           outcome.result == assembly_control.RESULT_ACCEPTED
           and assembly_control.stop_requested(root, pipe.mission_id)
           and not any(path.exists() for path in release_artifacts))
    _check(checks, "26", "control actions are recorded in their own log",
           bool(assembly_control.control_log(root, pipe.mission_id)))


_WRITE_VERBS = ("commit", "push", "merge", "reset", "restore", "clean",
                "stash", "rebase", "switch", "checkout")


def _scan_git_writes(paths: list[Path]) -> list[str]:
    offenders: list[str] = []
    for target in paths:
        files = ([target] if target.is_file()
                 else sorted(target.rglob("*.py")) if target.is_dir() else [])
        for path in files:
            source = path.read_text(encoding="utf-8")
            for verb in _WRITE_VERBS:
                if f'"git", "{verb}"' in source:
                    offenders.append(f"{path.name}:{verb}")
    return offenders


def _case_trust_boundaries(root: str, checks: list[AcceptanceCheck]) -> None:
    package = Path(__file__).resolve().parents[1]
    operator_dir = package / "operator"
    runtime = package / "runtime_control.py"
    _check(checks, "27", "runtime control cannot perform release writes",
           "subprocess" not in runtime.read_text(encoding="utf-8")
           and not _scan_git_writes([runtime, operator_dir]))
    guarded = ["agents", "assembly", "benchmark", "missions", "observability"]
    targets = [package / name for name in guarded]
    offenders = _scan_git_writes(targets)
    _check(checks, "28", "implementation/review modules never write Git",
           not offenders, offenders=offenders)
    _check(checks, "28", "agents cannot mint a release authorization",
           _error_code(lambda: authorize(release_model.GATE_GO_MERGE, TOKEN,
                                         actor="agent"))
           == release_model.R_UNAUTHORIZED
           and _error_code(lambda: unauthorized(
               release_model.GATE_GO_MERGE).validate())
           == release_model.R_UNAUTHORIZED)


def _case_release_gates(root: str, checks: list[AcceptanceCheck]) -> None:
    resolved = release_policy(clock=ScriptedClock())
    policy = resolved.policy
    _check(checks, "29", "release profile preserves both human gates",
           policy.go_commit_required and policy.go_merge_required
           and policy.exact_head_ci_required
           and policy.final_review_required)
    codes = [
        _error_code(lambda field=field: release_policy(
            clock=ScriptedClock(), overrides={field: False}))
        for field in ("go_commit_required", "go_merge_required",
                      "exact_head_ci_required")]
    _check(checks, "29", "release human/CI gates cannot be disabled",
           all(code in (model.E_POLICY_INVALID,
                        model.E_POLICY_OVERRIDE_INVALID) for code in codes),
           codes=codes)


def _case_policy_determinism(root: str, checks: list[AcceptanceCheck]) -> None:
    first = release_policy(clock=ScriptedClock())
    second = resolve_policy(PROFILE_RELEASE, clock=ScriptedClock())
    _check(checks, "30", "policy resolution is deterministic",
           first.policy_id == second.policy_id
           and first.to_dict() == second.to_dict())
    code = _error_code(lambda: resolve_policy(
        PROFILE_RELEASE, source="environment", clock=ScriptedClock()))
    _check(checks, "30", "the environment cannot mutate policy",
           code == model.E_POLICY_ENVIRONMENT, code=code)


def _case_legacy_compat(root: str, checks: list[AcceptanceCheck],
                        mission_id: str) -> None:
    pipe = _pipeline(root, mission_id, tick=1)
    status = obs_store.load_status(pipe.mission_root)
    gate = pipe.review_gate()
    reconstructed = release_closure.reconstruct_release(root, mission_id)
    types = {event.type for event in operator_events.derive_events(
        root, mission_id)}
    _check(checks, "31", "legacy M030-M039 artifacts remain readable",
           status["run_id"] == mission_id
           and gate.ready
           and reconstructed["mission_id"] == mission_id
           and operator_events.T_LEGACY_CANONICAL in types
           and operator_events.T_RELEASE_CLOSURE in types)


def _case_dashboard_matches(root: str, checks: list[AcceptanceCheck],
                            mission_id: str) -> None:
    pipe = _pipeline(root, mission_id, tick=1)
    state = operator_state.build_operator_state(root, mission_id)
    status = obs_store.load_status(pipe.mission_root)
    commit = release_store.load_commit_result(pipe.mission_root)
    binding = release_store.load_pr_binding(pipe.mission_root)
    merge = release_store.load_merge_result(pipe.mission_root)
    _check(checks, "32", "dashboard state matches the canonical artifacts",
           state.lifecycle == status["state"]
           and state.readiness == status["readiness"]
           and state.commit_sha == commit.commit_sha
           and state.pr_number == binding.number
           and state.merge_sha == merge.merge_sha
           and state.release_closure == "CLOSED")


def _case_terminal_vs_readiness(root: str,
                                checks: list[AcceptanceCheck]) -> None:
    pipe = _pipeline(root, "m047-terminal")
    pipe.start(overrides={"implementation_provider": "ollama",
                          "implementation_model": "deepseek-flash"})
    status = obs_store.load_status(pipe.mission_root)
    state = operator_state.build_operator_state(root, pipe.mission_id)
    _check(checks, "33", "terminal lifecycle stays distinct from readiness",
           status["state"] == obs_model.LC_COMPLETE
           and status["readiness"] == obs_model.RD_BLOCKED
           and state.lifecycle == obs_model.LC_COMPLETE
           and state.readiness == obs_model.RD_BLOCKED
           and state.readiness != obs_model.RD_READY_FOR_COMMIT)


def _case_semantic_identity(root: str, checks: list[AcceptanceCheck],
                            mission_id: str) -> None:
    pipe = _pipeline(root, mission_id, tick=1)
    handoff = release_store.load_commit_handoff(pipe.mission_root)
    commit = release_store.load_commit_result(pipe.mission_root)
    closure = release_store.load_release_closure(pipe.mission_root)
    gate = pipe.review_gate()
    _check(checks, "34", "semantic staging identity remains authoritative",
           handoff.patch_sha256 == handoff.reviewed_patch_sha256
           and commit.reviewed_patch_sha256 == handoff.reviewed_patch_sha256
           and closure.reviewed_patch_sha256
           == gate.semantic_patch_identity
           and closure.final_patch_sha256 == handoff.patch_sha256)


def _case_no_prose_success(root: str,
                           checks: list[AcceptanceCheck]) -> None:
    pipe = _pipeline(root, "m047-no-prose")
    pipe.ready()
    mission_root = pipe.mission_root
    events_path = mission_root / obs_store.EVENTS_NAME
    kept = [
        line for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and '"REVIEW_COMPLETED"' not in line
    ]
    events_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    gate = pipe.review_gate()
    state = operator_state.build_operator_state(root, pipe.mission_id)
    _check(checks, "35", "no success is inferred from prose or status alone",
           not gate.ready
           and gate.reason == release_model.R_STALE_REVIEW
           and state.review_fresh is False
           and state.readiness == obs_model.RD_READY_FOR_COMMIT)


# --- helper executors ---------------------------------------------------------


def _crashing_reviewer_factory() -> Any:
    from trajectory_os.benchmark import review as bench_review

    class _CrashClient:
        def review(self, request: bench_review.ReviewRequest,
                   ) -> bench_review.ReviewerInvocation:
            raise RuntimeError("simulated review process death")

    def factory() -> bench_review.ReviewerClient:
        return _CrashClient()

    return factory


def _force_review_crash(pipe: OperatorPipeline) -> bool:
    """Run one attempt with a reviewer that dies, then report the crash."""
    from trajectory_os.benchmark import review as bench_review

    class _CrashClient:
        def review(self, request: bench_review.ReviewRequest,
                   ) -> bench_review.ReviewerInvocation:
            raise RuntimeError("simulated review process death")

    try:
        pipe.plane.resume(pipe.mission_id,
                          executor=FixtureExecutor(interrupt_once=False),
                          reviewer_factory=lambda: _CrashClient())
    except RuntimeError:
        return True
    return False


def _remove_fixture_solution(workspace: str) -> None:
    for name in ("calc.py", "total.py", "strings_util.py"):
        path = Path(workspace) / name
        if path.is_file():
            path.unlink()


# --- matrix -------------------------------------------------------------------

_CASES = (
    ("1", "Complete mission + release happy path"),
    ("2", "Self-hosting dogfood path"),
    ("3", "Stale review blocks release"),
    ("4", "Changed semantic patch blocks GO COMMIT"),
    ("5", "Moved branch blocks GO COMMIT"),
    ("6", "Moved HEAD blocks GO COMMIT"),
    ("7", "Unavailable backend fails closed"),
    ("8", "Explicit fallback works and is persisted"),
    ("9", "Phantom reviewer cannot appear"),
    ("10", "Reviewer routing identity is explicit"),
    ("11", "Execution crash resumes safely"),
    ("12", "Review crash resumes safely"),
    ("13", "Handoff crash resumes safely"),
    ("14", "Commit-result crash does not duplicate commit"),
    ("15", "PR-bind crash does not duplicate PR"),
    ("16", "CI watch is exact-head only"),
    ("17", "Missing CI blocks merge"),
    ("18", "Failed CI blocks merge"),
    ("19", "Moved PR head blocks merge"),
    ("20", "Merge requires human authorization"),
    ("21", "Merge is not duplicated after recovery"),
    ("22", "Release closure reconstructs exact identities"),
    ("23", "Event replay reconstructs unified projection"),
    ("24", "Repeated replay is idempotent"),
    ("25", "Observation is read-only"),
    ("26", "Operator controls remain isolated"),
    ("27", "Runtime control cannot perform release writes"),
    ("28", "Implementation/review agents cannot write Git"),
    ("29", "Release profile preserves both human gates"),
    ("30", "Policy resolution is deterministic"),
    ("31", "Legacy M030-M039 artifacts remain readable"),
    ("32", "Dashboard status matches canonical artifacts"),
    ("33", "Terminal lifecycle remains distinct from readiness"),
    ("34", "Semantic staging identity remains authoritative"),
    ("35", "No success is inferred from prose"),
)


def run_acceptance(root: str | Path, *,
                   generated_at: str | None = None,
                   ) -> OperatorAcceptanceReport:
    """Run the deterministic M040–M047 operator acceptance matrix."""
    root_str = str(root)
    Path(root_str).mkdir(parents=True, exist_ok=True)
    checks: list[AcceptanceCheck] = []

    happy_mission = _case_happy_path(root_str, checks)
    _case_self_hosting(root_str, checks)
    _case_stale_review(root_str, checks)
    _case_changed_patch(root_str, checks)
    _case_moved_head(root_str, checks)
    _case_moved_branch(root_str, checks)
    _case_unavailable_backend(root_str, checks)
    _case_explicit_fallback(root_str, checks)
    _case_phantom_reviewer(root_str, checks)
    _case_routing_identity(root_str, checks)
    _case_execution_resume(root_str, checks)
    _case_review_resume(root_str, checks)
    _case_handoff_resume(root_str, checks)
    _case_commit_no_duplicate(root_str, checks)
    _case_pr_no_duplicate(root_str, checks)
    _case_ci_exact_head(root_str, checks)
    _case_ci_blocks(root_str, checks, case="17", state=release_model.CI_MISSING,
                    label="missing")
    _case_ci_blocks(root_str, checks, case="18", state=release_model.CI_FAILURE,
                    label="failed")
    _case_moved_pr_head(root_str, checks)
    _case_merge_authorization(root_str, checks)
    _case_merge_no_duplicate(root_str, checks)
    _case_closure_identity(root_str, checks, happy_mission)
    _case_event_replay(root_str, checks, happy_mission)
    _case_read_only(root_str, checks, happy_mission)
    _case_control_isolation(root_str, checks)
    _case_trust_boundaries(root_str, checks)
    _case_release_gates(root_str, checks)
    _case_policy_determinism(root_str, checks)
    _case_legacy_compat(root_str, checks, happy_mission)
    _case_dashboard_matches(root_str, checks, happy_mission)
    _case_terminal_vs_readiness(root_str, checks)
    _case_semantic_identity(root_str, checks, happy_mission)
    _case_no_prose_success(root_str, checks)

    cases = tuple(
        AcceptanceCase(
            case=code, title=title,
            ok=all(check.ok for check in checks if check.case == code),
            checks=tuple(check for check in checks if check.case == code))
        for code, title in _CASES)
    return OperatorAcceptanceReport(
        version=ACCEPTANCE_VERSION, root=root_str,
        generated_at=generated_at or utc_now(), cases=cases)


__all__ = [
    "ACCEPTANCE_VERSION",
    "AcceptanceCase",
    "AcceptanceCheck",
    "OperatorAcceptanceReport",
    "OperatorPipeline",
    "run_acceptance",
]
