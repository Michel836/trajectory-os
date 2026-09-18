"""Mission 016 — goal-level proof engine (read-only composition).

This module composes the canonical M008-M015 stores into one bounded,
deterministic, derived goal-proof projection. It introduces no second graph,
no second mission engine and no second trust source: every fact is resolved
read-only from the authoritative stores and the result is never a source of
truth.

Fail-closed semantics:

* structurally malformed, unsupported-version, identity-mismatched,
  contradictory or impossible canonical state raises
  :class:`~trajectory_os.graph.proof.model.GoalProofError` — the proof itself
  cannot be trusted;
* semantically *unproven* conditions (unresolved missions, blocked/invalid
  nodes, stale scheduler/reuse state, legacy attestation, unresolved reuse,
  ambiguous criterion mapping, missing scheduler state, unresolved replans)
  are surfaced as explicit risks and force ``INCOMPLETE`` with a stable
  reason code, never a silent ``COMPLETE``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trajectory_os.graph import model as graph_model
from trajectory_os.graph import readiness
from trajectory_os.graph import store as graph_store
from trajectory_os.graph.proof import evidence as proof_evidence
from trajectory_os.graph.proof import identity as proof_identity
from trajectory_os.graph.proof import model as proof_model
from trajectory_os.graph.proof import store as proof_store
from trajectory_os.graph.replan import model as replan_model
from trajectory_os.graph.replan import store as replan_store
from trajectory_os.graph.reuse import engine as reuse_engine
from trajectory_os.graph.reuse import model as reuse_model
from trajectory_os.graph.reuse import resolver as reuse_resolver
from trajectory_os.graph.scheduler import engine as scheduler_engine
from trajectory_os.graph.scheduler import model as sched_model
from trajectory_os.graph.scheduler import store as sched_store
from trajectory_os.missions import gate as mission_gate
from trajectory_os.missions import store as mission_store

#: The scheduler validation error codes that mean "stale generation state".
_STALE_SCHEDULER_CODES = frozenset({sched_model.E_STALE_GENERATION})


@dataclass(frozen=True)
class ReconstructionRead:
    """Read-only reconstruction of one persisted goal-proof projection."""

    persisted: proof_model.GoalProof
    live: proof_model.GoalProof

    @property
    def reconstructed(self) -> bool:
        return self.persisted.proof_id == self.live.proof_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "VALID" if self.reconstructed
                      else proof_model.E_RECONSTRUCTION_MISMATCH,
            "goal_id": self.persisted.goal_id,
            "persisted_proof_id": self.persisted.proof_id,
            "live_proof_id": self.live.proof_id,
            "reconstructed": self.reconstructed,
            "final_state": self.persisted.final_state,
            "final_reason": self.persisted.final_reason,
            "complete": self.persisted.complete,
        }


def _required_nodes(graph: graph_model.GoalGraph) -> tuple[str, ...]:
    """Nodes whose completion is required for the goal to be proven."""
    return tuple(sorted(
        node.node_id for node in graph.nodes
        if node.acceptance_criteria
        or (node.mission_ref is not None and node.mission_ref.required)
    ))


def _replan_projection(
    root: str, goal_id: str, graph: graph_model.GoalGraph,
) -> tuple[proof_model.GenerationRef | None, dict[str, Any], list[proof_model.Risk]]:
    try:
        state = replan_store.reconstruct(root, goal_id, graph)
    except replan_model.ReplanValidationError as exc:
        raise proof_model.GoalProofError(
            proof_model.E_GENERATION_MISMATCH, "replan", str(exc)) from exc
    generation = state.generation
    if generation.graph_id != graph.graph_id:
        raise proof_model.GoalProofError(
            proof_model.E_GENERATION_MISMATCH, "replan",
            f"{generation.graph_id} != {graph.graph_id}")
    ref = proof_model.GenerationRef(
        generation_id=generation.generation_id,
        generation_number=generation.generation_number,
        parent_generation_id=generation.parent_generation_id,
        graph_id=generation.graph_id,
        plan_id=generation.plan_id,
        trigger_id=generation.trigger_id,
    )
    accepted = [event for event in state.events
                if event.status == replan_model.DS_ACCEPTED]
    rejected = [event for event in state.events
                if event.status == replan_model.DS_REJECTED]
    supersessions = sum(
        1
        for event in accepted
        for change in event.changes
        if change.get("op") in (replan_model.OP_SUPERSEDE_NODE,
                                replan_model.OP_REMOVE_NODE)
    )
    projection: dict[str, Any] = {
        "present": True,
        "generation": ref.to_dict(),
        "generation_count": len(state.generations),
        "event_count": len(state.events),
        "accepted": len(accepted),
        "rejected": len(rejected),
        "supersessions": supersessions,
        "generations": [g.generation_id for g in state.generations],
        "events": [event.event_id for event in state.events],
    }
    risks: list[proof_model.Risk] = []
    if rejected:
        projection["last_rejected_reason"] = rejected[-1].reason
    return ref, projection, risks


def _scheduler_projection(
    root: str, goal_id: str, required: tuple[str, ...],
    readiness_by_id: dict[str, readiness.NodeReadiness],
) -> tuple[dict[str, Any], dict[str, Any], list[proof_model.Risk]]:
    if not sched_store.state_exists(root, goal_id):
        # No scheduling has ever run. This is only a risk while required work
        # is still unproven: a goal whose acceptance criteria are all proven
        # by direct mission execution does not require a scheduling record
        # (M012/M014 graphs predate the M013 scheduler, so requiring one
        # unconditionally would break backward compatibility). Scheduling is
        # never proof of success; it is only reported as missing while work
        # genuinely remains unresolved.
        projection: dict[str, Any] = {
            "present": False,
            "decision_id": None,
            "input_projection_id": None,
            "generation_id": None,
            "state_generation_id": None,
            "stale": False,
            "stale_reason": None,
            "counts": {"admitted": 0, "deferred": 0, "blocked": 0,
                       "active": 0, "completed": 0},
            "blockers": [],
        }
        resources: dict[str, Any] = {"present": False}
        risks: list[proof_model.Risk] = []
        incomplete = [
            node_id for node_id in required
            if readiness_by_id[node_id].state != readiness.RS_COMPLETE
        ]
        if incomplete:
            risks.append(proof_model.Risk.build(
                risk_class=proof_model.RISK_UNRESOLVED,
                reason=proof_model.R_SCHEDULER_MISSING,
                subject="scheduler",
                detail=f"no scheduler state; incomplete={incomplete}"))
        return projection, resources, risks
    try:
        read = scheduler_engine.reconstruct(root, goal_id)
    except sched_model.SchedulerValidationError as exc:
        stale = exc.code in _STALE_SCHEDULER_CODES
        projection = {
            "present": True,
            "decision_id": None,
            "input_projection_id": None,
            "generation_id": None,
            "state_generation_id": None,
            "stale": stale,
            "stale_reason": exc.code,
            "counts": {"admitted": 0, "deferred": 0, "blocked": 0,
                       "active": 0, "completed": 0},
            "blockers": [],
        }
        risk_class = proof_model.RISK_STALE if stale \
            else proof_model.RISK_CONTRADICTORY
        reason = proof_model.R_SCHEDULER_STALE if stale \
            else proof_model.R_MALFORMED_STATE
        return projection, {"present": True}, [
            proof_model.Risk.build(
                risk_class=risk_class, reason=reason, subject="scheduler",
                detail=f"{exc.code}: {exc.detail}"[:proof_model.MAX_DETAIL_LEN])
        ]
    except reuse_model.ReuseValidationError as exc:
        projection = {
            "present": True,
            "decision_id": None,
            "input_projection_id": None,
            "generation_id": None,
            "state_generation_id": None,
            "stale": False,
            "stale_reason": exc.code,
            "counts": {"admitted": 0, "deferred": 0, "blocked": 0,
                       "active": 0, "completed": 0},
            "blockers": [],
        }
        return projection, {"present": True}, [
            proof_model.Risk.build(
                risk_class=proof_model.RISK_CONTRADICTORY,
                reason=proof_model.R_MALFORMED_STATE, subject="scheduler",
                detail=f"{exc.code}: {exc.detail}"[:proof_model.MAX_DETAIL_LEN])
        ]
    state = read.state
    latest = read.latest_decision
    blockers = _scheduler_blockers(latest, readiness_by_id)
    counts = (latest.counts() if latest is not None
              else {"admitted": 0, "deferred": 0, "blocked": 0,
                    "active": 0, "completed": 0})
    projection = {
        "present": True,
        "decision_id": (latest.decision_id if latest is not None else None),
        "input_projection_id": (latest.input_projection_id
                                if latest is not None else None),
        "generation_id": state.generation_id,
        "state_generation_id": state.graph_id,
        "stale": False,
        "stale_reason": None,
        "counts": counts,
        "blockers": blockers,
    }
    totals = sched_model.ReservationTotals.of(read.reservations)
    resources = {
        "present": True,
        "policy_id": state.policy.policy_id,
        "cpu_slots": state.policy.cpu_slots,
        "gpu_slots": state.policy.gpu_slots,
        "gpu_mem_bytes": state.policy.gpu_mem_bytes,
        "global_concurrency": state.policy.global_concurrency,
        "reserved_cpu_slots": totals.cpu_slots,
        "reserved_gpu_slots": totals.gpu_slots,
        "reserved_gpu_mem_bytes": totals.gpu_mem_bytes,
        "reserved_count": totals.count,
        "reservations": [r.to_dict() for r in read.reservations],
    }
    risks = []
    for blocker in blockers:
        risks.append(proof_model.Risk.build(
            risk_class=proof_model.RISK_BLOCKED,
            reason=proof_model.R_SCHEDULER_BLOCKER,
            subject=str(blocker["node_id"]),
            detail=str(blocker["reason"])))
    return projection, resources, risks


def _scheduler_blockers(
    latest: sched_model.ScheduleDecision | None,
    readiness_by_id: dict[str, readiness.NodeReadiness],
) -> list[dict[str, Any]]:
    if latest is None:
        return []
    blockers: list[dict[str, Any]] = []
    for node in (*latest.blocked, *latest.deferred):
        status = readiness_by_id.get(node.node_id)
        if status is not None and status.state == readiness.RS_COMPLETE:
            # A previously blocked/deferred node that is now proven complete
            # is a resolved blocker, never a current risk.
            continue
        blockers.append({"node_id": node.node_id, "outcome": node.outcome,
                         "reason": node.reason, "mission_id": node.mission_id})
    blockers.sort(key=lambda item: (str(item["node_id"]),
                                    str(item["reason"])))
    return blockers


def _reuse_projection(
    root: str, goal_id: str, graph: graph_model.GoalGraph,
) -> tuple[dict[str, Any], list[proof_model.Risk]]:
    live = reuse_resolver.build_projection(root, graph)
    persisted = reuse_engine.load_projection(root, goal_id)
    persisted_id = persisted.projection_id if persisted is not None else None
    stale = persisted_id is not None and persisted_id != live.projection_id
    graph_mismatch = (persisted is not None
                      and persisted.graph_id != graph.graph_id)
    projection: dict[str, Any] = {
        "present": persisted is not None,
        "declared": len(live.inputs),
        "projection_id": live.projection_id,
        "persisted_projection_id": persisted_id,
        "stale": stale or graph_mismatch,
        "counts": live.counts(),
        "blocked_nodes": list(live.blocked_nodes()),
        "blocking_reason": {
            node_id: live.blocking_reason(node_id)
            for node_id in live.blocked_nodes()
        },
        "inputs": [item.to_dict() for item in live.inputs],
    }
    risks: list[proof_model.Risk] = []
    if stale or graph_mismatch:
        risks.append(proof_model.Risk.build(
            risk_class=proof_model.RISK_STALE, reason=proof_model.R_REUSE_STALE,
            subject="reuse",
            detail=f"persisted={persisted_id} live={live.projection_id}"))
    for node_id in live.blocked_nodes():
        reason = live.blocking_reason(node_id) \
            or reuse_model.RR_PRODUCER_UNRESOLVED
        rejected = any(
            item.consumer_node_id == node_id and item.blocking
            and item.status == reuse_model.ST_REJECTED
            for item in live.inputs
        )
        risks.append(proof_model.Risk.build(
            risk_class=(proof_model.RISK_INVALID if rejected
                        else proof_model.RISK_UNRESOLVED),
            reason=(proof_model.R_REUSE_REJECTED if rejected
                    else proof_model.R_REUSE_UNRESOLVED),
            subject=node_id, detail=reason))
    return projection, risks


def _node_proofs(
    graph: graph_model.GoalGraph,
    projection: readiness.ReadinessProjection,
    chains: dict[str, proof_evidence.MissionChain],
    bindings: tuple[proof_model.CriterionBinding, ...],
) -> tuple[proof_model.NodeProof, ...]:
    statuses = projection.by_id()
    counts: dict[str, list[int]] = {}
    for binding in bindings:
        bucket = counts.setdefault(binding.node_id, [0, 0])
        bucket[0] += 1
        if binding.proven:
            bucket[1] += 1
    nodes: list[proof_model.NodeProof] = []
    for node_id in projection.topological_order:
        node = graph.node_map()[node_id]
        status = statuses[node_id]
        chain = (chains.get(node.mission_ref.mission_id)
                 if node.mission_ref is not None else None)
        total, proven = counts.get(node_id, (0, 0))
        nodes.append(proof_model.NodeProof(
            node_id=node_id,
            title=node.title,
            priority=node.priority,
            depends_on=tuple(node.depends_on),
            state=status.state,
            reason=status.reason,
            own_status=status.own_status,
            eligible=status.eligible,
            mission_id=(node.mission_ref.mission_id
                        if node.mission_ref is not None else None),
            mission_state=(chain.state if chain is not None else None),
            mission_reason=(chain.reason if chain is not None else None),
            mission_proven=(chain.proven_complete if chain is not None
                            else False),
            criteria_total=total,
            criteria_proven=proven,
        ))
    return tuple(nodes)


def _node_risks(
    graph: graph_model.GoalGraph,
    projection: readiness.ReadinessProjection,
    chains: dict[str, proof_evidence.MissionChain],
) -> list[proof_model.Risk]:
    statuses = projection.by_id()
    risks: list[proof_model.Risk] = []
    for node_id in _required_nodes(graph):
        node = graph.node_map()[node_id]
        status = statuses[node_id]
        if status.state == readiness.RS_INVALID:
            risks.append(proof_model.Risk.build(
                risk_class=proof_model.RISK_CONTRADICTORY,
                reason=proof_model.R_NODE_INVALID, subject=node_id,
                detail=status.reason))
        elif status.state == readiness.RS_UNRESOLVED:
            risks.append(proof_model.Risk.build(
                risk_class=proof_model.RISK_UNRESOLVED,
                reason=proof_model.R_NODE_UNRESOLVED, subject=node_id,
                detail=status.reason))
        elif status.state == readiness.RS_BLOCKED:
            risks.append(proof_model.Risk.build(
                risk_class=proof_model.RISK_BLOCKED,
                reason=proof_model.R_NODE_BLOCKED, subject=node_id,
                detail=status.reason))
        elif status.state != readiness.RS_COMPLETE:
            risks.append(proof_model.Risk.build(
                risk_class=proof_model.RISK_UNPROVEN,
                reason=proof_model.R_NODE_INCOMPLETE, subject=node_id,
                detail=status.reason))
        if node.mission_ref is None:
            continue
        chain = chains.get(node.mission_ref.mission_id)
        if chain is not None and chain.legacy:
            risks.append(proof_model.Risk.build(
                risk_class=proof_model.RISK_LEGACY,
                reason=proof_model.R_MISSION_LEGACY_EVIDENCE,
                subject=node_id,
                detail=f"mission {chain.mission_id} has model-heavy phases "
                       "without verified exact attestation"))
    return risks


def _gate_with_root(
    root: str, *, complete: bool, graph: graph_model.GoalGraph,
) -> proof_model.GateProof:
    """Gate derivation that loads each mission from the exact root.

    A required mission whose canonical document is missing or malformed is a
    fail-closed condition: the goal gate is ``STOP`` and the operator must
    resolve the missing evidence before any further autonomous or commit
    decision.
    """
    required_ids = sorted({
        node.mission_ref.mission_id for node in graph.nodes
        if node.mission_ref is not None and node.mission_ref.required
    })
    mission_ids = sorted({
        node.mission_ref.mission_id for node in graph.nodes
        if node.mission_ref is not None
    })
    views: list[dict[str, Any]] = []
    missing_required = False
    for mission_id in mission_ids:
        try:
            mission, _ = mission_store.load_mission(root, mission_id)
        except (mission_store.MissionNotFound,
                mission_store.MalformedMissionError):
            if mission_id in required_ids:
                missing_required = True
            continue
        views.append(mission_gate.gate_view(mission))
    if missing_required:
        return proof_model.GateProof(
            state=mission_gate.GATE_STOP,
            reason=mission_gate.REASON_CONSOLIDATED,
            human_action_required=True,
            next_human_action=("resolve the missing or malformed required "
                               "mission evidence, then re-run the goal "
                               "proof; no autonomous continuation"))
    gate_states = [view["gate"] for view in views]
    if complete:
        if gate_states and all(state == mission_gate.GATE_DONE
                               for state in gate_states):
            return proof_model.GateProof(
                state=mission_gate.GATE_DONE,
                reason=mission_gate.REASON_MERGE_APPROVED,
                human_action_required=False, next_human_action=None)
        if mission_gate.GATE_GO_MERGE in gate_states:
            return proof_model.GateProof(
                state=mission_gate.GATE_GO_MERGE,
                reason=mission_gate.REASON_COMMIT_APPROVED,
                human_action_required=True,
                next_human_action="record GO MERGE after the reviewed worktree "
                                  "was merged externally")
        return proof_model.GateProof(
            state=mission_gate.GATE_GO_COMMIT,
            reason=mission_gate.REASON_READY_FOR_COMMIT,
            human_action_required=True,
            next_human_action="record GO COMMIT for the exact reviewed "
                              "worktree revision")
    if mission_gate.GATE_STOP in gate_states:
        return proof_model.GateProof(
            state=mission_gate.GATE_STOP,
            reason=mission_gate.REASON_CONSOLIDATED,
            human_action_required=True,
            next_human_action="review the consolidated unresolved goal "
                              "evidence; no autonomous continuation")
    if not views or any(state == mission_gate.GATE_LAUNCH
                        for state in gate_states):
        return proof_model.GateProof(
            state=mission_gate.GATE_LAUNCH,
            reason=mission_gate.REASON_LAUNCH_PENDING,
            human_action_required=True,
            next_human_action="launch the eligible goal work through the "
                              "production mission path")
    return proof_model.GateProof(
        state=mission_gate.GATE_AUTONOMOUS,
        reason=mission_gate.REASON_AUTONOMOUS,
        human_action_required=False, next_human_action=None)


def _counts(
    graph: graph_model.GoalGraph,
    node_proofs: tuple[proof_model.NodeProof, ...],
    criteria: tuple[proof_model.CriterionBinding, ...],
    chains: dict[str, proof_evidence.MissionChain],
    risks: list[proof_model.Risk],
    replan: dict[str, Any],
) -> proof_model.GoalCounts:
    by_state: dict[str, int] = {}
    for node in node_proofs:
        by_state[node.state] = by_state.get(node.state, 0) + 1
    criteria_by_status: dict[str, int] = {}
    for criterion in criteria:
        criteria_by_status[criterion.status] = \
            criteria_by_status.get(criterion.status, 0) + 1
    risks_by_class: dict[str, int] = {}
    for risk in risks:
        risks_by_class[risk.risk_class] = risks_by_class.get(risk.risk_class, 0) + 1
    missions = set(chains)
    proven = sum(1 for chain in chains.values() if chain.proven_complete)
    missing = sum(1 for chain in chains.values()
                  if chain.error is not None)
    incomplete = len(missions) - proven
    return proof_model.GoalCounts(
        nodes_total=len(node_proofs),
        nodes_complete=by_state.get(readiness.RS_COMPLETE, 0),
        nodes_ready=by_state.get(readiness.RS_READY, 0),
        nodes_in_progress=by_state.get(readiness.RS_IN_PROGRESS, 0),
        nodes_blocked=by_state.get(readiness.RS_BLOCKED, 0),
        nodes_unresolved=by_state.get(readiness.RS_UNRESOLVED, 0),
        nodes_invalid=by_state.get(readiness.RS_INVALID, 0),
        criteria_total=len(criteria),
        criteria_proven=criteria_by_status.get(proof_model.CS_PROVEN, 0),
        criteria_unproven=criteria_by_status.get(proof_model.CS_UNPROVEN, 0),
        criteria_contradictory=criteria_by_status.get(
            proof_model.CS_CONTRADICTORY, 0),
        criteria_invalid=criteria_by_status.get(proof_model.CS_INVALID, 0),
        missions_total=len(missions),
        missions_proven=proven,
        missions_incomplete=incomplete,
        missions_missing=missing,
        risks_total=len(risks),
        risks_unresolved=risks_by_class.get(proof_model.RISK_UNRESOLVED, 0),
        risks_blocked=risks_by_class.get(proof_model.RISK_BLOCKED, 0),
        risks_stale=risks_by_class.get(proof_model.RISK_STALE, 0),
        risks_legacy=risks_by_class.get(proof_model.RISK_LEGACY, 0),
        risks_unproven=risks_by_class.get(proof_model.RISK_UNPROVEN, 0),
        risks_contradictory=risks_by_class.get(
            proof_model.RISK_CONTRADICTORY, 0),
        risks_invalid=risks_by_class.get(proof_model.RISK_INVALID, 0),
        risks_impossible=risks_by_class.get(proof_model.RISK_IMPOSSIBLE, 0),
        replans=int(replan.get("accepted", 0)),
        supersessions=int(replan.get("supersessions", 0)),
    )


def build_proof(root: str, goal_id: str, *,
                computed_at: str | None = None) -> proof_model.GoalProof:
    """Build the deterministic derived goal-proof projection (read-only)."""
    graph, _ = graph_store.load_graph(root, goal_id)
    projection = readiness.project_with_store(root, graph)
    statuses = projection.by_id()

    mission_ids = sorted({
        node.mission_ref.mission_id for node in graph.nodes
        if node.mission_ref is not None
    })
    chains = {
        mission_id: proof_evidence.resolve_mission_chain(root, mission_id)
        for mission_id in mission_ids
    }

    generation, replan, replan_risks = _replan_projection(root, goal_id, graph)

    criteria: list[proof_model.CriterionBinding] = []
    for node in sorted(graph.nodes, key=lambda item: item.node_id):
        chain = (chains.get(node.mission_ref.mission_id)
                 if node.mission_ref is not None else None)
        for criterion in sorted(node.acceptance_criteria,
                                key=lambda item: item.criterion_id):
            criteria.append(proof_evidence.bind_criterion(
                node_id=node.node_id, criterion=criterion, chain=chain))
    ordered_criteria = tuple(criteria)

    node_proofs = _node_proofs(graph, projection, chains, ordered_criteria)
    required = _required_nodes(graph)
    sched_proj, resources, sched_risks = _scheduler_projection(
        root, goal_id, required, statuses)
    reuse_proj, reuse_risks = _reuse_projection(root, goal_id, graph)

    risks: list[proof_model.Risk] = []
    risks.extend(_node_risks(graph, projection, chains))
    for binding in ordered_criteria:
        if binding.status == proof_model.CS_INVALID:
            risks.append(proof_model.Risk.build(
                risk_class=proof_model.RISK_INVALID,
                reason=binding.reason,
                subject=f"{binding.node_id}/{binding.criterion_id}",
                detail="acceptance criterion cannot be bound to exact "
                       "evidence"))
    risks.extend(reuse_risks)
    risks.extend(sched_risks)
    risks.extend(replan_risks)
    if replan.get("rejected"):
        risks.append(proof_model.Risk.build(
            risk_class=proof_model.RISK_UNPROVEN,
            reason=proof_model.R_REPLAN_UNRESOLVED, subject="replan",
            detail=f"{replan['rejected']} rejected replan event(s); "
                   f"last reason={replan.get('last_rejected_reason')}"))

    complete = not risks and bool(ordered_criteria) \
        and all(c.proven for c in ordered_criteria)
    gate = _gate_with_root(root, complete=complete, graph=graph)
    counts = _counts(graph, node_proofs, ordered_criteria, chains, risks,
                     replan)
    path = proof_evidence.critical_path(graph)
    return proof_model.GoalProof.build(
        goal_id=graph.goal_id,
        objective=graph.objective,
        graph_id=graph.graph_id,
        spec_sha256=graph.spec_sha256,
        generation=generation,
        replan=replan,
        nodes=node_proofs,
        critical_path=path,
        criteria=ordered_criteria,
        reuse=reuse_proj,
        scheduler=sched_proj,
        resources=resources,
        risks=risks,
        gate=gate,
        counts=counts,
        computed_at=computed_at,
    )


def reconstruct(root: str, goal_id: str) -> ReconstructionRead:
    """Reconstruct the persisted proof and revalidate it against live state.

    Fails closed when no projection was ever recorded, and when the live
    recomputation no longer reproduces the exact persisted ``proof_id``.
    """
    paths = proof_store.proof_paths(root, goal_id)
    persisted = proof_store.load_projection(root, goal_id)
    if persisted is None:
        raise proof_model.GoalProofError(
            proof_model.E_MISSING_EVIDENCE, str(paths["projection"]),
            "goal-proof projection missing")
    if persisted.goal_id != goal_id:
        raise proof_model.GoalProofError(
            proof_model.E_IDENTITY_MISMATCH, str(paths["projection"]),
            f"goal {persisted.goal_id} != {goal_id}")
    live = build_proof(root, goal_id)
    if persisted.proof_id != live.proof_id:
        raise proof_model.GoalProofError(
            proof_model.E_RECONSTRUCTION_MISMATCH, str(paths["projection"]),
            f"{persisted.proof_id} != {live.proof_id}")
    return ReconstructionRead(persisted=persisted, live=live)


def identity_refs(proof: proof_model.GoalProof) -> dict[str, str | None]:
    """Labeled goal-proof identity block for operator output."""
    return proof_identity.identity_refs(
        proof=proof.proof_id,
        generation=(None if proof.generation is None
                    else proof.generation.generation_id),
        graph=proof.graph_id,
    )
