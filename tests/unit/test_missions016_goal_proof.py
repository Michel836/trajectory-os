"""Mission 016 — goal-proof model/identity/evidence unit tests (pure)."""

from __future__ import annotations

import pytest

from trajectory_os.graph import model as graph_model
from trajectory_os.graph import store as graph_store
from trajectory_os.graph.proof import evidence as proof_evidence
from trajectory_os.graph.proof import identity as proof_identity
from trajectory_os.graph.proof import model

# --- identity -----------------------------------------------------------------


def test_goal_proof_domains_are_distinct() -> None:
    payload = {"goal": "g", "n": 1}
    digests = {proof_identity.digest(domain, payload)
               for domain in proof_identity.DOMAIN_IDS}
    assert len(digests) == len(proof_identity.DOMAIN_IDS)
    for domain in proof_identity.DOMAIN_IDS:
        assert proof_identity.digest(domain, payload)
    with pytest.raises(ValueError):
        proof_identity.digest("trajectory-os.nope.v1", payload)


def test_goal_proof_domains_distinct_from_prior_missions() -> None:
    from trajectory_os.graph import identity as graph_identity
    from trajectory_os.graph.replan import identity as replan_identity
    from trajectory_os.graph.reuse import identity as reuse_identity
    from trajectory_os.graph.scheduler import identity as sched_identity
    from trajectory_os.missions import identity as patch_identity

    for domain in proof_identity.DOMAIN_IDS:
        assert domain not in patch_identity.DOMAIN_IDS
        assert domain not in graph_identity.DOMAIN_IDS
        assert domain not in sched_identity.DOMAIN_IDS
        assert domain not in reuse_identity.DOMAIN_IDS
        assert domain not in replan_identity.DOMAIN_IDS


# --- criterion binding --------------------------------------------------------


def _criterion(criterion_id: str = "ac-1",
               verification: str | None = None) \
        -> graph_model.AcceptanceCriterion:
    return graph_model.AcceptanceCriterion(
        criterion_id=criterion_id, statement="done",
        verification=verification)


def _chain(*, proven: bool, legacy: bool = False,
           phases: tuple[proof_evidence.PhaseEvidence, ...] = ()) \
        -> proof_evidence.MissionChain:
    return proof_evidence.MissionChain(
        mission_id="m-1", resolved=True, error=None, state="COMPLETE",
        reason="COMPLETE", proven_complete=proven, legacy=legacy,
        phases=phases)


def _phase(phase_id: str, *, exact: bool = True, state: str = "PASSED",
           attestation: str | None = "VERIFIED") -> proof_evidence.PhaseEvidence:
    return proof_evidence.PhaseEvidence(
        phase_id=phase_id, kind="VALIDATE", state=state, subrun_id="s-1",
        classification="COMPLETED", semantic_status="SUCCESS",
        attestation=attestation,
        trust=model.TRUST_PROVEN if exact else model.TRUST_LEGACY, exact=exact)


def test_bind_criterion_unbound_is_unproven() -> None:
    binding = proof_evidence.bind_criterion(
        node_id="n-1", criterion=_criterion(), chain=None)
    assert binding.status == model.CS_UNPROVEN
    assert binding.reason == model.R_CRITERION_UNBOUND
    assert not binding.proven
    assert binding.binding_id == binding.compute_binding_id()


def test_bind_criterion_whole_mission_proven() -> None:
    chain = _chain(proven=True, phases=(_phase("validate"), _phase("review")))
    binding = proof_evidence.bind_criterion(
        node_id="n-1", criterion=_criterion(), chain=chain)
    assert binding.status == model.CS_PROVEN
    assert binding.trust == model.TRUST_PROVEN
    assert len(binding.evidence) == 2
    assert binding.evidence_sha256


def test_bind_criterion_explicit_phase_binding() -> None:
    chain = _chain(proven=True, phases=(_phase("validate"),))
    binding = proof_evidence.bind_criterion(
        node_id="n-1", criterion=_criterion(verification="validate"),
        chain=chain)
    assert binding.status == model.CS_PROVEN
    assert binding.phase_id == "validate"
    assert len(binding.evidence) == 1


def test_bind_criterion_ambiguous_mapping_fails_closed() -> None:
    chain = _chain(proven=True, phases=(_phase("validate"),))
    binding = proof_evidence.bind_criterion(
        node_id="n-1", criterion=_criterion(verification="missing-phase"),
        chain=chain)
    assert binding.status == model.CS_INVALID
    assert binding.reason == model.R_CRITERION_AMBIGUOUS
    assert not binding.proven


def test_bind_criterion_explicit_non_passed_phase_fails_closed() -> None:
    failed = proof_evidence.PhaseEvidence(
        phase_id="validate", kind="VALIDATE", state="FAILED",
        subrun_id="s-1", classification="FAILED", semantic_status=None,
        attestation=None, trust=model.TRUST_NONE, exact=False)
    chain = proof_evidence.MissionChain(
        mission_id="m-1", resolved=True, error=None, state="COMPLETE",
        reason="COMPLETE", proven_complete=False, legacy=False,
        phases=(failed,))
    binding = proof_evidence.bind_criterion(
        node_id="n-1", criterion=_criterion(verification="validate"),
        chain=chain)
    assert binding.status == model.CS_UNPROVEN
    assert not binding.proven


def test_bind_criterion_impossible_evidence_is_contradictory() -> None:
    # Trusted complete but no contributing PASSED phase evidence is an
    # impossible/contradictory state; it must never be silently unproven.
    chain = _chain(proven=True, phases=())
    binding = proof_evidence.bind_criterion(
        node_id="n-1", criterion=_criterion(), chain=chain)
    assert binding.status == model.CS_CONTRADICTORY
    assert binding.reason == model.R_MISSION_EVIDENCE_MISSING


def test_bind_criterion_legacy_fails_closed() -> None:
    chain = _chain(proven=True, legacy=True,
                   phases=(_phase("review", exact=False),))
    binding = proof_evidence.bind_criterion(
        node_id="n-1", criterion=_criterion(), chain=chain)
    assert binding.status == model.CS_CONTRADICTORY
    assert binding.reason == model.R_MISSION_LEGACY_EVIDENCE
    assert binding.trust == model.TRUST_LEGACY


# --- goal proof identity and validation ---------------------------------------


def _graph() -> graph_model.GoalGraph:
    spec = {
        "schema_version": 1, "goal_id": "g-1", "objective": "goal",
        "nodes": [
            {"node_id": "n-1", "title": "n1", "priority": 10,
             "depends_on": [],
             "acceptance_criteria": [{"criterion_id": "ac-1",
                                      "statement": "done"}]},
            {"node_id": "n-2", "title": "n2", "priority": 5,
             "depends_on": ["n-1"],
             "acceptance_criteria": [{"criterion_id": "ac-1",
                                      "statement": "done"}]},
        ],
    }
    return graph_store.build_graph(spec_doc=spec, repo_root="r",
                                   baseline_revision="base",
                                   created_at="2026-01-01T00:00:00Z")


def _binding(node_id: str, *, proven: bool) -> model.CriterionBinding:
    if proven:
        return model.CriterionBinding.build(
            node_id=node_id, criterion_id="ac-1", statement="done",
            verification=None, required=True, status=model.CS_PROVEN,
            reason=model.R_ALL_CRITERIA_PROVEN, trust=model.TRUST_PROVEN,
            mission_id="m-1", mission_state="COMPLETE",
            mission_reason="COMPLETE", mission_proven=True, phase_id=None,
            evidence=(model.EvidenceRef(
                node_id=node_id, mission_id="m-1", phase_id="validate",
                subrun_id="s-1", classification="COMPLETED",
                semantic_status="SUCCESS", attestation="VERIFIED",
                trust=model.TRUST_PROVEN, exact=True),))
    return model.CriterionBinding.build(
        node_id=node_id, criterion_id="ac-1", statement="done",
        verification=None, required=True, status=model.CS_UNPROVEN,
        reason=model.R_CRITERION_UNPROVEN, trust=model.TRUST_NONE,
        mission_id="m-1", mission_state="RUNNING", mission_reason="OK",
        mission_proven=False, phase_id=None, evidence=())


def _gate() -> model.GateProof:
    return model.GateProof(state="GO_COMMIT", reason="READY_FOR_COMMIT",
                           human_action_required=True,
                           next_human_action="record GO COMMIT")


def _build(*, proven: bool, risks: tuple[model.Risk, ...] = ()) \
        -> model.GoalProof:
    graph = _graph()
    node = model.NodeProof(
        node_id="n-1", title="n1", priority=10, depends_on=(),
        state="COMPLETE" if proven else "IN_PROGRESS", reason="X",
        own_status="COMPLETE", eligible=False, mission_id="m-1",
        mission_state="COMPLETE" if proven else "RUNNING",
        mission_reason="COMPLETE" if proven else "OK",
        mission_proven=proven, criteria_total=1, criteria_proven=int(proven))
    counts = model.GoalCounts(
        nodes_total=1, nodes_complete=int(proven), nodes_ready=0,
        nodes_in_progress=0 if proven else 1, nodes_blocked=0,
        nodes_unresolved=0, nodes_invalid=0, criteria_total=1,
        criteria_proven=int(proven), criteria_unproven=int(not proven),
        criteria_contradictory=0, criteria_invalid=0, missions_total=1,
        missions_proven=int(proven), missions_incomplete=int(not proven),
        missions_missing=0, risks_total=len(risks),
        risks_unresolved=0, risks_blocked=0, risks_stale=0, risks_legacy=0,
        risks_unproven=len(risks), risks_contradictory=0, risks_invalid=0,
        risks_impossible=0, replans=0, supersessions=0)
    return model.GoalProof.build(
        goal_id=graph.goal_id, objective=graph.objective,
        graph_id=graph.graph_id, spec_sha256=graph.spec_sha256,
        generation=None, replan={"accepted": 0}, nodes=(node,),
        critical_path=("n-1",), criteria=(_binding("n-1", proven=proven),),
        reuse={"declared": 0}, scheduler={"present": False},
        resources={"present": False}, risks=risks, gate=_gate(),
        counts=counts)


def test_goal_proof_complete_and_roundtrip() -> None:
    proof = _build(proven=True)
    assert proof.complete
    assert proof.final_state == model.GS_COMPLETE
    assert proof.final_reason == model.R_ALL_CRITERIA_PROVEN
    restored = model.GoalProof.from_dict(proof.to_dict())
    assert restored.proof_id == proof.proof_id
    assert restored.to_dict() == proof.to_dict()


def test_goal_proof_incomplete_with_unproven_criterion() -> None:
    proof = _build(proven=False)
    assert not proof.complete
    assert proof.final_state == model.GS_INCOMPLETE
    assert proof.final_reason == model.R_CRITERION_UNPROVEN


def test_goal_proof_complete_rejected_with_risk() -> None:
    risk = model.Risk.build(risk_class=model.RISK_LEGACY,
                            reason=model.R_MISSION_LEGACY_EVIDENCE,
                            subject="n-1", detail="legacy")
    proof = _build(proven=True, risks=(risk,))
    assert not proof.complete
    assert proof.final_reason == model.R_MISSION_LEGACY_EVIDENCE


def test_goal_proof_identity_mismatch_fails_closed() -> None:
    proof = _build(proven=True)
    tampered = proof.to_dict()
    tampered["goal_id"] = "g-other"
    with pytest.raises(model.GoalProofError) as excinfo:
        model.GoalProof.from_dict(tampered)
    assert excinfo.value.code == model.E_IDENTITY_MISMATCH


def test_goal_proof_unsupported_version_fails_closed() -> None:
    proof = _build(proven=True)
    tampered = proof.to_dict()
    tampered["schema_version"] = 99
    with pytest.raises(model.GoalProofError) as excinfo:
        model.GoalProof.from_dict(tampered)
    assert excinfo.value.code == model.E_UNSUPPORTED_VERSION


def test_risk_sort_is_deterministic() -> None:
    risks = [
        model.Risk.build(risk_class=model.RISK_UNPROVEN,
                         reason=model.R_NODE_INCOMPLETE, subject="b",
                         detail="d"),
        model.Risk.build(risk_class=model.RISK_IMPOSSIBLE,
                         reason=model.R_IMPOSSIBLE_STATE, subject="a",
                         detail="d"),
    ]
    proof = _build(proven=True, risks=tuple(risks))
    assert [risk.risk_class for risk in proof.risks] == [
        model.RISK_IMPOSSIBLE, model.RISK_UNPROVEN]


# --- critical path ------------------------------------------------------------


def test_critical_path_deterministic() -> None:
    graph = _graph()
    assert proof_evidence.critical_path(graph) == ("n-1", "n-2")
    # Reversing node declaration order must not change the path.
    spec = {
        "schema_version": 1, "goal_id": "g-1", "objective": "goal",
        "nodes": [
            {"node_id": "n-2", "title": "n2", "priority": 5,
             "depends_on": ["n-1"],
             "acceptance_criteria": [{"criterion_id": "ac-1",
                                      "statement": "done"}]},
            {"node_id": "n-1", "title": "n1", "priority": 10,
             "depends_on": [],
             "acceptance_criteria": [{"criterion_id": "ac-1",
                                      "statement": "done"}]},
        ],
    }
    other = graph_store.build_graph(spec_doc=spec, repo_root="r",
                                    baseline_revision="base",
                                    created_at="2026-01-01T00:00:00Z")
    assert proof_evidence.critical_path(other) == ("n-1", "n-2")
