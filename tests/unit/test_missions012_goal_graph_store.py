"""Mission 012 — goal graph persistence and reconstruction tests.

Covers atomic persistence, strict reconstruction equivalence, identity
mismatch rejection, unsupported/malformed/oversized persisted state,
duplicate goal rejection, required mission reference validation (missing /
malformed / contradictory), and the invariant that the graph never
duplicates or promotes mission trust evidence.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trajectory_os.graph import evidence, model, store
from trajectory_os.missions import model as mission_model
from trajectory_os.missions import orchestrator
from trajectory_os.missions import store as mission_store
from trajectory_os.missions.runner import SubrunResult

GOAL = "g-store"


def _spec(nodes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "goal_id": GOAL,
        "objective": "persistence goal",
        "nodes": nodes or [
            {
                "node_id": "n-a",
                "title": "A",
                "priority": 70,
                "depends_on": [],
                "acceptance_criteria": [
                    {"criterion_id": "ac-1", "statement": "a done"}],
                "resources": {"cpu_slots": 1},
                "budgets": {"subruns": 2, "time_budget_s": 600},
            },
            {
                "node_id": "n-b",
                "title": "B",
                "priority": 20,
                "depends_on": ["n-a"],
                "acceptance_criteria": [
                    {"criterion_id": "ac-1", "statement": "b done"}],
            },
        ],
    }


class _OkRunner:
    def run(self, request: Any) -> SubrunResult:
        return SubrunResult(0, mission_model.CR_COMPLETED)


def _make_mission(root: str, mission_id: str) -> None:
    orchestrator.create_mission(root, orchestrator.MissionConfig(
        mission_id=mission_id,
        objective="referenced mission",
        phase_specs=orchestrator.default_phase_specs(
            {kind: ("true",) for kind in mission_model.CANONICAL_SEQUENCE}),
        baseline_revision="baseline-sha",
    ))


def _complete_mission(root: str, mission_id: str) -> None:
    _make_mission(root, mission_id)
    orchestrator.run_mission(root, mission_id, _OkRunner())


# --- persistence round-trip / reconstruction ---------------------------------


def test_persistence_round_trip_and_reconstruction_equivalence(
        tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    created = store.create_graph(
        root, _spec(), repo_root="/repo", baseline_revision="abc123",
        created_at="2026-09-18T00:00:00Z")
    reloaded, paths = store.load_graph(root, GOAL)
    assert paths["graph"].is_file()
    assert reloaded.to_dict() == created.to_dict()
    assert reloaded.graph_id == created.graph_id
    assert reloaded.spec_sha256 == created.spec_sha256
    assert [n.node_id for n in reloaded.nodes] == ["n-a", "n-b"]
    assert [e.to_dict() for e in reloaded.edges] == [
        {"from": "n-a", "to": "n-b"}]
    assert reloaded.topological_order() == created.topological_order()
    assert reloaded.provenance.repo_root == "/repo"
    assert reloaded.provenance.baseline_revision == "abc123"
    # reconstruction is stable when repeated
    again, _ = store.load_graph(root, GOAL)
    assert again.to_dict() == reloaded.to_dict()


def test_creation_event_is_appended(tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    created = store.create_graph(
        root, _spec(), created_at="2026-09-18T00:00:00Z")
    events = store.load_events(root, GOAL)
    assert len(events) == 1
    assert events[0]["event"] == "created"
    assert events[0]["graph_id"] == created.graph_id
    assert events[0]["spec_sha256"] == created.spec_sha256


def test_duplicate_goal_rejected(tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    store.create_graph(root, _spec())
    with pytest.raises(store.GraphExists):
        store.create_graph(root, _spec())


def test_missing_goal_raises_not_found(tmp_path: Path) -> None:
    with pytest.raises(store.GraphNotFound):
        store.load_graph(str(tmp_path / "root"), "g-absent")
    assert store.list_goal_ids(str(tmp_path / "root")) == []


def test_list_goal_ids_is_sorted(tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    for goal_id in ("g-b", "g-a"):
        store.create_graph(root, {**_spec(), "goal_id": goal_id})
    assert store.list_goal_ids(root) == ["g-a", "g-b"]


# --- strict reconstruction / fail-closed -------------------------------------


def _graph_path(root: str) -> Path:
    return store.graph_paths(root, GOAL)["graph"]


def test_identity_mismatch_on_tampered_node(tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    store.create_graph(root, _spec())
    raw = json.loads(_graph_path(root).read_text(encoding="utf-8"))
    raw["nodes"][0]["title"] = "tampered"
    _graph_path(root).write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(model.GraphValidationError) as exc:
        store.load_graph(root, GOAL)
    assert exc.value.code == model.E_IDENTITY_MISMATCH


def test_identity_mismatch_on_tampered_graph_id(tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    store.create_graph(root, _spec())
    raw = json.loads(_graph_path(root).read_text(encoding="utf-8"))
    raw["graph_id"] = "0" * 64
    _graph_path(root).write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(model.GraphValidationError) as exc:
        store.load_graph(root, GOAL)
    assert exc.value.code == model.E_IDENTITY_MISMATCH


def test_tampered_edge_set_rejected(tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    store.create_graph(root, _spec())
    raw = json.loads(_graph_path(root).read_text(encoding="utf-8"))
    raw["edges"] = []
    _graph_path(root).write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(model.GraphValidationError) as exc:
        store.load_graph(root, GOAL)
    assert exc.value.code == model.E_MALFORMED


def test_unsupported_persisted_schema_version_rejected(tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    store.create_graph(root, _spec())
    raw = json.loads(_graph_path(root).read_text(encoding="utf-8"))
    raw["schema_version"] = 99
    _graph_path(root).write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(model.GraphValidationError) as exc:
        store.load_graph(root, GOAL)
    assert exc.value.code == model.E_UNSUPPORTED_VERSION


def test_malformed_persisted_json_rejected(tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    store.create_graph(root, _spec())
    _graph_path(root).write_text("{not json", encoding="utf-8")
    with pytest.raises(model.GraphValidationError) as exc:
        store.load_graph(root, GOAL)
    assert exc.value.code == model.E_MALFORMED


def test_unknown_persisted_field_rejected(tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    store.create_graph(root, _spec())
    raw = json.loads(_graph_path(root).read_text(encoding="utf-8"))
    raw["unexpected"] = 1
    _graph_path(root).write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(model.GraphValidationError) as exc:
        store.load_graph(root, GOAL)
    assert exc.value.code == model.E_MALFORMED


def test_stale_temp_file_does_not_break_reconstruction(tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    created = store.create_graph(root, _spec())
    # A crashed writer may leave a private temp file behind; the committed
    # document stays authoritative and is never partially read.
    stale = _graph_path(root).parent / ".graph.json.stale.tmp"
    stale.write_text("{incomplete", encoding="utf-8")
    reloaded, _ = store.load_graph(root, GOAL)
    assert reloaded.graph_id == created.graph_id


# --- required mission reference validation -----------------------------------


def test_missing_required_reference_rejected(tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    spec = _spec([{
        "node_id": "n-a", "title": "A", "priority": 1, "depends_on": [],
        "acceptance_criteria": [{"criterion_id": "ac-1", "statement": "s"}],
        "mission_ref": {"mission_id": "m-absent", "required": True},
    }])
    with pytest.raises(model.GraphValidationError) as exc:
        store.create_graph(root, spec)
    assert exc.value.code == model.E_UNRESOLVED_REFERENCE
    assert not store.graph_exists(root, GOAL)


def test_malformed_referenced_mission_rejected(tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    _make_mission(root, "m-bad")
    mission_path = mission_store.mission_paths(root, "m-bad")["mission"]
    raw = json.loads(mission_path.read_text(encoding="utf-8"))
    raw["unexpected"] = True
    mission_path.write_text(json.dumps(raw), encoding="utf-8")
    spec = _spec([{
        "node_id": "n-a", "title": "A", "priority": 1, "depends_on": [],
        "acceptance_criteria": [{"criterion_id": "ac-1", "statement": "s"}],
        "mission_ref": {"mission_id": "m-bad", "required": True},
    }])
    with pytest.raises(model.GraphValidationError) as exc:
        store.create_graph(root, spec)
    assert exc.value.code == model.E_REFERENCE_INVALID


def test_contradictory_referenced_mission_rejected(tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    _complete_mission(root, "m-contra")
    mission, paths = mission_store.load_mission(root, "m-contra")
    last = mission.phase("plan").subrun_ids[-1]
    record = mission_store.load_subrun(paths, last)
    record.classification = mission_model.CR_FAILED
    mission_store.save_subrun(record, paths)
    spec = _spec([{
        "node_id": "n-a", "title": "A", "priority": 1, "depends_on": [],
        "acceptance_criteria": [{"criterion_id": "ac-1", "statement": "s"}],
        "mission_ref": {"mission_id": "m-contra", "required": True},
    }])
    with pytest.raises(model.GraphValidationError) as exc:
        store.create_graph(root, spec)
    assert exc.value.code == model.E_CONTRADICTORY_REFERENCE


def test_non_required_missing_reference_is_allowed(tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    spec = _spec([{
        "node_id": "n-a", "title": "A", "priority": 1, "depends_on": [],
        "acceptance_criteria": [{"criterion_id": "ac-1", "statement": "s"}],
        "mission_ref": {"mission_id": "m-future", "required": False},
    }])
    graph = store.create_graph(root, spec)
    assert graph.nodes[0].mission_ref is not None


def test_required_reference_to_existing_mission_accepted(tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    _complete_mission(root, "m-live")
    spec = _spec([{
        "node_id": "n-a", "title": "A", "priority": 1, "depends_on": [],
        "acceptance_criteria": [{"criterion_id": "ac-1", "statement": "s"}],
        "mission_ref": {"mission_id": "m-live", "required": True},
    }])
    store.create_graph(root, spec)
    record = evidence.resolve_mission_evidence(root, "m-live")
    assert record.proven_complete is True


# --- graph is not a second source of truth -----------------------------------


def test_persisted_graph_does_not_duplicate_mission_evidence(
        tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    _complete_mission(root, "m-live")
    spec = _spec([{
        "node_id": "n-a", "title": "A", "priority": 1, "depends_on": [],
        "acceptance_criteria": [{"criterion_id": "ac-1", "statement": "s"}],
        "mission_ref": {"mission_id": "m-live", "required": True},
    }])
    store.create_graph(root, spec)
    raw = _graph_path(root).read_text(encoding="utf-8")
    for forbidden in (
        "mission_state", "mission_reason", "classification", "attestation",
        "semantic_status", "patch_sha256", "subrun_id",
        "worktree_patch_sha256", "exit_code",
    ):
        assert forbidden not in raw, forbidden
    # only the reference identity is persisted
    assert "m-live" in raw


def test_evidence_resolution_is_read_only(tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    _complete_mission(root, "m-live")
    mission_path = mission_store.mission_paths(root, "m-live")["mission"]
    before = mission_path.read_bytes()
    evidence.resolve_mission_evidence(root, "m-live")
    assert mission_path.read_bytes() == before
