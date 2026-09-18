"""Mission 012 — goal graph operator CLI tests.

Covers the production operator surface from Issue #215: create from a
declarative spec, inspect/list nodes and edges, deterministic topological
order, ready and blocked nodes, dependency-block explanations, machine-
readable M013 projection, reconstruction/validation, exit codes, and the
absence of operator micro-gates or autonomous Git writes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from trajectory_os.graph import cli, evidence, model
from trajectory_os.graph import store as graph_store
from trajectory_os.missions import model as mission_model
from trajectory_os.missions import orchestrator
from trajectory_os.missions import store as mission_store

GOAL = "g-cli"


def _node(node_id: str, *, depends_on: list[str] | None = None,
          mission_ref: dict[str, Any] | None = None,
          priority: int = 50) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "title": f"node {node_id}",
        "priority": priority,
        "depends_on": list(depends_on or []),
        "acceptance_criteria": [
            {"criterion_id": "ac-1", "statement": f"{node_id} done"}],
        "mission_ref": mission_ref,
        "resources": {"cpu_slots": 1},
        "budgets": {"subruns": 2},
    }


def _spec(nodes: list[dict[str, Any]], *, goal_id: str = GOAL) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "goal_id": goal_id,
        "objective": "CLI goal",
        "nodes": nodes,
    }


def _write_spec(tmp_path: Path, spec: object,
                name: str = "spec.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(spec), encoding="utf-8")
    return str(path)


def _create(tmp_path: Path, spec: object) -> str:
    root = str(tmp_path / "root")
    code = cli.main([
        "--root", root, "create", "--spec", _write_spec(tmp_path, spec),
        "--repo", str(tmp_path), "--head", "deadbeef",
    ])
    assert code == cli.EXIT_OK
    return root


def _last_json(text: str) -> dict[str, Any]:
    return json.loads(text)


@pytest.fixture()
def root(tmp_path: Path) -> str:
    return _create(tmp_path, _spec([_node("n-a"), _node("n-b", depends_on=["n-a"])]))


# --- create -------------------------------------------------------------------


def test_create_reports_ready_and_blocked(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = str(tmp_path / "root")
    code = cli.main([
        "--root", root, "create", "--spec", _write_spec(tmp_path, _spec([
            _node("n-a", priority=90),
            _node("n-b", priority=10, depends_on=["n-a"]),
        ])),
        "--repo", str(tmp_path), "--head", "deadbeef",
    ])
    assert code == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "created  : g-cli" in out
    assert "baseline=deadbeef" in out
    assert "ready    : 1 [n-a]" in out
    assert "blocked  : 1 [n-b]" in out
    assert graph_store.graph_exists(root, GOAL)


def test_create_json_is_machine_readable(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = str(tmp_path / "root")
    code = cli.main([
        "--root", root, "--json", "create", "--spec",
        _write_spec(tmp_path, _spec([_node("n-a")])),
        "--repo", str(tmp_path), "--head", "deadbeef",
    ])
    assert code == cli.EXIT_OK
    payload = _last_json(capsys.readouterr().out)
    assert payload["status"] == "CREATED"
    assert payload["goal_id"] == GOAL
    assert payload["ready"] == ["n-a"]
    assert len(payload["graph_id"]) == 64
    assert payload["topological_order"] == ["n-a"]


def test_create_duplicate_goal_rejected(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _create(tmp_path, _spec([_node("n-a")]))
    code = cli.main([
        "--root", root, "create", "--spec",
        _write_spec(tmp_path, _spec([_node("n-a")]), "spec2.json"),
    ])
    assert code == cli.EXIT_REJECTED
    assert "already exists" in capsys.readouterr().err


def test_create_missing_spec_is_usage_error(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = str(tmp_path / "root")
    code = cli.main([
        "--root", root, "create", "--spec", str(tmp_path / "absent.json")])
    assert code == cli.EXIT_USAGE
    assert "spec" in capsys.readouterr().err


def test_create_invalid_json_is_usage_error(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    code = cli.main([
        "--root", str(tmp_path / "root"), "create", "--spec", str(path)])
    assert code == cli.EXIT_USAGE
    assert "not valid JSON" in capsys.readouterr().err


def test_create_unsupported_version_rejected(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    spec = _spec([_node("n-a")])
    spec["schema_version"] = 7
    code = cli.main([
        "--root", str(tmp_path / "root"), "create", "--spec",
        _write_spec(tmp_path, spec)])
    assert code == cli.EXIT_REJECTED
    assert model.E_UNSUPPORTED_VERSION in capsys.readouterr().err


def test_create_cycle_rejected(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main([
        "--root", str(tmp_path / "root"), "create", "--spec",
        _write_spec(tmp_path, _spec([
            _node("n-a", depends_on=["n-b"]),
            _node("n-b", depends_on=["n-a"]),
        ]))])
    assert code == cli.EXIT_REJECTED
    assert model.E_CYCLE in capsys.readouterr().err


def test_create_missing_required_reference_rejected(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main([
        "--root", str(tmp_path / "root"), "create", "--spec",
        _write_spec(tmp_path, _spec([
            _node("n-a", mission_ref={"mission_id": "m-absent"}),
        ]))])
    assert code == cli.EXIT_REJECTED
    assert model.E_UNRESOLVED_REFERENCE in capsys.readouterr().err


# --- inspection ---------------------------------------------------------------


def test_show_human_output(root: str,
                           capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--root", root, "show", GOAL]) == cli.EXIT_OK
    out = capsys.readouterr().out
    for expected in ("goal      : g-cli", "objective : CLI goal", "graph     :",
                     "spec      :", "size      : nodes=2 edges=1",
                     "order     : n-a n-b", "ready     : 1 [n-a]",
                     "blocked   : 1 [n-b]", "complete  : 0"):
        assert expected in out, expected


def test_nodes_edges_order_human_and_json(
        root: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--root", root, "nodes", GOAL]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "n-a  READY" in out and "n-b  BLOCKED" in out

    assert cli.main(["--root", root, "edges", GOAL]) == cli.EXIT_OK
    assert "n-a -> n-b" in capsys.readouterr().out

    assert cli.main(["--root", root, "order", GOAL, "--json"]) == cli.EXIT_OK
    payload = _last_json(capsys.readouterr().out)
    assert payload["topological_order"] == ["n-a", "n-b"]

    assert cli.main(["--root", root, "nodes", GOAL, "--json"]) == cli.EXIT_OK
    payload = _last_json(capsys.readouterr().out)
    assert [n["node_id"] for n in payload["nodes"]] == ["n-a", "n-b"]
    assert payload["nodes"][1]["state"] == "BLOCKED"

    assert cli.main(["--root", root, "edges", GOAL, "--json"]) == cli.EXIT_OK
    payload = _last_json(capsys.readouterr().out)
    assert payload["edges"] == [{"from": "n-a", "to": "n-b"}]


def test_ready_and_blocked_with_reasons(
        root: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--root", root, "ready", GOAL]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "ready (1): n-a" in out

    assert cli.main(["--root", root, "blocked", GOAL]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "blocked (1): n-b" in out
    assert "UPSTREAM_NOT_PROVEN" in out
    assert "blocked_by=n-a=READY" in out

    assert cli.main(["--root", root, "blocked", GOAL, "--json"]) == cli.EXIT_OK
    payload = _last_json(capsys.readouterr().out)
    assert payload["blocked"] == ["n-b"]
    assert payload["nodes"][0]["dependencies"][0]["proven"] is False


def test_why_explains_block_reason(
        root: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--root", root, "why", GOAL, "n-b"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "node      : n-b" in out
    assert "state     : BLOCKED (UPSTREAM_NOT_PROVEN)" in out
    assert "n-a [READY] proven=False UPSTREAM_READY" in out
    assert "ac-1: n-b done" in out


def test_why_unknown_node_is_usage_error(
        root: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--root", root, "why", GOAL, "n-absent"]) \
        == cli.EXIT_USAGE
    assert "node not found" in capsys.readouterr().err


def test_missing_goal_is_not_found(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = str(tmp_path / "root")
    assert cli.main(["--root", root, "show", "g-absent"]) == cli.EXIT_NOT_FOUND
    assert "no goal graph" in capsys.readouterr().err


# --- machine projection / validation -----------------------------------------


def test_project_emits_m013_projection(root: str,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--root", root, "--json", "project", GOAL]) == cli.EXIT_OK
    payload = _last_json(capsys.readouterr().out)
    assert payload["goal_id"] == GOAL
    assert payload["ready"] == ["n-a"]
    assert payload["blocked"] == ["n-b"]
    assert payload["topological_order"] == ["n-a", "n-b"]
    node = payload["nodes"][0]
    assert node["acceptance_criteria"][0]["criterion_id"] == "ac-1"
    assert "resources" in node and "budgets" in node


def test_validate_reports_identity_and_references(
        root: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--root", root, "validate", GOAL, "--json"]) == cli.EXIT_OK
    payload = _last_json(capsys.readouterr().out)
    assert payload["status"] == "VALID"
    assert payload["size"] == {"nodes": 2, "edges": 1}
    assert payload["topological_order"] == ["n-a", "n-b"]
    assert len(payload["graph_id"]) == 64
    assert payload["identity"]["graph"]["domain"] == \
        "trajectory-os.goal-decomposition-graph.v1"
    assert payload["identity"]["spec"]["domain"] == \
        "trajectory-os.goal-decomposition-spec.v1"

    graph_path = graph_store.graph_paths(root, GOAL)["graph"]
    raw = json.loads(graph_path.read_text(encoding="utf-8"))
    raw["nodes"][0]["priority"] = 99
    graph_path.write_text(json.dumps(raw), encoding="utf-8")
    assert cli.main(["--root", root, "validate", GOAL]) \
        == cli.EXIT_REJECTED
    assert "GRAPH_IDENTITY_MISMATCH" in capsys.readouterr().err


def test_list_reports_goals(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _create(tmp_path, _spec([_node("n-a")], goal_id="g-one"))
    capsys.readouterr()
    root = str(tmp_path / "root")
    assert cli.main(["--root", root, "list", "--json"]) == cli.EXIT_OK
    payload = _last_json(capsys.readouterr().out)
    assert [entry["goal_id"] for entry in payload["goals"]] == ["g-one"]
    assert cli.main(["--root", root, "list"]) == cli.EXIT_OK
    assert "g-one" in capsys.readouterr().out


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["version"]) == cli.EXIT_OK
    assert "trajectory-pi-goals" in capsys.readouterr().out


# --- reduced-intervention / trust boundary -----------------------------------


def test_read_only_commands_do_not_mutate_state(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = str(tmp_path / "root")
    graph_path = graph_store.graph_paths(root, GOAL)["graph"]
    cli.main(["--root", root, "create", "--spec",
              _write_spec(tmp_path, _spec([_node("n-a")])),
              "--repo", str(tmp_path), "--head", "deadbeef"])
    capsys.readouterr()
    before = graph_path.read_bytes()
    for argv in (["show", GOAL], ["nodes", GOAL], ["edges", GOAL],
                 ["order", GOAL], ["ready", GOAL], ["blocked", GOAL],
                 ["why", GOAL, "n-a"], ["project", GOAL],
                 ["validate", GOAL], ["list"]):
        assert cli.main(["--root", root, *argv]) == cli.EXIT_OK
        capsys.readouterr()
    assert graph_path.read_bytes() == before


def test_graph_projection_never_writes_mission_state(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = str(tmp_path / "root")
    orchestrator.create_mission(root, orchestrator.MissionConfig(
        mission_id="m-live", objective="live",
        phase_specs=orchestrator.default_phase_specs(
            {kind: ("true",) for kind in mission_model.CANONICAL_SEQUENCE}),
    ))
    mission_path = mission_store.mission_paths(root, "m-live")["mission"]
    before = mission_path.read_bytes()
    cli.main(["--root", root, "create", "--spec", _write_spec(tmp_path, _spec([
        _node("n-a", mission_ref={"mission_id": "m-live", "required": True}),
    ])), "--repo", str(tmp_path), "--head", "deadbeef"])
    capsys.readouterr()
    cli.main(["--root", root, "validate", GOAL])
    capsys.readouterr()
    assert evidence.resolve_mission_evidence(root, "m-live").resolved is True
    assert mission_path.read_bytes() == before


_GIT_WRITE_VERBS = frozenset({
    "commit", "push", "merge", "reset", "restore", "clean", "stash",
    "rebase", "switch", "checkout", "add", "am", "pull", "fetch", "tag",
    "remote", "cherry-pick", "revert",
})


def test_no_autonomous_git_trust_boundary_write() -> None:
    for module in (cli, graph_store, model, evidence):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for match in re.finditer(r'"git"\s*,\s*"([a-z0-9-]+)"', source):
            assert match.group(1) in {
                "rev-parse", "diff", "status", "log", "show", "cat-file",
                "ls-files", "symbolic-ref", "rev-list",
            }, (module.__name__, match.group(1))
        for verb in _GIT_WRITE_VERBS:
            assert f'"git", "{verb}"' not in source, (module.__name__, verb)
        assert "os.system" not in source
