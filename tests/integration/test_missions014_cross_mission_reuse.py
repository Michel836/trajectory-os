"""Mission 014 — production cross-mission reuse dogfood + compatibility.

Production-path deterministic proof for explicit fail-closed cross-mission
evidence reuse (Issue #219 / program #207 checkpoint S14):

1.  create every referenced mission and the M014 goal graph;
2.  prove one immutable proven upstream artifact is consumed by multiple
    downstream nodes without copying or promoting upstream success;
3.  prove unresolved mandatory reuse blocks scheduler admission/dispatch;
4.  prove stale-reference and identity-mismatch references fail closed;
5.  prove legacy/historical evidence is distinguishable and never silently
    promoted beyond its trust level;
6.  prove persistence/reconstruction reproduces the exact projection and
    append-only consumption history;
7.  exercise the operator CLI surface;
8.  M008-M013 compatibility and Git-safety.

All reuse resolution is read-only over the canonical mission store; all
dispatch uses the existing canonical orchestrator (no second engine).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trajectory_os.graph import cli
from trajectory_os.graph import model as graph_model
from trajectory_os.graph import store as graph_store
from trajectory_os.graph.reuse import engine as reuse_engine
from trajectory_os.graph.reuse import model as reuse_model
from trajectory_os.graph.reuse import store as reuse_store
from trajectory_os.graph.scheduler import engine as scheduler_engine
from trajectory_os.graph.scheduler import model as sched_model
from trajectory_os.missions import model as mission_model
from trajectory_os.missions import orchestrator, semantic
from trajectory_os.missions import store as mission_store
from trajectory_os.missions.runner import SubrunResult

REPO_ROOT = Path(__file__).resolve().parents[2]
DOGFOOD_SPEC = REPO_ROOT / "examples" / "cross_mission_reuse_dogfood.json"
GOAL = "g-m014-dogfood"
POLICY = sched_model.DEFAULT_POLICY


class AttestedRunner:
    def run(self, request: Any) -> SubrunResult:
        return SubrunResult(
            0, mission_model.CR_COMPLETED,
            semantic_status=semantic.STATUS_SUCCESS,
            attestation=semantic.ATTESTATION_VERIFIED)


class LegacyRunner:
    def run(self, request: Any) -> SubrunResult:
        return SubrunResult(0, mission_model.CR_COMPLETED)


def _dogfood_spec() -> dict[str, Any]:
    return json.loads(DOGFOOD_SPEC.read_text(encoding="utf-8"))


def _create_mission(root: str, mission_id: str) -> None:
    orchestrator.create_mission(root, orchestrator.MissionConfig(
        mission_id=mission_id,
        objective="cross-mission reuse dogfood mission",
        phase_specs=orchestrator.default_phase_specs(
            {kind: ("true",) for kind in mission_model.CANONICAL_SEQUENCE}),
        baseline_revision="base",
    ))


def _setup(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> str:
    root = str(tmp_path / "root")
    spec = _dogfood_spec()
    for node in spec["nodes"]:
        _create_mission(root, node["mission_ref"]["mission_id"])
    code = cli.main([
        "--root", root, "create", "--spec", str(DOGFOOD_SPEC),
        "--repo", str(REPO_ROOT), "--head", "base",
    ])
    assert code == cli.EXIT_OK
    capsys.readouterr()
    return root


def _last_json(text: str) -> dict[str, Any]:
    return json.loads(text)


def _run_to_production(root: str) -> None:
    report = orchestrator.run_mission(root, "m-reuse-up", AttestedRunner())
    assert report.mission_state == mission_model.MS_COMPLETE


def _producer_evidence(root: str) -> Path:
    paths = mission_store.mission_paths(root, "m-reuse-up")
    return mission_store.evidence_path(paths, "validate")


# ---------------------------------------------------------------------------
# 1-2: successful reuse, multiple consumers, immutable producer
# ---------------------------------------------------------------------------


def test_dogfood_successful_reuse_and_multiple_consumers(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _setup(tmp_path, capsys)
    _run_to_production(root)
    evidence_before = _producer_evidence(root).read_bytes()

    assert cli.main(["--root", root, "reuse", GOAL, "--json"]) == cli.EXIT_OK
    document = _last_json(capsys.readouterr().out)
    by_consumer = {item["consumer_node_id"]: item
                   for item in document["inputs"]}
    assert by_consumer["n-down"]["status"] == reuse_model.ST_RESOLVED
    assert by_consumer["n-down"]["trust"] == reuse_model.TRUST_PROVEN
    assert by_consumer["n-down2"]["status"] == reuse_model.ST_RESOLVED
    assert by_consumer["n-unresolved"]["status"] == \
        reuse_model.ST_UNRESOLVED
    assert by_consumer["n-unresolved"]["reason"] == \
        reuse_model.RR_EVIDENCE_MISSING
    # Both consumers reference the exact same immutable upstream artifact.
    assert (by_consumer["n-down"]["artifact_content_sha256"]
            == by_consumer["n-down2"]["artifact_content_sha256"])

    assert cli.main(["--root", root, "reuse-resolve", GOAL,
                     "--json"]) == cli.EXIT_OK
    resolved = _last_json(capsys.readouterr().out)
    assert resolved["status"] == "BLOCKED"
    assert len(resolved["appended"]) == 2  # only resolved inputs consumed

    # Scheduling admits only proven-reuse consumers; the unresolved one stays
    # deferred with its stable reason and is never dispatched.
    decision = scheduler_engine.build_decision(
        root, GOAL, POLICY, created_at="t1")
    assert decision.input_projection_id is not None
    assert {n.node_id for n in decision.admitted} == {"n-down", "n-down2"}
    unresolved = {n.node_id: n.reason for n in decision.deferred}
    assert unresolved["n-unresolved"] == reuse_model.RR_EVIDENCE_MISSING

    dispatcher = scheduler_engine.MissionPathDispatcher(
        runner_factory=AttestedRunner, session_subruns=1)
    result = scheduler_engine.run_cycle(
        root, GOAL, POLICY, dispatch=True, dispatcher=dispatcher,
        created_at="t2")
    assert {r.node_id for r in result.dispatched} == {"n-down", "n-down2"}
    assert "n-unresolved" not in {r.node_id for r in result.dispatched}
    # The upstream proof is never rewritten by downstream consumption.
    assert _producer_evidence(root).read_bytes() == evidence_before


def test_dogfood_unresolved_without_persisted_projection(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _setup(tmp_path, capsys)
    _run_to_production(root)
    # Producer is proven, but no reuse projection was resolved: declared
    # reuse inputs must still fail closed (implicit evidence is never trusted).
    decision = scheduler_engine.build_decision(
        root, GOAL, POLICY, created_at="t0")
    reasons = {n.node_id: n.reason for n in decision.deferred}
    assert reasons["n-down"] == reuse_model.RR_PRODUCER_UNRESOLVED
    assert reasons["n-down2"] == reuse_model.RR_PRODUCER_UNRESOLVED
    assert decision.admitted == ()


def test_dogfood_input_projection_binds_reuse_identity(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _setup(tmp_path, capsys)
    _run_to_production(root)
    before = scheduler_engine.build_decision(
        root, GOAL, POLICY, created_at="t0")
    reuse_engine.resolve_and_record(root, GOAL, consumed_at="t1")
    after = scheduler_engine.build_decision(
        root, GOAL, POLICY, created_at="t0")  # same clock -> identity only
    assert before.input_projection_id != after.input_projection_id
    assert before.decision_id != after.decision_id


# ---------------------------------------------------------------------------
# 3-4: stale + identity-mismatch failure
# ---------------------------------------------------------------------------


def test_dogfood_stale_reference_fails_closed(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _setup(tmp_path, capsys)
    _run_to_production(root)
    reuse_engine.resolve_and_record(root, GOAL, consumed_at="t1")
    assert cli.main(["--root", root, "reuse-validate", GOAL,
                     "--json"]) == cli.EXIT_OK
    capsys.readouterr()

    evidence = _producer_evidence(root)
    document = json.loads(evidence.read_text(encoding="utf-8"))
    document["attempt"] = 999
    evidence.write_text(json.dumps(document), encoding="utf-8")

    assert cli.main(["--root", root, "reuse-validate", GOAL]) \
        == cli.EXIT_REJECTED
    assert "REUSE_EVIDENCE_STALE" in capsys.readouterr().err
    with pytest.raises(reuse_model.ReuseValidationError) as excinfo:
        reuse_engine.reconstruct(root, GOAL)
    assert excinfo.value.code == reuse_model.E_STALE


def test_dogfood_scheduler_reconstruction_binds_reuse(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _setup(tmp_path, capsys)
    _run_to_production(root)
    reuse_engine.resolve_and_record(root, GOAL, consumed_at="t1")
    assert cli.main(["--root", root, "schedule", GOAL]) == cli.EXIT_OK
    assert "status    : PLANNED" in capsys.readouterr().out
    assert cli.main(["--root", root, "validate-schedule", GOAL]) \
        == cli.EXIT_OK
    capsys.readouterr()
    # Once the upstream reuse evidence drifts, the scheduler can no longer be
    # reconstructed independently of the exact reusable evidence it consumed.
    evidence = _producer_evidence(root)
    document = json.loads(evidence.read_text(encoding="utf-8"))
    document["attempt"] = 999
    evidence.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(reuse_model.ReuseValidationError):
        scheduler_engine.reconstruct(root, GOAL)
    assert cli.main(["--root", root, "validate-schedule", GOAL]) \
        == cli.EXIT_REJECTED


def test_dogfood_identity_mismatch_fails_closed(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = str(tmp_path / "root")
    _create_mission(root, "m-mm-up")
    _create_mission(root, "m-mm-down")
    spec = {
        "schema_version": 1, "goal_id": "g-mm", "objective": "mismatch",
        "nodes": [
            {"node_id": "n-up", "title": "up", "priority": 90,
             "depends_on": [],
             "acceptance_criteria": [{"criterion_id": "ac-1",
                                      "statement": "s"}],
             "mission_ref": {"mission_id": "m-mm-up", "required": True},
             "resources": {"cpu_slots": 1}},
            {"node_id": "n-down", "title": "down", "priority": 50,
             "depends_on": ["n-up"],
             "acceptance_criteria": [{"criterion_id": "ac-1",
                                      "statement": "s"}],
             "mission_ref": {"mission_id": "m-mm-down", "required": True},
             "reuse_inputs": [{
                 "input_id": "up-validate", "producer_node_id": "n-up",
                 "evidence_kind": "PHASE_EVIDENCE", "phase_id": "validate",
                 "required": True, "min_trust": "PROVEN",
                 "expected_sha256": "0" * 64}],
             "resources": {"cpu_slots": 1}},
        ],
    }
    graph_store.create_graph(root, spec, repo_root=root,
                             baseline_revision="base")
    assert orchestrator.run_mission(root, "m-mm-up", AttestedRunner()
                                    ).mission_state == mission_model.MS_COMPLETE
    projection = reuse_engine.resolve_and_record(
        root, "g-mm", consumed_at="t1").projection
    item = projection.inputs[0]
    assert item.status == reuse_model.ST_REJECTED
    assert item.reason == reuse_model.RR_IDENTITY_MISMATCH
    decision = scheduler_engine.build_decision(
        root, "g-mm", POLICY, created_at="t1")
    deferred = {n.node_id: n.reason for n in decision.deferred}
    assert deferred["n-down"] == reuse_model.RR_IDENTITY_MISMATCH


# ---------------------------------------------------------------------------
# 5: legacy evidence is distinguishable and never silently promoted
# ---------------------------------------------------------------------------


def test_dogfood_legacy_evidence_is_not_promoted(
        tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    _create_mission(root, "m-leg-up")
    _create_mission(root, "m-leg-proven")
    _create_mission(root, "m-leg-accept")
    spec = {
        "schema_version": 1, "goal_id": "g-leg", "objective": "legacy",
        "nodes": [
            {"node_id": "n-up", "title": "up", "priority": 90,
             "depends_on": [],
             "acceptance_criteria": [{"criterion_id": "ac-1",
                                      "statement": "s"}],
             "mission_ref": {"mission_id": "m-leg-up", "required": True},
             "resources": {"cpu_slots": 1}},
            {"node_id": "n-proven", "title": "p", "priority": 70,
             "depends_on": ["n-up"],
             "acceptance_criteria": [{"criterion_id": "ac-1",
                                      "statement": "s"}],
             "mission_ref": {"mission_id": "m-leg-proven", "required": True},
             "reuse_inputs": [{
                 "input_id": "up", "producer_node_id": "n-up",
                 "evidence_kind": "PHASE_EVIDENCE", "phase_id": "validate",
                 "required": True, "min_trust": "PROVEN"}],
             "resources": {"cpu_slots": 1}},
            {"node_id": "n-accept", "title": "a", "priority": 60,
             "depends_on": ["n-up"],
             "acceptance_criteria": [{"criterion_id": "ac-1",
                                      "statement": "s"}],
             "mission_ref": {"mission_id": "m-leg-accept", "required": True},
             "reuse_inputs": [{
                 "input_id": "up", "producer_node_id": "n-up",
                 "evidence_kind": "PHASE_EVIDENCE", "phase_id": "validate",
                 "required": True, "min_trust": "LEGACY"}],
             "resources": {"cpu_slots": 1}},
        ],
    }
    graph_store.create_graph(root, spec, repo_root=root,
                             baseline_revision="base")
    assert orchestrator.run_mission(root, "m-leg-up", LegacyRunner()
                                    ).mission_state == mission_model.MS_COMPLETE
    projection = reuse_engine.resolve_and_record(
        root, "g-leg", consumed_at="t1").projection
    by_consumer = {item.consumer_node_id: item for item in projection.inputs}
    # PROVEN-required consumer rejects; LEGACY-accepting consumer resolves.
    assert by_consumer["n-proven"].status == reuse_model.ST_REJECTED
    assert by_consumer["n-proven"].reason == \
        reuse_model.RR_LEGACY_NOT_PROMOTABLE
    assert by_consumer["n-proven"].trust == reuse_model.TRUST_LEGACY
    assert by_consumer["n-accept"].status == reuse_model.ST_RESOLVED
    assert by_consumer["n-accept"].trust == reuse_model.TRUST_LEGACY


# ---------------------------------------------------------------------------
# 6: persistence / reconstruction + append-only consumption history
# ---------------------------------------------------------------------------


def test_dogfood_reconstruction_reproduces_exact_projection(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _setup(tmp_path, capsys)
    _run_to_production(root)
    first = reuse_engine.resolve_and_record(root, GOAL, consumed_at="t1")
    second = reuse_engine.resolve_and_record(root, GOAL, consumed_at="t2")
    assert second.appended == ()  # idempotent, timestamp preserved
    read = reuse_engine.reconstruct(root, GOAL)
    assert read.projection.projection_id == first.projection.projection_id
    assert read.projection.to_dict() == first.projection.to_dict()
    assert len(read.consumptions) == 2
    assert all(c.consumed_at == "t1" for c in read.consumptions)
    # Restart reconstruction is byte-stable.
    assert reuse_engine.reconstruct(root, GOAL).to_dict() == read.to_dict()

    assert cli.main(["--root", root, "reuse-history", GOAL,
                     "--json"]) == cli.EXIT_OK
    history = _last_json(capsys.readouterr().out)
    assert history["count"] == 2
    assert history["projection_id"] == first.projection.projection_id

    assert cli.main(["--root", root, "provenance", GOAL,
                     "--json"]) == cli.EXIT_OK
    provenance = _last_json(capsys.readouterr().out)
    assert len(provenance["chains"]) == 3
    producer_ids = {chain["producer_node_id"]
                    for chain in provenance["chains"]}
    assert producer_ids == {"n-up"}


# ---------------------------------------------------------------------------
# 7: operator CLI surface
# ---------------------------------------------------------------------------


def test_dogfood_operator_surface(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _setup(tmp_path, capsys)
    _run_to_production(root)

    assert cli.main(["--root", root, "reuse", GOAL]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "projection:" in out
    assert "n-down <- n-up/validate" in out

    assert cli.main(["--root", root, "why-reuse", GOAL, "n-down"]) \
        == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "up-validate" in out
    assert "REUSE_RESOLVED" in out

    assert cli.main(["--root", root, "why-reuse", GOAL, "n-unresolved"]) \
        == cli.EXIT_OK
    assert "REUSE_EVIDENCE_MISSING" in capsys.readouterr().out

    assert cli.main(["--root", root, "why-reuse", GOAL, "n-ghost"]) \
        != cli.EXIT_OK

    assert cli.main(["--root", root, "reuse-resolve", GOAL]) == cli.EXIT_OK
    assert "status    : BLOCKED" in capsys.readouterr().out

    assert cli.main(["--root", root, "reuse-validate", GOAL]) == cli.EXIT_OK
    assert "valid     : g-m014-dogfood reuse" in capsys.readouterr().out

    # The compact portfolio summary exposes unresolved reuse blockers.
    assert cli.main(["--root", root, "portfolio", GOAL]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "reuse_blocked=1" in out
    assert "n-unresolved" in out


# ---------------------------------------------------------------------------
# 8: M008-M013 compatibility + Git safety
# ---------------------------------------------------------------------------


def test_m008_m013_compatibility_regression(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from trajectory_os.graph import identity as graph_identity
    from trajectory_os.graph.reuse import identity as reuse_identity
    from trajectory_os.graph.scheduler import identity as sched_identity
    from trajectory_os.missions import identity as patch_identity

    for domain in reuse_identity.DOMAIN_IDS:
        assert domain not in patch_identity.DOMAIN_IDS
        assert domain not in graph_identity.DOMAIN_IDS
        assert domain not in sched_identity.DOMAIN_IDS
    # M012 graphs without reuse declarations keep their exact identity.
    root = str(tmp_path / "root")
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
    graph = graph_store.build_graph(spec_doc=spec)
    assert "reuse_inputs" not in json.dumps(graph.to_dict())
    reloaded = graph_model.GoalGraph.from_dict(graph.to_dict(), "g")
    assert reloaded.graph_id == graph.graph_id


_GIT_WRITE_VERBS = frozenset({
    "commit", "push", "merge", "reset", "restore", "clean", "stash",
    "rebase", "switch", "checkout", "add", "am", "pull", "fetch", "tag",
    "remote", "cherry-pick", "revert",
})


def test_no_autonomous_git_trust_boundary_write() -> None:
    from trajectory_os.graph.reuse import identity as reuse_identity
    from trajectory_os.graph.reuse import resolver, summary

    for module in (cli, graph_store, graph_model, reuse_engine,
                   reuse_identity, reuse_model, reuse_store, resolver,
                   summary):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for verb in _GIT_WRITE_VERBS:
            assert f'"git", "{verb}"' not in source, (module.__name__, verb)
        assert "os.system" not in source
