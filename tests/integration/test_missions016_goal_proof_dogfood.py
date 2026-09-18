"""Mission 016 — production goal-level proof dogfood + compatibility.

Production-path deterministic proof for goal-level proof and operator control
(Issue #222 / program #207 checkpoint S16):

1.  create every referenced mission and the M016 strategic goal graph;
2.  prove a fail-closed INCOMPLETE goal while work is unresolved;
3.  drive a controlled failure and a bounded deterministic replan;
4.  preserve the superseded generation exactly;
5.  consume explicit proven cross-mission evidence;
6.  schedule dependency/resource-ready work;
7.  reach COMPLETE only after every acceptance criterion is proven;
8.  reconstruct the exact goal-proof identity after interruption;
9.  exercise the operator dashboard/JSON/why CLI surface;
10. M008-M015 compatibility + Git safety + bounded DeepSeek qualification.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trajectory_os.agents import model as agent_model
from trajectory_os.agents import qualification
from trajectory_os.graph import cli
from trajectory_os.graph import store as graph_store
from trajectory_os.graph.proof import engine as proof_engine
from trajectory_os.graph.proof import model as proof_model
from trajectory_os.graph.proof import store as proof_store
from trajectory_os.graph.proof import summary as proof_summary
from trajectory_os.graph.replan import engine as replan_engine
from trajectory_os.graph.replan import store as replan_store
from trajectory_os.graph.reuse import engine as reuse_engine
from trajectory_os.graph.scheduler import engine as sched_engine
from trajectory_os.graph.scheduler import model as sched_model
from trajectory_os.missions import model as mission_model
from trajectory_os.missions import orchestrator, semantic
from trajectory_os.missions.runner import SubrunResult

REPO_ROOT = Path(__file__).resolve().parents[2]
DOGFOOD_SPEC = REPO_ROOT / "examples" / "final_program_dogfood.json"
REPLAN_REQUEST = (
    REPO_ROOT / "examples" / "final_program_replan_request.json")
GOAL = "g-m016-program"
MISSIONS = (
    "m-m016-foundation", "m-m016-runtime", "m-m016-failing",
    "m-m016-failing2", "m-m016-integration", "m-m016-proof",
)


class AttestedRunner:
    def run(self, request: Any) -> SubrunResult:
        return SubrunResult(
            0, mission_model.CR_COMPLETED,
            semantic_status=semantic.STATUS_SUCCESS,
            attestation=semantic.ATTESTATION_VERIFIED)


class FailingRunner:
    """Deterministically fails the validator phase (controlled failure)."""

    def run(self, request: Any) -> SubrunResult:
        if request.phase_id == "validate":
            return SubrunResult(1, mission_model.CR_FAILED)
        return SubrunResult(
            0, mission_model.CR_COMPLETED,
            semantic_status=semantic.STATUS_SUCCESS,
            attestation=semantic.ATTESTATION_VERIFIED)


def _create_mission(root: str, mission_id: str) -> None:
    orchestrator.create_mission(root, orchestrator.MissionConfig(
        mission_id=mission_id,
        objective="M016 goal-level proof dogfood mission",
        phase_specs=orchestrator.default_phase_specs(
            {kind: ("true",) for kind in mission_model.CANONICAL_SEQUENCE},
            repair_budget=0),
        baseline_revision="base"))


def _setup(tmp_path: Path) -> str:
    root = str(tmp_path / "root")
    for mission_id in MISSIONS:
        _create_mission(root, mission_id)
    graph_store.create_graph(
        root, json.loads(DOGFOOD_SPEC.read_text(encoding="utf-8")),
        repo_root=str(REPO_ROOT), baseline_revision="base")
    return root


def _json(text: str) -> dict[str, Any]:
    return json.loads(text)


# ---------------------------------------------------------------------------
# 1-2: initial fail-closed incomplete goal
# ---------------------------------------------------------------------------


def test_dogfood_initial_goal_is_fail_closed_incomplete(
        tmp_path: Path) -> None:
    root = _setup(tmp_path)
    proof = proof_engine.build_proof(root, GOAL, computed_at="t0")
    assert proof.final_state == proof_model.GS_INCOMPLETE
    assert not proof.complete
    assert proof.gate.state in ("LAUNCH", "STOP", "AUTONOMOUS")
    # Every acceptance criterion is explicitly unproven, never silently
    # assumed: COMPLETE is impossible without exact evidence.
    assert all(not c.proven for c in proof.criteria)
    assert proof.counts.criteria_total == 6
    assert proof.counts.criteria_proven == 0


# ---------------------------------------------------------------------------
# 3-7: controlled failure, bounded replan, evidence reuse, COMPLETE
# ---------------------------------------------------------------------------


def test_dogfood_fail_closed_then_complete_after_replan(
        tmp_path: Path) -> None:
    root = _setup(tmp_path)
    # 1. Producer completes with exact attested evidence.
    report = orchestrator.run_mission(root, "m-m016-foundation",
                                      AttestedRunner())
    assert report.mission_state == mission_model.MS_COMPLETE

    # 2. Controlled failure of the node that the replan will supersede.
    failed = orchestrator.run_mission(root, "m-m016-failing", FailingRunner())
    assert failed.mission_state in (mission_model.MS_FAILED,
                                    mission_model.MS_BLOCKED)

    # 3. The goal stays fail-closed INCOMPLETE and is recorded as evidence.
    incomplete = proof_engine.build_proof(root, GOAL, computed_at="t1")
    assert incomplete.final_state == proof_model.GS_INCOMPLETE
    risks = {(risk.risk_class, risk.reason, risk.subject)
             for risk in incomplete.risks}
    assert (proof_model.RISK_BLOCKED, proof_model.R_NODE_BLOCKED,
            "n-failing") in risks
    assert any(risk.subject == "n-runtime"
               and risk.reason == proof_model.R_NODE_INCOMPLETE
               for risk in incomplete.risks)
    proof_store.record_proof(root, GOAL, computed_at="t1")

    # 4. Bounded deterministic replan supersedes the failed node.
    base = replan_engine.current_generation(root, GOAL)
    old_graph, _ = graph_store.load_graph(root, GOAL)
    old_doc = old_graph.to_dict()
    old_generation_id = base.generation.generation_id
    applied = replan_engine.apply_spec(
        root, GOAL,
        json.loads(REPLAN_REQUEST.read_text(encoding="utf-8")),
        created_at="t2")
    assert applied.accepted, applied.reason
    assert applied.generation.generation_number == 2

    # 5. Historical evidence is preserved exactly (never rewritten).
    archived, archived_graph = replan_store.load_generation(
        replan_store.replan_paths(root, GOAL), old_generation_id)
    assert archived.generation_number == 1
    assert archived_graph.to_dict() == old_doc

    # 6. The replan alone never proves anything: the goal is still
    #    INCOMPLETE until the replacement carries exact evidence.
    after_replan = proof_engine.build_proof(root, GOAL)
    assert after_replan.final_state == proof_model.GS_INCOMPLETE

    # 7. Run the replacement and every remaining node.
    assert orchestrator.run_mission(root, "m-m016-failing2",
                                    AttestedRunner()).mission_state \
        == mission_model.MS_COMPLETE

    # 8. Explicit cross-mission evidence reuse for the active generation.
    reuse = reuse_engine.resolve_and_record(root, GOAL, consumed_at="t3")
    assert not reuse.blocked
    assert reuse.projection.counts()["resolved"] == 1

    # 9. Dependency/resource-aware scheduling of eligible work.
    cycle = sched_engine.run_cycle(
        root, GOAL, sched_model.DEFAULT_POLICY, dispatch=False,
        created_at="t4")
    assert "n-runtime" in cycle.decision.candidate_order

    for mission_id in ("m-m016-runtime", "m-m016-integration",
                       "m-m016-proof"):
        assert orchestrator.run_mission(root, mission_id,
                                        AttestedRunner()).mission_state \
            == mission_model.MS_COMPLETE

    # 10. A fresh scheduling cycle sees only proven, ready work.
    final_cycle = sched_engine.run_cycle(
        root, GOAL, sched_model.DEFAULT_POLICY, dispatch=False,
        created_at="t5")
    assert final_cycle.decision.counts()["blocked"] == 0

    # 11. COMPLETE only now that every criterion has exact proven evidence.
    proof = proof_engine.build_proof(root, GOAL, computed_at="t6")
    assert proof.complete, [
        (risk.risk_class, risk.reason, risk.subject)
        for risk in proof.risks
    ]
    assert proof.final_state == proof_model.GS_COMPLETE
    assert proof.final_reason == proof_model.R_ALL_CRITERIA_PROVEN
    assert proof.counts.criteria_total == 6
    assert proof.counts.criteria_proven == 6
    assert proof.generation is not None
    assert proof.generation.generation_number == 2
    assert proof.counts.replans == 1
    assert proof.counts.supersessions == 1
    assert proof.gate.state == "GO_COMMIT"
    # Every proven binding names its exact mission/phase/sub-run evidence.
    for criterion in proof.criteria:
        assert criterion.status == proof_model.CS_PROVEN
        assert criterion.evidence
        assert criterion.evidence_sha256
        assert criterion.mission_id is not None
        assert criterion.mission_state == mission_model.MS_COMPLETE
    # The explicit phase binding is bound to exactly the named phase.
    explicit = next(c for c in proof.criteria
                    if c.criterion_id == "ac-evidence")
    assert explicit.phase_id == "validate"
    assert len(explicit.evidence) == 1
    assert explicit.evidence[0].phase_id == "validate"


def test_dogfood_missing_required_mission_stops_fail_closed(
        tmp_path: Path) -> None:
    import shutil

    root = _setup(tmp_path)
    mission_dir = tmp_path / "root" / "missions" / "m-m016-proof"
    assert mission_dir.is_dir()
    shutil.rmtree(mission_dir)
    proof = proof_engine.build_proof(root, GOAL)
    assert proof.final_state == proof_model.GS_INCOMPLETE
    assert proof.gate.state == "STOP"
    assert proof.gate.human_action_required is True
    assert any(risk.subject == "n-proof"
               and risk.reason == proof_model.R_NODE_UNRESOLVED
               for risk in proof.risks)


# ---------------------------------------------------------------------------
# 8: reconstruction after interruption
# ---------------------------------------------------------------------------


def test_dogfood_reconstruction_returns_exact_same_proof(
        tmp_path: Path) -> None:
    root = _setup(tmp_path)
    for mission_id in MISSIONS:
        if mission_id == "m-m016-failing":
            continue
        assert orchestrator.run_mission(root, mission_id,
                                        AttestedRunner()).mission_state \
            == mission_model.MS_COMPLETE
    # The failing node is not part of the active generation after replan.
    replan_engine.apply_spec(
        root, GOAL,
        json.loads(REPLAN_REQUEST.read_text(encoding="utf-8")),
        created_at="t1")
    assert orchestrator.run_mission(root, "m-m016-failing2",
                                    AttestedRunner()).mission_state \
        == mission_model.MS_COMPLETE
    reuse_engine.resolve_and_record(root, GOAL, consumed_at="t2")
    sched_engine.run_cycle(root, GOAL, sched_model.DEFAULT_POLICY,
                           dispatch=False, created_at="t3")

    proof, event, appended = proof_store.record_proof(
        root, GOAL, computed_at="t4")
    assert proof.complete
    assert appended

    # Simulate a restart: reload everything from disk and revalidate.
    read = proof_engine.reconstruct(root, GOAL)
    assert read.reconstructed
    assert read.persisted.proof_id == proof.proof_id
    assert read.live.proof_id == proof.proof_id
    assert read.to_dict()["status"] == "VALID"

    # Recording an unchanged proof is idempotent.
    _, event2, appended2 = proof_store.record_proof(
        root, GOAL, computed_at="t5")
    assert event2.event_id == event.event_id
    assert appended2 is False
    history = proof_summary.history_document(root, GOAL)
    assert history["count"] == 1

    # A tampered persisted projection fails closed on reconstruction.
    paths = proof_store.proof_paths(root, GOAL)
    tampered = json.loads(paths["projection"].read_text(encoding="utf-8"))
    tampered["final_reason"] = proof_model.R_NO_ACCEPTANCE_CRITERIA
    paths["projection"].write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(proof_model.GoalProofError):
        proof_store.load_projection(root, GOAL)


# ---------------------------------------------------------------------------
# 9: operator CLI surface
# ---------------------------------------------------------------------------


def test_dogfood_operator_dashboard_cli(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _setup(tmp_path)
    assert cli.main(["--root", root, "goal", GOAL, "--json"]) == cli.EXIT_OK
    document = _json(capsys.readouterr().out)
    assert document["final"]["state"] == proof_model.GS_INCOMPLETE
    assert document["goal"]["goal_id"] == GOAL
    assert document["counts"]["criteria_total"] == 6
    assert document["critical_path"]
    assert "criteria" in document and "risks" in document

    assert cli.main(["--root", root, "goal", GOAL]) == cli.EXIT_OK
    human = capsys.readouterr().out
    assert "goal      :" in human
    assert "criteria  :" in human
    assert len(human.splitlines()) < 60

    assert cli.main(["--root", root, "goal-proof", GOAL]) == cli.EXIT_OK
    machine = _json(capsys.readouterr().out)
    assert machine["proof_id"]
    assert machine["identity"]["domain"] == \
        "trajectory-os.goal-proof.v1"
    assert machine["final_state"] == proof_model.GS_INCOMPLETE

    assert cli.main(["--root", root, "goal-explain", GOAL, "n-foundation",
                     "--json"]) == cli.EXIT_OK
    explained = _json(capsys.readouterr().out)
    assert explained["node"]["node_id"] == "n-foundation"

    assert cli.main(["--root", root, "goal-risks", GOAL, "--json"]) \
        == cli.EXIT_OK
    risks = _json(capsys.readouterr().out)
    assert risks["final_state"] == proof_model.GS_INCOMPLETE

    assert cli.main(["--root", root, "goal-criteria", GOAL, "--json"]) \
        == cli.EXIT_OK
    criteria = _json(capsys.readouterr().out)
    assert len(criteria["criteria"]) == 6

    # Recording then validating the exact identity round-trips through the CLI.
    assert cli.main(["--root", root, "goal-record", GOAL, "--json"]) \
        == cli.EXIT_OK
    recorded = _json(capsys.readouterr().out)
    assert recorded["proof_id"]
    assert cli.main(["--root", root, "goal-validate", GOAL, "--json"]) \
        == cli.EXIT_OK
    validated = _json(capsys.readouterr().out)
    assert validated["reconstructed"] is True
    assert validated["persisted_proof_id"] == recorded["proof_id"]

    assert cli.main(["--root", root, "goal-history", GOAL, "--json"]) \
        == cli.EXIT_OK
    history = _json(capsys.readouterr().out)
    assert history["count"] == 1


# ---------------------------------------------------------------------------
# 10: compatibility + Git safety + bounded DeepSeek qualification
# ---------------------------------------------------------------------------


class _FakeBackend:
    def __init__(self, name: str, *, available: bool = True,
                 sdk: bool = False, result: agent_model.AgentResult
                 | None = None) -> None:
        self.name = name
        self._available = available
        self._sdk = sdk
        self._result = result

    def probe(self) -> agent_model.BackendProbe:
        return agent_model.BackendProbe(
            backend=self.name, available=self._available,
            reason=(agent_model.R_OK if self._available
                    else agent_model.R_RUNTIME_MISSING),
            transport=(agent_model.TRANSPORT_RUNTIME
                       if self.name == agent_model.BACKEND_DEEPSEEK_HARNESS
                       else agent_model.TRANSPORT_SUBPROCESS),
            sdk_version="1.0" if self._sdk else None)

    def run(self, request: agent_model.AgentRequest, *,
            cancel: object | None = None) -> agent_model.AgentResult:
        assert self._result is not None
        return self._result


def _unavailable_dsh() -> _FakeBackend:
    result = agent_model.AgentResult.build(
        backend=agent_model.BACKEND_DEEPSEEK_HARNESS,
        status=agent_model.RS_UNAVAILABLE,
        reason=agent_model.R_CREDENTIALS_MISSING,
        error="no credential for provider route",
        completion=agent_model.CompletionEvidence.build(
            source=agent_model.CS_NONE, reliable=False,
            detail=agent_model.R_CREDENTIALS_MISSING),
        transport=agent_model.TRANSPORT_RUNTIME)
    return _FakeBackend(agent_model.BACKEND_DEEPSEEK_HARNESS, sdk=True,
                        result=result)


def test_deepseek_qualification_states_are_bounded_and_secret_free() -> None:
    def unavailable_factory(name: str) -> _FakeBackend:
        if name == agent_model.BACKEND_DEEPSEEK_HARNESS:
            return _unavailable_dsh()
        return _FakeBackend(agent_model.BACKEND_PI)

    unavailable = qualification.qualify(
        workspace="/tmp/m016-qualify", timeout_s=30,
        factory=unavailable_factory)
    assert unavailable.status == qualification.QS_UNAVAILABLE
    assert unavailable.reason == agent_model.R_CREDENTIALS_MISSING
    assert unavailable.fallback["backend"] == agent_model.BACKEND_PI
    assert unavailable.fallback["authoritative"] is True
    assert unavailable.fallback["executed"] is False
    assert unavailable.git_writes is False
    payload = unavailable.to_dict()
    # Bounded, secret-free evidence: lifecycle only, no final response text.
    assert "final_response" not in json.dumps(payload)
    assert payload["canary"]["completion"]["reliable"] is False
    assert payload["comparison"]["completion_reliable"] is False

    # A structured protocol error is a deterministic incompatibility.
    incompatible_result = agent_model.AgentResult.build(
        backend=agent_model.BACKEND_DEEPSEEK_HARNESS,
        status=agent_model.RS_FAILED, reason=agent_model.R_RPC_ERROR,
        transport=agent_model.TRANSPORT_RUNTIME)

    def incompatible_factory(name: str) -> _FakeBackend:
        if name == agent_model.BACKEND_DEEPSEEK_HARNESS:
            return _FakeBackend(agent_model.BACKEND_DEEPSEEK_HARNESS, sdk=True,
                                result=incompatible_result)
        return _FakeBackend(agent_model.BACKEND_PI)

    incompatible = qualification.qualify(
        workspace="/tmp/m016-qualify", timeout_s=30,
        factory=incompatible_factory)
    assert incompatible.status == qualification.QS_INCOMPATIBLE

    # A reliable structured completion qualifies the backend.
    completion = agent_model.CompletionEvidence.build(
        source=agent_model.CS_LIFECYCLE_IDLE, reliable=True, detail="idle")
    qualified_result = agent_model.AgentResult.build(
        backend=agent_model.BACKEND_DEEPSEEK_HARNESS,
        status=agent_model.RS_COMPLETED, reason=agent_model.R_OK,
        completion=completion, transport=agent_model.TRANSPORT_RUNTIME)

    def qualified_factory(name: str) -> _FakeBackend:
        if name == agent_model.BACKEND_DEEPSEEK_HARNESS:
            return _FakeBackend(agent_model.BACKEND_DEEPSEEK_HARNESS, sdk=True,
                                result=qualified_result)
        return _FakeBackend(agent_model.BACKEND_PI)

    qualified = qualification.qualify(
        workspace="/tmp/m016-qualify", timeout_s=30,
        factory=qualified_factory)
    assert qualified.status == qualification.QS_QUALIFIED
    assert qualified.comparison["structured_lifecycle"] is True
    assert qualified.fallback["executed"] is False


_GIT_WRITE_VERBS = frozenset({
    "commit", "push", "merge", "reset", "restore", "clean", "stash",
    "rebase", "switch", "checkout", "add", "am", "pull", "fetch", "tag",
    "remote", "cherry-pick", "revert",
})


def test_m008_m015_compatibility_and_git_safety(
        tmp_path: Path) -> None:
    # A goal with no replan history keeps the exact M012/M014 identity and
    # never gains a proof field on the canonical graph.
    root = str(tmp_path / "plain")
    _create_mission(root, "m-plain")
    spec = {
        "schema_version": 1, "goal_id": "g-plain", "objective": "p",
        "nodes": [{"node_id": "n-1", "title": "n", "priority": 1,
                   "depends_on": [],
                   "acceptance_criteria": [{"criterion_id": "ac-1",
                                            "statement": "s"}],
                   "mission_ref": {"mission_id": "m-plain",
                                   "required": True}}],
    }
    graph = graph_store.create_graph(root, spec, repo_root=root,
                                     baseline_revision="base")
    from trajectory_os.graph import model as graph_model

    reloaded = graph_model.GoalGraph.from_dict(graph.to_dict(), "g")
    assert reloaded.graph_id == graph.graph_id
    assert "proof" not in graph.to_dict()
    # The proof layer is read-only derived evidence: no canonical store is
    # written by merely building the proof.
    engine_before = proof_store.projection_exists(root, "g-plain")
    proof_engine.build_proof(root, "g-plain")
    assert proof_store.projection_exists(root, "g-plain") == engine_before

    from trajectory_os.agents import canary, deepseek_harness, registry
    from trajectory_os.agents import cli as agent_cli
    from trajectory_os.graph.proof import engine as proof_engine_module
    from trajectory_os.graph.proof import identity as proof_identity
    from trajectory_os.graph.proof import store as proof_store_module
    from trajectory_os.graph.proof import summary as proof_summary_module
    from trajectory_os.graph.replan import engine as replan_engine_module
    from trajectory_os.graph.replan import model as replan_model_module
    from trajectory_os.graph.replan import store as replan_store_module
    from trajectory_os.graph.replan import summary as replan_summary
    from trajectory_os.graph.scheduler import engine as sched_engine_module

    for module in (cli, graph_store, graph_model, proof_engine_module,
                   proof_identity, proof_store_module, proof_summary_module,
                   replan_engine_module, replan_model_module,
                   replan_store_module, replan_summary, sched_engine_module,
                   agent_cli, canary, deepseek_harness, registry,
                   qualification):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for verb in _GIT_WRITE_VERBS:
            assert f'"git", "{verb}"' not in source, (module.__name__, verb)
        assert "os.system" not in source
