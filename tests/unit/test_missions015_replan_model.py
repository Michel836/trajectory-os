"""Mission 015 — adaptive-replanning model unit tests (pure, no store)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from trajectory_os.graph import model as graph_model
from trajectory_os.graph import store as graph_store
from trajectory_os.graph.replan import identity as replan_identity
from trajectory_os.graph.replan import model


def _spec() -> dict[str, object]:
    return {
        "schema_version": 1, "goal_id": "g-unit", "objective": "o",
        "nodes": [
            {"node_id": "n-a", "title": "A", "priority": 50,
             "depends_on": [],
             "acceptance_criteria": [{"criterion_id": "ac-1",
                                      "statement": "s"}]},
            {"node_id": "n-b", "title": "B", "priority": 40,
             "depends_on": ["n-a"],
             "acceptance_criteria": [{"criterion_id": "ac-1",
                                      "statement": "s"}]},
        ],
    }


def _graph() -> graph_model.GoalGraph:
    return graph_store.build_graph(spec_doc=_spec())


# --- identity domains ---------------------------------------------------------


def test_replan_domains_are_distinct_and_separated() -> None:
    payload = {"a": 1}
    digests = {
        replan_identity.digest(domain, payload)
        for domain in replan_identity.DOMAIN_IDS
    }
    assert len(digests) == len(replan_identity.DOMAIN_IDS)
    with pytest.raises(ValueError):
        replan_identity.digest("unknown.domain", payload)


# --- trigger ------------------------------------------------------------------


def test_trigger_roundtrip_and_evidence_rules() -> None:
    trigger = model.ReplanTrigger.build(
        kind=model.TK_MISSION_FAILURE, source="n-a",
        generation_id="a" * 64, graph_id="b" * 64,
        provenance=model.EvidenceProvenance(mission_id="m-a"))
    assert trigger.trigger_id == trigger.compute_trigger_id()
    assert trigger.has_evidence()
    reloaded = model.ReplanTrigger.from_dict(trigger.to_dict())
    assert reloaded == trigger

    no_evidence = model.ReplanTrigger.build(
        kind=model.TK_MISSION_FAILURE, source="n-a",
        generation_id="a" * 64, graph_id="b" * 64)
    assert not no_evidence.has_evidence()
    operator = model.ReplanTrigger.build(
        kind=model.TK_OPERATOR_REQUEST, source="operator",
        generation_id="a" * 64, graph_id="b" * 64, detail="please replan")
    assert operator.has_evidence()
    with pytest.raises(model.ReplanValidationError):
        model.ReplanTrigger.from_dict({**trigger.to_dict(), "kind": "BOGUS"})


# --- policy -------------------------------------------------------------------


def test_policy_identity_and_roundtrip() -> None:
    policy = model.ReplanPolicy.build(max_changes=3, allow_node_removal=False)
    assert policy.policy_id == policy.compute_policy_id()
    reloaded = model.ReplanPolicy.from_dict(policy.to_dict())
    assert reloaded == policy
    with pytest.raises(model.ReplanValidationError):
        model.ReplanPolicy.build(max_changes=0)


# --- changes ------------------------------------------------------------------


def test_change_shapes_fail_closed() -> None:
    add = model.ReplanChange.add_node(
        node_spec={"node_id": "n-c", "title": "C", "priority": 10,
                   "depends_on": [], "acceptance_criteria": [
                       {"criterion_id": "ac-1", "statement": "s"}]},
        reason="add")
    assert add.op == model.OP_ADD_NODE
    assert model.ReplanChange.from_dict(add.to_dict()) == add
    with pytest.raises(model.ReplanValidationError):
        model.ReplanChange.from_dict({"op": "ADD_NODE", "reason": "x"})
    with pytest.raises(model.ReplanValidationError):
        model.ReplanChange.supersede_node(
            old_node_id="n-a",
            node_spec={"node_id": "n-a", "title": "same", "priority": 1,
                       "depends_on": [], "acceptance_criteria": []},
            reason="same id")
    with pytest.raises(model.ReplanValidationError):
        model.ReplanChange.from_dict({"op": "NOPE", "reason": "x"})


# --- generation / event -------------------------------------------------------


def test_generation_identity_and_roundtrip() -> None:
    graph = _graph()
    base = replace(model.Generation.base(graph=graph), activated_at="t0")
    assert base.generation_id == base.compute_generation_id()
    assert model.Generation.from_dict(base.to_dict()) == base
    child = replace(
        model.Generation.activated(
            parent=base, graph=graph, trigger_id="a" * 64, plan_id="b" * 64),
        activated_at="t1")
    assert child.generation_number == 2
    assert child.parent_generation_id == base.generation_id
    assert model.Generation.from_dict(child.to_dict()) == child


def test_event_identity_and_roundtrip() -> None:
    event = model.ReplanEvent.build(
        event=model.EV_GENERATION_ACTIVATED, status=model.DS_ACCEPTED,
        reason=model.RC_GENERATION_ACTIVATED, goal_id="g-unit",
        generation_id="a" * 64, parent_generation_id="b" * 64,
        plan_id="c" * 64, trigger_id="d" * 64,
        changes=[{"op": model.OP_REMOVE_NODE, "reason": "x"}],
        created_at="t")
    assert event.event_id == event.compute_event_id()
    assert model.ReplanEvent.from_dict(event.to_dict()) == event
    with pytest.raises(model.ReplanValidationError):
        model.ReplanEvent.from_dict(
            {**event.to_dict(), "status": "MAYBE"})


# --- plan ---------------------------------------------------------------------


def test_plan_identity_is_timestamp_free() -> None:
    graph = _graph()
    trigger = model.ReplanTrigger.build(
        kind=model.TK_OPERATOR_REQUEST, source="operator",
        generation_id="a" * 64, graph_id=graph.graph_id, detail="x")
    change = model.ReplanChange.remove_node(node_id="n-b", reason="drop")
    plan = model.ReplanPlan.build(
        goal_id="g-unit", parent_generation_id="a" * 64,
        parent_graph_id=graph.graph_id, trigger=trigger,
        policy=model.DEFAULT_POLICY, changes=[change],
        resulting_nodes=graph.nodes, resulting_edges=graph.edges,
        new_graph_id=graph.graph_id, new_spec_sha256=graph.spec_sha256)
    assert plan.plan_id == plan.compute_plan_id()
    assert plan.change_summary()[0]["op"] == model.OP_REMOVE_NODE
