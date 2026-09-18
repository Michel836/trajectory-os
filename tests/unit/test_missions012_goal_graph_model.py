"""Mission 012 — pure goal decomposition graph model tests.

Covers the model contract from Issue #215: valid creation, deterministic
normalization, deterministic topological order, stable tie-breaking,
bounded fields, and the fail-closed invariants (cycles, self/missing
dependencies, duplicate node/edge identity, malformed/unsupported schema,
oversized graph/fields, invalid priorities/resources/budgets, duplicate
mission references).
"""

from __future__ import annotations

from typing import Any

import pytest

from trajectory_os.graph import identity, model


def _criterion(cid: str = "ac-1", statement: str = "done") -> dict[str, Any]:
    return {"criterion_id": cid, "statement": statement}


def _node(node_id: str, *, priority: int = 50,
          depends_on: list[str] | None = None,
          mission_ref: dict[str, Any] | None = None,
          resources: dict[str, Any] | None = None,
          budgets: dict[str, Any] | None = None,
          criteria: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "title": f"node {node_id}",
        "priority": priority,
        "depends_on": list(depends_on or []),
        "acceptance_criteria": list(criteria or [_criterion()]),
        "mission_ref": mission_ref,
        "resources": resources,
        "budgets": budgets,
    }


def _spec(nodes: list[dict[str, Any]], *, goal_id: str = "g-test",
          objective: str = "test goal") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "goal_id": goal_id,
        "objective": objective,
        "nodes": nodes,
    }


def _graph(spec: dict[str, Any]) -> model.GoalGraph:
    normalized = model.normalize_spec(spec)
    return model.GoalGraph.build(
        goal_id=normalized.goal_id,
        objective=normalized.objective,
        nodes=normalized.nodes,
        provenance=model.GraphProvenance(
            created_at="2026-01-01T00:00:00Z",
            created_by="test",
            repo_root=None,
            baseline_revision=None,
        ),
    )


def _code(spec: dict[str, Any]) -> str:
    with pytest.raises(model.GraphValidationError) as exc:
        model.normalize_spec(spec)
    return exc.value.code


# --- valid creation / normalization ------------------------------------------


def test_valid_graph_normalizes_nodes_edges_and_order() -> None:
    spec = _spec([
        _node("n-b", priority=50, depends_on=["n-a"]),
        _node("n-a", priority=90),
        _node("n-c", priority=10, depends_on=["n-a", "n-b"]),
    ])
    graph = _graph(spec)
    assert [n.node_id for n in graph.nodes] == ["n-a", "n-b", "n-c"]
    assert [e.to_dict() for e in graph.edges] == [
        {"from": "n-a", "to": "n-b"},
        {"from": "n-a", "to": "n-c"},
        {"from": "n-b", "to": "n-c"},
    ]
    assert graph.topological_order() == ("n-a", "n-b", "n-c")
    assert graph.objective == "test goal"


def test_normalization_is_order_independent() -> None:
    forward = _spec([
        _node("n-a", priority=50),
        _node("n-b", priority=40, depends_on=["n-a"]),
        _node("n-c", priority=30, depends_on=["n-a", "n-b"]),
    ])
    shuffled = _spec([
        _node("n-c", priority=30, depends_on=["n-a", "n-b"]),
        _node("n-a", priority=50),
        _node("n-b", priority=40, depends_on=["n-a"]),
    ])
    graph_a = _graph(forward)
    graph_b = _graph(shuffled)
    assert graph_a.to_dict() == graph_b.to_dict()
    assert graph_a.graph_id == graph_b.graph_id
    assert graph_a.spec_sha256 == graph_b.spec_sha256


def test_depends_on_and_criteria_are_sorted() -> None:
    node = _node(
        "n-c",
        depends_on=["n-b", "n-a"],
        criteria=[_criterion("ac-z"), _criterion("ac-a")],
    )
    normalized = model.normalize_spec(_spec([_node("n-a"), _node("n-b"), node]))
    target = {n.node_id: n for n in normalized.nodes}["n-c"]
    assert target.depends_on == ("n-a", "n-b")
    assert [c.criterion_id for c in target.acceptance_criteria] == [
        "ac-a", "ac-z"]


# --- topological order and tie-breaking --------------------------------------


def test_priority_high_first_and_tie_break_by_node_id() -> None:
    spec = _spec([
        _node("n-d", priority=10),
        _node("n-c", priority=50),
        _node("n-b", priority=50),
        _node("n-a", priority=50),
    ])
    assert _graph(spec).topological_order() == ("n-a", "n-b", "n-c", "n-d")


def test_dependencies_precede_dependents_even_when_lower_priority() -> None:
    spec = _spec([
        _node("n-first", priority=1, depends_on=["n-second"]),
        _node("n-second", priority=100),
    ])
    assert _graph(spec).topological_order() == ("n-second", "n-first")


def test_topological_order_is_stable_across_repeated_calls() -> None:
    graph = _graph(_spec([_node("n-a", priority=5), _node("n-b", priority=5)]))
    assert graph.topological_order() == graph.topological_order() == ("n-a", "n-b")


# --- fail-closed invariants ---------------------------------------------------


def test_self_dependency_rejected() -> None:
    with pytest.raises(model.GraphValidationError) as exc:
        model.normalize_spec(_spec([_node("n-a", depends_on=["n-a"])]))
    assert exc.value.code == model.E_SELF_DEPENDENCY


def test_missing_dependency_rejected() -> None:
    with pytest.raises(model.GraphValidationError) as exc:
        model.normalize_spec(_spec([_node("n-a", depends_on=["n-missing"])]))
    assert exc.value.code == model.E_MISSING_DEPENDENCY


def test_duplicate_node_id_rejected() -> None:
    assert _code(_spec([_node("n-a"), _node("n-a", priority=10)])) \
        == model.E_DUPLICATE_NODE


def test_duplicate_edge_rejected() -> None:
    with pytest.raises(model.GraphValidationError) as exc:
        model.normalize_spec(_spec([
            _node("n-a"),
            _node("n-b", depends_on=["n-a", "n-a"]),
        ]))
    assert exc.value.code == model.E_DUPLICATE_EDGE


def test_two_node_cycle_rejected_with_path() -> None:
    with pytest.raises(model.GraphValidationError) as exc:
        model.normalize_spec(_spec([
            _node("n-a", depends_on=["n-b"]),
            _node("n-b", depends_on=["n-a"]),
        ]))
    assert exc.value.code == model.E_CYCLE
    assert "n-a" in exc.value.detail and "n-b" in exc.value.detail


def test_three_node_cycle_rejected() -> None:
    assert _code(_spec([
        _node("n-a", depends_on=["n-c"]),
        _node("n-b", depends_on=["n-a"]),
        _node("n-c", depends_on=["n-b"]),
    ])) == model.E_CYCLE


def test_unsupported_schema_version_rejected() -> None:
    spec = _spec([_node("n-a")])
    spec["schema_version"] = 2
    assert _code(spec) == model.E_UNSUPPORTED_VERSION


def test_missing_schema_version_rejected() -> None:
    spec: dict[str, Any] = {
        "goal_id": "g-test", "objective": "x", "nodes": [_node("n-a")]}
    assert _code(spec) == model.E_MALFORMED


def test_malformed_schema_rejected() -> None:
    assert _code("not-an-object") == model.E_MALFORMED  # type: ignore[arg-type]
    unknown = _spec([_node("n-a")])
    unknown["extra"] = True
    assert _code(unknown) == model.E_MALFORMED
    non_list = _spec([_node("n-a")])
    non_list["nodes"] = {"n-a": {}}
    assert _code(non_list) == model.E_MALFORMED
    non_object_node = _spec([_node("n-a")])
    non_object_node["nodes"] = ["n-a"]
    assert _code(non_object_node) == model.E_MALFORMED


def test_empty_graph_rejected() -> None:
    assert _code(_spec([])) == model.E_EMPTY_GRAPH


def test_invalid_id_rejected() -> None:
    assert _code(_spec([_node("A")])) == model.E_INVALID_ID
    assert _code(_spec([_node("n-a")], goal_id="x")) == model.E_INVALID_ID


def test_oversized_graph_rejected() -> None:
    nodes = [_node(f"n-{i:03d}") for i in range(model.MAX_NODES + 1)]
    assert _code(_spec(nodes)) == model.E_OVERSIZED


def test_oversized_field_rejected() -> None:
    node = _node("n-a")
    node["title"] = "x" * (model.MAX_TITLE_LEN + 1)
    assert _code(_spec([node])) == model.E_FIELD_OVERSIZED


def test_too_many_dependencies_rejected() -> None:
    deps = [f"n-d{i:03d}" for i in range(model.MAX_DEPENDENCIES_PER_NODE + 1)]
    nodes = [_node("n-a"), *[_node(d) for d in deps],
             _node("n-z", depends_on=deps)]
    assert _code(_spec(nodes)) == model.E_OVERSIZED


def test_edge_count_is_bounded() -> None:
    # A graph at the node cap but whose dependency declarations exceed the
    # edge cap is rejected before any identity is minted.
    nodes = [_node(f"n-{i:03d}") for i in range(model.MAX_NODES)]
    for index, node in enumerate(nodes[1:], start=1):
        node["depends_on"] = [f"n-{i:03d}" for i in range(index)]
    with pytest.raises(model.GraphValidationError) as exc:
        model.normalize_spec(_spec(nodes))
    assert exc.value.code == model.E_OVERSIZED


def test_invalid_priority_rejected() -> None:
    assert _code(_spec([_node("n-a", priority=model.MAX_PRIORITY + 1)])) \
        == model.E_INVALID_PRIORITY
    assert _code(_spec([_node("n-a", priority=-1)])) \
        == model.E_INVALID_PRIORITY
    bad = _node("n-a")
    bad["priority"] = True
    assert _code(_spec([bad])) == model.E_INVALID_PRIORITY
    bad_str = _node("n-a")
    bad_str["priority"] = "high"
    assert _code(_spec([bad_str])) == model.E_INVALID_PRIORITY


def test_invalid_resource_rejected() -> None:
    assert _code(_spec([_node("n-a", resources={"cpu_slots": 0})])) \
        == model.E_INVALID_RESOURCE
    assert _code(_spec([_node("n-a", resources={"gpu": "yes"})])) \
        == model.E_INVALID_RESOURCE
    assert _code(_spec([
        _node("n-a", resources={"gpu": False, "gpu_mem_bytes": 1024}),
    ])) == model.E_INVALID_RESOURCE
    assert _code(_spec([_node("n-a", resources={"unknown": 1})])) \
        == model.E_INVALID_RESOURCE


def test_invalid_budget_rejected() -> None:
    assert _code(_spec([_node("n-a", budgets={"subruns": 0})])) \
        == model.E_INVALID_BUDGET
    assert _code(_spec([
        _node("n-a", budgets={"repair_budget": 99}),
    ])) == model.E_INVALID_BUDGET
    assert _code(_spec([
        _node("n-a", budgets={"time_budget_s": 1}),
    ])) == model.E_INVALID_BUDGET
    assert _code(_spec([_node("n-a", budgets={"unknown": 1})])) \
        == model.E_INVALID_BUDGET


def test_acceptance_criteria_required() -> None:
    node = _node("n-a")
    node["acceptance_criteria"] = []
    assert _code(_spec([node])) == model.E_INVALID_ACCEPTANCE


def test_duplicate_criterion_id_rejected() -> None:
    node = _node("n-a", criteria=[_criterion("ac-1"), _criterion("ac-1")])
    assert _code(_spec([node])) == model.E_DUPLICATE_CRITERION


def test_duplicate_mission_reference_rejected() -> None:
    ref = {"mission_id": "m-one", "required": True}
    assert _code(_spec([
        _node("n-a", mission_ref=ref),
        _node("n-b", mission_ref=ref),
    ])) == model.E_DUPLICATE_MISSION_REF


def test_mission_ref_defaults_to_required_and_is_bounded() -> None:
    normalized = model.normalize_spec(_spec([
        _node("n-a", mission_ref={"mission_id": "m-one"}),
    ]))
    ref = normalized.nodes[0].mission_ref
    assert ref is not None and ref.required is True
    bad = _node("n-a", mission_ref={"mission_id": "m-one", "required": "yes"})
    assert _code(_spec([bad])) == model.E_MALFORMED


# --- exact preservation -------------------------------------------------------


def test_acceptance_criteria_preserved_exactly() -> None:
    criteria = [
        {
            "criterion_id": "ac-verify",
            "statement": "quality gate passes",
            "verification": "bash scripts/quality.sh",
        },
        {"criterion_id": "ac-doc", "statement": "ADR updated"},
    ]
    graph = _graph(_spec([_node("n-a", criteria=criteria)]))
    node = graph.nodes[0]
    assert [c.to_dict() for c in node.acceptance_criteria] == [
        {
            "criterion_id": "ac-doc",
            "statement": "ADR updated",
            "verification": None,
        },
        {
            "criterion_id": "ac-verify",
            "statement": "quality gate passes",
            "verification": "bash scripts/quality.sh",
        },
    ]


def test_resources_and_budgets_preserved_exactly() -> None:
    resources = {
        "cpu_slots": 4,
        "gpu": True,
        "gpu_mem_bytes": 8589934592,
        "exclusive": True,
        "model_heavy": True,
    }
    budgets = {
        "subruns": 8,
        "time_budget_s": 3600,
        "repair_budget": 2,
        "max_attempts": 3,
    }
    graph = _graph(_spec([
        _node("n-a", resources=resources, budgets=budgets),
    ]))
    node = graph.nodes[0]
    assert node.resources.to_dict() == resources
    assert node.budgets.to_dict() == budgets


# --- identity domains ---------------------------------------------------------


def test_identity_domains_are_distinct_from_mission_patch_domains() -> None:
    from trajectory_os.missions import identity as mission_identity

    assert identity.GRAPH_DOMAIN not in mission_identity.DOMAIN_IDS
    assert identity.SPEC_DOMAIN not in mission_identity.DOMAIN_IDS
    assert identity.domain_separation_holds({"goal_id": "g-test"})
    assert identity.is_valid_digest(_graph(_spec([_node("n-a")])).graph_id)
