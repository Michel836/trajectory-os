"""M017 — bounded end-to-end goal execution through the production path.

The runner is a *bounded composition loop* over the existing canonical
engines. It introduces no second execution engine and no second store:

1. provision (idempotently) the canonical missions the graph references;
2. resolve the persisted cross-mission reuse projection;
3. run one portfolio scheduling cycle, dispatching admitted work through the
   production mission path (``MissionPathDispatcher`` -> ``run_mission``);
4. derive and record the goal-level proof;
5. repeat until the goal is COMPLETE, work stalls, the operator requests a
   safe stop, or the cycle bound is reached.

Safe stop is a request file only: an in-flight bounded sub-run is never
killed. The loop declines to launch new work once the current step returns.
No Git trust-boundary write is ever performed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from trajectory_os.goals import launch as goal_launch
from trajectory_os.goals import snapshot as goal_snapshot
from trajectory_os.graph import model as graph_model
from trajectory_os.graph import store as graph_store
from trajectory_os.graph.proof import store as proof_store
from trajectory_os.graph.replan import model as replan_model
from trajectory_os.graph.reuse import engine as reuse_engine
from trajectory_os.graph.reuse import model as reuse_model
from trajectory_os.graph.scheduler import engine as sched_engine
from trajectory_os.graph.scheduler import model as sched_model
from trajectory_os.missions import model as mission_model
from trajectory_os.missions import orchestrator
from trajectory_os.missions import store as mission_store
from trajectory_os.missions.runner import PhaseRunner
from trajectory_os.runs.store import atomic_write_json

#: Bounded default loop cap (each cycle dispatches at most one session per
#: admitted node). A bounded loop can never spin.
DEFAULT_MAX_CYCLES = 32

#: Runner terminal statuses (stable, machine-readable).
GS_COMPLETE = "COMPLETE"
GS_STOPPED = "STOPPED"
GS_STALLED = "STALLED"
GS_DISPATCH_ERROR = "DISPATCH_ERROR"
GS_REJECTED = "REJECTED"
GS_CYCLE_BOUND = "CYCLE_BOUND"


@dataclass
class GoalRunConfig:
    """Bounded execution configuration for one goal run."""

    policy: sched_model.SchedulerPolicy = field(
        default_factory=lambda: sched_model.DEFAULT_POLICY)
    session_subruns: int = sched_engine.DEFAULT_SESSION_SUBRUNS
    max_cycles: int = DEFAULT_MAX_CYCLES
    launch: goal_launch.MissionLaunchDefaults = field(
        default_factory=goal_launch.MissionLaunchDefaults)
    clear_stop: bool = True

    def validate(self) -> None:
        if not (1 <= self.max_cycles <= 4096):
            raise ValueError(f"max_cycles out of bounds: {self.max_cycles}")
        if not (1 <= self.session_subruns
                <= mission_model.MAX_SESSION_SUBRUNS):
            raise ValueError(
                f"session_subruns out of bounds: {self.session_subruns}")
        self.policy.validate()


@dataclass(frozen=True)
class GoalRunReport:
    """One bounded end-to-end goal run outcome (never a proof by itself)."""

    goal_id: str
    status: str
    reason: str
    cycles: int
    complete: bool
    final_state: str
    final_reason: str
    proof_id: str | None
    missions: dict[str, str]
    transitions: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "goal_id": self.goal_id,
            "cycles": self.cycles,
            "complete": self.complete,
            "final_state": self.final_state,
            "final_reason": self.final_reason,
            "proof_id": self.proof_id,
            "missions": dict(self.missions),
            "transitions": [dict(t) for t in self.transitions],
        }


def request_stop(root: str, goal_id: str, *, reason: str | None = None,
                 requested_at: str | None = None) -> dict[str, Any]:
    """Request a safe stop (request file only; never kills running work)."""
    document = {
        "goal_id": goal_id,
        "reason": reason or "OPERATOR_SAFE_STOP",
        "requested_at": requested_at or goal_snapshot.utc_now_iso(),
    }
    atomic_write_json(goal_snapshot.stop_marker_path(root, goal_id), document)
    return document


def clear_stop(root: str, goal_id: str) -> None:
    """Clear a prior safe-stop request (idempotent)."""
    path = goal_snapshot.stop_marker_path(root, goal_id)
    if path.is_file():
        path.unlink()


def is_stop_requested(root: str, goal_id: str) -> bool:
    return goal_snapshot.read_stop_request(root, goal_id) is not None


def _record_proof(root: str, goal_id: str, clock: Callable[[], str],
                  ) -> Any:
    proof, _event, _appended = proof_store.record_proof(
        root, goal_id, computed_at=clock())
    return proof


def _dispatch_summary(result: sched_engine.CycleResult) -> dict[str, Any]:
    return {
        "decision_id": result.decision.decision_id,
        "admitted": [n.node_id for n in result.decision.admitted],
        "deferred": [
            {"node_id": n.node_id, "reason": n.reason}
            for n in result.decision.deferred
        ],
        "blocked": [
            {"node_id": n.node_id, "reason": n.reason}
            for n in result.decision.blocked
        ],
        "dispatched": [
            {
                "node_id": record.node_id,
                "mission_id": record.mission_id,
                "mission_state": record.mission_state,
                "mission_reason": record.mission_reason,
                "stop": record.stop,
            }
            for record in result.dispatched
        ],
        "dispatch_errors": [dict(error) for error in result.dispatch_errors],
    }


#: Mission states that mean work has genuinely started and must be
#: continued (PLANNING has not launched anything; terminal states are done).
_CONTINUABLE_STATES = frozenset({
    mission_model.MS_RUNNING,
    mission_model.MS_VALIDATING,
    mission_model.MS_REVIEWING,
    mission_model.MS_REPAIRING,
    mission_model.MS_CONSOLIDATING,
})


def _active_mission_ids(proof: Any) -> list[str]:
    """Missions that are started but not terminal (bounded continuation)."""
    mission_ids: list[str] = []
    for node in proof.nodes:
        if node.mission_id is None:
            continue
        if node.mission_state not in _CONTINUABLE_STATES:
            continue
        mission_ids.append(node.mission_id)
    return sorted(set(mission_ids))


def _continue_active_missions(
    root: str,
    proof: Any,
    *,
    runner_factory: Callable[[], PhaseRunner] | None,
    session_subruns: int,
    clock: Callable[[], str],
) -> list[dict[str, Any]]:
    """Continue bounded in-flight missions (never re-admits completed work).

    A mission that consumed its bounded session but is not terminal is
    resumed through the same canonical ``run_mission`` call the dispatcher
    uses, so the goal loop can never spin on ``ALREADY_ACTIVE`` work.
    """
    outcomes: list[dict[str, Any]] = []
    for mission_id in _active_mission_ids(proof):
        try:
            mission_store.load_mission(root, mission_id)
        except (mission_store.MissionNotFound,
                mission_store.MalformedMissionError):
            continue
        runner = (runner_factory() if runner_factory is not None
                  else orchestrator.new_runner("process"))
        report = orchestrator.run_mission(
            root, mission_id, runner, max_session_subruns=session_subruns,
            clock=clock)
        outcomes.append({
            "mission_id": mission_id,
            "mission_state": report.mission_state,
            "mission_reason": report.mission_reason,
            "stop": report.stop,
        })
    return outcomes


def run_goal(
    root: str,
    goal_id: str,
    *,
    config: GoalRunConfig | None = None,
    dispatcher: sched_engine.Dispatcher | None = None,
    runner_factory: Callable[[], PhaseRunner] | None = None,
    clock: Callable[[], str] = goal_snapshot.utc_now_iso,
) -> GoalRunReport:
    """Drive a goal end to end through the production mission path (bounded)."""
    config = config or GoalRunConfig()
    config.validate()
    if config.clear_stop:
        clear_stop(root, goal_id)

    graph, _ = graph_store.load_graph(root, goal_id)
    missions = goal_launch.ensure_missions(
        root, graph.nodes, graph.objective, config.launch)

    if dispatcher is None:
        dispatcher = sched_engine.MissionPathDispatcher(
            runner_factory=runner_factory,
            session_subruns=config.session_subruns)

    transitions: list[dict[str, Any]] = []
    proof = _record_proof(root, goal_id, clock)
    transitions.append({
        "cycle": 0,
        "action": "observe",
        "goal_state": proof.final_state,
        "goal_reason": proof.final_reason,
        "complete": proof.complete,
        "proof_id": proof.proof_id,
    })
    if proof.complete:
        return _report(goal_id, GS_COMPLETE, "ALL_CRITERIA_PROVEN", 0, proof,
                       missions, transitions)

    cycles = 0
    for cycle in range(1, config.max_cycles + 1):
        cycles = cycle
        if is_stop_requested(root, goal_id):
            return _report(goal_id, GS_STOPPED, "SAFE_STOP_REQUESTED", cycles,
                           proof, missions, transitions)

        continued = _continue_active_missions(
            root, proof, runner_factory=runner_factory,
            session_subruns=config.session_subruns, clock=clock)
        if continued:
            proof = _record_proof(root, goal_id, clock)
            transitions.append({
                "cycle": cycle,
                "action": "continue",
                "goal_state": proof.final_state,
                "goal_reason": proof.final_reason,
                "complete": proof.complete,
                "proof_id": proof.proof_id,
                "continued": continued,
            })
            if proof.complete:
                return _report(goal_id, GS_COMPLETE, "ALL_CRITERIA_PROVEN",
                               cycles, proof, missions, transitions)
            if is_stop_requested(root, goal_id):
                return _report(goal_id, GS_STOPPED, "SAFE_STOP_REQUESTED",
                               cycles, proof, missions, transitions)

        reuse_status = "NOT_DECLARED"
        if any(node.reuse_inputs for node in graph.nodes):
            try:
                reuse = reuse_engine.resolve_and_record(
                    root, goal_id, consumed_at=clock())
                reuse_status = ("RESOLVED" if not reuse.blocked
                                else "BLOCKED")
            except (reuse_model.ReuseValidationError,
                    graph_model.GraphValidationError):
                reuse_status = "REJECTED"

        try:
            result = sched_engine.run_cycle(
                root, goal_id, config.policy, dispatch=True,
                dispatcher=dispatcher, created_at=clock())
        except (sched_model.SchedulerValidationError,
                graph_model.GraphValidationError,
                replan_model.ReplanValidationError) as exc:
            transitions.append({
                "cycle": cycle,
                "action": "rejected",
                "error": type(exc).__name__,
                "reason": getattr(exc, "code", str(exc)),
            })
            return _report(goal_id, GS_REJECTED,
                           str(getattr(exc, "code", "REJECTED")), cycles,
                           proof, missions, transitions)

        proof = _record_proof(root, goal_id, clock)
        summary = _dispatch_summary(result)
        transitions.append({
            "cycle": cycle,
            "action": "dispatch",
            "reuse": reuse_status,
            "goal_state": proof.final_state,
            "goal_reason": proof.final_reason,
            "complete": proof.complete,
            "proof_id": proof.proof_id,
            **summary,
        })

        if proof.complete:
            return _report(goal_id, GS_COMPLETE, "ALL_CRITERIA_PROVEN",
                           cycles, proof, missions, transitions)

        admitted = len(summary["admitted"])
        dispatched = len(summary["dispatched"])
        if is_stop_requested(root, goal_id):
            return _report(goal_id, GS_STOPPED, "SAFE_STOP_REQUESTED",
                           cycles, proof, missions, transitions)
        if admitted == 0 and dispatched == 0 and not continued:
            return _report(goal_id, GS_STALLED, "NO_ADMISSIBLE_WORK", cycles,
                           proof, missions, transitions)
        if admitted > 0 and dispatched == 0:
            return _report(goal_id, GS_DISPATCH_ERROR, "DISPATCH_FAILED",
                           cycles, proof, missions, transitions)

    return _report(goal_id, GS_CYCLE_BOUND, "CYCLE_BOUND", cycles, proof,
                   missions, transitions)


def _report(goal_id: str, status: str, reason: str, cycles: int,
            proof: Any, missions: dict[str, str],
            transitions: list[dict[str, Any]]) -> GoalRunReport:
    return GoalRunReport(
        goal_id=goal_id,
        status=status,
        reason=reason,
        cycles=cycles,
        complete=bool(proof.complete),
        final_state=proof.final_state,
        final_reason=proof.final_reason,
        proof_id=proof.proof_id,
        missions=dict(missions),
        transitions=tuple(transitions),
    )


def run_goal_resume(
    root: str,
    goal_id: str,
    *,
    config: GoalRunConfig | None = None,
    dispatcher: sched_engine.Dispatcher | None = None,
    runner_factory: Callable[[], PhaseRunner] | None = None,
    clock: Callable[[], str] = goal_snapshot.utc_now_iso,
) -> GoalRunReport:
    """Resume a goal: clear the safe-stop request, then run (bounded)."""
    config = config or GoalRunConfig()
    config.clear_stop = True
    return run_goal(root, goal_id, config=config, dispatcher=dispatcher,
                    runner_factory=runner_factory, clock=clock)
