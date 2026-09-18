"""Mission 016 — read-only exact goal-proof evidence resolution.

This module resolves the bounded, exact proof chain behind one strategic
goal strictly read-only from the canonical M008-M015 stores:

* the M012 goal graph (:mod:`trajectory_os.graph.store`);
* the canonical mission store (:mod:`trajectory_os.missions.store`) and the
  read-only mission evidence resolver (:mod:`trajectory_os.graph.evidence`);
* the M013 scheduler, M014 reuse and M015 replan persisted state.

Nothing here mutates, terminalizes, repairs or promotes any state. A missing
or malformed canonical document is surfaced as an explicit, fail-closed fact
and is never guessed. The resolved chain is *derived evidence*, never a
second source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass

from trajectory_os.graph import evidence as graph_evidence
from trajectory_os.graph import model as graph_model
from trajectory_os.graph.proof import model as proof_model
from trajectory_os.missions import model as mission_model
from trajectory_os.missions import store as mission_store

#: The canonical M008 exact-attestation marker (re-imported for clarity).
ATTESTATION_VERIFIED = "VERIFIED"


@dataclass(frozen=True)
class PhaseEvidence:
    """One contributing phase's exact, bounded terminal evidence identity."""

    phase_id: str
    kind: str
    state: str
    subrun_id: str | None
    classification: str | None
    semantic_status: str | None
    attestation: str | None
    trust: str
    exact: bool

    def to_ref(self, *, node_id: str, mission_id: str) -> proof_model.EvidenceRef:
        return proof_model.EvidenceRef(
            node_id=node_id,
            mission_id=mission_id,
            phase_id=self.phase_id,
            subrun_id=self.subrun_id,
            classification=self.classification,
            semantic_status=self.semantic_status,
            attestation=self.attestation,
            trust=self.trust,
            exact=self.exact,
        )


@dataclass(frozen=True)
class MissionChain:
    """Bounded, read-only proof chain for one referenced mission."""

    mission_id: str
    resolved: bool
    error: str | None
    state: str | None
    reason: str | None
    proven_complete: bool
    legacy: bool
    phases: tuple[PhaseEvidence, ...]

    @property
    def exact(self) -> bool:
        """True when every contributing phase carries exact proven evidence."""
        return (self.proven_complete and not self.legacy
                and bool(self.phases)
                and all(p.exact for p in self.phases if p.state
                        == mission_model.PS_PASSED))


def _load_phases(root: str, mission_id: str) -> tuple[PhaseEvidence, ...]:
    """Resolve every contributing phase strictly read-only (fail closed)."""
    mission, paths = mission_store.load_mission(root, mission_id)
    phases: list[PhaseEvidence] = []
    for phase in mission.phases:
        last_classification: str | None = None
        semantic_status: str | None = None
        attestation: str | None = None
        subrun_id: str | None = phase.subrun_ids[-1] if phase.subrun_ids \
            else None
        if subrun_id is not None:
            record = mission_store.load_subrun(paths, subrun_id)
            last_classification = record.classification
            semantic_status = record.semantic_status
            attestation = record.attestation
        passed = phase.state == mission_model.PS_PASSED
        model_heavy = mission_model.is_model_heavy(phase.kind)
        if not passed:
            trust = proof_model.TRUST_NONE
            exact = False
        elif model_heavy:
            exact = (
                last_classification == mission_model.CR_COMPLETED
                and semantic_status == "SUCCESS"
                and attestation == ATTESTATION_VERIFIED
            )
            trust = proof_model.TRUST_PROVEN if exact \
                else proof_model.TRUST_LEGACY
        else:
            exact = last_classification == mission_model.CR_COMPLETED
            trust = proof_model.TRUST_PROVEN if exact \
                else proof_model.TRUST_NONE
        phases.append(PhaseEvidence(
            phase_id=phase.phase_id,
            kind=phase.kind,
            state=phase.state,
            subrun_id=subrun_id,
            classification=last_classification,
            semantic_status=semantic_status,
            attestation=attestation,
            trust=trust,
            exact=exact,
        ))
    return tuple(phases)


def resolve_mission_chain(root: str, mission_id: str) -> MissionChain:
    """Resolve one referenced mission's exact proof chain (read-only)."""
    record = graph_evidence.resolve_mission_evidence(root, mission_id)
    if record.error is not None or not record.resolved:
        return MissionChain(
            mission_id=mission_id, resolved=record.resolved,
            error=record.error, state=record.state, reason=record.reason,
            proven_complete=False, legacy=False, phases=())
    try:
        phases = _load_phases(root, mission_id)
    except mission_store.MalformedMissionError:
        return MissionChain(
            mission_id=mission_id, resolved=True,
            error=graph_evidence.ERR_CONTRADICTION, state=record.state,
            reason=record.reason, proven_complete=False, legacy=False,
            phases=())
    legacy = any(
        p.state == mission_model.PS_PASSED
        and p.kind in mission_model.MODEL_HEAVY_KINDS
        and p.attestation != ATTESTATION_VERIFIED
        for p in phases
    )
    return MissionChain(
        mission_id=mission_id, resolved=True, error=None, state=record.state,
        reason=record.reason, proven_complete=record.proven_complete,
        legacy=legacy, phases=phases)


def _phase_ref(chain: MissionChain, phase_id: str) -> PhaseEvidence | None:
    for phase in chain.phases:
        if phase.phase_id == phase_id:
            return phase
    return None


def _unproven_phase_binding(
    *, node_id: str, criterion: graph_model.AcceptanceCriterion,
    chain: MissionChain, phase_id: str | None, status: str, reason: str,
    trust: str,
) -> proof_model.CriterionBinding:
    return proof_model.CriterionBinding.build(
        node_id=node_id, criterion_id=criterion.criterion_id,
        statement=criterion.statement, verification=criterion.verification,
        required=True, status=status, reason=reason, trust=trust,
        mission_id=chain.mission_id, mission_state=chain.state,
        mission_reason=chain.reason, mission_proven=chain.proven_complete,
        phase_id=phase_id, evidence=())


def bind_criterion(
    *,
    node_id: str,
    criterion: graph_model.AcceptanceCriterion,
    chain: MissionChain | None,
) -> proof_model.CriterionBinding:
    """Bind one acceptance criterion to its exact proven evidence (pure).

    The criterion's optional ``verification`` field is interpreted
    conservatively: when it is exactly one canonical phase id it is an
    explicit machine binding; when it name-shapes as a phase id but no such
    phase exists the mapping is *ambiguous* and fails closed; otherwise it is
    descriptive prose and the criterion is proven by the whole mission proof.
    """
    if chain is None:
        return proof_model.CriterionBinding.build(
            node_id=node_id, criterion_id=criterion.criterion_id,
            statement=criterion.statement,
            verification=criterion.verification, required=True,
            status=proof_model.CS_UNPROVEN,
            reason=proof_model.R_CRITERION_UNBOUND,
            trust=proof_model.TRUST_NONE, mission_id=None,
            mission_state=None, mission_reason=None, mission_proven=False,
            phase_id=None, evidence=())

    explicit = (criterion.verification
                if criterion.verification
                and mission_model.ID_RE.fullmatch(criterion.verification)
                else None)
    if explicit is not None:
        phase = _phase_ref(chain, explicit)
        if phase is None:
            return _unproven_phase_binding(
                node_id=node_id, criterion=criterion, chain=chain,
                phase_id=explicit, status=proof_model.CS_INVALID,
                reason=proof_model.R_CRITERION_AMBIGUOUS,
                trust=proof_model.TRUST_NONE)
        if phase.state != mission_model.PS_PASSED:
            # An explicit machine binding to a phase that did not pass is
            # never proven; the exact reason distinguishes an incomplete
            # mission from a deterministically failed/blocked phase.
            return _unproven_phase_binding(
                node_id=node_id, criterion=criterion, chain=chain,
                phase_id=explicit, status=proof_model.CS_UNPROVEN,
                reason=(_mission_reason(chain) if not chain.proven_complete
                        else proof_model.R_CRITERION_UNPROVEN),
                trust=proof_model.TRUST_NONE)
        if not phase.exact:
            return _unproven_phase_binding(
                node_id=node_id, criterion=criterion, chain=chain,
                phase_id=explicit,
                status=(proof_model.CS_CONTRADICTORY
                        if phase.trust == proof_model.TRUST_LEGACY
                        else proof_model.CS_UNPROVEN),
                reason=(proof_model.R_MISSION_LEGACY_EVIDENCE
                        if phase.trust == proof_model.TRUST_LEGACY
                        else proof_model.R_CRITERION_UNPROVEN),
                trust=phase.trust)
        if not chain.proven_complete:
            return _unproven_phase_binding(
                node_id=node_id, criterion=criterion, chain=chain,
                phase_id=explicit, status=proof_model.CS_UNPROVEN,
                reason=_mission_reason(chain), trust=proof_model.TRUST_NONE)
        ref = phase.to_ref(node_id=node_id, mission_id=chain.mission_id)
        return proof_model.CriterionBinding.build(
            node_id=node_id, criterion_id=criterion.criterion_id,
            statement=criterion.statement, verification=criterion.verification,
            required=True, status=proof_model.CS_PROVEN,
            reason=proof_model.R_ALL_CRITERIA_PROVEN,
            trust=proof_model.TRUST_PROVEN, mission_id=chain.mission_id,
            mission_state=chain.state, mission_reason=chain.reason,
            mission_proven=True, phase_id=explicit, evidence=(ref,))

    # Whole-mission binding: the mission must be exactly proven end to end.
    if chain.legacy:
        return _unproven_phase_binding(
            node_id=node_id, criterion=criterion, chain=chain, phase_id=None,
            status=proof_model.CS_CONTRADICTORY,
            reason=proof_model.R_MISSION_LEGACY_EVIDENCE,
            trust=proof_model.TRUST_LEGACY)
    if not chain.proven_complete:
        return _unproven_phase_binding(
            node_id=node_id, criterion=criterion, chain=chain, phase_id=None,
            status=proof_model.CS_UNPROVEN, reason=_mission_reason(chain),
            trust=proof_model.TRUST_NONE)
    refs = tuple(
        phase.to_ref(node_id=node_id, mission_id=chain.mission_id)
        for phase in chain.phases
        if phase.state == mission_model.PS_PASSED
    )
    if not refs:
        # A mission that is trusted complete but contributes no PASSED phase
        # evidence is an impossible/contradictory state: fail closed rather
        # than presenting an unproven criterion as merely unresolved.
        return _unproven_phase_binding(
            node_id=node_id, criterion=criterion, chain=chain, phase_id=None,
            status=proof_model.CS_CONTRADICTORY,
            reason=proof_model.R_MISSION_EVIDENCE_MISSING,
            trust=proof_model.TRUST_NONE)
    return proof_model.CriterionBinding.build(
        node_id=node_id, criterion_id=criterion.criterion_id,
        statement=criterion.statement, verification=criterion.verification,
        required=True, status=proof_model.CS_PROVEN,
        reason=proof_model.R_ALL_CRITERIA_PROVEN,
        trust=proof_model.TRUST_PROVEN, mission_id=chain.mission_id,
        mission_state=chain.state, mission_reason=chain.reason,
        mission_proven=True, phase_id=None, evidence=refs)


def _mission_reason(chain: MissionChain) -> str:
    if chain.error == graph_evidence.ERR_NOT_FOUND:
        return proof_model.R_MISSION_UNRESOLVED
    if chain.error in graph_evidence.INVALID_ERRORS:
        return proof_model.R_MISSION_INVALID
    if chain.state == mission_model.MS_FAILED:
        return proof_model.R_MISSION_FAILED
    return proof_model.R_MISSION_NOT_PROVEN


def critical_path(graph: graph_model.GoalGraph) -> tuple[str, ...]:
    """Deterministic longest dependency path (length DESC, lexicographic ASC).

    The path is computed over the canonical dependency edges only, so it
    depends solely on normalized graph content. Ties are broken by the
    lexicographically smallest node-id sequence, making the result stable
    across runs and filesystems.
    """
    node_map = graph.node_map()
    memo: dict[str, tuple[str, ...]] = {}

    def longest(node_id: str) -> tuple[str, ...]:
        cached = memo.get(node_id)
        if cached is not None:
            return cached
        best: tuple[str, ...] = (node_id,)
        for dep in node_map[node_id].depends_on:
            candidate = (*longest(dep), node_id)
            if (len(candidate) > len(best)
                    or (len(candidate) == len(best) and candidate < best)):
                best = candidate
        memo[node_id] = best
        return best

    best_path: tuple[str, ...] = ()
    for node_id in sorted(node_map):
        candidate = longest(node_id)
        if (len(candidate) > len(best_path)
                or (len(candidate) == len(best_path)
                    and candidate < best_path)):
            best_path = candidate
    return best_path
