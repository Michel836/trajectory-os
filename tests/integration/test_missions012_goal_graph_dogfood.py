"""Mission 012 — production dogfood + M008-M011 compatibility regression.

Production-path deterministic proof for the bounded goal decomposition
graph (Issue #215 / program #207 checkpoint S12):

1.  load the checked-in declarative goal spec and create the graph;
2.  persist it and reconstruct it;
3.  assert identical deterministic topological order and provenance;
4.  assert dependency-ready nodes and blocked nodes with explicit reasons;
5.  transition readiness when authoritative referenced mission evidence
    permits it (without fabricating completion evidence);
6.  controlled malformed/cyclic graph rejection;
7.  M008/M009/M010/M011 compatibility regression.

All graph operations use the production CLI/library surface; mission work
uses the canonical orchestrator (no second execution engine).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trajectory_os.graph import cli, model, readiness, store
from trajectory_os.missions import gate, identity, orchestrator, runner, semantic, summary
from trajectory_os.missions import model as mission_model
from trajectory_os.missions import store as mission_store
from trajectory_os.missions.runner import SubrunResult

REPO_ROOT = Path(__file__).resolve().parents[2]
DOGFOOD_SPEC = REPO_ROOT / "examples" / "goal_decomposition_dogfood.json"
GOAL = "g-m012-dogfood"


class _OkRunner:
    def run(self, request: Any) -> SubrunResult:
        return SubrunResult(0, mission_model.CR_COMPLETED)


def _create_mission(root: str, mission_id: str) -> None:
    orchestrator.create_mission(root, orchestrator.MissionConfig(
        mission_id=mission_id,
        objective="dogfood reference",
        phase_specs=orchestrator.default_phase_specs(
            {kind: ("true",) for kind in mission_model.CANONICAL_SEQUENCE}),
        baseline_revision="baseline-sha",
    ))


def _write_spec(tmp_path: Path, spec: object, name: str) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(spec), encoding="utf-8")
    return str(path)


def _create_graph(tmp_path: Path, root: str, spec: object,
                  name: str = "spec.json") -> Path:
    assert cli.main([
        "--root", root, "create", "--spec", _write_spec(tmp_path, spec, name),
        "--repo", str(tmp_path), "--head", "baseline-sha",
    ]) == cli.EXIT_OK
    return store.graph_paths(root, GOAL)["graph"]


# ---------------------------------------------------------------------------
# 1-4: create, persist, reconstruct, order, readiness
# ---------------------------------------------------------------------------


def test_production_dogfood_create_persist_reconstruct(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = str(tmp_path / "root")
    spec = json.loads(DOGFOOD_SPEC.read_text(encoding="utf-8"))
    graph_path = _create_graph(tmp_path, root, spec)
    assert graph_path.is_file()
    capsys.readouterr()

    created, _ = store.load_graph(root, GOAL)
    reloaded, _ = store.load_graph(root, GOAL)
    # 2 + 3: reconstruction is byte-stable and yields the same identity,
    # nodes, edges, order and provenance.
    assert created.to_dict() == reloaded.to_dict()
    assert created.graph_id == reloaded.graph_id
    order = created.topological_order()
    assert order == reloaded.topological_order()
    assert store.load_graph(root, GOAL)[0].topological_order() == order

    # Structure requirements: multiple roots, fan-in, multi-level chain,
    # explicit criteria, different priorities, different resources.
    node_map = created.node_map()
    roots = sorted(n.node_id for n in created.nodes if not n.depends_on)
    assert roots == ["n-model", "n-spec"]
    fan_in = node_map["n-cli"]
    assert sorted(fan_in.depends_on) == ["n-readiness", "n-spec", "n-store"]
    assert "n-model" in node_map["n-store"].depends_on
    assert "n-store" in fan_in.depends_on
    assert "n-cli" in node_map["n-dogfood"].depends_on
    assert len({n.priority for n in created.nodes}) == len(created.nodes)
    assert all(n.acceptance_criteria for n in created.nodes)
    assert node_map["n-dogfood"].resources.gpu is True
    assert node_map["n-dogfood"].resources.exclusive is True
    assert node_map["n-spec"].resources.model_heavy is True
    assert node_map["n-store"].resources.gpu is None

    # 4: dependency readiness with explicit blocked reasons.
    projection = readiness.project_with_store(root, created)
    assert set(projection.ready()) == {"n-model", "n-spec"}
    assert "n-cli" in projection.blocked()
    assert "n-dogfood" in projection.blocked()
    for node_id in projection.blocked():
        status = projection.by_id()[node_id]
        assert status.eligible is False
        assert status.reason == readiness.REASON_UPSTREAM_NOT_PROVEN
        assert all(not dep.proven for dep in status.dependencies)


def test_production_dogfood_cli_operator_surface(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = str(tmp_path / "root")
    spec = json.loads(DOGFOOD_SPEC.read_text(encoding="utf-8"))
    _create_graph(tmp_path, root, spec)
    capsys.readouterr()
    assert cli.main(["--root", root, "show", GOAL]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert f"goal      : {GOAL}" in out
    assert "size      : nodes=6 edges=6" in out
    assert "ready     : 2 [n-spec n-model]" in out

    assert cli.main(["--root", root, "blocked", GOAL, "--json"]) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert set(payload["blocked"]) == {"n-cli", "n-dogfood", "n-readiness",
                                       "n-store"}

    assert cli.main(["--root", root, "--json", "project", GOAL]) == cli.EXIT_OK
    scheduler = json.loads(capsys.readouterr().out)
    assert scheduler["topological_order"][0] == "n-spec"
    assert set(scheduler["ready"]) == {"n-model", "n-spec"}

    assert cli.main(["--root", root, "validate", GOAL]) == cli.EXIT_OK
    assert "valid    : g-m012-dogfood" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 5: readiness transition on authoritative mission evidence
# ---------------------------------------------------------------------------


def test_dogfood_readiness_transitions_on_mission_evidence(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = str(tmp_path / "root")
    _create_mission(root, "m-dog-a")
    _create_mission(root, "m-dog-b")
    spec = {
        "schema_version": 1,
        "goal_id": "g-transition",
        "objective": "readiness transition",
        "nodes": [
            {
                "node_id": "n-a",
                "title": "first mission",
                "priority": 80,
                "depends_on": [],
                "acceptance_criteria": [
                    {"criterion_id": "ac-1", "statement": "a proven"}],
                "mission_ref": {"mission_id": "m-dog-a", "required": True},
            },
            {
                "node_id": "n-b",
                "title": "dependent mission",
                "priority": 40,
                "depends_on": ["n-a"],
                "acceptance_criteria": [
                    {"criterion_id": "ac-1", "statement": "b proven"}],
                "mission_ref": {"mission_id": "m-dog-b", "required": True},
            },
            {
                "node_id": "n-c",
                "title": "final integration",
                "priority": 10,
                "depends_on": ["n-b"],
                "acceptance_criteria": [
                    {"criterion_id": "ac-1", "statement": "c proven"}],
            },
        ],
    }
    assert cli.main([
        "--root", root, "create", "--spec",
        _write_spec(tmp_path, spec, "transition.json"),
        "--repo", str(tmp_path), "--head", "baseline-sha",
    ]) == cli.EXIT_OK
    capsys.readouterr()
    graph, _ = store.load_graph(root, "g-transition")

    before = readiness.project_with_store(root, graph)
    assert before.by_id()["n-a"].state == readiness.RS_IN_PROGRESS
    assert before.by_id()["n-c"].state == readiness.RS_BLOCKED

    # Only the authoritative mission run can advance the proof.
    orchestrator.run_mission(root, "m-dog-a", _OkRunner())
    after_a = readiness.project_with_store(root, graph)
    assert after_a.by_id()["n-a"].state == readiness.RS_COMPLETE
    assert after_a.by_id()["n-b"].state == readiness.RS_IN_PROGRESS
    assert after_a.by_id()["n-c"].state == readiness.RS_BLOCKED

    orchestrator.run_mission(root, "m-dog-b", _OkRunner())
    final = readiness.project_with_store(root, graph)
    assert final.by_id()["n-b"].state == readiness.RS_COMPLETE
    assert final.by_id()["n-c"].state == readiness.RS_READY
    assert final.ready() == ("n-c",)
    # The graph itself never stores the proof.
    raw = store.graph_paths(root, "g-transition")["graph"].read_text("utf-8")
    assert "mission_state" not in raw
    assert "classification" not in raw


# ---------------------------------------------------------------------------
# 6: controlled malformed / cyclic rejection
# ---------------------------------------------------------------------------


def test_dogfood_rejects_malformed_and_cyclic_graphs(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = str(tmp_path / "root")
    for name, spec, expected in (
        ("cycle.json", {
            "schema_version": 1, "goal_id": "g-cycle", "objective": "cycle",
            "nodes": [
                {"node_id": "n-a", "title": "A", "priority": 1,
                 "depends_on": ["n-b"],
                 "acceptance_criteria": [{"criterion_id": "ac-1",
                                          "statement": "s"}]},
                {"node_id": "n-b", "title": "B", "priority": 2,
                 "depends_on": ["n-a"],
                 "acceptance_criteria": [{"criterion_id": "ac-1",
                                          "statement": "s"}]},
            ]}, model.E_CYCLE),
        ("self.json", {
            "schema_version": 1, "goal_id": "g-self", "objective": "self",
            "nodes": [
                {"node_id": "n-a", "title": "A", "priority": 1,
                 "depends_on": ["n-a"],
                 "acceptance_criteria": [{"criterion_id": "ac-1",
                                          "statement": "s"}]},
            ]}, model.E_SELF_DEPENDENCY),
        ("version.json", {
            "schema_version": 9, "goal_id": "g-ver", "objective": "ver",
            "nodes": [
                {"node_id": "n-a", "title": "A", "priority": 1,
                 "depends_on": [],
                 "acceptance_criteria": [{"criterion_id": "ac-1",
                                          "statement": "s"}]},
            ]}, model.E_UNSUPPORTED_VERSION),
    ):
        code = cli.main([
            "--root", root, "create", "--spec",
            _write_spec(tmp_path, spec, name)])
        assert code == cli.EXIT_REJECTED, name
        assert expected in capsys.readouterr().err
        assert not store.graph_exists(root, spec["goal_id"])


# ---------------------------------------------------------------------------
# 7: M008 / M009 / M010 / M011 compatibility regression
# ---------------------------------------------------------------------------


def test_m008_m009_m010_m011_compatibility_regression(tmp_path: Path) -> None:
    # M008: a SUCCESS without an independently verified exact attestation is
    # never a COMPLETED.
    assert runner.classify_subrun(
        0, semantic_status=semantic.STATUS_SUCCESS,
        attestation=None).classification == mission_model.CR_UNPROVEN
    assert runner.classify_subrun(
        0, semantic_status=semantic.STATUS_SUCCESS,
        attestation=semantic.ATTESTATION_VERIFIED).classification \
        == mission_model.CR_COMPLETED

    # M010: the two patch identity domains remain distinct and are not
    # overloaded by the new graph/spec domains.
    assert identity.WRAPPER_SNAPSHOT_DOMAIN != identity.MISSION_WORKTREE_DOMAIN
    assert identity.WRAPPER_SNAPSHOT_FIELD != identity.MISSION_WORKTREE_FIELD
    from trajectory_os.graph import identity as graph_identity
    assert graph_identity.GRAPH_DOMAIN not in identity.DOMAIN_IDS
    assert graph_identity.SPEC_DOMAIN not in identity.DOMAIN_IDS

    # M009 + M011: a green mission still projects the attestation block,
    # patch identity domains and the single GO COMMIT human gate.
    root = str(tmp_path / "root")
    _create_mission(root, "m-compat")
    report = orchestrator.run_mission(root, "m-compat", _OkRunner())
    assert report.mission_state == mission_model.MS_COMPLETE
    mission, paths = mission_store.load_mission(root, "m-compat")
    document = summary.mission_summary(mission, paths)
    assert set(document["attestation"]) == {
        "model_heavy_subruns", "verified", "unproven", "legacy"}
    assert "patch_identity" in document
    assert document["operator_gate"]["gate"] == gate.GATE_GO_COMMIT
    assert document["operator_gate"]["human_action_required"] is True
    # GO COMMIT is recorded, not performed: no Git write anywhere.
    assert mission.commit_approved_at is None
