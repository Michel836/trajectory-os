"""Mission 013 — deterministic portfolio scheduling and resource arbitration.

Pure planning: given one M012 goal graph, its authoritative dependency
readiness projection, read-only mission runtime evidence, an explicit
capacity policy and the prior scheduler state, :func:`plan` produces exactly
one :class:`~trajectory_os.graph.scheduler.model.ScheduleDecision`. It
performs no I/O, reads no clock and mutates nothing — the same inputs always
produce the same decision identity.

Deterministic candidate order (ADR-011): ``(priority DESC, node_id ASC)``.
Only nodes whose M012 dependency proof is complete (every dependency
``proven``) and whose own mission is not terminal are candidates; dependency
readiness is authoritative and is never silently promoted.

Deterministic admission precedence (one stable reason per node):
``INVALID_RESOURCE_SPEC`` > ``BUDGET_EXHAUSTED`` > ``EXCLUSIVE_CONFLICT`` >
``CONCURRENCY_LIMIT`` > ``CPU_CAPACITY`` > ``GPU_CAPACITY`` > ``GPU_MEMORY``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from trajectory_os.graph import model as graph_model
from trajectory_os.graph import readiness
from trajectory_os.graph.scheduler import evidence as sched_evidence
from trajectory_os.graph.scheduler import identity as sched_identity
from trajectory_os.graph.scheduler import model


def dependency_ready(status: readiness.NodeReadiness) -> bool:
    """M012-authoritative dependency eligibility (all upstream proven)."""
    return all(dep.proven for dep in status.dependencies)


def derive_reservations(
    graph: graph_model.GoalGraph,
    runtime: Mapping[str, sched_evidence.MissionRuntimeEvidence],
    dispatch_records: Sequence[model.DispatchRecord],
) -> tuple[model.Reservation, ...]:
    """Current reservations = started, non-terminal missions (fail closed)."""
    owned_missions = {record.mission_id for record in dispatch_records}
    reservations: list[model.Reservation] = []
    for node in graph.nodes:
        if node.mission_ref is None:
            continue
        mission_id = node.mission_ref.mission_id
        record = runtime.get(mission_id)
        if record is None or not record.active:
            continue
        demand = model.derive_demand(node)
        reservations.append(model.Reservation(
            node_id=node.node_id,
            mission_id=mission_id,
            execution=demand.execution,
            cpu_slots=demand.cpu_slots,
            gpu_slots=demand.gpu_slots,
            gpu_mem_bytes=demand.gpu_mem_bytes,
            exclusive=demand.exclusive,
            owned=mission_id in owned_missions,
            decision_id=None,
        ))
    return tuple(sorted(reservations, key=lambda r: r.node_id))


def input_projection_payload(
    graph: graph_model.GoalGraph,
    projection: readiness.ReadinessProjection,
    policy: model.SchedulerPolicy,
    runtime: Mapping[str, sched_evidence.MissionRuntimeEvidence],
    reservations: Sequence[model.Reservation],
    dispatch_records: Sequence[model.DispatchRecord],
) -> dict[str, object]:
    """Canonical, bounded identity payload for the scheduling inputs."""
    statuses = projection.by_id()
    node_map = graph.node_map()
    nodes: list[dict[str, object]] = []
    for node_id in projection.topological_order:
        node = node_map[node_id]
        status = statuses[node_id]
        mission_id = (node.mission_ref.mission_id
                      if node.mission_ref is not None else None)
        runtime_record = runtime.get(mission_id) if mission_id else None
        nodes.append({
            "node_id": node_id,
            "priority": node.priority,
            "m012_state": status.state,
            "m012_eligible": status.eligible,
            "m012_reason": status.reason,
            "own_status": status.own_status,
            "depends_on": [
                {"node_id": dep.node_id, "state": dep.state,
                 "proven": dep.proven}
                for dep in status.dependencies
            ],
            "mission": (None if runtime_record is None
                        else runtime_record.to_dict()),
        })
    return {
        "goal_id": graph.goal_id,
        "graph_id": graph.graph_id,
        "spec_sha256": graph.spec_sha256,
        "policy": policy.to_dict(),
        "nodes": nodes,
        "reservations": [r.to_dict() for r in reservations],
        "dispatch_records": [r.to_dict() for r in dispatch_records],
    }


def compute_input_projection_id(
    graph: graph_model.GoalGraph,
    projection: readiness.ReadinessProjection,
    policy: model.SchedulerPolicy,
    runtime: Mapping[str, sched_evidence.MissionRuntimeEvidence],
    reservations: Sequence[model.Reservation],
    dispatch_records: Sequence[model.DispatchRecord],
) -> str:
    return sched_identity.projection_id(input_projection_payload(
        graph, projection, policy, runtime, reservations, dispatch_records))


# --- classification -----------------------------------------------------------


def _node_decision(status: readiness.NodeReadiness, node: graph_model.GraphNode,
                   demand: model.NodeDemand, outcome: str,
                   reason: str) -> model.NodeDecision:
    return model.NodeDecision(
        node_id=node.node_id,
        priority=node.priority,
        m012_state=status.state,
        outcome=outcome,
        reason=reason,
        demand=demand,
    )


def _budget_exhausted(
    demand: model.NodeDemand,
    record: sched_evidence.MissionRuntimeEvidence,
    counters: Mapping[str, int],
) -> bool:
    if demand.max_attempts is not None \
            and counters.get(demand.node_id, 0) >= demand.max_attempts:
        return True
    if record.budget_exhausted:
        return True
    if demand.subrun_budget is not None and not record.terminal \
            and record.subrun_started >= demand.subrun_budget:
        return True
    return (demand.repair_budget is not None
            and record.repairs_used > demand.repair_budget)


# --- admission ----------------------------------------------------------------


def _admission_reason(
    candidate: model.NodeDecision,
    *,
    used_cpu: int,
    used_gpu: int,
    used_vram: int,
    concurrency: int,
    exclusive_active: bool,
    policy: model.SchedulerPolicy,
) -> str | None:
    demand = candidate.demand
    if demand.exclusive and concurrency > 0:
        return model.R_EXCLUSIVE_CONFLICT
    if exclusive_active:
        return model.R_EXCLUSIVE_CONFLICT
    if concurrency >= policy.global_concurrency:
        return model.R_CONCURRENCY_LIMIT
    if used_cpu + demand.cpu_slots > policy.cpu_slots:
        return model.R_CPU_CAPACITY
    if used_gpu + demand.gpu_slots > policy.gpu_slots:
        return model.R_GPU_CAPACITY
    if used_vram + demand.gpu_mem_bytes > policy.gpu_mem_bytes:
        return model.R_GPU_MEMORY
    return None


def plan(
    graph: graph_model.GoalGraph,
    projection: readiness.ReadinessProjection,
    policy: model.SchedulerPolicy,
    runtime: Mapping[str, sched_evidence.MissionRuntimeEvidence],
    dispatch_records: Sequence[model.DispatchRecord],
    dispatch_counters: Mapping[str, int],
    *,
    created_at: str,
    prior_active_node_ids: Sequence[str] = (),
) -> model.ScheduleDecision:
    """Pure deterministic scheduling decision (see module docstring)."""
    policy.validate()
    node_map = graph.node_map()
    statuses = projection.by_id()
    owned_missions = {record.mission_id for record in dispatch_records}
    dispatched_nodes = {record.node_id for record in dispatch_records}

    current = derive_reservations(graph, runtime, dispatch_records)
    current_ids = {reservation.node_id for reservation in current}
    released = tuple(
        model.ReleaseRecord(
            node_id=node_id,
            mission_id=_mission_id_of(node_map, node_id),
            reason=_release_reason(node_map, node_id, runtime),
        )
        for node_id in sorted(set(prior_active_node_ids) - current_ids)
        if node_id in node_map
    )

    admitted: list[model.NodeDecision] = []
    deferred: list[model.NodeDecision] = []
    blocked: list[model.NodeDecision] = []
    active: list[model.NodeDecision] = []
    completed: list[model.NodeDecision] = []
    candidates: list[model.NodeDecision] = []

    for node_id in projection.topological_order:
        node = node_map[node_id]
        status = statuses[node_id]
        try:
            demand = model.derive_demand(node)
        except model.SchedulerValidationError:
            demand = _invalid_demand(node)
            deferred.append(_node_decision(
                status, node, demand, model.O_DEFERRED,
                model.R_INVALID_RESOURCE_SPEC))
            continue
        mission_id = demand.mission_id
        record = runtime.get(mission_id) if mission_id else None

        if status.state == readiness.RS_COMPLETE:
            completed.append(_node_decision(
                status, node, demand, model.O_COMPLETE,
                model.R_ALREADY_COMPLETE))
            continue
        if status.own_status == readiness.OS_INVALID \
                or status.state == readiness.RS_INVALID:
            blocked.append(_node_decision(
                status, node, demand, model.O_BLOCKED,
                model.R_INVALID_EVIDENCE))
            continue
        if status.own_status == readiness.OS_FAILED:
            blocked.append(_node_decision(
                status, node, demand, model.O_BLOCKED,
                model.R_MISSION_FAILED))
            continue
        if not dependency_ready(status):
            blocked.append(_node_decision(
                status, node, demand, model.O_BLOCKED,
                model.R_DEPENDENCY_BLOCKED))
            continue
        if status.state == readiness.RS_UNRESOLVED:
            blocked.append(_node_decision(
                status, node, demand, model.O_BLOCKED,
                model.R_UNRESOLVED_EVIDENCE))
            continue
        if record is not None and record.error is not None:
            blocked.append(_node_decision(
                status, node, demand, model.O_BLOCKED,
                model.R_INVALID_EVIDENCE))
            continue
        if record is not None and record.active:
            active.append(_node_decision(
                status, node, demand, model.O_ACTIVE,
                model.R_ALREADY_ACTIVE))
            continue
        if mission_id is None:
            deferred.append(_node_decision(
                status, node, demand, model.O_DEFERRED,
                model.R_MISSING_MISSION_REFERENCE))
            continue
        if record is None or not record.resolved:
            deferred.append(_node_decision(
                status, node, demand, model.O_DEFERRED,
                model.R_UNRESOLVED_EVIDENCE))
            continue
        if mission_id in owned_missions or node_id in dispatched_nodes:
            # A dispatch already exists but the mission is not active:
            # never silently replay a proven dispatch decision.
            if record.terminal:
                blocked.append(_node_decision(
                    status, node, demand, model.O_BLOCKED,
                    model.R_MISSION_FAILED))
            else:
                deferred.append(_node_decision(
                    status, node, demand, model.O_DEFERRED,
                    model.R_ALREADY_DISPATCHED))
            continue
        if _budget_exhausted(demand, record, dispatch_counters):
            deferred.append(_node_decision(
                status, node, demand, model.O_DEFERRED,
                model.R_BUDGET_EXHAUSTED))
            continue
        candidates.append(_node_decision(
            status, node, demand, model.O_DEFERRED, model.R_ADMITTED))

    candidates.sort(key=lambda c: (-c.priority, c.node_id))
    candidate_order = tuple(c.node_id for c in candidates)

    totals = model.ReservationTotals.of(current)
    used_cpu = totals.cpu_slots
    used_gpu = totals.gpu_slots
    used_vram = totals.gpu_mem_bytes
    concurrency = totals.count
    exclusive_active = any(r.exclusive for r in current)
    newly: list[model.Reservation] = []

    for candidate in candidates:
        reason = _admission_reason(
            candidate,
            used_cpu=used_cpu,
            used_gpu=used_gpu,
            used_vram=used_vram,
            concurrency=concurrency,
            exclusive_active=exclusive_active,
            policy=policy,
        )
        if reason is not None:
            deferred.append(_node_decision(
                statuses[candidate.node_id], node_map[candidate.node_id],
                candidate.demand, model.O_DEFERRED, reason))
            continue
        admitted.append(_node_decision(
            statuses[candidate.node_id], node_map[candidate.node_id],
            candidate.demand, model.O_ADMITTED, model.R_ADMITTED))
        demand = candidate.demand
        used_cpu += demand.cpu_slots
        used_gpu += demand.gpu_slots
        used_vram += demand.gpu_mem_bytes
        concurrency += 1
        exclusive_active = exclusive_active or demand.exclusive
        newly.append(model.Reservation(
            node_id=demand.node_id,
            mission_id=demand.mission_id or "",
            execution=demand.execution,
            cpu_slots=demand.cpu_slots,
            gpu_slots=demand.gpu_slots,
            gpu_mem_bytes=demand.gpu_mem_bytes,
            exclusive=demand.exclusive,
            owned=True,
            decision_id=None,
        ))

    scheduler_owned = tuple(r for r in current if r.owned)
    snapshot = model.ReservationSnapshot(
        current=current,
        scheduler_owned=scheduler_owned,
        released=released,
        newly_reserved=tuple(sorted(newly, key=lambda r: r.node_id)),
    )
    projection_id = compute_input_projection_id(
        graph, projection, policy, runtime, current, dispatch_records)
    return model.ScheduleDecision.build(
        goal_id=graph.goal_id,
        graph_id=graph.graph_id,
        spec_sha256=graph.spec_sha256,
        input_projection_id=projection_id,
        policy=policy,
        concurrency_limit=policy.global_concurrency,
        candidate_order=candidate_order,
        admitted=admitted,
        deferred=deferred,
        blocked=blocked,
        active=active,
        completed=completed,
        reservations=snapshot,
        dispatch=dispatch_records,
        budget_counters=dispatch_counters,
        created_at=created_at,
    )


def _invalid_demand(node: graph_model.GraphNode) -> model.NodeDemand:
    return model.NodeDemand(
        node_id=node.node_id,
        mission_id=(node.mission_ref.mission_id
                    if node.mission_ref is not None else None),
        execution=model.X_UNSPECIFIED,
        cpu_slots=0,
        gpu_slots=0,
        gpu_mem_bytes=0,
        exclusive=False,
        model_heavy=False,
        max_attempts=None,
        subrun_budget=None,
        time_budget_s=None,
        repair_budget=None,
    )


def _mission_id_of(node_map: Mapping[str, graph_model.GraphNode],
                   node_id: str) -> str:
    node = node_map.get(node_id)
    if node is None or node.mission_ref is None:
        return ""
    return node.mission_ref.mission_id


def _release_reason(
    node_map: Mapping[str, graph_model.GraphNode],
    node_id: str,
    runtime: Mapping[str, sched_evidence.MissionRuntimeEvidence],
) -> str:
    node = node_map.get(node_id)
    if node is None or node.mission_ref is None:
        return "MISSION_INVALID"
    record = runtime.get(node.mission_ref.mission_id)
    if record is None or not record.resolved or record.error is not None:
        return "MISSION_INVALID"
    if record.proven_complete:
        return "MISSION_COMPLETE"
    if record.state == "FAILED":
        return "MISSION_FAILED"
    if record.state == "BLOCKED":
        return "MISSION_BLOCKED"
    return "MISSION_COMPLETE"
