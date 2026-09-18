"""Mission 014 — deterministic read-only cross-mission evidence resolution.

This module resolves explicit reuse declarations against the canonical
mission store. It is strictly read-only: no mission document is written,
terminalized, repaired or promoted, and no implicit filesystem, process or
environment state ever becomes trusted cross-mission state. A reuse input is
resolved to a bounded, exact artifact identity or fails closed with a stable
reason code.

Only **explicit** declarations from the canonical M012 graph are resolved.
Anything not declared is never discovered, guessed or inferred.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from trajectory_os.graph import evidence as graph_evidence
from trajectory_os.graph import model as graph_model
from trajectory_os.graph.reuse import identity as reuse_identity
from trajectory_os.graph.reuse import model as reuse_model
from trajectory_os.missions import model as mission_model
from trajectory_os.missions import semantic
from trajectory_os.missions import store as mission_store


def artifact_payload(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Canonical, bounded identity payload for one phase-evidence artifact.

    The payload is the *parsed, canonical* evidence document plus its
    authoritative proving-subrun identity. It is the exact content identity a
    consumer binds to; it is never the consumer's own success proof.
    """
    worktree = evidence.get("worktree")
    patch_sha: str | None = None
    if isinstance(worktree, dict):
        raw_patch = worktree.get("worktree_patch_sha256")
        if isinstance(raw_patch, str):
            patch_sha = raw_patch
    last = evidence.get("last_subrun")
    subrun: dict[str, Any] = last if isinstance(last, dict) else {}
    return {
        "phase": evidence.get("phase"),
        "kind": evidence.get("kind"),
        "state": evidence.get("state"),
        "attempt": evidence.get("attempt"),
        "evidence": evidence,
        "subrun_id": subrun.get("subrun_id"),
        "classification": subrun.get("classification"),
        "attestation": subrun.get("attestation"),
        "semantic_status": subrun.get("semantic_status"),
        "worktree_patch_sha256": patch_sha,
    }


def _status(
    consumer: graph_model.GraphNode,
    reuse_input: graph_model.ReuseInput,
    *,
    status: str,
    reason: str,
    trust: str | None = None,
    producer_mission_id: str | None = None,
    artifact_content_sha256: str | None = None,
    artifact_patch_sha256: str | None = None,
    artifact_attestation: str | None = None,
    artifact_subrun_id: str | None = None,
    artifact_semantic_status: str | None = None,
) -> reuse_model.ReuseInputStatus:
    consumer_mission = (consumer.mission_ref.mission_id
                        if consumer.mission_ref is not None else None)
    return reuse_model.ReuseInputStatus(
        consumer_node_id=consumer.node_id,
        consumer_mission_id=consumer_mission,
        input_id=reuse_input.input_id,
        producer_node_id=reuse_input.producer_node_id,
        producer_mission_id=producer_mission_id,
        evidence_kind=reuse_input.evidence_kind,
        phase_id=reuse_input.phase_id,
        required=reuse_input.required,
        min_trust=reuse_input.min_trust,
        status=status,
        reason=reason,
        trust=trust,
        artifact_content_sha256=artifact_content_sha256,
        artifact_patch_sha256=artifact_patch_sha256,
        artifact_attestation=artifact_attestation,
        artifact_subrun_id=artifact_subrun_id,
        artifact_semantic_status=artifact_semantic_status,
        expected_sha256=reuse_input.expected_sha256,
    )


def _producer_mission_id(producer: graph_model.GraphNode) -> str | None:
    if producer.mission_ref is None:
        return None
    return producer.mission_ref.mission_id


def resolve_input(
    root: str,
    graph: graph_model.GoalGraph,
    consumer: graph_model.GraphNode,
    reuse_input: graph_model.ReuseInput,
    evidence: Mapping[str, graph_evidence.MissionEvidenceRecord],
) -> reuse_model.ReuseInputStatus:
    """Resolve one explicit reuse input read-only (fail closed)."""
    node_map = graph.node_map()
    producer = node_map.get(reuse_input.producer_node_id)
    if producer is None:
        return _status(consumer, reuse_input, status=reuse_model.ST_UNRESOLVED,
                       reason=reuse_model.RR_PRODUCER_MISSING)
    if reuse_input.producer_node_id not in consumer.depends_on:
        return _status(consumer, reuse_input, status=reuse_model.ST_REJECTED,
                       reason=reuse_model.RR_DEPENDENCY_RELATION)
    producer_mission = _producer_mission_id(producer)
    if producer_mission is None:
        return _status(consumer, reuse_input, status=reuse_model.ST_UNRESOLVED,
                       reason=reuse_model.RR_PRODUCER_UNRESOLVED)
    if (reuse_input.producer_mission_id is not None
            and reuse_input.producer_mission_id != producer_mission):
        return _status(consumer, reuse_input, status=reuse_model.ST_REJECTED,
                       reason=reuse_model.RR_IDENTITY_MISMATCH,
                       producer_mission_id=producer_mission)
    record = evidence.get(producer_mission)
    if record is None:
        return _status(consumer, reuse_input, status=reuse_model.ST_UNRESOLVED,
                       reason=reuse_model.RR_PRODUCER_UNRESOLVED,
                       producer_mission_id=producer_mission)
    if record.error is not None:
        rejected = record.error in graph_evidence.INVALID_ERRORS
        return _status(
            consumer, reuse_input,
            status=(reuse_model.ST_REJECTED if rejected
                    else reuse_model.ST_UNRESOLVED),
            reason=reuse_model.RR_PRODUCER_UNRESOLVED,
            producer_mission_id=producer_mission)
    if record.state in (mission_model.MS_FAILED, mission_model.MS_BLOCKED):
        return _status(consumer, reuse_input, status=reuse_model.ST_REJECTED,
                       reason=reuse_model.RR_PRODUCER_FAILED,
                       producer_mission_id=producer_mission)
    if not record.proven_complete:
        return _status(consumer, reuse_input, status=reuse_model.ST_UNRESOLVED,
                       reason=reuse_model.RR_PRODUCER_NOT_PROVEN,
                       producer_mission_id=producer_mission)
    try:
        mission, paths = mission_store.load_mission(root, producer_mission)
    except mission_store.MissionNotFound:
        return _status(consumer, reuse_input, status=reuse_model.ST_UNRESOLVED,
                       reason=reuse_model.RR_PRODUCER_UNRESOLVED,
                       producer_mission_id=producer_mission)
    except mission_store.MalformedMissionError:
        return _status(consumer, reuse_input, status=reuse_model.ST_REJECTED,
                       reason=reuse_model.RR_EVIDENCE_MALFORMED,
                       producer_mission_id=producer_mission)
    phase = next((p for p in mission.phases
                  if p.phase_id == reuse_input.phase_id), None)
    if phase is None:
        return _status(consumer, reuse_input, status=reuse_model.ST_UNRESOLVED,
                       reason=reuse_model.RR_EVIDENCE_MISSING,
                       producer_mission_id=producer_mission)
    if phase.state != mission_model.PS_PASSED:
        return _status(consumer, reuse_input, status=reuse_model.ST_REJECTED,
                       reason=reuse_model.RR_EVIDENCE_STALE,
                       producer_mission_id=producer_mission)
    evidence_path = mission_store.evidence_path(paths, reuse_input.phase_id)
    if not evidence_path.is_file():
        return _status(consumer, reuse_input, status=reuse_model.ST_UNRESOLVED,
                       reason=reuse_model.RR_EVIDENCE_MISSING,
                       producer_mission_id=producer_mission)
    try:
        payload = mission_store.load_phase_evidence(paths, reuse_input.phase_id)
    except mission_store.MalformedMissionError:
        return _status(consumer, reuse_input, status=reuse_model.ST_REJECTED,
                       reason=reuse_model.RR_EVIDENCE_MALFORMED,
                       producer_mission_id=producer_mission)
    if not phase.subrun_ids:
        return _status(consumer, reuse_input, status=reuse_model.ST_REJECTED,
                       reason=reuse_model.RR_EVIDENCE_STALE,
                       producer_mission_id=producer_mission)
    try:
        subrun = mission_store.load_subrun(paths, phase.subrun_ids[-1])
    except mission_store.MalformedMissionError:
        return _status(consumer, reuse_input, status=reuse_model.ST_REJECTED,
                       reason=reuse_model.RR_EVIDENCE_MALFORMED,
                       producer_mission_id=producer_mission)
    if subrun.classification != mission_model.CR_COMPLETED:
        return _status(consumer, reuse_input, status=reuse_model.ST_REJECTED,
                       reason=reuse_model.RR_EVIDENCE_STALE,
                       producer_mission_id=producer_mission)
    trust = (reuse_model.TRUST_PROVEN
             if subrun.attestation == semantic.ATTESTATION_VERIFIED
             else reuse_model.TRUST_LEGACY)
    if reuse_model.TRUST_ORDER[trust] < \
            reuse_model.TRUST_ORDER[reuse_input.min_trust]:
        return _status(consumer, reuse_input, status=reuse_model.ST_REJECTED,
                       reason=reuse_model.RR_LEGACY_NOT_PROMOTABLE,
                       trust=trust,
                       producer_mission_id=producer_mission,
                       artifact_attestation=subrun.attestation,
                       artifact_subrun_id=subrun.subrun_id,
                       artifact_semantic_status=subrun.semantic_status)
    artifact = artifact_payload(payload)
    content_sha = reuse_identity.artifact_id(artifact)
    worktree = payload.get("worktree")
    patch_sha: str | None = None
    if isinstance(worktree, dict):
        raw_patch = worktree.get("worktree_patch_sha256")
        if isinstance(raw_patch, str):
            patch_sha = raw_patch
    if (reuse_input.expected_sha256 is not None
            and reuse_input.expected_sha256 != content_sha):
        return _status(consumer, reuse_input, status=reuse_model.ST_REJECTED,
                       reason=reuse_model.RR_IDENTITY_MISMATCH,
                       trust=trust,
                       producer_mission_id=producer_mission,
                       artifact_content_sha256=content_sha,
                       artifact_patch_sha256=patch_sha,
                       artifact_attestation=subrun.attestation,
                       artifact_subrun_id=subrun.subrun_id,
                       artifact_semantic_status=subrun.semantic_status)
    return _status(
        consumer, reuse_input, status=reuse_model.ST_RESOLVED,
        reason=reuse_model.RR_RESOLVED, trust=trust,
        producer_mission_id=producer_mission,
        artifact_content_sha256=content_sha,
        artifact_patch_sha256=patch_sha,
        artifact_attestation=subrun.attestation,
        artifact_subrun_id=subrun.subrun_id,
        artifact_semantic_status=subrun.semantic_status)


def collect_inputs(graph: graph_model.GoalGraph) -> list[tuple[
        graph_model.GraphNode, graph_model.ReuseInput]]:
    """Deterministic consumer/input pairs declared by the canonical graph."""
    pairs: list[tuple[graph_model.GraphNode, graph_model.ReuseInput]] = []
    for node in graph.nodes:
        for reuse_input in node.reuse_inputs:
            pairs.append((node, reuse_input))
    pairs.sort(key=lambda pair: (pair[0].node_id, pair[1].input_id))
    return pairs


def build_projection(
    root: str,
    graph: graph_model.GoalGraph,
    *,
    evidence: Mapping[str, graph_evidence.MissionEvidenceRecord] | None = None,
) -> reuse_model.ReuseProjection:
    """Resolve every declared reuse input into one deterministic projection."""
    pairs = collect_inputs(graph)
    if evidence is None:
        mission_ids = {
            producer.mission_ref.mission_id
            for producer in graph.nodes
            if producer.mission_ref is not None
        }
        evidence = graph_evidence.collect_evidence(root, mission_ids)
    statuses = [
        resolve_input(root, graph, consumer, reuse_input, evidence)
        for consumer, reuse_input in pairs
    ]
    return reuse_model.ReuseProjection.build(
        goal_id=graph.goal_id,
        graph_id=graph.graph_id,
        spec_sha256=graph.spec_sha256,
        inputs=statuses,
    )
