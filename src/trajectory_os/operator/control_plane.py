"""M041 — unified operator control plane.

One primary operator-facing surface over mission + release. It reuses the
existing assembly, observability, runtime-control and release layers and
duplicates none of their business rules:

* observation (``status`` / ``follow`` / ``pr_status`` / ``reconstruct`` /
  ``dashboard``) is strictly read-only;
* control (``pause`` / ``request-stop`` / ``cancel`` / ``resume`` /
  ``recover``) is explicit and mission-scoped;
* release Git writes (``go-commit`` / ``go-merge``) remain possible only
  through the existing explicit human authorization gates;
* one mission/release identity (``mission_id == run_id``) is preserved.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trajectory_os.assembly import control as assembly_control
from trajectory_os.assembly import model as assembly_model
from trajectory_os.assembly import orchestrator as assembly_run
from trajectory_os.assembly import store as assembly_store
from trajectory_os.benchmark.executor import FixtureExecutor, TrialExecutor
from trajectory_os.benchmark.review import ReviewerClient
from trajectory_os.observability import model as obs_model
from trajectory_os.observability import run as obs_run
from trajectory_os.operator import events as operator_events
from trajectory_os.operator import model
from trajectory_os.operator import recovery as operator_recovery
from trajectory_os.operator import state as operator_state
from trajectory_os.operator._util import utc_now
from trajectory_os.operator.policy import (
    PROFILE_RELEASE,
    ResolvedPolicy,
    resolve_policy,
)
from trajectory_os.operator.routing import (
    BackendCapability,
    RoutingDecision,
    persist_routing,
    resolve_routing,
)
from trajectory_os.release import closure as release_closure
from trajectory_os.release import handoff as release_handoff
from trajectory_os.release import merge_gate
from trajectory_os.release import model as release_model
from trajectory_os.release import pull_request as release_pr
from trajectory_os.release import store as release_store
from trajectory_os.release.authorization import authorize
from trajectory_os.release.git_adapter import GitAdapter, LocalGitAdapter
from trajectory_os.release.github_adapter import GhCliGitHubAdapter, GitHubAdapter

DEFAULT_REMOTE = "origin"

#: A deterministic fixture reviewer response used by the safe local mode.
FIXTURE_PASS_REVIEW = (
    "VERDICT: PASS\nBLOCKERS:\n- none\nMAJORS:\n- none\nMINORS:\n- none\n"
    "FINAL RECOMMENDATION: GO COMMIT\n")


@dataclass(frozen=True)
class StartOutcome:
    """The bounded result of starting one policy-driven mission."""

    mission_id: str
    policy: Mapping[str, Any]
    routing: Mapping[str, Any]
    result: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "policy": dict(self.policy),
            "routing": dict(self.routing),
            "result": dict(self.result),
        }


def trust_policy_for(resolved: ResolvedPolicy) -> assembly_model.TrustPolicy:
    """Derive the mission trust policy from a resolved policy (no drift)."""
    policy = resolved.policy
    return assembly_model.TrustPolicy(
        require_review=policy.final_review_required,
        final_reviewer_model=policy.final_reviewer_model,
        inline_review_enabled=policy.inline_review_enabled,
        max_repairs=policy.repair_attempt_limit,
        stop_at=obs_model.RD_READY_FOR_COMMIT,
        allow_git_trust_writes=False,
    ).validate()


def apply_policy_to_request(
    request: assembly_run.MissionRequest,
    resolved: ResolvedPolicy,
    routing: RoutingDecision,
) -> assembly_run.MissionRequest:
    """Return a mission request whose execution route follows the decision."""
    implementation = routing.implementation
    return dataclasses.replace(
        request,
        trust_policy=trust_policy_for(resolved),
        backend=implementation.backend or request.backend,
        provider=implementation.provider,
        model=implementation.model,
        telemetry_mode=resolved.policy.telemetry_level,
    )


def build_fixture_executor() -> FixtureExecutor:
    """A deterministic, safe local executor for fixture/dogfood proof only."""
    return FixtureExecutor(interrupt_once=False)


def build_fixture_reviewer_factory() -> Callable[[], ReviewerClient]:
    return obs_run.scripted_reviewer_factory([FIXTURE_PASS_REVIEW])


class ControlPlane:
    """One primary operator control surface over mission + release."""

    def __init__(
        self,
        root: str | Path,
        *,
        repo: str | Path | None = None,
        git: GitAdapter | None = None,
        github: GitHubAdapter | None = None,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self._root = str(Path(root))
        Path(self._root).mkdir(parents=True, exist_ok=True)
        self._repo = str(repo) if repo is not None else None
        self._git = git
        self._github = github
        self._clock = clock

    @property
    def root(self) -> str:
        return self._root

    @property
    def repo(self) -> str | None:
        return self._repo

    @property
    def clock(self) -> Callable[[], str]:
        return self._clock

    # -- resolvers -------------------------------------------------------------

    def resolve_policy(self, profile: str = PROFILE_RELEASE, *,
                       overrides: Mapping[str, object] | None = None,
                       ) -> ResolvedPolicy:
        return resolve_policy(profile, overrides=overrides,
                              clock=self._clock)

    def resolve_routing(
        self, mission_id: str, resolved: ResolvedPolicy, *,
        capabilities: Mapping[str, BackendCapability] | None = None,
    ) -> RoutingDecision:
        return resolve_routing(mission_id, resolved, capabilities=capabilities,
                               clock=self._clock)

    def persist_policy(self, mission_id: str, resolved: ResolvedPolicy) -> None:
        mission_root = assembly_store.mission_root(self._root, mission_id)
        from trajectory_os.operator._util import write_json

        write_json(Path(mission_root) / model.POLICY_NAME, resolved.to_dict())

    def persist_routing(self, mission_id: str,
                        routing: RoutingDecision) -> None:
        persist_routing(self._root, mission_id, routing)

    # -- start / lifecycle -----------------------------------------------------

    def start(
        self,
        request: assembly_run.MissionRequest,
        *,
        profile: str = PROFILE_RELEASE,
        overrides: Mapping[str, object] | None = None,
        capabilities: Mapping[str, BackendCapability] | None = None,
        executor: TrialExecutor | None = None,
        reviewer_factory: Callable[[], ReviewerClient] | None = None,
        interrupt_after_phase: str | None = None,
    ) -> StartOutcome:
        resolved = self.resolve_policy(profile, overrides=overrides)
        mission_id = request.mission_id or assembly_model.generate_mission_id(
            request.objective, self._clock())
        assembly_store.ensure_mission_root(self._root, mission_id)
        self.persist_policy(mission_id, resolved)
        routing = self.resolve_routing(mission_id, resolved,
                                       capabilities=capabilities)
        self.persist_routing(mission_id, routing)
        self._record(
            mission_id, type=operator_events.T_POLICY_RESOLVED,
            source=operator_events.S_POLICY, phase="POLICY",
            payload={"policy_id": resolved.policy_id, "profile": profile})
        self._record(
            mission_id, type=operator_events.T_ROUTING_DECIDED,
            source=operator_events.S_ROUTING, phase="ROUTING",
            payload=routing.to_dict())
        mission_request = apply_policy_to_request(
            dataclasses.replace(request, mission_id=mission_id), resolved,
            routing)
        orchestrator = assembly_run.MissionOrchestrator(
            self._root, executor=executor or build_fixture_executor(),
            reviewer_factory=(reviewer_factory
                              or build_fixture_reviewer_factory()),
            clock=self._clock)
        result = orchestrator.start(
            mission_request, interrupt_after_phase=interrupt_after_phase)
        return StartOutcome(
            mission_id=mission_id, policy=resolved.to_dict(),
            routing=routing.to_dict(), result=result.to_dict())

    def resume(
        self, mission_id: str, *,
        executor: TrialExecutor | None = None,
        reviewer_factory: Callable[[], ReviewerClient] | None = None,
        interrupt_after_phase: str | None = None,
    ) -> dict[str, Any]:
        orchestrator = assembly_run.MissionOrchestrator(
            self._root, executor=executor or build_fixture_executor(),
            reviewer_factory=(reviewer_factory
                              or build_fixture_reviewer_factory()),
            clock=self._clock)
        result = orchestrator.resume(
            mission_id, interrupt_after_phase=interrupt_after_phase)
        self._record(
            mission_id, type=operator_events.T_CONTROL,
            source=operator_events.S_CONTROL, phase="RESUME",
            payload={"action": "RESUME", "interrupted": result.interrupted})
        return result.to_dict()

    def pause(self, mission_id: str, *,
              reason: str = "operator paused mission") -> dict[str, Any]:
        outcome = assembly_control.pause(
            self._root, mission_id, reason=reason, clock=self._clock)
        self._record(mission_id, type=operator_events.T_CONTROL,
                     source=operator_events.S_CONTROL, phase="PAUSE",
                     payload=outcome.to_dict())
        return outcome.to_dict()

    def request_stop(self, mission_id: str, *,
                     reason: str = "operator requested stop",
                     ) -> dict[str, Any]:
        outcome = assembly_control.request_stop(
            self._root, mission_id, reason=reason, clock=self._clock)
        self._record(mission_id, type=operator_events.T_CONTROL,
                     source=operator_events.S_CONTROL, phase="REQUEST_STOP",
                     payload=outcome.to_dict())
        return outcome.to_dict()

    def cancel(self, mission_id: str, *,
               reason: str = "operator cancelled mission") -> dict[str, Any]:
        outcome = assembly_control.cancel(
            self._root, mission_id, reason=reason, clock=self._clock)
        self._record(mission_id, type=operator_events.T_CONTROL,
                     source=operator_events.S_CONTROL, phase="CANCEL",
                     payload=outcome.to_dict())
        return outcome.to_dict()

    def recover(self, mission_id: str, *, record: bool = True,
                ) -> dict[str, Any]:
        decision = operator_recovery.decide_recovery(
            self._root, mission_id, clock=self._clock)
        if record:
            operator_recovery.record_recovery(self._root, decision)
            self._record(
                mission_id, type=operator_events.T_RECOVERY_DECIDED,
                source=operator_events.S_RECOVERY, phase=decision.stage,
                payload=decision.to_dict())
        return decision.to_dict()

    # -- observation (read-only) ----------------------------------------------

    def status(self, mission_id: str) -> dict[str, Any]:
        return self._state(mission_id).to_dict()

    def dashboard(self, mission_id: str) -> dict[str, Any]:
        state = self._state(mission_id)
        return {"state": state.to_dict(), "screen": operator_state.render_text(
            state)}

    def follow(self, mission_id: str, *, iterations: int = 1,
               interval_s: float = 0.0) -> list[dict[str, Any]]:
        """Read-only repeated status (never mutates product state)."""
        snapshots: list[dict[str, Any]] = []
        for index in range(max(1, iterations)):
            if index > 0 and interval_s > 0:
                time.sleep(interval_s)
            snapshots.append(self.status(mission_id))
        return snapshots

    def pr_status(self, mission_id: str) -> dict[str, Any]:
        mission_root = assembly_store.mission_root(self._root, mission_id)
        if not release_store.exists(mission_root,
                                    release_store.PR_BINDING_NAME):
            return {"mission_id": mission_id, "pr": None,
                    "reason": "NO_PULL_REQUEST_BINDING"}
        binding = release_store.load_pr_binding(mission_root)
        return {"mission_id": mission_id, "pr": binding.to_dict()}

    def reconstruct(self, mission_id: str) -> dict[str, Any]:
        replay = operator_events.replay(self._root, mission_id)
        release: dict[str, Any] | None = None
        try:
            release = release_closure.reconstruct_release(self._root,
                                                          mission_id)
        except (release_model.ReleaseError, assembly_model.AssemblyError):
            release = None
        return {
            "mission_id": mission_id,
            "operator_replay": replay,
            "release": release,
        }

    # -- release handoffs (explicit mutations) --------------------------------

    def handoff(self, mission_id: str, *, base_branch: str = "main",
                issue: str | None = None) -> dict[str, Any]:
        handoff = release_handoff.build_commit_handoff(
            self._root, mission_id, git=self.git_adapter(),
            base_branch=base_branch, issue=issue, clock=self._clock)
        self._record(
            mission_id, type=operator_events.T_COMMIT_HANDOFF,
            source=operator_events.S_RELEASE, phase="COMMIT_HANDOFF",
            payload=handoff.to_dict(), release_id=mission_id)
        return handoff.to_dict()

    def go_commit(self, mission_id: str, *, token: str | None,
                  actor: str = "operator",
                  remote: str = DEFAULT_REMOTE) -> dict[str, Any]:
        authorization = authorize(release_model.GATE_GO_COMMIT, token,
                                  actor=actor, clock=self._clock)
        result = release_handoff.go_commit(
            self._root, mission_id, git=self.git_adapter(),
            authorization=authorization, remote=remote, clock=self._clock)
        self._record(
            mission_id, type=operator_events.T_GO_COMMIT,
            source=operator_events.S_RELEASE, phase="GO_COMMIT",
            payload=result.to_dict(), release_id=mission_id)
        return result.to_dict()

    def bind_pr(self, mission_id: str, *, base_branch: str | None = None,
                title: str | None = None, body: str | None = None,
                ) -> dict[str, Any]:
        binding = release_pr.bind_pull_request(
            self._root, mission_id, git=self.git_adapter(),
            github=self.github_adapter(), base_branch=base_branch,
            title=title, body=body, clock=self._clock)
        self._record(
            mission_id, type=operator_events.T_PR_BOUND,
            source=operator_events.S_RELEASE, phase="PR_BIND",
            payload=binding.to_dict(), release_id=mission_id)
        return binding.to_dict()

    def watch_ci(self, mission_id: str, *, persist: bool = False,
                 ) -> dict[str, Any]:
        status = release_pr.watch_ci(
            self._root, mission_id, github=self.github_adapter(),
            persist=persist, clock=self._clock)
        if persist:
            self._record(
                mission_id, type=operator_events.T_CI_OBSERVED,
                source=operator_events.S_RELEASE, phase="CI_WATCH",
                payload=status.to_dict(), release_id=mission_id)
        return status.to_dict()

    def merge_handoff(self, mission_id: str, *,
                      base_branch: str | None = None,
                      merge_method: str = release_model.DEFAULT_MERGE_METHOD,
                      ) -> dict[str, Any]:
        handoff = merge_gate.build_merge_handoff(
            self._root, mission_id, github=self.github_adapter(),
            base_branch=base_branch, merge_method=merge_method,
            clock=self._clock)
        self._record(
            mission_id, type=operator_events.T_MERGE_HANDOFF,
            source=operator_events.S_RELEASE, phase="MERGE_HANDOFF",
            payload=handoff.to_dict(), release_id=mission_id)
        return handoff.to_dict()

    def go_merge(self, mission_id: str, *, token: str | None,
                 actor: str = "operator",
                 merge_method: str | None = None) -> dict[str, Any]:
        authorization = authorize(release_model.GATE_GO_MERGE, token,
                                  actor=actor, clock=self._clock)
        result = merge_gate.go_merge(
            self._root, mission_id, github=self.github_adapter(),
            authorization=authorization, merge_method=merge_method,
            clock=self._clock)
        self._record(
            mission_id, type=operator_events.T_GO_MERGE,
            source=operator_events.S_RELEASE, phase="GO_MERGE",
            payload=result.to_dict(), release_id=mission_id)
        return result.to_dict()

    def closure(self, mission_id: str, *, base_branch: str | None = None,
                issue: str | None = None) -> dict[str, Any]:
        closure = release_closure.build_release_closure(
            self._root, mission_id, github=self.github_adapter(),
            base_branch=base_branch, issue=issue, clock=self._clock)
        self._record(
            mission_id, type=operator_events.T_RELEASE_CLOSURE,
            source=operator_events.S_RELEASE, phase="RELEASE_CLOSURE",
            payload=closure.to_dict(), release_id=mission_id)
        return closure.to_dict()

    # -- adapters --------------------------------------------------------------

    def git_adapter(self) -> GitAdapter:
        if self._git is not None:
            return self._git
        return LocalGitAdapter(repo=self._repo or str(Path.cwd()))

    def github_adapter(self) -> GitHubAdapter:
        if self._github is not None:
            return self._github
        return GhCliGitHubAdapter(repo=self._repo or str(Path.cwd()))

    # -- internals -------------------------------------------------------------

    def _state(self, mission_id: str) -> operator_state.OperatorState:
        git_state: Mapping[str, Any] | None = None
        try:
            snapshot = self.git_adapter().read_state()
            if snapshot.available:
                git_state = snapshot.to_dict()
        except (release_model.ReleaseError, OSError):
            git_state = None
        return operator_state.build_operator_state(
            self._root, mission_id, git_state=git_state)

    def _record(self, mission_id: str, *, type: str, source: str, phase: str,
                payload: Mapping[str, Any],
                release_id: str | None = None) -> None:
        operator_events.record_event(
            self._root, mission_id=mission_id, type=type, source=source,
            phase=phase, payload=payload, release_id=release_id,
            actor="operator", clock=self._clock)


__all__ = [
    "DEFAULT_REMOTE",
    "FIXTURE_PASS_REVIEW",
    "ControlPlane",
    "StartOutcome",
    "apply_policy_to_request",
    "build_fixture_executor",
    "build_fixture_reviewer_factory",
    "trust_policy_for",
]
