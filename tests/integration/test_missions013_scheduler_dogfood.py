"""Mission 013 — production dogfood + M008-M012 compatibility regression.

Production-path deterministic proof for the portfolio scheduler and resource
arbiter (Issue #217 / program #207 checkpoint S13):

1.  create every referenced mission and the M012 goal graph;
2.  preview and persist one deterministic scheduling cycle;
3.  prove priority ordering, stable tie-breaking and every deferral reason;
4.  prove remote model-heavy work reserves no local GPU while local GPU work
    reserves its declared VRAM;
5.  dispatch admitted work through the existing production mission path and
    reconstruct scheduler state exactly;
6.  prove downstream readiness changes only after authoritative upstream
    completion evidence;
7.  exercise the operator CLI surface;
8.  M008/M009/M010/M011/M012 compatibility regression and Git-safety.

All scheduling uses the production CLI/library surface; all mission work uses
the canonical orchestrator (no second execution engine).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trajectory_os.graph import cli
from trajectory_os.graph import identity as graph_identity
from trajectory_os.graph import store as graph_store
from trajectory_os.graph.scheduler import engine
from trajectory_os.graph.scheduler import model as sched_model
from trajectory_os.graph.scheduler import summary as sched_summary
from trajectory_os.missions import gate, identity, orchestrator, runner, semantic, summary
from trajectory_os.missions import model as mission_model
from trajectory_os.missions import store as mission_store
from trajectory_os.missions.runner import SubrunResult

REPO_ROOT = Path(__file__).resolve().parents[2]
DOGFOOD_SPEC = REPO_ROOT / "examples" / "portfolio_scheduler_dogfood.json"
DOGFOOD_POLICY = REPO_ROOT / "examples" / "portfolio_scheduler_policy.json"
GOAL = "g-m013-dogfood"


class OkRunner:
    def run(self, request: Any) -> SubrunResult:
        return SubrunResult(0, mission_model.CR_COMPLETED)


def _policy() -> sched_model.SchedulerPolicy:
    return sched_model.SchedulerPolicy.from_dict(
        json.loads(DOGFOOD_POLICY.read_text(encoding="utf-8")))


def _spec() -> dict[str, Any]:
    return json.loads(DOGFOOD_SPEC.read_text(encoding="utf-8"))


def _setup(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> str:
    root = str(tmp_path / "root")
    spec = _spec()
    for node in spec["nodes"]:
        mission_id = node["mission_ref"]["mission_id"]
        orchestrator.create_mission(root, orchestrator.MissionConfig(
            mission_id=mission_id,
            objective="portfolio scheduler dogfood mission",
            phase_specs=orchestrator.default_phase_specs(
                {kind: ("true",) for kind in mission_model.CANONICAL_SEQUENCE}),
            baseline_revision="base",
        ))
    code = cli.main([
        "--root", root, "create", "--spec", str(DOGFOOD_SPEC),
        "--repo", str(REPO_ROOT), "--head", "base",
    ])
    assert code == cli.EXIT_OK
    capsys.readouterr()
    return root


def _last_json(text: str) -> dict[str, Any]:
    return json.loads(text)


def _reasons(decision: sched_model.ScheduleDecision) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in (*decision.admitted, *decision.deferred, *decision.blocked,
                 *decision.active, *decision.completed):
        out[node.node_id] = node.reason
    return out


# ---------------------------------------------------------------------------
# 1-4: deterministic admission and arbitration
# ---------------------------------------------------------------------------


def test_dogfood_preview_is_deterministic_and_argued(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _setup(tmp_path, capsys)
    policy = _policy()
    code = cli.main(["--root", root, "preview", GOAL, "--policy",
                     str(DOGFOOD_POLICY), "--json"])
    assert code == cli.EXIT_OK
    preview = _last_json(capsys.readouterr().out)
    assert preview["decision_id"] == sched_model.ScheduleDecision.from_dict(
        preview, "preview").decision_id

    # Multiple simultaneously eligible nodes, distinct priorities and a
    # deterministic tie-break (n-cpu-a before n-cpu-b at equal priority).
    assert preview["candidate_order"][:4] == [
        "n-cpu-a", "n-cpu-b", "n-remote", "n-gpu"]
    assert {n["node_id"] for n in preview["admitted"]} == {
        "n-cpu-a", "n-cpu-b", "n-remote", "n-gpu", "n-cpu-d", "n-gpu2"}
    reasons = {n["node_id"]: n["reason"] for n in
               (*preview["admitted"], *preview["deferred"],
                *preview["blocked"])}
    # Every required conflict category is represented by a stable reason.
    assert reasons["n-excl"] == sched_model.R_EXCLUSIVE_CONFLICT
    assert reasons["n-vram"] == sched_model.R_GPU_MEMORY
    assert reasons["n-gpu2"] in (
        sched_model.R_ADMITTED, sched_model.R_GPU_CAPACITY)
    assert reasons["n-cpu-c"] == sched_model.R_CPU_CAPACITY
    assert reasons["n-cpu-e"] == sched_model.R_CONCURRENCY_LIMIT
    assert reasons["n-blocked"] == sched_model.R_DEPENDENCY_BLOCKED
    assert reasons["n-chain"] == sched_model.R_DEPENDENCY_BLOCKED

    # The same inputs produce the same decision identity.
    first = engine.build_decision(root, GOAL, policy, created_at="a")
    second = engine.build_decision(root, GOAL, policy, created_at="b")
    assert first.decision_id == second.decision_id


def test_dogfood_remote_model_heavy_reserves_no_local_gpu(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _setup(tmp_path, capsys)
    policy = _policy()
    decision = engine.build_decision(root, GOAL, policy, created_at="a")
    newly = {r.node_id: r for r in decision.reservations.newly_reserved}
    # Remote DeepSeek-like work is admitted without claiming local GPU/VRAM.
    assert newly["n-remote"].execution == sched_model.X_REMOTE_MODEL
    assert newly["n-remote"].gpu_slots == 0
    assert newly["n-remote"].gpu_mem_bytes == 0
    # Local GPU work is admitted and reserves its explicit VRAM.
    assert newly["n-gpu"].execution == sched_model.X_LOCAL_GPU
    assert newly["n-gpu"].gpu_slots == 1
    assert newly["n-gpu"].gpu_mem_bytes == 6442450944
    assert sum(r.gpu_slots for r in newly.values()) == 2  # policy.gpu_slots


def test_dogfood_controlled_gpu_vram_conflict(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _setup(tmp_path, capsys)
    decision = engine.build_decision(root, GOAL, _policy(), created_at="a")
    reasons = _reasons(decision)
    # 6 GiB admitted + 4 GiB requested > 8 GiB configured VRAM.
    assert reasons["n-vram"] == sched_model.R_GPU_MEMORY
    # Exclusive work conflicts with the already-admitted local work.
    assert reasons["n-excl"] == sched_model.R_EXCLUSIVE_CONFLICT


# ---------------------------------------------------------------------------
# 5-6: persistence, reconstruction, authoritative unlocking
# ---------------------------------------------------------------------------


def test_dogfood_dispatch_persist_reconstruct_and_unlock(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _setup(tmp_path, capsys)
    policy = _policy()
    dispatcher = engine.MissionPathDispatcher(
        runner_factory=OkRunner, session_subruns=1)
    result = engine.run_cycle(root, GOAL, policy, dispatch=True,
                              dispatcher=dispatcher, created_at="t1")
    assert result.status == "DISPATCHED"
    # Only dependency-ready, resource-admitted nodes were dispatched.
    assert {r.node_id for r in result.dispatched} == {
        n.node_id for n in result.decision.admitted}
    assert "n-blocked" not in {r.node_id for r in result.dispatched}

    read = engine.reconstruct(root, GOAL)
    assert read.latest_decision is not None
    assert read.latest_decision.decision_id == result.decision.decision_id
    # Restart reconstruction is exact and idempotent.
    assert engine.reconstruct(root, GOAL).to_dict() == read.to_dict()
    active = {r.node_id: r for r in read.reservations}
    assert active["n-remote"].gpu_slots == 0
    assert sum(r.gpu_slots for r in read.reservations) == 2

    # Only authoritative mission evidence unlocks downstream nodes.
    for record in result.dispatched:
        orchestrator.run_mission(root, record.mission_id, OkRunner())
    after = engine.build_decision(root, GOAL, policy, created_at="t2")
    reasons = _reasons(after)
    assert reasons["n-blocked"] != sched_model.R_DEPENDENCY_BLOCKED
    assert reasons["n-chain"] != sched_model.R_DEPENDENCY_BLOCKED
    assert "n-blocked" in {n.node_id for n in after.admitted}
    assert after.reservations.released  # released on proven completion


# ---------------------------------------------------------------------------
# 7: operator CLI surface
# ---------------------------------------------------------------------------


def test_dogfood_operator_surface(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _setup(tmp_path, capsys)
    assert cli.main(["--root", root, "capacity", "--policy",
                     str(DOGFOOD_POLICY)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "capacity  : cpu=8 gpu=2" in out

    assert cli.main(["--root", root, "schedule", GOAL, "--policy",
                     str(DOGFOOD_POLICY)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "status    : PLANNED" in out
    assert "+ n-cpu-a" in out
    assert "- n-vram GPU_MEMORY" in out
    assert "! n-blocked DEPENDENCY_BLOCKED" in out

    assert cli.main(["--root", root, "portfolio", GOAL]) == cli.EXIT_OK
    assert "portfolio : g-m013-dogfood" in capsys.readouterr().out

    assert cli.main(["--root", root, "resources", GOAL]) == cli.EXIT_OK
    assert "configured : cpu=8" in capsys.readouterr().out

    assert cli.main(["--root", root, "history", GOAL, "--json"]) == cli.EXIT_OK
    history = _last_json(capsys.readouterr().out)
    assert history["count"] == 1

    assert cli.main(["--root", root, "why-schedule", GOAL, "n-vram"]) \
        == cli.EXIT_OK
    assert "GPU_MEMORY" in capsys.readouterr().out

    assert cli.main(["--root", root, "validate-schedule", GOAL, "--json"]) \
        == cli.EXIT_OK
    validated = _last_json(capsys.readouterr().out)
    assert validated["status"] == "VALID"
    assert validated["decisions"] == [history["decisions"][0]["decision_id"]]

    document = sched_summary.status_document(root, GOAL)
    assert document["policy_id"] == _policy().policy_id
    assert "portfolio" in sched_summary.render_portfolio(document)


# ---------------------------------------------------------------------------
# 8: compatibility + git safety
# ---------------------------------------------------------------------------


def test_dogfood_rejects_idempotent_replay_of_existing_dispatch(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _setup(tmp_path, capsys)
    dispatcher = engine.MissionPathDispatcher(
        runner_factory=OkRunner, session_subruns=1)
    first = engine.run_cycle(root, GOAL, _policy(), dispatch=True,
                             dispatcher=dispatcher, created_at="t1")
    # The missions are still running; a second dispatch cycle must not replay
    # any proven dispatch decision.
    second = engine.run_cycle(root, GOAL, _policy(), dispatch=True,
                              dispatcher=dispatcher, created_at="t2")
    assert second.dispatched == ()
    assert {r.node_id for r in second.decision.active} == {
        r.node_id for r in first.dispatched}


def test_m008_m009_m010_m011_m012_compatibility_regression(
        tmp_path: Path) -> None:
    # M008: SUCCESS without an independently verified exact attestation is
    # never COMPLETED.
    assert runner.classify_subrun(
        0, semantic_status=semantic.STATUS_SUCCESS,
        attestation=None).classification == mission_model.CR_UNPROVEN
    assert runner.classify_subrun(
        0, semantic_status=semantic.STATUS_SUCCESS,
        attestation=semantic.ATTESTATION_VERIFIED).classification \
        == mission_model.CR_COMPLETED

    # M010: patch identity domains stay distinct from the graph/scheduler
    # domains and are not overloaded.
    assert identity.WRAPPER_SNAPSHOT_DOMAIN != identity.MISSION_WORKTREE_DOMAIN
    from trajectory_os.graph.scheduler import identity as sched_identity
    for domain in sched_identity.DOMAIN_IDS:
        assert domain not in identity.DOMAIN_IDS
        assert domain not in graph_identity.DOMAIN_IDS

    # M012: the graph remains the canonical decomposition/dependency source;
    # the scheduler consumes it read-only and never copies mission evidence.
    root = str(tmp_path / "root")
    orchestrator.create_mission(root, orchestrator.MissionConfig(
        mission_id="m-compat",
        objective="compat",
        phase_specs=orchestrator.default_phase_specs(
            {kind: ("true",) for kind in mission_model.CANONICAL_SEQUENCE}),
        baseline_revision="base"))
    spec = {
        "schema_version": 1, "goal_id": "g-compat", "objective": "compat",
        "nodes": [{
            "node_id": "n-a", "title": "A", "priority": 50,
            "depends_on": [],
            "acceptance_criteria": [{"criterion_id": "ac-1",
                                     "statement": "s"}],
            "mission_ref": {"mission_id": "m-compat", "required": True},
            "resources": {"cpu_slots": 1},
        }],
    }
    graph = graph_store.create_graph(root, spec, repo_root=root,
                                     baseline_revision="base")
    engine.run_cycle(root, "g-compat", sched_model.DEFAULT_POLICY,
                     dispatch=False, created_at="t")
    raw_graph = graph_store.graph_paths(
        root, "g-compat")["graph"].read_text(encoding="utf-8")
    assert "mission_state" not in raw_graph
    raw_decision = (
        engine.reconstruct(root, "g-compat").latest_decision)
    assert raw_decision is not None
    assert raw_decision.graph_id == graph.graph_id

    # M009 + M011: a green mission still projects attestation, patch identity
    # and the single GO COMMIT human gate.
    report = orchestrator.run_mission(root, "m-compat", OkRunner())
    assert report.mission_state == mission_model.MS_COMPLETE
    mission, paths = mission_store.load_mission(root, "m-compat")
    document = summary.mission_summary(mission, paths)
    assert set(document["attestation"]) == {
        "model_heavy_subruns", "verified", "unproven", "legacy"}
    assert "patch_identity" in document
    assert document["operator_gate"]["gate"] == gate.GATE_GO_COMMIT
    assert document["operator_gate"]["human_action_required"] is True
    assert mission.commit_approved_at is None


_GIT_WRITE_VERBS = frozenset({
    "commit", "push", "merge", "reset", "restore", "clean", "stash",
    "rebase", "switch", "checkout", "add", "am", "pull", "fetch", "tag",
    "remote", "cherry-pick", "revert",
})


def test_no_autonomous_git_trust_boundary_write() -> None:
    from trajectory_os.graph import model
    from trajectory_os.graph.scheduler import arbiter as sched_arbiter
    from trajectory_os.graph.scheduler import evidence
    from trajectory_os.graph.scheduler import identity as sched_identity
    from trajectory_os.graph.scheduler import store as sched_store

    for module in (cli, graph_store, model, sched_arbiter, engine,
                   sched_identity, sched_model, sched_store, sched_summary,
                   evidence):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for verb in _GIT_WRITE_VERBS:
            assert f'"git", "{verb}"' not in source, (module.__name__, verb)
        assert "os.system" not in source
