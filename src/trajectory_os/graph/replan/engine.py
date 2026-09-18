"""Mission 015 — adaptive replanning cycle (composition, no new engine).

This module composes the pure replan model and the durable replan store with
the canonical M012 graph and the M013 scheduler. It introduces no second
graph and no second execution engine: an activated replan is a new immutable
generation of the *same* canonical graph, and the previous generation is
archived exactly.

Guarantees (ADR-013):

* only one explicit machine-readable trigger can drive a replan, and it must
  pin the exact generation it was observed against (stale triggers fail
  closed);
* a plan is fully validated (schema, dependencies, cycles, resources, reuse)
  before activation, and a rejected plan writes no new generation;
* the previous generation is archived before the new one becomes active, so
  history is never destructively rewritten;
* replanning changes graph structure only — it never marks a node complete,
  never copies mission success and never promotes semantic evidence;
* the M013 scheduler is deterministically re-bound to the newest validated
  generation, so stale-generation work can never be dispatched.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from trajectory_os.graph import model as graph_model
from trajectory_os.graph import store as graph_store
from trajectory_os.graph.replan import identity as replan_identity
from trajectory_os.graph.replan import model, store
from trajectory_os.missions import store as mission_store
from trajectory_os.runs.store import atomic_write_json

#: Provenance author recorded on a replanned generation.
REPLANNED_BY = "trajectory-pi-goals replan"


class _PlanRejection(Exception):
    """Internal deterministic plan rejection (mapped to a stable reason)."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass
class ApplyResult:
    """One deterministic replan apply outcome (accepted or rejected)."""

    status: str
    reason: str
    generation: model.Generation
    plan: model.ReplanPlan | None
    event: model.ReplanEvent
    adopted_scheduler: bool = False
    previous_generation_id: str | None = None

    @property
    def accepted(self) -> bool:
        return self.status == model.DS_ACCEPTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "accepted": self.accepted,
            "reason": self.reason,
            "goal_id": self.generation.goal_id,
            "generation_id": self.generation.generation_id,
            "generation_number": self.generation.generation_number,
            "parent_generation_id": self.generation.parent_generation_id,
            "previous_generation_id": self.previous_generation_id,
            "graph_id": self.generation.graph_id,
            "plan_id": None if self.plan is None else self.plan.plan_id,
            "trigger_id": self.plan.trigger.trigger_id if self.plan else None,
            "change_summary": ([] if self.plan is None
                               else list(self.plan.change_summary())),
            "adopted_scheduler": self.adopted_scheduler,
            "event_id": self.event.event_id,
        }


# --- read-only current generation ---------------------------------------------


def current_generation(root: str, goal_id: str) -> store.ReconstructedReplan:
    """Reconstruct the current generation (read-only, fail closed)."""
    graph, _ = graph_store.load_graph(root, goal_id)
    return store.reconstruct(root, goal_id, graph)


# --- plan construction --------------------------------------------------------


def build_plan(
    root: str,
    goal_id: str,
    trigger: model.ReplanTrigger,
    changes: Sequence[model.ReplanChange],
    policy: model.ReplanPolicy | None = None,
    *,
    created_at: str,
) -> model.ReplanDecision:
    """Build and validate one deterministic replan plan (pure, no writes)."""
    graph, _ = graph_store.load_graph(root, goal_id)
    policy = policy or model.DEFAULT_POLICY
    state = store.reconstruct(root, goal_id, graph)
    current = state.generation

    def rejected(reason: str) -> model.ReplanDecision:
        return model.ReplanDecision(
            status=model.DS_REJECTED, reason=reason, goal_id=goal_id,
            trigger=trigger, current_generation_id=current.generation_id,
            plan=None)

    if (trigger.generation_id != current.generation_id
            or trigger.graph_id != graph.graph_id):
        return rejected(model.RC_TRIGGER_STALE)
    applied = {event.trigger_id for event in state.events
               if event.status == model.DS_ACCEPTED
               and event.trigger_id is not None}
    if trigger.trigger_id in applied:
        return rejected(model.RC_TRIGGER_ALREADY_APPLIED)
    if policy.require_trigger_evidence and not trigger.has_evidence():
        return rejected(model.RC_TRIGGER_EVIDENCE_REQUIRED)
    if len(changes) > policy.max_changes:
        return rejected(model.RC_CHANGE_LIMIT)
    if current.generation_number + 1 > policy.max_generations:
        return rejected(model.RC_GENERATION_LIMIT)
    if not changes:
        return rejected(model.RC_NO_OP)
    for change in changes:
        gate = _policy_gate(change, policy)
        if gate is not None:
            return rejected(gate)
    try:
        nodes = _apply_changes(graph, changes)
        new_graph = _build_generation_graph(
            graph, nodes, created_at=created_at)
        _validate_resources(new_graph)
    except _PlanRejection as exc:
        return rejected(exc.reason)
    except graph_model.GraphValidationError as exc:
        return rejected(_map_graph_error(exc))
    plan = model.ReplanPlan.build(
        goal_id=goal_id,
        parent_generation_id=current.generation_id,
        parent_graph_id=graph.graph_id,
        trigger=trigger,
        policy=policy,
        changes=changes,
        resulting_nodes=new_graph.nodes,
        resulting_edges=new_graph.edges,
        new_graph_id=new_graph.graph_id,
        new_spec_sha256=new_graph.spec_sha256,
    )
    return model.ReplanDecision(
        status=model.DS_ACCEPTED, reason=model.RC_APPLIED, goal_id=goal_id,
        trigger=trigger, current_generation_id=current.generation_id,
        plan=plan)


def _policy_gate(change: model.ReplanChange,
                 policy: model.ReplanPolicy) -> str | None:
    if change.op == model.OP_REMOVE_NODE and not policy.allow_node_removal:
        return model.RC_POLICY_DENIED
    if change.op == model.OP_SUPERSEDE_NODE and not policy.allow_supersession:
        return model.RC_POLICY_DENIED
    if (change.op == model.OP_INVALIDATE_REUSE
            and not policy.allow_reuse_invalidation):
        return model.RC_POLICY_DENIED
    return None


def _apply_changes(
    graph: graph_model.GoalGraph,
    changes: Sequence[model.ReplanChange],
) -> dict[str, dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {
        node.node_id: node.to_dict() for node in graph.nodes
    }
    for change in changes:
        if change.op == model.OP_ADD_NODE:
            assert change.node_id is not None and change.node_spec is not None
            if change.node_id in nodes:
                raise _PlanRejection(model.RC_VALIDATION_FAILED,
                                     f"node exists: {change.node_id}")
            nodes[change.node_id] = dict(change.node_spec)
        elif change.op == model.OP_REMOVE_NODE:
            assert change.node_id is not None
            if change.node_id not in nodes:
                raise _PlanRejection(model.RC_INVALID_DEPENDENCY,
                                     f"unknown node: {change.node_id}")
            del nodes[change.node_id]
        elif change.op == model.OP_SUPERSEDE_NODE:
            assert change.node_id is not None and change.node_spec is not None
            old_id = change.node_id
            new_id = str(change.node_spec["node_id"])
            if old_id not in nodes:
                raise _PlanRejection(model.RC_INVALID_DEPENDENCY,
                                     f"unknown node: {old_id}")
            if new_id in nodes:
                raise _PlanRejection(model.RC_VALIDATION_FAILED,
                                     f"replacement exists: {new_id}")
            del nodes[old_id]
            nodes[new_id] = dict(change.node_spec)
            for spec in nodes.values():
                deps = list(spec.get("depends_on") or [])
                if old_id in deps:
                    spec["depends_on"] = [
                        new_id if dep == old_id else dep for dep in deps
                    ]
        elif change.op == model.OP_ADD_DEPENDENCY:
            assert change.dependency_from is not None
            assert change.dependency_to is not None
            _mutate_dependency(nodes, change.dependency_from,
                               change.dependency_to, add=True)
        elif change.op == model.OP_REMOVE_DEPENDENCY:
            assert change.dependency_from is not None
            assert change.dependency_to is not None
            _mutate_dependency(nodes, change.dependency_from,
                               change.dependency_to, add=False)
        elif change.op == model.OP_INVALIDATE_REUSE:
            assert change.consumer_node_id is not None
            assert change.input_id is not None
            _invalidate_reuse(nodes, change.consumer_node_id, change.input_id)
    return nodes


def _mutate_dependency(nodes: dict[str, dict[str, Any]], dep_from: str,
                       dep_to: str, *, add: bool) -> None:
    if dep_from not in nodes:
        raise _PlanRejection(model.RC_INVALID_DEPENDENCY,
                             f"unknown dependency: {dep_from}")
    if dep_to not in nodes:
        raise _PlanRejection(model.RC_INVALID_DEPENDENCY,
                             f"unknown dependent: {dep_to}")
    if dep_from == dep_to:
        raise _PlanRejection(model.RC_INVALID_DEPENDENCY, "self dependency")
    spec = nodes[dep_to]
    deps = list(spec.get("depends_on") or [])
    if add:
        if dep_from in deps:
            raise _PlanRejection(model.RC_VALIDATION_FAILED,
                                 f"dependency exists: {dep_from}->{dep_to}")
        deps.append(dep_from)
    else:
        if dep_from not in deps:
            raise _PlanRejection(model.RC_INVALID_DEPENDENCY,
                                 f"dependency absent: {dep_from}->{dep_to}")
        deps.remove(dep_from)
    spec["depends_on"] = deps


def _invalidate_reuse(nodes: dict[str, dict[str, Any]], consumer: str,
                      input_id: str) -> None:
    spec = nodes.get(consumer)
    if spec is None:
        raise _PlanRejection(model.RC_INVALID_REUSE,
                             f"unknown consumer: {consumer}")
    inputs = list(spec.get("reuse_inputs") or [])
    remaining = [item for item in inputs
                 if not (isinstance(item, Mapping)
                         and item.get("input_id") == input_id)]
    if len(remaining) == len(inputs):
        raise _PlanRejection(model.RC_INVALID_REUSE,
                             f"unknown reuse input: {consumer}/{input_id}")
    if remaining:
        spec["reuse_inputs"] = remaining
    else:
        spec.pop("reuse_inputs", None)


def _build_generation_graph(
    parent: graph_model.GoalGraph,
    nodes: Mapping[str, Mapping[str, Any]],
    *,
    created_at: str,
) -> graph_model.GoalGraph:
    parsed = [
        graph_model.GraphNode.from_dict(dict(spec),
                                        f"replan[node {spec.get('node_id')}]")
        for spec in nodes.values()
    ]
    provenance = graph_model.GraphProvenance(
        created_at=created_at,
        created_by=REPLANNED_BY,
        repo_root=parent.provenance.repo_root,
        baseline_revision=parent.provenance.baseline_revision,
    )
    return graph_model.GoalGraph.build(
        goal_id=parent.goal_id, objective=parent.objective, nodes=parsed,
        provenance=provenance)


def _validate_resources(graph: graph_model.GoalGraph) -> None:
    # Imported lazily to keep the module import graph acyclic.
    from trajectory_os.graph.scheduler import model as scheduler_model
    for node in graph.nodes:
        try:
            scheduler_model.derive_demand(node)
        except scheduler_model.SchedulerValidationError as exc:
            raise _PlanRejection(model.RC_INVALID_RESOURCE,
                                 f"{node.node_id}: {exc}") from exc


def _map_graph_error(exc: graph_model.GraphValidationError) -> str:
    if exc.code == graph_model.E_CYCLE:
        return model.RC_CYCLE
    if exc.code in (
        graph_model.E_INVALID_RESOURCE, graph_model.E_INVALID_BUDGET,
    ):
        return model.RC_INVALID_RESOURCE
    if exc.code in (
        graph_model.E_INVALID_REUSE, graph_model.E_DUPLICATE_REUSE_INPUT,
        graph_model.E_REUSE_PRODUCER, graph_model.E_REUSE_REFERENCE,
    ):
        return model.RC_INVALID_REUSE
    if exc.code in (
        graph_model.E_MISSING_DEPENDENCY, graph_model.E_SELF_DEPENDENCY,
        graph_model.E_DUPLICATE_EDGE, graph_model.E_DUPLICATE_NODE,
    ):
        return model.RC_INVALID_DEPENDENCY
    return model.RC_VALIDATION_FAILED


# --- apply --------------------------------------------------------------------


def apply_plan(
    root: str,
    goal_id: str,
    plan: model.ReplanPlan,
    *,
    created_at: str,
) -> ApplyResult:
    """Validate and atomically activate one replan plan (fail closed)."""
    graph, graph_paths = graph_store.load_graph(root, goal_id)
    state = store.reconstruct(root, goal_id, graph)
    current = state.generation
    if (plan.parent_generation_id != current.generation_id
            or plan.parent_graph_id != graph.graph_id):
        return _reject(root, goal_id, plan, current,
                       model.RC_TRIGGER_STALE, created_at)
    # Recompute the plan from the live generation; a mismatch means the inputs
    # changed between preview and apply and is never activated.
    decision = build_plan(root, goal_id, plan.trigger, plan.changes,
                          plan.policy, created_at=created_at)
    if decision.plan is None or decision.plan.plan_id != plan.plan_id:
        return _reject(root, goal_id, plan, current,
                       model.RC_TRIGGER_STALE, created_at)

    new_graph = _build_generation_graph(graph, _apply_changes(graph,
                                                             plan.changes),
                                        created_at=created_at)
    new_generation = model.Generation.activated(
        parent=current, graph=new_graph, trigger_id=plan.trigger.trigger_id,
        plan_id=plan.plan_id)
    new_generation = _stamp(new_generation, created_at)
    paths = store.replan_paths(root, goal_id)
    # Preserve the previous generation exactly before activating the new one.
    store.archive_generation(paths, graph, current)
    store.archive_generation(paths, new_graph, new_generation)
    # Pointer first, then the active graph: a crash between the two leaves a
    # fail-closed mismatch (never a silently-lossy state).
    store.save_current(paths, new_generation)
    atomic_write_json(graph_paths["graph"], new_graph.to_dict())
    mission_store.append_event({"events": graph_paths["events"]}, {
        "ts": created_at,
        "event": "replanned",
        "goal_id": goal_id,
        "graph_id": new_graph.graph_id,
        "spec_sha256": new_graph.spec_sha256,
        "generation_id": new_generation.generation_id,
        "parent_generation_id": current.generation_id,
        "plan_id": plan.plan_id,
        "trigger_id": plan.trigger.trigger_id,
        "trigger_kind": plan.trigger.kind,
    })
    event = model.ReplanEvent.build(
        event=model.EV_GENERATION_ACTIVATED, status=model.DS_ACCEPTED,
        reason=model.RC_GENERATION_ACTIVATED, goal_id=goal_id,
        generation_id=new_generation.generation_id,
        parent_generation_id=current.generation_id, plan_id=plan.plan_id,
        trigger_id=plan.trigger.trigger_id, changes=plan.change_summary(),
        created_at=created_at)
    store.append_event(paths, event)
    adopted = _adopt_scheduler(root, goal_id, created_at=created_at)
    return ApplyResult(
        status=model.DS_ACCEPTED, reason=model.RC_GENERATION_ACTIVATED,
        generation=new_generation, plan=plan, event=event,
        adopted_scheduler=adopted,
        previous_generation_id=current.generation_id)


def _reject(root: str, goal_id: str, plan: model.ReplanPlan,
            current: model.Generation, reason: str,
            created_at: str) -> ApplyResult:
    paths = store.replan_paths(root, goal_id)
    event = model.ReplanEvent.build(
        event=model.EV_PLAN_REJECTED, status=model.DS_REJECTED, reason=reason,
        goal_id=goal_id, generation_id=None,
        parent_generation_id=current.generation_id, plan_id=plan.plan_id,
        trigger_id=plan.trigger.trigger_id, changes=plan.change_summary(),
        created_at=created_at)
    store.append_event(paths, event)
    return ApplyResult(
        status=model.DS_REJECTED, reason=reason, generation=current,
        plan=plan, event=event,
        previous_generation_id=current.generation_id)


def _stamp(generation: model.Generation,
           created_at: str) -> model.Generation:
    from dataclasses import replace
    return replace(generation, activated_at=created_at)


def _adopt_scheduler(root: str, goal_id: str, *, created_at: str) -> bool:
    from trajectory_os.graph.scheduler import engine as scheduler_engine
    from trajectory_os.graph.scheduler import store as sched_store
    if not sched_store.state_exists(root, goal_id):
        return False
    scheduler_engine.adopt_generation(root, goal_id, created_at=created_at)
    return True


# --- operator spec composition ------------------------------------------------


def parse_spec(doc: object) -> tuple[object | None,
                                     list[model.ReplanChange],
                                     model.ReplanPolicy]:
    """Parse an operator replan spec (``{trigger, changes, policy}``)."""
    if not isinstance(doc, dict):
        raise model.ReplanValidationError(
            model.E_MALFORMED, "spec", "spec must be a JSON object")
    allowed = {"schema_version", "trigger", "changes", "policy"}
    unknown = set(doc) - allowed
    if unknown:
        raise model.ReplanValidationError(
            model.E_MALFORMED, "spec", f"unknown field(s): {sorted(unknown)}")
    version = doc.get("schema_version", model.SCHEMA_VERSION)
    if version != model.SCHEMA_VERSION:
        raise model.ReplanValidationError(
            model.E_UNSUPPORTED_VERSION, "spec",
            f"schema_version={version!r}")
    raw_changes = doc.get("changes")
    if not isinstance(raw_changes, list):
        raise model.ReplanValidationError(
            model.E_MALFORMED, "spec", "changes must be a list")
    changes = [
        model.ReplanChange.from_dict(item, f"spec[changes/{index}]")
        for index, item in enumerate(raw_changes)
    ]
    raw_policy = doc.get("policy")
    policy = (model.ReplanPolicy.from_dict(raw_policy)
              if raw_policy is not None else model.DEFAULT_POLICY)
    return doc.get("trigger"), changes, policy


def build_trigger(
    root: str,
    goal_id: str,
    doc: object,
) -> model.ReplanTrigger:
    """Build a trigger from operator input, pinning the current generation.

    ``generation_id``/``graph_id`` may be omitted to pin the live generation
    (the normal operator request). Supplying them explicitly is how an
    operator or automation routes a decision observed against an earlier
    generation, which the engine then deterministically rejects as stale.
    """
    if not isinstance(doc, dict):
        raise model.ReplanValidationError(
            model.E_MALFORMED, "trigger", "trigger object required")
    state = current_generation(root, goal_id)
    generation_id = doc.get("generation_id", state.generation.generation_id)
    graph_id = doc.get("graph_id", state.generation.graph_id)
    provenance = model.EvidenceProvenance.from_dict(
        doc.get("provenance"), "trigger[provenance]")
    detail = doc.get("detail") or ""
    kind = doc.get("kind")
    source = doc.get("source") or "operator"
    if not isinstance(kind, str):
        raise model.ReplanValidationError(
            model.E_MALFORMED, "trigger", "kind is required")
    return model.ReplanTrigger.build(
        kind=kind, source=str(source), generation_id=str(generation_id),
        graph_id=str(graph_id), provenance=provenance, detail=str(detail))


def preview_spec(
    root: str, goal_id: str, doc: object, *, created_at: str,
) -> model.ReplanDecision:
    """Build (and validate) a plan from an operator spec without writing."""
    trigger_doc, changes, policy = parse_spec(doc)
    trigger = build_trigger(root, goal_id, trigger_doc)
    return build_plan(root, goal_id, trigger, changes, policy,
                      created_at=created_at)


def apply_spec(
    root: str, goal_id: str, doc: object, *, created_at: str,
) -> ApplyResult:
    """Build and atomically apply a plan from an operator spec."""
    decision = preview_spec(root, goal_id, doc, created_at=created_at)
    if decision.plan is None:
        current = current_generation(root, goal_id).generation
        event = model.ReplanEvent.build(
            event=model.EV_PLAN_REJECTED, status=model.DS_REJECTED,
            reason=decision.reason, goal_id=goal_id, generation_id=None,
            parent_generation_id=current.generation_id, plan_id=None,
            trigger_id=decision.trigger.trigger_id, changes=(),
            created_at=created_at)
        store.append_event(store.replan_paths(root, goal_id), event)
        return ApplyResult(
            status=model.DS_REJECTED, reason=decision.reason,
            generation=current, plan=None, event=event,
            previous_generation_id=current.generation_id)
    return apply_plan(root, goal_id, decision.plan, created_at=created_at)


# --- M016-facing machine projection -------------------------------------------


def machine_projection(root: str, goal_id: str) -> dict[str, Any]:
    """Deterministic machine-readable replanning projection for M016."""
    graph, _ = graph_store.load_graph(root, goal_id)
    state = store.reconstruct(root, goal_id, graph)
    identity_payload = {
        "schema_version": model.SCHEMA_VERSION,
        "replan_version": model.REPLAN_VERSION,
        "goal_id": goal_id,
        "active_graph_id": graph.graph_id,
        "active_spec_sha256": graph.spec_sha256,
        "generation": state.generation.identity_payload(),
        "generations": [g.identity_payload() for g in state.generations],
        "events": [e.identity_payload() for e in state.events],
    }
    projection = replan_identity.projection_id(identity_payload)
    return {
        **identity_payload,
        "status": "OK",
        "projection_id": projection,
        "generation": state.generation.to_dict(),
        "generations": [g.to_dict() for g in state.generations],
        "events": [e.to_dict() for e in state.events],
    }
