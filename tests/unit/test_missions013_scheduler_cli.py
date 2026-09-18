"""Mission 013 — portfolio scheduler CLI operator-surface tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trajectory_os.graph import cli
from trajectory_os.graph import store as graph_store
from trajectory_os.graph.scheduler import store as sched_store
from trajectory_os.missions import model as mission_model
from trajectory_os.missions import orchestrator

GOAL = "g-sched-cli"
POLICY = {"schema_version": 1, "cpu_slots": 4, "gpu_slots": 1,
          "gpu_mem_bytes": 1 << 30, "global_concurrency": 2}


def _mission(root: str, mission_id: str) -> None:
    orchestrator.create_mission(root, orchestrator.MissionConfig(
        mission_id=mission_id,
        objective="cli",
        phase_specs=orchestrator.default_phase_specs(
            {kind: ("true",) for kind in mission_model.CANONICAL_SEQUENCE}),
        baseline_revision="base"))


def _setup(tmp_path: Path) -> tuple[str, str]:
    root = str(tmp_path / "root")
    _mission(root, "m-a")
    _mission(root, "m-b")
    spec = {
        "schema_version": 1, "goal_id": GOAL, "objective": "cli",
        "nodes": [
            {"node_id": "n-a", "title": "A", "priority": 50,
             "depends_on": [],
             "acceptance_criteria": [{"criterion_id": "ac-1",
                                      "statement": "s"}],
             "mission_ref": {"mission_id": "m-a", "required": True},
             "resources": {"cpu_slots": 1}},
            {"node_id": "n-b", "title": "B", "priority": 40,
             "depends_on": ["n-a"],
             "acceptance_criteria": [{"criterion_id": "ac-1",
                                      "statement": "s"}],
             "mission_ref": {"mission_id": "m-b", "required": True},
             "resources": {"cpu_slots": 1}},
        ],
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(POLICY), encoding="utf-8")
    assert cli.main(["--root", root, "create", "--spec", str(spec_path),
                     "--repo", str(tmp_path), "--head", "base"]) == cli.EXIT_OK
    return root, str(policy_path)


def _json(text: str) -> dict[str, Any]:
    return json.loads(text)


def test_capacity_and_preview_human_and_json(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, policy = _setup(tmp_path)
    capsys.readouterr()
    assert cli.main(["--root", root, "capacity", "--policy", policy]) \
        == cli.EXIT_OK
    assert "cpu=4 gpu=1" in capsys.readouterr().out
    assert cli.main(["--root", root, "--json", "capacity", "--policy",
                     policy]) == cli.EXIT_OK
    payload = _json(capsys.readouterr().out)
    assert payload["policy"]["cpu_slots"] == 4
    assert len(payload["policy_id"]) == 64

    assert cli.main(["--root", root, "preview", GOAL, "--policy", policy,
                     "--json"]) == cli.EXIT_OK
    decision = _json(capsys.readouterr().out)
    assert decision["counts"]["admitted"] == 1
    assert {n["node_id"] for n in decision["admitted"]} == {"n-a"}
    assert not sched_store.state_exists(root, GOAL)


def test_schedule_persists_and_validate_reconstructs(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, policy = _setup(tmp_path)
    capsys.readouterr()
    assert cli.main(["--root", root, "schedule", GOAL, "--policy", policy,
                     "--json"]) == cli.EXIT_OK
    result = _json(capsys.readouterr().out)
    assert result["persisted"] is True
    assert sched_store.state_exists(root, GOAL)

    assert cli.main(["--root", root, "validate-schedule", GOAL]) == cli.EXIT_OK
    assert "decisions=1" in capsys.readouterr().out
    assert cli.main(["--root", root, "history", GOAL, "--json"]) == cli.EXIT_OK
    assert _json(capsys.readouterr().out)["count"] == 1


def test_stale_decision_id_is_refused(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, policy = _setup(tmp_path)
    capsys.readouterr()
    code = cli.main(["--root", root, "schedule", GOAL, "--policy", policy,
                     "--decision-id", "0" * 64])
    assert code == cli.EXIT_REJECTED
    assert "STALE_DECISION_INPUT" in capsys.readouterr().err
    assert not sched_store.state_exists(root, GOAL)


def test_invalid_policy_is_usage_error(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, _ = _setup(tmp_path)
    bad = tmp_path / "bad-policy.json"
    bad.write_text(json.dumps({"cpu_slots": 0, "gpu_slots": 0,
                               "gpu_mem_bytes": 0, "global_concurrency": 1}),
                   encoding="utf-8")
    code = cli.main(["--root", root, "preview", GOAL, "--policy", str(bad)])
    assert code == cli.EXIT_USAGE
    assert "policy" in capsys.readouterr().err


def test_missing_goal_is_not_found(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, policy = _setup(tmp_path)
    capsys.readouterr()
    assert cli.main(["--root", root, "schedule", "g-absent", "--policy",
                     policy]) == cli.EXIT_NOT_FOUND
    assert "no goal graph" in capsys.readouterr().err


def test_read_only_scheduler_commands_do_not_mutate_state(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, policy = _setup(tmp_path)
    capsys.readouterr()
    graph_path = graph_store.graph_paths(root, GOAL)["graph"]
    before_graph = graph_path.read_bytes()
    assert cli.main(["--root", root, "schedule", GOAL, "--policy", policy]) \
        == cli.EXIT_OK
    capsys.readouterr()
    state_path = sched_store.scheduler_paths(root, GOAL)["state"]
    before_state = state_path.read_bytes()
    for argv in (["capacity", GOAL, "--policy", policy],
                 ["preview", GOAL, "--policy", policy],
                 ["portfolio", GOAL], ["resources", GOAL],
                 ["history", GOAL], ["why-schedule", GOAL, "n-a"],
                 ["validate-schedule", GOAL]):
        assert cli.main(["--root", root, *argv]) == cli.EXIT_OK
        capsys.readouterr()
    assert state_path.read_bytes() == before_state
    assert graph_path.read_bytes() == before_graph
