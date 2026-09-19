"""Missions 017-019 — visible operator product-layer dogfood.

Deterministic end-to-end proof that a real bounded strategic goal is
launchable through the production path and visibly observable without
reading raw ``.trajectory-pi`` logs:

* M017 — bounded end-to-end execution (goal -> missions -> graph ->
  readiness -> scheduling -> running agent/model -> validation ->
  blockers -> replans -> final goal proof);
* M018 — unified operator CLI (start/status/inspect/explain/dashboard/
  evidence/resume/stop/proof);
* M019 — live one-screen TUI rendering the same canonical snapshot.

Everything is derived from the canonical M012-M016 stores; the deterministic
runner only replaces the model subprocess (exactly as the M016 harness does),
so no network/GPU/Git write is involved.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from trajectory_os.goals import cli, launch, runner, snapshot, tui
from trajectory_os.graph import store as graph_store
from trajectory_os.graph.proof import engine as proof_engine
from trajectory_os.graph.replan import engine as replan_engine
from trajectory_os.missions import model as mission_model
from trajectory_os.missions import semantic
from trajectory_os.missions.runner import SubrunResult

GOAL = "g-visible-operator"
BASE_MISSION = "m-visible-base"
RISK_MISSION = "m-visible-risk"
FINAL_MISSION = "m-visible-final"


class AttestedRunner:
    """Deterministic exact-attestation success for every phase."""

    def run(self, request: Any) -> SubrunResult:
        return SubrunResult(
            0, mission_model.CR_COMPLETED,
            semantic_status=semantic.STATUS_SUCCESS,
            attestation=semantic.ATTESTATION_VERIFIED)


class FailingRunner(AttestedRunner):
    """Fails the RISK mission's validate phase (bounded controlled failure)."""

    def run(self, request: Any) -> SubrunResult:
        if (request.mission_id == RISK_MISSION
                and request.phase_id == "validate"):
            return SubrunResult(1, mission_model.CR_FAILED)
        return super().run(request)


def _spec() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "goal_id": GOAL,
        "objective": "Prove the visible M017-M019 operator product layer.",
        "nodes": [
            {
                "node_id": "n-base",
                "title": "Foundation mission",
                "priority": 90,
                "depends_on": [],
                "acceptance_criteria": [
                    {"criterion_id": "ac-base",
                     "statement": "Foundation work is proven"}],
                "mission_ref": {"mission_id": BASE_MISSION, "required": True},
                "resources": {"cpu_slots": 1},
                "budgets": {"repair_budget": 0, "max_attempts": 1},
            },
            {
                "node_id": "n-risk",
                "title": "Risk mission",
                "priority": 70,
                "depends_on": ["n-base"],
                "acceptance_criteria": [
                    {"criterion_id": "ac-risk",
                     "statement": "Risk work is proven"}],
                "mission_ref": {"mission_id": RISK_MISSION, "required": True},
                "resources": {"cpu_slots": 1},
                "budgets": {"repair_budget": 0, "max_attempts": 1},
            },
            {
                "node_id": "n-final",
                "title": "Final proof mission",
                "priority": 30,
                "depends_on": ["n-risk"],
                "acceptance_criteria": [
                    {"criterion_id": "ac-final",
                     "statement": "Final work is proven"}],
                "mission_ref": {"mission_id": FINAL_MISSION, "required": True},
                "resources": {"cpu_slots": 1},
                "budgets": {"repair_budget": 0, "max_attempts": 1},
            },
        ],
    }


def _defaults() -> launch.MissionLaunchDefaults:
    return launch.MissionLaunchDefaults(
        repo_root=None, baseline_revision="base", repair_budget=0)


def _setup(root: str, spec: dict[str, Any],
           defaults: launch.MissionLaunchDefaults | None = None) -> Any:
    defaults = defaults or _defaults()
    normalized, _outcomes = launch.provision_from_spec(root, spec, defaults)
    return graph_store.create_graph(
        root, spec, repo_root=defaults.repo_root,
        baseline_revision=defaults.baseline_revision)


def _replan_spec() -> dict[str, Any]:
    return {
        "trigger": {
            "kind": "MISSION_FAILURE",
            "source": "m017-m019 dogfood",
            "detail": "bounded deterministic supersession of the failed node",
            "provenance": {"mission_id": RISK_MISSION},
        },
        "changes": [{
            "op": "SUPERSEDE_NODE",
            "reason": "replace the failed risk node",
            "node_id": "n-risk",
            "node_spec": {
                "node_id": "n-risk2",
                "title": "Recovered risk mission",
                "priority": 70,
                "depends_on": ["n-base"],
                "acceptance_criteria": [
                    {"criterion_id": "ac-risk",
                     "statement": "Risk work is proven"}],
                "mission_ref": {"mission_id": "m-visible-risk2",
                                "required": True},
                "resources": {"cpu_slots": 1},
                "budgets": {"repair_budget": 0, "max_attempts": 1},
            },
        }],
    }


# ---------------------------------------------------------------------------
# M017 — real visible end-to-end execution
# ---------------------------------------------------------------------------


def test_goal_completes_end_to_end_with_visible_transitions(
        tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    _setup(root, _spec())
    report = runner.run_goal(
        root, GOAL, runner_factory=AttestedRunner)
    assert report.status == runner.GS_COMPLETE
    assert report.complete
    assert report.final_reason == "ALL_CRITERIA_PROVEN"
    assert report.proof_id
    # The transitions visibly show readiness, scheduling and dispatch.
    dispatch_steps = [t for t in report.transitions
                      if t["action"] == "dispatch"]
    assert dispatch_steps
    dispatched_missions = [
        d["mission_id"] for step in dispatch_steps
        for d in step["dispatched"]
    ]
    assert BASE_MISSION in dispatched_missions
    assert RISK_MISSION in dispatched_missions
    assert FINAL_MISSION in dispatched_missions
    # Dependency readiness is respected: the base node is dispatched before
    # the downstream node, and blocked nodes carry explicit reasons.
    first_cycle = dispatch_steps[0]
    assert first_cycle["admitted"] == ["n-base"]
    assert any(b["node_id"] == "n-risk"
               for b in first_cycle["blocked"])

    snap = snapshot.build_snapshot(root, GOAL)
    assert snap["final"]["complete"] is True
    assert snap["proof"]["stale"] is False
    assert snap["counts"]["missions_proven"] == 3
    assert snap["counts"]["criteria_proven"] == 3
    assert snap["readiness"]["complete"] == ["n-base", "n-risk", "n-final"]
    assert snap["critical_path"]
    # The unified human status and the live TUI render the same snapshot.
    status = snapshot.render_status(snap)
    assert "goal      :" in status
    assert "criteria  :" in status
    frame = tui.render_frame(snap, width=100, height=40)
    for expected in (
            "TrajectoryOS goal dashboard", "goal      :", "generation:",
            "missions  :", "nodes     :", "criteria  :", "path      :",
            "gate      :", "resources :", "active:", "replans   :",
            "blockers  :", "evidence:"):
        assert expected in frame, expected


def test_goal_safe_stop_blocks_new_work_and_resume_completes(
        tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    _setup(root, _spec())
    # No state is scheduled yet; request the stop before the first cycle.
    runner.request_stop(root, GOAL, reason="operator requested")
    snap = snapshot.build_snapshot(root, GOAL)
    assert snap["stop"] is not None
    assert snap["stop"]["reason"] == "operator requested"
    report = runner.run_goal(root, GOAL, runner_factory=AttestedRunner,
                             config=runner.GoalRunConfig(clear_stop=False))
    assert report.status == runner.GS_STOPPED
    assert report.reason == "SAFE_STOP_REQUESTED"
    assert report.complete is False
    # No mission was launched while the safe stop was requested.
    assert not [step for step in report.transitions
                if step["action"] == "dispatch" and step.get("dispatched")]

    # Resume explicitly clears the stop and drives to completion.
    resumed = runner.run_goal_resume(
        root, GOAL, runner_factory=AttestedRunner)
    assert resumed.status == runner.GS_COMPLETE
    assert runner.is_stop_requested(root, GOAL) is False


def test_goal_failure_stalls_then_replan_resume_completes(
        tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    _setup(root, _spec())
    report = runner.run_goal(root, GOAL, runner_factory=FailingRunner)
    assert report.status in (runner.GS_STALLED, runner.GS_DISPATCH_ERROR)
    assert report.complete is False

    snap = snapshot.build_snapshot(root, GOAL)
    assert snap["final"]["complete"] is False
    # The blocker is visible and names the failed node/mission.
    assert any(b["subject"] == "n-risk"
               for b in snap["blockers"] if b["subject"] is not None)
    risk_node = next(n for n in snap["decomposition"]
                     if n["node_id"] == "n-risk")
    assert risk_node["mission_state"] in (
        mission_model.MS_FAILED, mission_model.MS_BLOCKED)

    # Bounded deterministic replan supersedes the failed node and adopts the
    # scheduler onto the new generation. The replacement mission is
    # provisioned by the resume path.
    applied = replan_engine.apply_spec(root, GOAL, _replan_spec(),
                                       created_at="t-replan")
    assert applied.accepted, applied.reason
    assert applied.generation.generation_number == 2

    resumed = runner.run_goal_resume(
        root, GOAL, runner_factory=AttestedRunner)
    assert resumed.status == runner.GS_COMPLETE

    snap = snapshot.build_snapshot(root, GOAL)
    assert snap["final"]["complete"] is True
    assert snap["generation"]["generation_number"] == 2
    assert snap["replans"]["projection"]["accepted"] == 1
    assert snap["replans"]["projection"]["supersessions"] == 1
    assert snap["replans"]["history"]["count"] >= 1
    assert any(n["node_id"] == "n-risk2"
               for n in snap["decomposition"])


def test_goal_completes_with_single_subrun_sessions(tmp_path: Path) -> None:
    """A bounded session (one sub-run) resumes in-flight work to completion."""
    root = str(tmp_path / "root")
    _setup(root, _spec())
    report = runner.run_goal(
        root, GOAL, runner_factory=AttestedRunner,
        config=runner.GoalRunConfig(session_subruns=1, max_cycles=64))
    assert report.status == runner.GS_COMPLETE
    actions = [step["action"] for step in report.transitions]
    assert "continue" in actions
    assert "dispatch" in actions


def test_snapshot_shows_running_agent_model_and_locality(
        tmp_path: Path) -> None:
    """M017: an in-flight mission is visibly attributed to agent/model."""
    from trajectory_os.graph.scheduler import engine as sched_engine
    from trajectory_os.graph.scheduler import model as sched_model

    root = str(tmp_path / "root")
    _setup(root, _spec())
    result = sched_engine.run_cycle(
        root, GOAL, sched_model.DEFAULT_POLICY, dispatch=True,
        dispatcher=sched_engine.MissionPathDispatcher(
            runner_factory=AttestedRunner, session_subruns=1),
        created_at="2026-01-01T00:00:00Z")
    assert result.dispatched
    # The base mission is deliberately mid-flight (one phase per session).
    snap = snapshot.build_snapshot(root, GOAL)
    agent = snap["activity"]["agent"]
    assert agent is not None
    assert agent["mission_id"] == BASE_MISSION
    assert agent["mission_state"] == mission_model.MS_RUNNING
    assert agent["agent_backend"] == "trajectory-pi"
    assert agent["model"] == "qwen3.8-dev3090"
    assert agent["provider"] == "ollama"
    assert agent["locality"] == "local"
    # Validation/review state is exposed even before those phases run.
    assert agent["validate_state"] == mission_model.PS_PENDING
    assert agent["review_state"] == mission_model.PS_PENDING


def test_snapshot_reconstruction_after_restart(tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    _setup(root, _spec())
    report = runner.run_goal(root, GOAL, runner_factory=AttestedRunner)
    assert report.complete
    before = snapshot.build_snapshot(root, GOAL)
    # A fresh read (process restart) reproduces the exact proof identity.
    after = snapshot.build_snapshot(root, GOAL)
    assert before["proof"]["proof_id"] == after["proof"]["proof_id"]
    assert before["proof"]["persisted_proof_id"] == \
        after["proof"]["persisted_proof_id"]
    read = proof_engine.reconstruct(root, GOAL)
    assert read.reconstructed is True
    assert read.persisted.proof_id == read.live.proof_id == report.proof_id


# ---------------------------------------------------------------------------
# M018 — unified operator CLI
# ---------------------------------------------------------------------------


def test_unified_cli_lifecycle(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    root = str(tmp_path / "root")
    spec_path = tmp_path / "goal.json"
    spec_path.write_text(json.dumps(_spec()), encoding="utf-8")

    real_run_goal = runner.run_goal

    def fake_run_goal(root_arg: str, goal_id: str, *,
                      config: Any = None, dispatcher: Any = None,
                      runner_factory: Any = None,
                      clock: Any = None) -> runner.GoalRunReport:
        return real_run_goal(root_arg, goal_id, config=config,
                             runner_factory=AttestedRunner)

    monkeypatch.setattr(cli.goal_runner, "run_goal", fake_run_goal)

    # start: provision missions + graph + bounded run.
    code = cli.main(["--root", root, "start", "--spec", str(spec_path),
                     "--json"])
    assert code == cli.EXIT_OK
    started = json.loads(capsys.readouterr().out)
    assert started["status"] == "STARTED"
    assert started["run"]["status"] == runner.GS_COMPLETE
    assert started["missions"][BASE_MISSION] == "created"

    # status / inspect / explain / dashboard / evidence / proof / list.
    assert cli.main(["--root", root, "status", GOAL, "--json"]) == cli.EXIT_OK
    status = json.loads(capsys.readouterr().out)
    assert status["final"]["complete"] is True

    assert cli.main(["--root", root, "inspect", GOAL, "--json"]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["goal"]["goal_id"] == GOAL

    assert cli.main(["--root", root, "explain", GOAL, "n-base",
                     "--json"]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["node"]["node_id"] == "n-base"

    assert cli.main(["--root", root, "dashboard", GOAL, "--json"]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["final"]["complete"] is True

    assert cli.main(["--root", root, "evidence", GOAL, "--json"]) == cli.EXIT_OK
    evidence = json.loads(capsys.readouterr().out)
    assert evidence["proof_id"] == started["run"]["proof_id"]
    assert len(evidence["missions"]) == 3

    assert cli.main(["--root", root, "proof", GOAL, "--json"]) == cli.EXIT_OK
    proof = json.loads(capsys.readouterr().out)
    assert proof["reconstructed"] is True
    assert proof["complete"] is True

    assert cli.main(["--root", root, "list", "--json"]) == cli.EXIT_OK
    listing = json.loads(capsys.readouterr().out)
    assert any(g["goal_id"] == GOAL for g in listing["goals"])

    # stop is a request only and is visible to the snapshot.
    assert cli.main(["--root", root, "stop", GOAL,
                     "--reason", "cli test", "--json"]) == cli.EXIT_OK
    stopped = json.loads(capsys.readouterr().out)
    assert stopped["status"] == "STOP_REQUESTED"
    assert snapshot.build_snapshot(root, GOAL)["stop"]["reason"] == "cli test"

    # A second start is rejected (never silently resumes).
    assert cli.main(["--root", root, "start", "--spec", str(spec_path),
                     "--json"]) == cli.EXIT_REJECTED


def test_unified_cli_tui_renders_one_frame(
        tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    _setup(root, _spec())
    runner.run_goal(root, GOAL, runner_factory=AttestedRunner)
    stream = io.StringIO()
    code = tui.run_tui(root, GOAL, iterations=1, stream=stream,
                       width=100, height=40)
    assert code == 0
    output = stream.getvalue()
    assert output.startswith(tui.CLEAR_SCREEN)
    assert "TrajectoryOS goal dashboard" in output
    assert "proof" in output


# ---------------------------------------------------------------------------
# M019 — pure frame properties
# ---------------------------------------------------------------------------


def test_tui_frame_is_bounded_and_deterministic(tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    _setup(root, _spec())
    runner.run_goal(root, GOAL, runner_factory=AttestedRunner)
    snap = snapshot.build_snapshot(root, GOAL)
    frame_a = tui.render_frame(snap, width=80, height=20)
    frame_b = tui.render_frame(snap, width=80, height=20)
    assert frame_a == frame_b
    lines = frame_a[len(tui.CLEAR_SCREEN):].splitlines()
    assert len(lines) <= 20
    assert all(len(line) <= 80 for line in lines)
    # A read error never leaks a traceback into the live frame.
    error = tui.render_error_frame("GraphNotFound: missing",
                                   width=80, height=20)
    assert "GraphNotFound: missing" in error
    assert "Traceback" not in error


def test_stop_marker_roundtrip_and_clear(tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    _setup(root, _spec())
    assert runner.is_stop_requested(root, GOAL) is False
    document = runner.request_stop(root, GOAL, reason="halt",
                                   requested_at="2026-01-01T00:00:00Z")
    assert document["reason"] == "halt"
    assert runner.is_stop_requested(root, GOAL) is True
    read = snapshot.read_stop_request(root, GOAL)
    assert read is not None and read["reason"] == "halt"
    runner.clear_stop(root, GOAL)
    assert runner.is_stop_requested(root, GOAL) is False


def test_snapshot_rejects_unknown_goal(tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    Path(root).mkdir(parents=True, exist_ok=True)
    with pytest.raises(graph_store.GraphNotFound):
        snapshot.build_snapshot(root, "g-does-not-exist")


def test_no_git_trust_boundary_writes_in_product_layer() -> None:
    git_write_verbs = (
        "commit", "push", "merge", "reset", "restore", "clean", "stash",
        "rebase", "switch", "checkout", "pull", "fetch", "cherry-pick",
    )
    for module in (cli, launch, runner, snapshot, tui):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "os.system" not in source
        for verb in git_write_verbs:
            assert f'"git", "{verb}"' not in source, (module.__name__, verb)
