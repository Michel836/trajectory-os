"""Mission 015 — production adaptive-replanning dogfood + compatibility.

Production-path deterministic proof for bounded adaptive replanning
(Issue #220 / program #207 checkpoint S15):

1.  create every referenced mission and the M015 goal graph;
2.  drive a controlled mission failure and a deterministic replacement
    (supersession) that rewires dependents;
3.  preserve the prior generation exactly and activate a successful new one;
4.  reject stale triggers and cycles without activating anything;
5.  invalidate M014 reusable evidence so downstream scheduling is blocked;
6.  prove the M013 scheduler only ever sees the latest validated generation
    and never dispatches stale-generation work;
7.  reconstruct exactly after interruption;
8.  exercise the operator CLI surface;
9.  M008-M014 compatibility and Git-safety.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trajectory_os.graph import cli
from trajectory_os.graph import model as graph_model
from trajectory_os.graph import store as graph_store
from trajectory_os.graph.replan import engine as replan_engine
from trajectory_os.graph.replan import model as replan_model
from trajectory_os.graph.replan import store as replan_store
from trajectory_os.graph.reuse import engine as reuse_engine
from trajectory_os.graph.scheduler import engine as scheduler_engine
from trajectory_os.graph.scheduler import model as sched_model
from trajectory_os.graph.scheduler import store as sched_store
from trajectory_os.missions import model as mission_model
from trajectory_os.missions import orchestrator, semantic
from trajectory_os.missions.runner import SubrunResult

REPO_ROOT = Path(__file__).resolve().parents[2]
DOGFOOD_SPEC = REPO_ROOT / "examples" / "adaptive_replanning_dogfood.json"
REPLAN_REQUEST = REPO_ROOT / "examples" / "adaptive_replanning_request.json"
GOAL = "g-m015-dogfood"
POLICY = sched_model.DEFAULT_POLICY
MISSIONS = ("m-m015-up", "m-m015-down", "m-m015-bad", "m-m015-tail",
            "m-m015-up2", "m-m015-bad2")


class AttestedRunner:
    def run(self, request: Any) -> SubrunResult:
        return SubrunResult(
            0, mission_model.CR_COMPLETED,
            semantic_status=semantic.STATUS_SUCCESS,
            attestation=semantic.ATTESTATION_VERIFIED)


class FailingRunner:
    def run(self, request: Any) -> SubrunResult:
        if request.phase_id == "validate":
            return SubrunResult(1, mission_model.CR_FAILED)
        return SubrunResult(0, mission_model.CR_COMPLETED)


def _spec() -> dict[str, Any]:
    return json.loads(DOGFOOD_SPEC.read_text(encoding="utf-8"))


def _request() -> dict[str, Any]:
    return json.loads(REPLAN_REQUEST.read_text(encoding="utf-8"))


def _create_mission(root: str, mission_id: str) -> None:
    orchestrator.create_mission(root, orchestrator.MissionConfig(
        mission_id=mission_id,
        objective="adaptive replanning dogfood mission",
        phase_specs=orchestrator.default_phase_specs(
            {kind: ("true",) for kind in mission_model.CANONICAL_SEQUENCE}),
        baseline_revision="base"))


def _setup(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> str:
    root = str(tmp_path / "root")
    for mission_id in MISSIONS:
        _create_mission(root, mission_id)
    code = cli.main([
        "--root", root, "create", "--spec", str(DOGFOOD_SPEC),
        "--repo", str(REPO_ROOT), "--head", "base",
    ])
    assert code == cli.EXIT_OK
    capsys.readouterr()
    return root


def _json(text: str) -> dict[str, Any]:
    return json.loads(text)


def _trigger(root: str, kind: str, source: str,
             detail: str = "") -> replan_model.ReplanTrigger:
    return replan_engine.build_trigger(root, GOAL, {
        "kind": kind, "source": source, "detail": detail,
        "provenance": {"mission_id": "m-m015-bad"},
    })


# ---------------------------------------------------------------------------
# 1-3: controlled failure, replacement, preserved + successful generations
# ---------------------------------------------------------------------------


def test_dogfood_controlled_failure_and_successful_generation(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _setup(tmp_path, capsys)
    # Produce one proven upstream artifact so M014 reuse is live before replan.
    assert orchestrator.run_mission(root, "m-m015-up", AttestedRunner()
                                    ).mission_state == mission_model.MS_COMPLETE
    # Controlled mission failure of the node to be superseded.
    report = orchestrator.run_mission(root, "m-m015-bad", FailingRunner())
    assert report.mission_state in (mission_model.MS_FAILED,
                                    mission_model.MS_BLOCKED)
    reuse_engine.resolve_and_record(root, GOAL, consumed_at="t1")

    base = replan_engine.current_generation(root, GOAL)
    assert base.generation.generation_number == 1
    base_graph, _ = graph_store.load_graph(root, GOAL)
    base_graph_doc = base_graph.to_dict()
    old_generation_id = base.generation.generation_id

    # Persist one scheduler cycle so generation adoption is exercised.
    scheduler_engine.run_cycle(root, GOAL, POLICY, dispatch=False,
                               created_at="t2")
    assert sched_store.load_state(root, GOAL) is not None

    result = replan_engine.apply_spec(
        root, GOAL, _request(), created_at="t3")
    assert result.accepted, result.reason
    assert result.generation.generation_number == 2
    assert result.generation.parent_generation_id == old_generation_id
    assert result.adopted_scheduler is True

    # The new generation deterministically replaced and rewired the node.
    graph, _ = graph_store.load_graph(root, GOAL)
    assert {n.node_id for n in graph.nodes} == {
        "n-up", "n-down", "n-bad2", "n-tail"}
    tail = graph.node_map()["n-tail"]
    assert tail.depends_on == ("n-bad2",)
    # Prior generation preserved exactly.
    archived, archived_graph = replan_store.load_generation(
        replan_store.replan_paths(root, GOAL), old_generation_id)
    assert archived.generation_number == 1
    assert archived_graph.to_dict() == base_graph_doc

    # Scheduler state was re-bound to the new generation, never stale.
    state = sched_store.load_state(root, GOAL)
    assert state is not None
    assert state.generation_id == result.generation.generation_id
    assert old_generation_id in state.superseded_generations
    assert replan_engine.current_generation(root, GOAL).generation.generation_id \
        == result.generation.generation_id

    # New generation work is schedulable; the failed replacement is not.
    decision = scheduler_engine.build_decision(
        root, GOAL, POLICY, created_at="t4")
    reasons = {n.node_id: n.reason
               for n in (*decision.admitted, *decision.deferred,
                         *decision.blocked, *decision.active,
                         *decision.completed)}
    assert "n-bad2" in reasons
    assert reasons["n-bad2"] in (sched_model.R_REUSE_UNRESOLVED,
                                 sched_model.R_ADMITTED,
                                 sched_model.R_UNRESOLVED_EVIDENCE)


def test_dogfood_invalidated_reuse_blocks_downstream(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _setup(tmp_path, capsys)
    assert orchestrator.run_mission(root, "m-m015-up", AttestedRunner()
                                    ).mission_state == mission_model.MS_COMPLETE
    reuse_engine.resolve_and_record(root, GOAL, consumed_at="t1")
    before = scheduler_engine.build_decision(
        root, GOAL, POLICY, created_at="t1")
    assert "n-down" in {n.node_id for n in before.admitted}

    trigger = _trigger(root, replan_model.TK_INVALIDATED_ASSUMPTION, "n-up",
                       "upstream proof invalidated")
    changes = [
        replan_model.ReplanChange.supersede_node(
            old_node_id="n-up", reason="replace invalidated producer",
            node_spec={
                "node_id": "n-up2", "title": "Replacement producer",
                "priority": 90, "depends_on": [],
                "acceptance_criteria": [
                    {"criterion_id": "ac-1", "statement": "done"}],
                "mission_ref": {"mission_id": "m-m015-up2", "required": True},
                "resources": {"cpu_slots": 1}}),
        replan_model.ReplanChange.invalidate_reuse(
            consumer_node_id="n-down", input_id="up-validate",
            reason="explicitly invalidate prior reusable evidence"),
    ]
    decision = replan_engine.build_plan(root, GOAL, trigger, changes,
                                        created_at="t2")
    assert decision.plan is not None, decision.reason
    applied = replan_engine.apply_plan(root, GOAL, decision.plan,
                                       created_at="t2")
    assert applied.accepted

    after = scheduler_engine.build_decision(root, GOAL, POLICY, created_at="t3")
    reasons = {n.node_id: n.reason
               for n in (*after.admitted, *after.deferred, *after.blocked)}
    # The consumer can no longer reuse the invalidated proof and its new
    # dependency is unproven, so it is never admitted or dispatched.
    assert "n-down" not in {n.node_id for n in after.admitted}
    assert reasons["n-down"] in (sched_model.R_DEPENDENCY_BLOCKED,
                                 sched_model.R_REUSE_UNRESOLVED)
    graph, _ = graph_store.load_graph(root, GOAL)
    assert graph.node_map()["n-down"].reuse_inputs == ()


# ---------------------------------------------------------------------------
# 4: stale trigger + cycle rejection
# ---------------------------------------------------------------------------


def test_dogfood_stale_trigger_and_cycle_rejected(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _setup(tmp_path, capsys)
    base = replan_engine.current_generation(root, GOAL)
    old_graph, _ = graph_store.load_graph(root, GOAL)

    stale = replan_model.ReplanTrigger.build(
        kind=replan_model.TK_OPERATOR_REQUEST, source="operator",
        generation_id=base.generation.generation_id,
        graph_id=old_graph.graph_id,
        provenance=replan_model.EvidenceProvenance(note="stale decision"))
    replacement = replan_model.ReplanChange.supersede_node(
        old_node_id="n-bad", reason="replace",
        node_spec={
            "node_id": "n-bad2", "title": "Replacement", "priority": 60,
            "depends_on": [],
            "acceptance_criteria": [
                {"criterion_id": "ac-1", "statement": "done"}],
            "mission_ref": {"mission_id": "m-m015-bad2", "required": True},
            "resources": {"cpu_slots": 1}})
    # First activation moves the generation forward.
    first = replan_engine.build_plan(root, GOAL, stale, [replacement],
                                     created_at="t1")
    assert first.plan is not None, first.reason
    assert replan_engine.apply_plan(root, GOAL, first.plan, created_at="t1"
                                    ).accepted

    # Replaying the same trigger against the superseded generation fails.
    again = replan_engine.build_plan(root, GOAL, stale, [replacement],
                                     created_at="t2")
    assert again.status == replan_model.DS_REJECTED
    assert again.reason == replan_model.RC_TRIGGER_STALE

    # A cycle-producing change is rejected before activation.
    live = replan_engine.current_generation(root, GOAL)
    cycle_trigger = replan_model.ReplanTrigger.build(
        kind=replan_model.TK_OPERATOR_REQUEST, source="operator",
        generation_id=live.generation.generation_id,
        graph_id=live.generation.graph_id,
        provenance=replan_model.EvidenceProvenance(note="cycle"))
    cycle_changes = [
        replan_model.ReplanChange.add_dependency(
            dependency_from="n-down", dependency_to="n-tail",
            reason="first half of a cycle"),
        replan_model.ReplanChange.add_dependency(
            dependency_from="n-tail", dependency_to="n-down",
            reason="close the cycle"),
    ]
    rejected = replan_engine.build_plan(root, GOAL, cycle_trigger,
                                        cycle_changes, created_at="t3")
    assert rejected.status == replan_model.DS_REJECTED
    assert rejected.reason == replan_model.RC_CYCLE
    before_graph, _ = graph_store.load_graph(root, GOAL)
    before_id = before_graph.graph_id
    applied = replan_engine.apply_spec(
        root, GOAL, {"trigger": {"kind": "OPERATOR_REQUEST",
                                 "source": "operator", "detail": "cycle"},
                     "changes": [c.to_dict() for c in cycle_changes]},
        created_at="t4")
    assert not applied.accepted
    assert applied.reason == replan_model.RC_CYCLE
    assert graph_store.load_graph(root, GOAL)[0].graph_id == before_id


def test_dogfood_reuse_and_resource_validation_fail_closed(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _setup(tmp_path, capsys)
    trigger = _trigger(root, replan_model.TK_OPERATOR_REQUEST, "operator",
                       "validation")
    # Superseding a producer without invalidating the consumer's reuse
    # reference fails closed (never silently rewired).
    supersede_only = replan_model.ReplanChange.supersede_node(
        old_node_id="n-up", reason="replace producer",
        node_spec={
            "node_id": "n-up2", "title": "Replacement", "priority": 90,
            "depends_on": [],
            "acceptance_criteria": [
                {"criterion_id": "ac-1", "statement": "done"}],
            "mission_ref": {"mission_id": "m-m015-up2", "required": True},
            "resources": {"cpu_slots": 1}})
    rejected = replan_engine.build_plan(root, GOAL, trigger, [supersede_only],
                                        created_at="t1")
    assert rejected.status == replan_model.DS_REJECTED
    assert rejected.reason == replan_model.RC_INVALID_REUSE

    # An invalid resource declaration is rejected before activation.
    bad_resource = replan_model.ReplanChange.add_node(
        reason="invalid resource",
        node_spec={
            "node_id": "n-bad-res", "title": "Bad", "priority": 10,
            "depends_on": [],
            "acceptance_criteria": [
                {"criterion_id": "ac-1", "statement": "done"}],
            "mission_ref": {"mission_id": "m-m015-up2", "required": True},
            "resources": {"gpu_mem_bytes": 1073741824}})
    bad = replan_engine.build_plan(root, GOAL, trigger, [bad_resource],
                                   created_at="t2")
    assert bad.status == replan_model.DS_REJECTED
    assert bad.reason == replan_model.RC_INVALID_RESOURCE
    # Nothing was activated by the rejected plans.
    assert replan_engine.current_generation(
        root, GOAL).generation.generation_number == 1


# ---------------------------------------------------------------------------
# 5-7: scheduler generation guard, reconstruction, projection
# ---------------------------------------------------------------------------


def test_dogfood_scheduler_only_sees_latest_generation(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _setup(tmp_path, capsys)
    reuse_engine.resolve_and_record(root, GOAL, consumed_at="t0")
    first = scheduler_engine.run_cycle(root, GOAL, POLICY, dispatch=False,
                                       created_at="t1")
    assert first.decision.graph_id != ""
    old_generation = replan_engine.current_generation(root, GOAL).generation

    result = replan_engine.apply_spec(root, GOAL, _request(), created_at="t2")
    assert result.accepted
    state = sched_store.load_state(root, GOAL)
    assert state is not None
    assert state.generation_id == result.generation.generation_id
    assert state.generation_id != old_generation.generation_id
    # The superseded scheduler state was archived, not destroyed.
    archived = Path(root) / "goals" / GOAL / "scheduler" / "superseded"
    assert archived.is_dir()
    assert any(archived.iterdir())

    # A stale decision id from the previous generation can never dispatch.
    with pytest.raises(sched_model.SchedulerValidationError) as excinfo:
        scheduler_engine.run_cycle(
            root, GOAL, POLICY, dispatch=False, created_at="t3",
            expected_decision_id=first.decision.decision_id)
    assert excinfo.value.code == sched_model.E_IDENTITY_MISMATCH

    # First scheduling cycle on the new generation succeeds and binds it.
    fresh = scheduler_engine.run_cycle(root, GOAL, POLICY, dispatch=False,
                                       created_at="t4")
    assert fresh.decision.graph_id == graph_store.load_graph(
        root, GOAL)[0].graph_id
    # Re-resolve reuse for the new generation so scheduler reconstruction can
    # bind the exact reusable evidence of the active generation.
    reuse_engine.resolve_and_record(root, GOAL, consumed_at="t5")
    scheduler_engine.run_cycle(root, GOAL, POLICY, dispatch=False,
                               created_at="t6")
    rebuilt = scheduler_engine.reconstruct(root, GOAL)
    assert rebuilt.latest_decision is not None
    assert rebuilt.latest_decision.graph_id == fresh.decision.graph_id


def test_dogfood_reconstruction_after_interruption(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _setup(tmp_path, capsys)
    result = replan_engine.apply_spec(root, GOAL, _request(), created_at="t1")
    assert result.accepted
    # Simulate a restart: reload everything from disk.
    read = replan_engine.current_generation(root, GOAL)
    assert read.generation.generation_id == result.generation.generation_id
    assert len(read.generations) == 2
    assert read.generations[0].generation_number == 1
    assert read.to_dict() == replan_engine.current_generation(root, GOAL).to_dict()

    projection = replan_engine.machine_projection(root, GOAL)
    assert projection["generation"]["generation_id"] == \
        result.generation.generation_id
    assert projection["projection_id"]
    assert projection == replan_engine.machine_projection(root, GOAL)

    assert cli.main(["--root", root, "replan-validate", GOAL,
                     "--json"]) == cli.EXIT_OK
    validated = _json(capsys.readouterr().out)
    assert validated["status"] == "VALID"


# ---------------------------------------------------------------------------
# 8: operator CLI surface
# ---------------------------------------------------------------------------


def test_dogfood_operator_cli_surface(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _setup(tmp_path, capsys)
    assert cli.main(["--root", root, "current-generation", GOAL,
                     "--json"]) == cli.EXIT_OK
    current = _json(capsys.readouterr().out)
    assert current["generation_number"] == 1

    assert cli.main(["--root", root, "replan-preview", GOAL, "--spec",
                     str(REPLAN_REQUEST), "--json"]) == cli.EXIT_OK
    preview = _json(capsys.readouterr().out)
    assert preview["status"] == replan_model.DS_ACCEPTED
    assert preview["reason"] == replan_model.RC_APPLIED

    assert cli.main(["--root", root, "replan-apply", GOAL, "--spec",
                     str(REPLAN_REQUEST), "--json"]) == cli.EXIT_OK
    applied = _json(capsys.readouterr().out)
    assert applied["accepted"] is True
    assert applied["generation_number"] == 2

    assert cli.main(["--root", root, "generation", GOAL]) == cli.EXIT_OK
    assert "generation :" in capsys.readouterr().out

    assert cli.main(["--root", root, "replan-history", GOAL,
                     "--json"]) == cli.EXIT_OK
    history = _json(capsys.readouterr().out)
    assert history["count"] >= 1
    assert history["generations"][0]

    assert cli.main(["--root", root, "replan-why", GOAL, "n-bad2",
                     "--json"]) == cli.EXIT_OK
    explained = _json(capsys.readouterr().out)
    assert explained["status"] == "OK"
    assert explained["node"] is not None
    assert explained["replan_events"]

    assert cli.main(["--root", root, "replan-project", GOAL,
                     "--json"]) == cli.EXIT_OK
    projected = _json(capsys.readouterr().out)
    assert projected["projection_id"]
    assert projected["generation"]["generation_number"] == 2

    # The scheduler generation can be re-bound explicitly (idempotent here).
    assert cli.main(["--root", root, "adopt-generation", GOAL,
                     "--json"]) == cli.EXIT_OK
    adopted = _json(capsys.readouterr().out)
    assert adopted["status"] == "ADOPTED"
    assert adopted["generation_id"] == applied["generation_id"]

    # A replan that would produce an invalid graph is rejected with exit 3.
    bad_spec = {
        "trigger": {"kind": "OPERATOR_REQUEST", "source": "operator",
                    "detail": "remove a needed dependency"},
        "changes": [{"op": "REMOVE_NODE", "reason": "break", "node_id":
                     "n-bad2"}],
    }
    bad_path = tmp_path / "bad.json"
    bad_path.write_text(json.dumps(bad_spec), encoding="utf-8")
    assert cli.main(["--root", root, "replan-apply", GOAL, "--spec",
                     str(bad_path)]) == cli.EXIT_REJECTED


# ---------------------------------------------------------------------------
# 9: compatibility + Git safety
# ---------------------------------------------------------------------------


def test_m008_m014_compatibility_regression(tmp_path: Path) -> None:
    from trajectory_os.graph import identity as graph_identity
    from trajectory_os.graph.replan import identity as replan_identity
    from trajectory_os.graph.reuse import identity as reuse_identity
    from trajectory_os.graph.scheduler import identity as sched_identity
    from trajectory_os.missions import identity as patch_identity

    for domain in replan_identity.DOMAIN_IDS:
        assert domain not in patch_identity.DOMAIN_IDS
        assert domain not in graph_identity.DOMAIN_IDS
        assert domain not in sched_identity.DOMAIN_IDS
        assert domain not in reuse_identity.DOMAIN_IDS

    # A graph with no replan history keeps its exact M012/M014 identity and
    # never gains a generation field.
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
    graph = graph_store.create_graph(root, spec, repo_root=root,
                                     baseline_revision="base")
    reloaded = graph_model.GoalGraph.from_dict(graph.to_dict(), "g")
    assert reloaded.graph_id == graph.graph_id
    assert not replan_store.replan_exists(root, "g-plain")
    state = replan_engine.current_generation(root, "g-plain")
    assert state.generation.generation_number == 1


_GIT_WRITE_VERBS = frozenset({
    "commit", "push", "merge", "reset", "restore", "clean", "stash",
    "rebase", "switch", "checkout", "add", "am", "pull", "fetch", "tag",
    "remote", "cherry-pick", "revert",
})


def test_no_autonomous_git_trust_boundary_write() -> None:
    from trajectory_os.agents import canary, deepseek_harness, registry
    from trajectory_os.agents import cli as agent_cli
    from trajectory_os.graph.replan import engine as replan_engine_module
    from trajectory_os.graph.replan import model as replan_model_module
    from trajectory_os.graph.replan import store as replan_store_module
    from trajectory_os.graph.replan import summary as replan_summary
    from trajectory_os.graph.scheduler import engine as sched_engine

    for module in (cli, graph_store, graph_model, replan_engine_module,
                   replan_model_module, replan_store_module, replan_summary,
                   sched_engine, agent_cli, canary, deepseek_harness,
                   registry):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for verb in _GIT_WRITE_VERBS:
            assert f'"git", "{verb}"' not in source, (module.__name__, verb)
        assert "os.system" not in source
