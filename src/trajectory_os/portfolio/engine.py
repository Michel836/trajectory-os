"""M020 — multi-goal portfolio scheduling engine (composition, no fork).

The engine composes the canonical per-goal graph/readiness/scheduler/proof
surfaces into one deterministic portfolio decision. It introduces no second
execution engine and no second per-goal store:

* planning is pure with respect to member-goal state — it only *reads* the
  canonical stores and selects which goals may advance this cycle;
* a selected goal is advanced through the existing production goal path
  (``trajectory_os.goals.runner`` -> mission orchestrator + scheduler), with
  the goal's own graph generation, mission state, scheduler decisions and
  independent proof left intact;
* every per-goal identity is recorded exactly, so goal A can never inherit
  goal B's graph, scheduler decision or completion proof.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from trajectory_os.graph import readiness
from trajectory_os.graph import store as graph_store
from trajectory_os.graph.proof import engine as proof_engine
from trajectory_os.graph.replan import store as replan_store
from trajectory_os.graph.scheduler import engine as sched_engine
from trajectory_os.graph.scheduler import model as sched_model
from trajectory_os.graph.scheduler import store as sched_store
from trajectory_os.graph.scheduler import summary as sched_summary
from trajectory_os.missions.runner import PhaseRunner
from trajectory_os.portfolio import identity as portfolio_identity
from trajectory_os.portfolio import model
from trajectory_os.portfolio import store as portfolio_store
from trajectory_os.runs.store import atomic_write_json

#: Portfolio cycle statuses (stable, machine-readable).
PS_PLANNED = "PLANNED"
PS_DISPATCHED = "DISPATCHED"
PS_NO_DISPATCH = "NO_DISPATCH"
PS_STOPPED = "STOPPED"

#: Safe-stop marker file name (under the portfolio root).
STOP_MARKER_NAME = "stop.json"


def utc_now_iso() -> str:
    return (
        datetime.datetime.now(datetime.UTC)
        .replace(microsecond=0, tzinfo=None)
        .isoformat()
        + "Z"
    )


def stop_marker_path(root: str | Path) -> Path:
    return portfolio_store.portfolio_paths(root)["root"] / STOP_MARKER_NAME


def request_stop(root: str, *, reason: str | None = None,
                 requested_at: str | None = None) -> dict[str, Any]:
    """Request a bounded portfolio safe stop (a request, never a kill)."""
    document: dict[str, Any] = {
        "reason": reason or "OPERATOR_SAFE_STOP",
        "requested_at": requested_at or utc_now_iso(),
    }
    atomic_write_json(stop_marker_path(root), document)
    return document


def clear_stop(root: str) -> None:
    path = stop_marker_path(root)
    if path.is_file():
        path.unlink()


def read_stop_request(root: str) -> dict[str, Any] | None:
    path = stop_marker_path(root)
    if not path.is_file():
        return None
    try:
        raw = path.read_bytes()[:4097]
        doc = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"requested": True, "reason": "UNREADABLE", "requested_at": None}
    if not isinstance(doc, dict):
        return {"requested": True, "reason": "MALFORMED", "requested_at": None}
    return {
        "requested": True,
        "reason": doc.get("reason"),
        "requested_at": doc.get("requested_at"),
    }


def is_stop_requested(root: str) -> bool:
    return read_stop_request(root) is not None


def portfolio_identity_for(goal_ids: Sequence[str]) -> str:
    """Stable membership identity: the sorted set of member goals."""
    return portfolio_identity.portfolio_id({
        "schema_version": model.SCHEMA_VERSION,
        "kind": "portfolio-identity",
        "goals": sorted(set(goal_ids)),
    })


def _max_priority(graph: Any) -> int:
    priorities = [int(node.priority) for node in graph.nodes]
    return max(priorities) if priorities else 0


def _reserved_totals(status: Mapping[str, Any]) -> tuple[int, int, int]:
    """Strictly read canonical reservation totals (fail closed).

    ``sched_summary.status_document`` always builds ``reservation_totals``
    from ``ReservationTotals.to_dict``, which emits every key. A missing or
    malformed key therefore signals an inconsistent authoritative scheduler
    status; defaulting it to zero would under-count live reservations and
    could over-admit new work, so such state fails closed instead.
    """
    totals = status.get("reservation_totals")
    if not isinstance(totals, Mapping):
        raise model.PortfolioError(
            model.E_MALFORMED, "reservation_totals",
            "canonical scheduler status missing reservation totals")
    values: list[int] = []
    for key in ("cpu_slots", "gpu_slots", "gpu_mem_bytes"):
        value = totals.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise model.PortfolioError(
                model.E_MALFORMED, f"reservation_totals.{key}",
                "canonical scheduler reservation total is malformed")
        values.append(value)
    return values[0], values[1], values[2]


def load_goal_view(root: str, goal_id: str) -> model.PortfolioGoalView:
    """Read the canonical, side-effect-free view of one member goal.

    The admissible work set is the scheduler's own freshly computed decision
    preview (never a readiness-state shortcut), so the portfolio selects a
    goal only when the canonical per-goal scheduler would actually admit
    dependent-ready work.
    """
    graph, _ = graph_store.load_graph(root, goal_id)
    proof = proof_engine.build_proof(root, goal_id)
    status = sched_summary.status_document(root, goal_id)
    state = sched_store.load_state(root, goal_id)
    generation_id: str | None = None
    if replan_store.replan_exists(root, goal_id):
        generation = replan_store.load_current(root, goal_id)
        generation_id = generation.generation_id if generation is not None \
            else None
    stale = state is not None and state.graph_id != graph.graph_id
    admitted: tuple[str, ...] = ()
    active_nodes: tuple[str, ...] = ()
    if not stale:
        policy = state.policy if state is not None else sched_model.DEFAULT_POLICY
        try:
            preview = sched_engine.build_decision(
                root, goal_id, policy, created_at="1970-01-01T00:00:00Z")
            admitted = tuple(node.node_id for node in preview.admitted)
            active_nodes = tuple(node.node_id for node in preview.active)
        except sched_model.SchedulerValidationError:
            admitted = ()
            active_nodes = ()
    reservations = status.get("reservations")
    reservation_list = reservations if isinstance(reservations, list) else []
    active_missions = sorted({
        str(item.get("mission_id"))
        for item in reservation_list
        if isinstance(item, dict) and item.get("mission_id")
    })
    cpu_slots, gpu_slots, gpu_mem_bytes = _reserved_totals(status)
    blocked = tuple(
        node.node_id for node in readiness.project_with_store(root, graph).nodes
        if node.state == readiness.RS_BLOCKED)
    active_count = len(reservation_list)
    stalled = (
        not proof.complete and not stale and not admitted and not active_nodes
        and active_count == 0)
    return model.PortfolioGoalView(
        goal_id=graph.goal_id,
        graph_id=graph.graph_id,
        generation_id=generation_id,
        priority=_max_priority(graph),
        complete=bool(proof.complete),
        final_state=proof.final_state,
        final_reason=proof.final_reason,
        ready_nodes=admitted,
        blocked_nodes=blocked,
        in_progress_nodes=active_nodes,
        active_reservations=active_count,
        reserved_cpu_slots=cpu_slots,
        reserved_gpu_slots=gpu_slots,
        reserved_gpu_mem_bytes=gpu_mem_bytes,
        active_missions=tuple(active_missions),
        scheduler_policy_id=(
            state.policy.policy_id if state is not None else None),
        stale_generation=stale,
        stalled=stalled,
    )


def _classify(
    view: model.PortfolioGoalView,
    dependencies: model.PortfolioDependencies,
    views: dict[str, model.PortfolioGoalView],
    policy: model.PortfolioPolicy,
    *,
    safe_stop: bool,
    active_goal_count: int,
    totals: dict[str, int],
) -> tuple[str, str]:
    """Return ``(outcome, reason)`` for one goal (deterministic order)."""
    if safe_stop:
        return model.O_EXCLUDED, model.R_SAFE_STOP
    if view.stale_generation:
        return model.O_EXCLUDED, model.R_STALE_GENERATION
    if view.complete:
        return model.O_EXCLUDED, model.R_TERMINAL_COMPLETE
    for prereq in dependencies.for_goal(view.goal_id):
        other = views.get(prereq)
        if other is None:
            return model.O_EXCLUDED, model.R_DEPENDENCY_FAILED
        if other.complete:
            continue
        if other.stalled:
            return model.O_EXCLUDED, model.R_DEPENDENCY_FAILED
        return model.O_EXCLUDED, model.R_DEPENDENCY_PENDING
    if not view.ready_nodes and view.active_reservations == 0:
        return model.O_EXCLUDED, model.R_NO_READY_WORK
    # Aggregate portfolio budgets gate *new* goal admissions only. A goal that
    # already holds live reservations is always allowed to continue: blocking
    # it could not reduce load and would starve in-flight work.
    if view.active_reservations == 0:
        if (totals["cpu_slots"] >= policy.cpu_slots
                or totals["gpu_slots"] >= policy.gpu_slots
                or totals["gpu_mem_bytes"] >= policy.gpu_mem_bytes
                or totals["reservations"] >= policy.global_concurrency):
            return model.O_EXCLUDED, model.R_BUDGET_EXHAUSTED
        if active_goal_count >= policy.max_active_goals:
            return model.O_EXCLUDED, model.R_CONCURRENCY_LIMIT
    return model.O_SELECTED, model.R_SELECTED


def plan_portfolio(root: str, goal_ids: Sequence[str],
                   policy: model.PortfolioPolicy,
                   dependencies: model.PortfolioDependencies | None = None,
                   *, created_at: str | None = None,
                   safe_stop: bool = False) -> model.PortfolioDecision:
    """Compute one deterministic portfolio decision (no persistence)."""
    policy.validate()
    goals = tuple(sorted(set(goal_ids)))
    if not goals:
        raise model.PortfolioError(model.E_MALFORMED, "goals",
                                    "portfolio must reference at least one goal")
    if len(goals) > model.MAX_GOALS:
        raise model.PortfolioError(model.E_OVERFLOW, "goals",
                                   f"more than {model.MAX_GOALS} goals")
    deps = dependencies or model.PortfolioDependencies()
    model.validate_dependencies(deps, goals)
    views = {goal_id: load_goal_view(root, goal_id) for goal_id in goals}
    ordered = sorted(views.values(), key=lambda v: (-v.priority, v.goal_id))
    active_goal_count = sum(
        1 for view in views.values() if view.active_reservations > 0)
    totals = {
        "reservations": sum(v.active_reservations for v in views.values()),
        "cpu_slots": sum(v.reserved_cpu_slots for v in views.values()),
        "gpu_slots": sum(v.reserved_gpu_slots for v in views.values()),
        "gpu_mem_bytes": sum(v.reserved_gpu_mem_bytes for v in views.values()),
    }
    entries: list[model.PortfolioEntry] = []
    for view in ordered:
        outcome, reason = _classify(
            view, deps, views, policy, safe_stop=safe_stop,
            active_goal_count=active_goal_count, totals=totals)
        if outcome == model.O_SELECTED and view.active_reservations == 0:
            active_goal_count += 1
        entries.append(model.PortfolioEntry(
            goal_id=view.goal_id,
            graph_id=view.graph_id,
            generation_id=view.generation_id,
            priority=view.priority,
            outcome=outcome,
            reason=reason,
            ready_nodes=view.ready_nodes,
            active_reservations=view.active_reservations,
        ))
    model.assert_isolation(entries)
    return model.PortfolioDecision.build(
        portfolio_id=portfolio_identity_for(goals),
        goals=goals,
        policy=policy,
        created_at=created_at or utc_now_iso(),
        entries=entries,
    )


# --- per-goal advancement -----------------------------------------------------


@dataclass(frozen=True)
class GoalStep:
    """One bounded production-path step for one selected member goal."""

    goal_id: str
    status: str
    reason: str
    complete: bool
    proof_id: str | None
    dispatched: tuple[str, ...]
    scheduler_decision_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "status": self.status,
            "reason": self.reason,
            "complete": self.complete,
            "proof_id": self.proof_id,
            "dispatched": list(self.dispatched),
            "scheduler_decision_id": self.scheduler_decision_id,
        }


GoalStepper = Callable[[str, str], GoalStep]


def default_goal_stepper(
    *,
    session_subruns: int = 1,
    dispatcher: sched_engine.Dispatcher | None = None,
    runner_factory: Callable[[], PhaseRunner] | None = None,
) -> GoalStepper:
    """Build the default one-cycle production-path step (lazy import).

    Importing :mod:`trajectory_os.goals.runner` lazily keeps the portfolio
    engine free of an import cycle with the product layer that renders it.
    """
    def step(root: str, goal_id: str) -> GoalStep:
        from trajectory_os.goals import runner as goal_runner
        config = goal_runner.GoalRunConfig(
            session_subruns=session_subruns, max_cycles=1, clear_stop=False)
        report = goal_runner.run_goal(
            root, goal_id, config=config,
            dispatcher=dispatcher,
            runner_factory=runner_factory)
        dispatched = tuple(
            str(item.get("mission_id"))
            for transition in report.transitions
            for item in transition.get("dispatched", [])
            if isinstance(item, dict) and item.get("mission_id")
        )
        scheduler_decision_id = next(
            (str(transition["decision_id"])
             for transition in reversed(report.transitions)
             if isinstance(transition.get("decision_id"), str)),
            None)
        return GoalStep(
            goal_id=goal_id,
            status=report.status,
            reason=report.reason,
            complete=report.complete,
            proof_id=report.proof_id,
            dispatched=dispatched,
            scheduler_decision_id=scheduler_decision_id,
        )

    return step


# --- cycle --------------------------------------------------------------------


@dataclass(frozen=True)
class PortfolioCycleResult:
    """One portfolio scheduling cycle outcome (plan + per-goal progress)."""

    status: str
    decision: model.PortfolioDecision
    state: model.PortfolioState
    persisted: bool
    steps: tuple[GoalStep, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "persisted": self.persisted,
            "decision": self.decision.to_dict(),
            "state": self.state.to_dict(),
            "steps": [step.to_dict() for step in self.steps],
        }


def _advance_state(
    root: str,
    decision: model.PortfolioDecision,
    *,
    created_at: str,
) -> model.PortfolioState:
    existing = portfolio_store.load_state(root)
    if existing is not None \
            and existing.portfolio_id == decision.portfolio_id:
        decision_ids = (
            existing.decision_ids
            if existing.last_decision_id == decision.decision_id
            else (*existing.decision_ids, decision.decision_id)
        )
        return replace(
            existing,
            policy=decision.policy,
            decision_ids=decision_ids,
            last_decision_id=decision.decision_id,
            updated_at=created_at,
            cycle_count=existing.cycle_count + 1,
        )
    return replace(
        model.PortfolioState.initial(
            portfolio_id=decision.portfolio_id, policy=decision.policy,
            updated_at=created_at),
        decision_ids=(decision.decision_id,),
        last_decision_id=decision.decision_id,
        cycle_count=1,
    )


def run_cycle(
    root: str,
    goal_ids: Sequence[str],
    policy: model.PortfolioPolicy,
    *,
    dependencies: model.PortfolioDependencies | None = None,
    dispatch: bool = True,
    stepper: GoalStepper | None = None,
    session_subruns: int | None = None,
    dispatcher: sched_engine.Dispatcher | None = None,
    runner_factory: Callable[[], PhaseRunner] | None = None,
    created_at: str | None = None,
) -> PortfolioCycleResult:
    """Run one bounded portfolio cycle (plan, persist, advance selected)."""
    if session_subruns is None:
        session_subruns = 1
    if not (1 <= session_subruns <= 64):
        raise model.PortfolioError(
            model.E_INVALID_POLICY, "session_subruns", "out of bounds")
    created_at = created_at or utc_now_iso()
    safe_stop = is_stop_requested(root)
    decision = plan_portfolio(
        root, goal_ids, policy, dependencies, created_at=created_at,
        safe_stop=safe_stop)
    paths = portfolio_store.portfolio_paths(root)
    persisted = portfolio_store.save_decision(paths, decision)
    portfolio_store.append_event(paths, {
        "ts": created_at,
        "event": "decision",
        "portfolio_id": decision.portfolio_id,
        "decision_id": decision.decision_id,
        "selected": [e.goal_id for e in decision.selected()],
        "excluded": [
            {"goal_id": e.goal_id, "reason": e.reason}
            for e in decision.entries if e.outcome == model.O_EXCLUDED
        ],
    })

    steps: list[GoalStep] = []
    if dispatch and not safe_stop:
        step_fn = stepper or default_goal_stepper(
            session_subruns=session_subruns, dispatcher=dispatcher,
            runner_factory=runner_factory)
        for entry in decision.selected():
            step = step_fn(root, entry.goal_id)
            steps.append(step)
            portfolio_store.append_event(paths, {
                "ts": created_at,
                "event": "advance",
                "portfolio_id": decision.portfolio_id,
                "decision_id": decision.decision_id,
                **step.to_dict(),
            })

    state = _advance_state(root, decision, created_at=created_at)
    portfolio_store.save_state(paths, state)
    if safe_stop:
        status = PS_STOPPED
    elif steps:
        status = PS_DISPATCHED
    elif dispatch:
        status = PS_NO_DISPATCH
    else:
        status = PS_PLANNED
    return PortfolioCycleResult(
        status=status, decision=decision, state=state, persisted=persisted,
        steps=tuple(steps))


def reconstruct(root: str) -> tuple[model.PortfolioState,
                                    model.PortfolioDecision | None]:
    """Strictly reconstruct the durable portfolio state and latest decision."""
    state = portfolio_store.reconstruct(root)
    latest = None
    if state.last_decision_id is not None:
        latest = portfolio_store.load_decision(
            portfolio_store.portfolio_paths(root), state.last_decision_id)
    return state, latest
