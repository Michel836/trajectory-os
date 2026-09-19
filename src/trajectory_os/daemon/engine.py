"""M021 — bounded persistent daemon over the M020 portfolio engine.

The daemon is a restart-safe composition loop. It owns no authoritative work
state: every cycle is delegated to the canonical portfolio/goal path and the
daemon persists only its own bounded cycle accounting. A restarted process
reconstructs the exact prior state (cycles executed, decision identities,
last cycle) and resumes without replaying completed work.

Safe stop is a request file only. The daemon checks it between cycles; a
bounded cycle already in flight is never interrupted, so no mission lifecycle
evidence is ever lost. No module performs a Git trust-boundary write.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from trajectory_os.daemon import model
from trajectory_os.daemon import store as daemon_store
from trajectory_os.graph import model as graph_model
from trajectory_os.graph import store as graph_store
from trajectory_os.graph.scheduler import engine as sched_engine
from trajectory_os.missions.runner import PhaseRunner
from trajectory_os.portfolio import engine as portfolio_engine
from trajectory_os.portfolio import model as portfolio_model
from trajectory_os.runs.store import atomic_write_json

#: Daemon terminal reasons (stable, machine-readable).
DR_ALL_GOALS_COMPLETE = "ALL_GOALS_COMPLETE"
DR_SAFE_STOP = "SAFE_STOP_REQUESTED"
DR_CYCLE_BOUND = "CYCLE_BOUND"
DR_NO_ACTIVE_WORK = "NO_ACTIVE_WORK"
DR_ERROR = "DAEMON_ERROR"


def utc_now_iso() -> str:
    return (
        datetime.datetime.now(datetime.UTC)
        .replace(microsecond=0, tzinfo=None)
        .isoformat()
        + "Z"
    )


# --- safe stop ----------------------------------------------------------------


def request_stop(root: str, *, reason: str | None = None,
                 requested_at: str | None = None) -> dict[str, Any]:
    """Request a bounded daemon safe stop (never a mid-cycle kill)."""
    paths = daemon_store.daemon_paths(root)
    document: dict[str, Any] = {
        "reason": reason or "OPERATOR_SAFE_STOP",
        "requested_at": requested_at or utc_now_iso(),
    }
    atomic_write_json(paths["stop"], document)
    return document


def clear_stop(root: str) -> None:
    path = daemon_store.daemon_paths(root)["stop"]
    if path.is_file():
        path.unlink()


def read_stop_request(root: str) -> dict[str, Any] | None:
    path = daemon_store.daemon_paths(root)["stop"]
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


# --- session ------------------------------------------------------------------


@dataclass(frozen=True)
class DaemonReport:
    """One bounded daemon session outcome (never a completion proof itself)."""

    status: str
    reason: str
    session_cycles: int
    state: model.DaemonState
    cycles: tuple[model.DaemonCycle, ...]
    complete_goals: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "session_cycles": self.session_cycles,
            "state": self.state.to_dict(),
            "cycles": [cycle.to_dict() for cycle in self.cycles],
            "complete_goals": list(self.complete_goals),
        }


def _all_complete(root: str, goal_ids: Sequence[str]) -> tuple[bool, bool]:
    """Return ``(all_complete, quiescent)`` for the portfolio members."""
    views = [portfolio_engine.load_goal_view(root, goal_id)
             for goal_id in goal_ids]
    all_complete = bool(views) and all(view.complete for view in views)
    quiescent = bool(views) and all(
        not view.complete and not view.ready_nodes
        and not view.in_progress_nodes and view.active_reservations == 0
        for view in views)
    return all_complete, quiescent


def run_daemon(
    root: str,
    goal_ids: Sequence[str],
    *,
    config: model.DaemonConfig | None = None,
    stepper: portfolio_engine.GoalStepper | None = None,
    session_subruns: int | None = None,
    dispatcher: sched_engine.Dispatcher | None = None,
    runner_factory: Callable[[], PhaseRunner] | None = None,
    clock: Callable[[], str] = utc_now_iso,
    clear_stop_request: bool = False,
) -> DaemonReport:
    """Run one bounded, restart-safe daemon session (resume semantics)."""
    cfg = (config or model.DaemonConfig()).validate()
    goals = tuple(sorted(set(goal_ids)))
    if not goals:
        raise model.DaemonError(model.E_INVALID_CONFIG, "goals",
                                "at least one goal is required")
    portfolio_model.validate_dependencies(cfg.dependencies, goals)
    if clear_stop_request:
        clear_stop(root)
    paths = daemon_store.daemon_paths(root)
    state = daemon_store.load_state(root)
    if state is None:
        state = model.DaemonState.initial(started_at=clock())
    cycles = list(daemon_store.load_cycles(paths))
    state = replace(state, status=model.DS_RUNNING, updated_at=clock())
    daemon_store.save_state(paths, state)

    # A fully complete portfolio is idempotent: never burn a cycle re-proving
    # work that is already proven.
    already_complete, _ = _all_complete(root, goals)
    if already_complete:
        state = replace(state, status=model.DS_COMPLETE, updated_at=clock())
        daemon_store.save_state(paths, state)
        return DaemonReport(
            status=model.DS_COMPLETE, reason=DR_ALL_GOALS_COMPLETE,
            session_cycles=0, state=state, cycles=tuple(cycles),
            complete_goals=tuple(goals))

    session_cycles = 0
    reason = DR_CYCLE_BOUND
    complete_goals: tuple[str, ...] = ()
    while session_cycles < cfg.max_cycles:
        if is_stop_requested(root):
            reason = DR_SAFE_STOP
            state = replace(state, status=model.DS_STOPPED, updated_at=clock())
            daemon_store.save_state(paths, state)
            break
        session_cycles += 1
        try:
            result = portfolio_engine.run_cycle(
                root, goals, cfg.policy, dependencies=cfg.dependencies,
                dispatch=True, stepper=stepper,
                session_subruns=(session_subruns
                                 if session_subruns is not None
                                 else cfg.session_subruns),
                dispatcher=dispatcher, runner_factory=runner_factory,
                created_at=clock())
        except (portfolio_model.PortfolioError, graph_store.GraphNotFound,
                graph_model.GraphValidationError) as exc:
            reason = getattr(exc, "code", type(exc).__name__)
            state = replace(state, status=model.DS_ERROR, updated_at=clock())
            daemon_store.save_state(paths, state)
            return DaemonReport(
                status=model.DS_ERROR, reason=str(reason),
                session_cycles=session_cycles, state=state,
                cycles=tuple(cycles), complete_goals=complete_goals)
        decision = result.decision
        cycle = model.DaemonCycle(
            cycle=state.cycles_executed + 1,
            portfolio_id=decision.portfolio_id,
            decision_id=decision.decision_id,
            status=result.status,
            selected=tuple(entry.goal_id for entry in decision.selected()),
            completed_goals=tuple(sorted(
                {entry.goal_id for entry in decision.entries
                 if entry.reason == portfolio_model.R_TERMINAL_COMPLETE}
                | {step.goal_id for step in result.steps if step.complete})),
            created_at=clock(),
        )
        daemon_store.append_cycle(paths, cycle)
        cycles.append(cycle)
        decision_ids = (
            state.decision_ids
            if decision.decision_id in state.decision_ids
            else (*state.decision_ids, decision.decision_id))
        state = replace(
            state,
            status=model.DS_RUNNING,
            cycles_executed=state.cycles_executed + 1,
            portfolio_id=decision.portfolio_id,
            decision_ids=decision_ids,
            last_cycle=cycle,
            updated_at=clock(),
        )
        daemon_store.save_state(paths, state)

        all_complete, quiescent = _all_complete(root, goals)
        if all_complete:
            reason = DR_ALL_GOALS_COMPLETE
            state = replace(state, status=model.DS_COMPLETE, updated_at=clock())
            daemon_store.save_state(paths, state)
            break
        if quiescent and result.status == portfolio_engine.PS_NO_DISPATCH:
            reason = DR_NO_ACTIVE_WORK
            state = replace(state, status=model.DS_IDLE, updated_at=clock())
            daemon_store.save_state(paths, state)
            break
    if state.status == model.DS_RUNNING:
        reason = DR_CYCLE_BOUND
        state = replace(state, status=model.DS_CYCLE_BOUND, updated_at=clock())
        daemon_store.save_state(paths, state)
    complete_goals = tuple(sorted({
        goal_id for cycle in cycles for goal_id in cycle.completed_goals
    }))
    return DaemonReport(
        status=state.status,
        reason=reason,
        session_cycles=session_cycles,
        state=state,
        cycles=tuple(cycles),
        complete_goals=complete_goals,
    )


def resume_daemon(
    root: str,
    goal_ids: Sequence[str],
    *,
    config: model.DaemonConfig | None = None,
    stepper: portfolio_engine.GoalStepper | None = None,
    session_subruns: int | None = None,
    dispatcher: sched_engine.Dispatcher | None = None,
    runner_factory: Callable[[], PhaseRunner] | None = None,
    clock: Callable[[], str] = utc_now_iso,
) -> DaemonReport:
    """Clear a safe-stop request and resume the daemon (restart-safe)."""
    return run_daemon(
        root, goal_ids, config=config, stepper=stepper,
        session_subruns=session_subruns, dispatcher=dispatcher,
        runner_factory=runner_factory, clock=clock, clear_stop_request=True)


def reconstruct(root: str) -> tuple[model.DaemonState,
                                    tuple[model.DaemonCycle, ...]]:
    """Strictly reconstruct persisted daemon state (fail closed)."""
    return daemon_store.reconstruct(root)
