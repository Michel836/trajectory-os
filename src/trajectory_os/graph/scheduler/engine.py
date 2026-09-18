"""Mission 013 — portfolio scheduling cycle engine (composition, no new engine).

This module composes the pure arbiter with the durable scheduler store and,
when explicitly requested, with the **existing production mission path**
(``trajectory_os.missions.orchestrator``). It introduces no second execution
engine: a dispatch is one canonical ``run_mission`` call on the node's own
referenced mission, and a successful scheduler/subprocess result never proves
mission completion — only authoritative mission evidence does.

The cycle is deterministic: the same graph, mission evidence, policy and
resource snapshot produce the same decision identity. Dispatch attempts are
recorded append-only in the scheduler state and events, and an existing
dispatch is never silently replayed.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from trajectory_os.graph import model as graph_model
from trajectory_os.graph import readiness
from trajectory_os.graph import store as graph_store
from trajectory_os.graph.scheduler import arbiter, model
from trajectory_os.graph.scheduler import evidence as sched_evidence
from trajectory_os.graph.scheduler import identity as sched_identity
from trajectory_os.graph.scheduler import store as sched_store
from trajectory_os.missions import model as mission_model
from trajectory_os.missions import orchestrator
from trajectory_os.missions.runner import PhaseRunner

#: Bounded default number of sub-runs one dispatched mission session may run.
DEFAULT_SESSION_SUBRUNS = mission_model.MAX_SESSION_SUBRUNS


@dataclass(frozen=True)
class DispatchOutcome:
    """One canonical mission-path dispatch result (never a completion proof)."""

    mission_id: str
    dispatch_ref: str | None
    stop: str
    mission_state: str
    mission_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "dispatch_ref": self.dispatch_ref,
            "stop": self.stop,
            "mission_state": self.mission_state,
            "mission_reason": self.mission_reason,
        }


class Dispatcher(Protocol):
    """Composition seam over the existing mission production path."""

    def dispatch(self, *, root: str, goal_id: str,
                 node: graph_model.GraphNode,
                 decision: model.ScheduleDecision) -> DispatchOutcome:
        ...  # pragma: no cover


class MissionPathDispatcher:
    """Dispatch through the canonical mission orchestrator (no new engine)."""

    def __init__(
        self,
        *,
        runner_factory: Callable[[], PhaseRunner] | None = None,
        session_subruns: int = DEFAULT_SESSION_SUBRUNS,
    ) -> None:
        if not (1 <= session_subruns <= mission_model.MAX_SESSION_SUBRUNS):
            raise ValueError(f"session_subruns out of bounds: {session_subruns}")
        self._runner_factory = runner_factory
        self._session_subruns = session_subruns

    def dispatch(self, *, root: str, goal_id: str,
                 node: graph_model.GraphNode,
                 decision: model.ScheduleDecision) -> DispatchOutcome:
        if node.mission_ref is None:  # pragma: no cover - guarded by arbiter
            raise ValueError(f"node {node.node_id!r} has no mission reference")
        mission_id = node.mission_ref.mission_id
        runner = (self._runner_factory() if self._runner_factory is not None
                  else orchestrator.new_runner("process"))
        report = orchestrator.run_mission(
            root, mission_id, runner,
            max_session_subruns=self._session_subruns)
        return DispatchOutcome(
            mission_id=mission_id,
            dispatch_ref=f"{goal_id}/{node.node_id}/{mission_id}/{report.stop}",
            stop=report.stop,
            mission_state=report.mission_state,
            mission_reason=report.mission_reason,
        )


@dataclass
class CycleResult:
    """One deterministic scheduling cycle outcome."""

    status: str
    decision: model.ScheduleDecision
    state: model.SchedulerState
    persisted: bool
    dispatched: tuple[model.DispatchRecord, ...] = ()
    dispatch_errors: tuple[dict[str, str], ...] = ()

    @property
    def released(self) -> tuple[model.ReleaseRecord, ...]:
        return self.decision.reservations.released

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "persisted": self.persisted,
            "goal_id": self.decision.goal_id,
            "graph_id": self.decision.graph_id,
            "decision_id": self.decision.decision_id,
            "input_projection_id": self.decision.input_projection_id,
            "counts": self.decision.counts(),
            "candidate_order": list(self.decision.candidate_order),
            "admitted": [n.node_id for n in self.decision.admitted],
            "deferred": [
                {"node_id": n.node_id, "reason": n.reason}
                for n in self.decision.deferred
            ],
            "blocked": [
                {"node_id": n.node_id, "reason": n.reason}
                for n in self.decision.blocked
            ],
            "released": [r.to_dict() for r in self.released],
            "dispatched": [r.to_dict() for r in self.dispatched],
            "dispatch_errors": list(self.dispatch_errors),
            "reservations": self.decision.reservations.to_dict(),
            "state": self.state.to_dict(),
        }


@dataclass(frozen=True)
class CycleInputs:
    graph: graph_model.GoalGraph
    projection: readiness.ReadinessProjection
    runtime: dict[str, sched_evidence.MissionRuntimeEvidence]
    state: model.SchedulerState | None


def load_inputs(root: str, goal_id: str,
                policy: model.SchedulerPolicy) -> CycleInputs:
    """Load and validate all deterministic scheduling inputs (read-only)."""
    graph, _ = graph_store.load_graph(root, goal_id)
    projection = readiness.project_with_store(root, graph)
    mission_ids = [
        node.mission_ref.mission_id
        for node in graph.nodes if node.mission_ref is not None
    ]
    runtime = sched_evidence.collect_runtime(root, mission_ids)
    state = sched_store.load_state(root, goal_id)
    if state is not None:
        if state.goal_id != graph.goal_id:
            raise model.SchedulerValidationError(
                model.E_GRAPH_MISMATCH, "state",
                f"goal {state.goal_id} != {graph.goal_id}")
        if state.graph_id != graph.graph_id:
            raise model.SchedulerValidationError(
                model.E_GRAPH_MISMATCH, "state",
                f"graph {state.graph_id} != {graph.graph_id}")
    policy.validate()  # explicit capacity is validated before any planning
    return CycleInputs(graph=graph, projection=projection, runtime=runtime,
                       state=state)


def build_decision(root: str, goal_id: str, policy: model.SchedulerPolicy,
                   *, created_at: str,
                   inputs: CycleInputs | None = None) -> model.ScheduleDecision:
    """Compute one deterministic decision without persisting or dispatching."""
    inputs = inputs or load_inputs(root, goal_id, policy)
    state = inputs.state
    dispatch_records = state.dispatch_records if state is not None else ()
    counters = state.counter_map if state is not None else {}
    prior_active = state.active_node_ids if state is not None else ()
    return arbiter.plan(
        inputs.graph,
        inputs.projection,
        policy,
        inputs.runtime,
        dispatch_records,
        counters,
        created_at=created_at,
        prior_active_node_ids=prior_active,
    )


def run_cycle(
    root: str,
    goal_id: str,
    policy: model.SchedulerPolicy,
    *,
    dispatch: bool = False,
    dispatcher: Dispatcher | None = None,
    created_at: str,
    expected_decision_id: str | None = None,
) -> CycleResult:
    """Run one bounded scheduling cycle (optionally dispatching admitted work).

    When ``expected_decision_id`` is supplied, the freshly recomputed decision
    must match it; any mismatch fails closed with ``STALE_DECISION_INPUT`` and
    nothing is dispatched (a stale decision is never replayed).
    """
    inputs = load_inputs(root, goal_id, policy)
    state = inputs.state
    if state is None:
        state = model.SchedulerState.initial(
            goal_id=inputs.graph.goal_id, graph_id=inputs.graph.graph_id,
            policy=policy, updated_at=created_at)
    decision = build_decision(root, goal_id, policy, created_at=created_at,
                              inputs=inputs)
    if expected_decision_id is not None \
            and decision.decision_id != expected_decision_id:
        raise model.SchedulerValidationError(
            model.E_IDENTITY_MISMATCH, "decision",
            f"{model.R_STALE_DECISION_INPUT}: {decision.decision_id} != "
            f"{expected_decision_id}")

    paths = sched_store.scheduler_paths(root, goal_id)
    persisted = sched_store.save_decision(paths, decision)
    sched_store.append_event(paths, {
        "ts": created_at,
        "event": "decision",
        "goal_id": decision.goal_id,
        "graph_id": decision.graph_id,
        "decision_id": decision.decision_id,
        "input_projection_id": decision.input_projection_id,
        "admitted": [n.node_id for n in decision.admitted],
        "deferred": len(decision.deferred),
        "blocked": len(decision.blocked),
    })

    dispatched: list[model.DispatchRecord] = []
    errors: list[dict[str, str]] = []
    records = list(state.dispatch_records)
    counters = dict(state.counter_map)
    if dispatch:
        dispatcher = dispatcher or MissionPathDispatcher()
        node_map = inputs.graph.node_map()
        admitted_ids = {n.node_id for n in decision.admitted}
        for node_id in decision.candidate_order:
            if node_id not in admitted_ids:
                continue
            node = node_map[node_id]
            mission_id = (node.mission_ref.mission_id
                          if node.mission_ref is not None else "")
            attempt = counters.get(node_id, 0) + 1
            counters[node_id] = attempt
            dispatch_id = sched_identity.dispatch_id({
                "goal_id": decision.goal_id,
                "graph_id": decision.graph_id,
                "node_id": node_id,
                "mission_id": mission_id,
                "decision_id": decision.decision_id,
                "attempt": attempt,
            })
            try:
                outcome = dispatcher.dispatch(
                    root=root, goal_id=goal_id, node=node,
                    decision=decision)
            except Exception as exc:
                error = {
                    "node_id": node_id,
                    "mission_id": mission_id,
                    "dispatch_id": dispatch_id,
                    "error": type(exc).__name__,
                }
                errors.append(error)
                sched_store.append_event(paths, {
                    "ts": created_at, "event": "dispatch_failed", **error})
                continue
            record = model.DispatchRecord(
                node_id=node_id,
                mission_id=outcome.mission_id,
                decision_id=decision.decision_id,
                dispatch_id=dispatch_id,
                dispatch_ref=outcome.dispatch_ref,
                stop=outcome.stop,
                mission_state=outcome.mission_state,
                mission_reason=outcome.mission_reason,
                dispatched_at=created_at,
            )
            records.append(record)
            dispatched.append(record)
            sched_store.append_event(paths, {
                "ts": created_at, "event": "dispatch", **record.to_dict()})

    runtime_after = sched_evidence.collect_runtime(
        root, [node.mission_ref.mission_id for node in inputs.graph.nodes
               if node.mission_ref is not None])
    active_ids = sorted(
        r.node_id for r in arbiter.derive_reservations(
            inputs.graph, runtime_after, records)
    )
    new_state = model.SchedulerState(
        schema_version=model.SCHEMA_VERSION,
        goal_id=state.goal_id,
        graph_id=state.graph_id,
        policy=policy,
        dispatch_records=tuple(records),
        dispatch_counters=tuple(sorted(counters.items())),
        active_node_ids=tuple(active_ids),
        last_decision_id=decision.decision_id,
        decision_ids=(
            state.decision_ids
            if state.last_decision_id == decision.decision_id
            else (*state.decision_ids, decision.decision_id)
        ),
        updated_at=created_at,
    )
    sched_store.save_state(paths, new_state)
    status = ("DISPATCHED" if dispatched
              else ("PLANNED" if not dispatch else "NO_DISPATCH"))
    return CycleResult(
        status=status,
        decision=decision,
        state=new_state,
        persisted=persisted,
        dispatched=tuple(dispatched),
        dispatch_errors=tuple(errors),
    )


@dataclass
class ReconstructionRead:
    """Read-only reconstruction of scheduler state plus derived reservations."""

    reconstructed: model.ReconstructedScheduler
    reservations: tuple[model.Reservation, ...]
    runtime: dict[str, sched_evidence.MissionRuntimeEvidence]

    @property
    def state(self) -> model.SchedulerState:
        return self.reconstructed.state

    @property
    def latest_decision(self) -> model.ScheduleDecision | None:
        return self.reconstructed.latest_decision

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.reconstructed.to_dict(),
            "reservations": [r.to_dict() for r in self.reservations],
            "runtime": {mid: rec.to_dict()
                        for mid, rec in sorted(self.runtime.items())},
        }


def reconstruct(root: str, goal_id: str) -> ReconstructionRead:
    """Strictly reconstruct scheduler state and validate live accounting."""
    graph, _ = graph_store.load_graph(root, goal_id)
    reconstructed = sched_store.reconstruct(root, goal_id, graph)
    mission_ids = [
        node.mission_ref.mission_id
        for node in graph.nodes if node.mission_ref is not None
    ]
    runtime = sched_evidence.collect_runtime(root, mission_ids)
    reservations = arbiter.derive_reservations(
        graph, runtime, reconstructed.state.dispatch_records)
    totals = model.ReservationTotals.of(reservations)
    policy = reconstructed.state.policy
    if totals.gpu_slots > policy.gpu_slots:
        raise model.SchedulerValidationError(
            model.E_IMPOSSIBLE_ACCOUNTING, "reservations", "gpu_slots")
    if totals.gpu_mem_bytes > policy.gpu_mem_bytes:
        raise model.SchedulerValidationError(
            model.E_IMPOSSIBLE_ACCOUNTING, "reservations", "gpu_mem_bytes")
    if totals.count > policy.global_concurrency:
        raise model.SchedulerValidationError(
            model.E_IMPOSSIBLE_ACCOUNTING, "reservations",
            "global_concurrency")
    return ReconstructionRead(reconstructed=reconstructed,
                              reservations=reservations, runtime=runtime)


def latest_decision(root: str, goal_id: str) -> model.ScheduleDecision | None:
    """Read the latest persisted decision (or None) without reconstruction."""
    state = sched_store.load_state(root, goal_id)
    if state is None or state.last_decision_id is None:
        return None
    return sched_store.load_decision(
        sched_store.scheduler_paths(root, goal_id), state.last_decision_id)


def decision_history(root: str, goal_id: str) -> Sequence[model.ScheduleDecision]:
    state = sched_store.load_state(root, goal_id)
    if state is None:
        return ()
    paths = sched_store.scheduler_paths(root, goal_id)
    return tuple(sched_store.load_decision(paths, decision_id)
                 for decision_id in state.decision_ids)
