"""Mission 014 — cross-mission reuse model/store/resolver unit tests.

Deterministic coverage for Issue #219: explicit reuse declaration
normalization, fail-closed graph validation, projection identity, consumption
identity, strict persistence/reconstruction and read-only resolution of
proven vs legacy vs missing/mismatched upstream evidence.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trajectory_os.graph import model as graph_model
from trajectory_os.graph import store as graph_store
from trajectory_os.graph.reuse import engine as reuse_engine
from trajectory_os.graph.reuse import identity as reuse_identity
from trajectory_os.graph.reuse import model as reuse_model
from trajectory_os.graph.reuse import resolver
from trajectory_os.graph.reuse import store as reuse_store
from trajectory_os.missions import model as mission_model
from trajectory_os.missions import orchestrator, semantic
from trajectory_os.missions.runner import SubrunResult


class AttestedRunner:
    def run(self, request: Any) -> SubrunResult:
        return SubrunResult(
            0, mission_model.CR_COMPLETED,
            semantic_status=semantic.STATUS_SUCCESS,
            attestation=semantic.ATTESTATION_VERIFIED)


class LegacyRunner:
    def run(self, request: Any) -> SubrunResult:
        return SubrunResult(0, mission_model.CR_COMPLETED)


def _criterion() -> dict[str, str]:
    return {"criterion_id": "ac-1", "statement": "done"}


def _create_mission(root: str, mission_id: str) -> None:
    orchestrator.create_mission(root, orchestrator.MissionConfig(
        mission_id=mission_id,
        objective="reuse unit mission",
        phase_specs=orchestrator.default_phase_specs(
            {kind: ("true",) for kind in mission_model.CANONICAL_SEQUENCE}),
        baseline_revision="base",
    ))


def _producer_node(*, mission: str = "m-prod", node_id: str = "n-prod",
                   priority: int = 90) -> dict[str, Any]:
    return {
        "node_id": node_id, "title": node_id, "priority": priority,
        "depends_on": [], "acceptance_criteria": [_criterion()],
        "mission_ref": {"mission_id": mission, "required": True},
        "resources": {"cpu_slots": 1},
    }


def _consumer_node(*, mission: str = "m-cons", node_id: str = "n-cons",
                   deps: list[str] | None = None,
                   input_id: str = "up-validate",
                   phase_id: str = "validate",
                   required: bool = True,
                   min_trust: str = graph_model.TRUST_PROVEN,
                   expected: str | None = None,
                   producer_mission: str | None = None) -> dict[str, Any]:
    reuse_input: dict[str, Any] = {
        "input_id": input_id,
        "producer_node_id": deps[0] if deps else "n-prod",
        "evidence_kind": graph_model.EK_PHASE_EVIDENCE,
        "phase_id": phase_id,
        "required": required,
        "min_trust": min_trust,
    }
    if expected is not None:
        reuse_input["expected_sha256"] = expected
    if producer_mission is not None:
        reuse_input["producer_mission_id"] = producer_mission
    return {
        "node_id": node_id, "title": node_id, "priority": 50,
        "depends_on": list(deps if deps is not None else ["n-prod"]),
        "acceptance_criteria": [_criterion()],
        "mission_ref": {"mission_id": mission, "required": True},
        "reuse_inputs": [reuse_input],
        "resources": {"cpu_slots": 1},
    }


def _spec(nodes: list[dict[str, Any]], *, goal_id: str = "g-reuse"
          ) -> dict[str, Any]:
    return {"schema_version": 1, "goal_id": goal_id, "objective": "reuse",
            "nodes": nodes}


def _prepare(tmp_path: Path, nodes: list[dict[str, Any]],
             *, goal_id: str = "g-reuse") -> str:
    root = str(tmp_path / "root")
    for node in nodes:
        ref = node.get("mission_ref")
        if ref is not None:
            _create_mission(root, ref["mission_id"])
    graph_store.create_graph(root, _spec(nodes, goal_id=goal_id),
                             repo_root=str(tmp_path), baseline_revision="base")
    return root


# ---------------------------------------------------------------------------
# declaration normalization / graph fail-closed validation
# ---------------------------------------------------------------------------


def test_reuse_input_roundtrip_and_optional_identity() -> None:
    item = graph_model.ReuseInput(
        input_id="up-validate", producer_node_id="n-prod",
        phase_id="validate", required=True, min_trust=graph_model.TRUST_PROVEN)
    assert graph_model.ReuseInput.from_dict(item.to_dict(), "i") == item
    # A node without reuse inputs omits the field so M012 identity is stable.
    node = graph_model.GraphNode(
        node_id="n", title="n", priority=1, depends_on=(),
        acceptance_criteria=(), mission_ref=None,
        resources=graph_model.NodeResources(), budgets=graph_model.NodeBudget())
    assert "reuse_inputs" not in node.to_dict()


def test_graph_identity_covers_declared_reuse(tmp_path: Path) -> None:
    plain = graph_store.build_graph(spec_doc=_spec([_producer_node()]))
    with_reuse = graph_store.build_graph(
        spec_doc=_spec([_producer_node(), _consumer_node()]))
    assert plain.graph_id != with_reuse.graph_id
    assert with_reuse.node_map()["n-cons"].reuse_inputs[0].phase_id == "validate"


def test_graph_rejects_producer_that_is_not_a_dependency() -> None:
    node = _consumer_node(deps=[])
    with pytest.raises(graph_model.GraphValidationError) as excinfo:
        graph_store.build_graph(spec_doc=_spec([_producer_node(), node]))
    assert excinfo.value.code == graph_model.E_REUSE_PRODUCER


def test_graph_rejects_duplicate_reuse_input_id() -> None:
    consumer = _consumer_node()
    consumer["reuse_inputs"].append(dict(consumer["reuse_inputs"][0]))
    with pytest.raises(graph_model.GraphValidationError) as excinfo:
        graph_store.build_graph(
            spec_doc=_spec([_producer_node(), consumer]))
    assert excinfo.value.code == graph_model.E_DUPLICATE_REUSE_INPUT


def test_graph_rejects_producer_mission_identity_mismatch() -> None:
    consumer = _consumer_node(producer_mission="m-other")
    with pytest.raises(graph_model.GraphValidationError) as excinfo:
        graph_store.build_graph(
            spec_doc=_spec([_producer_node(), consumer]))
    assert excinfo.value.code == graph_model.E_REUSE_REFERENCE


def test_graph_accepts_matching_producer_mission_identity() -> None:
    consumer = _consumer_node(producer_mission="m-prod")
    graph = graph_store.build_graph(
        spec_doc=_spec([_producer_node(), consumer]))
    assert (graph.node_map()["n-cons"].reuse_inputs[0].producer_mission_id
            == "m-prod")


def test_graph_rejects_malformed_reuse_fields() -> None:
    consumer = _consumer_node()
    consumer["reuse_inputs"][0]["expected_sha256"] = "not-a-digest"
    with pytest.raises(graph_model.GraphValidationError) as excinfo:
        graph_store.build_graph(
            spec_doc=_spec([_producer_node(), consumer]))
    assert excinfo.value.code == graph_model.E_INVALID_REUSE
    consumer = _consumer_node()
    consumer["reuse_inputs"][0]["evidence_kind"] = "PROCESS_ENV"
    with pytest.raises(graph_model.GraphValidationError) as excinfo:
        graph_store.build_graph(
            spec_doc=_spec([_producer_node(), consumer]))
    assert excinfo.value.code == graph_model.E_INVALID_REUSE


# ---------------------------------------------------------------------------
# projection / consumption identity
# ---------------------------------------------------------------------------


def _status(consumer: str = "n-cons", *, status: str = reuse_model.ST_RESOLVED,
            reason: str = reuse_model.RR_RESOLVED, required: bool = True,
            content: str | None = None) -> reuse_model.ReuseInputStatus:
    digest = content or ("a" * 64)
    return reuse_model.ReuseInputStatus(
        consumer_node_id=consumer, consumer_mission_id="m-cons",
        input_id="up-validate", producer_node_id="n-prod",
        producer_mission_id="m-prod", evidence_kind="PHASE_EVIDENCE",
        phase_id="validate", required=required,
        min_trust=reuse_model.TRUST_PROVEN, status=status, reason=reason,
        trust=(reuse_model.TRUST_PROVEN if status == reuse_model.ST_RESOLVED
               else None),
        artifact_content_sha256=(digest if status == reuse_model.ST_RESOLVED
                                 else None),
        expected_sha256=None)


def test_projection_identity_is_deterministic_and_tamper_evident() -> None:
    projection = reuse_model.ReuseProjection.build(
        goal_id="g", graph_id="b" * 64, spec_sha256="c" * 64,
        inputs=[_status()])
    assert projection.projection_id == projection.compute_projection_id()
    assert reuse_model.ReuseProjection.from_dict(
        projection.to_dict(), "p") == projection
    tampered = projection.to_dict()
    tampered["inputs"][0]["artifact_content_sha256"] = "d" * 64
    with pytest.raises(reuse_model.ReuseValidationError) as excinfo:
        reuse_model.ReuseProjection.from_dict(tampered, "p")
    assert excinfo.value.code in (
        reuse_model.E_IDENTITY_MISMATCH, reuse_model.E_MALFORMED)


def test_projection_blocking_and_reason_selection() -> None:
    projection = reuse_model.ReuseProjection.build(
        goal_id="g", graph_id="b" * 64, spec_sha256="c" * 64,
        inputs=[_status(status=reuse_model.ST_UNRESOLVED,
                        reason=reuse_model.RR_PRODUCER_NOT_PROVEN),
                _status(consumer="n-down2", status=reuse_model.ST_RESOLVED,
                        reason=reuse_model.RR_RESOLVED)])
    assert projection.blocked is True
    assert projection.blocked_nodes() == ("n-cons",)
    assert projection.blocking_reason("n-cons") == \
        reuse_model.RR_PRODUCER_NOT_PROVEN
    assert projection.blocking_reason("n-down2") is None
    assert projection.counts()["blocking"] == 1


def test_consumption_identity_roundtrip() -> None:
    record = reuse_model.ConsumptionRecord.build(
        goal_id="g", graph_id="b" * 64, projection_id="e" * 64,
        input_status=_status(), consumed_at="t")
    assert reuse_model.ConsumptionRecord.from_dict(
        record.to_dict(), "c") == record
    tampered = record.to_dict()
    tampered["artifact_content_sha256"] = "f" * 64
    with pytest.raises(reuse_model.ReuseValidationError):
        reuse_model.ConsumptionRecord.from_dict(tampered, "c")


def test_consumption_requires_resolved_input() -> None:
    with pytest.raises(reuse_model.ReuseValidationError):
        reuse_model.ConsumptionRecord.build(
            goal_id="g", graph_id="b" * 64, projection_id="e" * 64,
            input_status=_status(status=reuse_model.ST_UNRESOLVED,
                                 reason=reuse_model.RR_PRODUCER_NOT_PROVEN),
            consumed_at="t")


def test_input_status_rejects_contradictory_shape() -> None:
    doc = _status().to_dict()
    doc["reason"] = reuse_model.RR_PRODUCER_NOT_PROVEN
    with pytest.raises(reuse_model.ReuseValidationError) as excinfo:
        reuse_model.ReuseInputStatus.from_dict(doc, "i")
    assert excinfo.value.code == reuse_model.E_CONTRADICTORY
    doc = _status(status=reuse_model.ST_REJECTED,
                  reason=reuse_model.RR_RESOLVED).to_dict()
    doc["reason"] = reuse_model.RR_RESOLVED
    with pytest.raises(reuse_model.ReuseValidationError) as excinfo:
        reuse_model.ReuseInputStatus.from_dict(doc, "i")
    assert excinfo.value.code == reuse_model.E_CONTRADICTORY


def test_reuse_identity_domains_are_distinct() -> None:
    assert len(reuse_identity.DOMAIN_IDS) == 3
    assert reuse_identity.ARTIFACT_DOMAIN not in (
        reuse_identity.PROJECTION_DOMAIN, reuse_identity.CONSUMPTION_DOMAIN)
    assert not reuse_identity.is_valid_digest("x" * 64)
    assert reuse_identity.is_valid_digest("a" * 64)


# ---------------------------------------------------------------------------
# resolver against canonical mission evidence
# ---------------------------------------------------------------------------


def test_resolver_proven_legacy_and_mismatch(tmp_path: Path) -> None:
    root = _prepare(tmp_path, [_producer_node(), _consumer_node()])
    # Not yet proven -> unresolved, never admitted.
    projection = resolver.build_projection(root, _load(root))
    assert projection.inputs[0].reason == reuse_model.RR_PRODUCER_NOT_PROVEN
    assert projection.inputs[0].status == reuse_model.ST_UNRESOLVED

    orchestrator.run_mission(root, "m-prod", AttestedRunner())
    projection = resolver.build_projection(root, _load(root))
    item = projection.inputs[0]
    assert item.status == reuse_model.ST_RESOLVED
    assert item.trust == reuse_model.TRUST_PROVEN
    assert item.artifact_content_sha256 is not None
    assert item.producer_mission_id == "m-prod"
    assert item.artifact_attestation == semantic.ATTESTATION_VERIFIED


def test_resolver_legacy_is_never_silently_promoted(tmp_path: Path) -> None:
    root = _prepare(tmp_path, [_producer_node(), _consumer_node()])
    orchestrator.run_mission(root, "m-prod", LegacyRunner())
    projection = resolver.build_projection(root, _load(root))
    item = projection.inputs[0]
    assert item.status == reuse_model.ST_REJECTED
    assert item.reason == reuse_model.RR_LEGACY_NOT_PROMOTABLE
    assert item.trust == reuse_model.TRUST_LEGACY
    assert projection.blocked is True


def test_resolver_legacy_consumer_accepts_legacy_trust(tmp_path: Path) -> None:
    root = _prepare(tmp_path, [
        _producer_node(),
        _consumer_node(min_trust=graph_model.TRUST_LEGACY),
    ])
    orchestrator.run_mission(root, "m-prod", LegacyRunner())
    projection = resolver.build_projection(root, _load(root))
    item = projection.inputs[0]
    assert item.status == reuse_model.ST_RESOLVED
    assert item.trust == reuse_model.TRUST_LEGACY


def test_resolver_expected_identity_mismatch(tmp_path: Path) -> None:
    root = _prepare(tmp_path, [
        _producer_node(),
        _consumer_node(expected="0" * 64),
    ])
    orchestrator.run_mission(root, "m-prod", AttestedRunner())
    projection = resolver.build_projection(root, _load(root))
    item = projection.inputs[0]
    assert item.status == reuse_model.ST_REJECTED
    assert item.reason == reuse_model.RR_IDENTITY_MISMATCH
    assert item.artifact_content_sha256 is not None


def test_resolver_missing_phase_is_unresolved(tmp_path: Path) -> None:
    root = _prepare(tmp_path, [
        _producer_node(),
        _consumer_node(phase_id="absent"),
    ])
    orchestrator.run_mission(root, "m-prod", AttestedRunner())
    projection = resolver.build_projection(root, _load(root))
    item = projection.inputs[0]
    assert item.status == reuse_model.ST_UNRESOLVED
    assert item.reason == reuse_model.RR_EVIDENCE_MISSING


def test_resolver_implicit_state_is_never_trusted(tmp_path: Path) -> None:
    # A graph with no reuse declaration resolves to an empty projection.
    root = _prepare(tmp_path, [_producer_node()])
    orchestrator.run_mission(root, "m-prod", AttestedRunner())
    projection = resolver.build_projection(root, _load(root))
    assert projection.inputs == ()
    assert projection.blocked is False


# ---------------------------------------------------------------------------
# store: persistence + strict reconstruction
# ---------------------------------------------------------------------------


def test_store_resolve_record_and_reconstruct(tmp_path: Path) -> None:
    root = _prepare(tmp_path, [_producer_node(), _consumer_node()])
    orchestrator.run_mission(root, "m-prod", AttestedRunner())
    result = reuse_engine.resolve_and_record(root, "g-reuse", consumed_at="t1")
    assert result.persisted is True
    assert len(result.appended) == 1
    assert reuse_engine.resolve_and_record(
        root, "g-reuse", consumed_at="t2").appended == ()
    reconstructed = reuse_engine.reconstruct(root, "g-reuse")
    assert reconstructed.projection.projection_id == \
        result.projection.projection_id
    assert len(reconstructed.consumptions) == 1


def test_store_missing_projection_fails_closed(tmp_path: Path) -> None:
    root = _prepare(tmp_path, [_producer_node(), _consumer_node()])
    orchestrator.run_mission(root, "m-prod", AttestedRunner())
    with pytest.raises(reuse_model.ReuseValidationError) as excinfo:
        reuse_engine.reconstruct(root, "g-reuse")
    assert excinfo.value.code == reuse_model.E_MALFORMED


def test_store_stale_evidence_fails_closed(tmp_path: Path) -> None:
    root = _prepare(tmp_path, [_producer_node(), _consumer_node()])
    orchestrator.run_mission(root, "m-prod", AttestedRunner())
    reuse_engine.resolve_and_record(root, "g-reuse", consumed_at="t1")
    paths = reuse_store.reuse_paths(root, "g-reuse")
    from trajectory_os.missions import store as mission_store
    ep = mission_store.evidence_path(
        mission_store.mission_paths(root, "m-prod"), "validate")
    document = json.loads(ep.read_text(encoding="utf-8"))
    document["attempt"] = 999
    ep.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(reuse_model.ReuseValidationError) as excinfo:
        reuse_engine.reconstruct(root, "g-reuse")
    assert excinfo.value.code == reuse_model.E_STALE
    assert paths["projection"].is_file()  # upstream file unmodified


def test_store_untracked_consumption_fails_closed(tmp_path: Path) -> None:
    root = _prepare(tmp_path, [_producer_node(), _consumer_node()])
    orchestrator.run_mission(root, "m-prod", AttestedRunner())
    result = reuse_engine.resolve_and_record(root, "g-reuse", consumed_at="t1")
    paths = reuse_store.reuse_paths(root, "g-reuse")
    record = result.projection.inputs[0]
    unknown = reuse_model.ConsumptionRecord.build(
        goal_id=result.projection.goal_id,
        graph_id=result.projection.graph_id,
        projection_id=result.projection.projection_id,
        input_status=reuse_model.ReuseInputStatus(
            **{**record.to_dict(), "consumer_node_id": "n-ghost"}),
        consumed_at="t2")
    reuse_store.append_consumption(paths, unknown)
    with pytest.raises(reuse_model.ReuseValidationError) as excinfo:
        reuse_engine.reconstruct(root, "g-reuse")
    assert excinfo.value.code == reuse_model.E_UNTRACKED_CONSUMPTION


def _load(root: str) -> graph_model.GoalGraph:
    graph, _ = graph_store.load_graph(root, "g-reuse")
    return graph
