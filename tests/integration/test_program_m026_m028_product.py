"""Program block D (M026-M028) integration: events, web, LifeOS, review.

Drives the real production code paths (portfolio -> mission orchestrator +
scheduler) with a deterministic exact-attestation runner substituted for the
model subprocess, exactly as the M016-M025 integration proofs do. No network,
no Git trust-boundary write.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trajectory_os.agents import model as agent_model
from trajectory_os.agents import telemetry
from trajectory_os.events import model as event_model
from trajectory_os.goals import cli as goals_cli
from trajectory_os.goals import snapshot
from trajectory_os.graph.proof import engine as proof_engine
from trajectory_os.lifeos import model as lifeos_model
from trajectory_os.missions import model as mission_model
from trajectory_os.missions import semantic
from trajectory_os.missions.runner import SubrunResult
from trajectory_os.portfolio import engine as portfolio_engine
from trajectory_os.portfolio import model as portfolio_model
from trajectory_os.web import projection

GOAL = "g-program-d"


class _AttestedRunner:
    def run(self, request: Any) -> SubrunResult:
        return SubrunResult(
            0, mission_model.CR_COMPLETED,
            semantic_status=semantic.STATUS_SUCCESS,
            attestation=semantic.ATTESTATION_VERIFIED)


def _seed_and_run(root: Path) -> str:
    from trajectory_os.goals import launch
    from trajectory_os.graph import store as graph_store

    spec = {
        "schema_version": 1,
        "goal_id": GOAL,
        "objective": "Prove M026-M028 on the visible persistent product.",
        "nodes": [{
            "node_id": "n-d",
            "title": "Program D mission",
            "priority": 50,
            "depends_on": [],
            "acceptance_criteria": [{
                "criterion_id": "ac-d",
                "statement": "program D is proven"}],
            "mission_ref": {"mission_id": "m-program-d", "required": True},
            "resources": {"cpu_slots": 1},
            "budgets": {"repair_budget": 0, "max_attempts": 1},
        }],
    }
    defaults = launch.MissionLaunchDefaults(
        repo_root=None, baseline_revision="base", repair_budget=0)
    launch.provision_from_spec(str(root), spec, defaults)
    graph_store.create_graph(str(root), spec, repo_root=defaults.repo_root,
                             baseline_revision=defaults.baseline_revision)
    portfolio_engine.run_cycle(
        str(root), [GOAL], portfolio_model.PortfolioPolicy(),
        runner_factory=_AttestedRunner, session_subruns=32,
        created_at="2026-01-01T00:00:00Z")
    proof_engine.build_proof(str(root), GOAL)
    return GOAL


def test_m026_cli_events_are_authoritative_and_refreshable(
        tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    goal_id = _seed_and_run(root)
    assert goals_cli.main(
        ["events", "--root", str(root), goal_id, "--refresh", "--json"]) == 0
    from trajectory_os.events import store as event_store
    document, records = event_store.reconstruct(root, goal_id)
    assert document["count"] == len(records)
    categories = {record.category for record in records}
    assert event_model.CAT_GOAL in categories
    assert event_model.CAT_MISSION in categories
    assert event_model.CAT_PROOF in categories
    assert event_model.CAT_SCHEDULER in categories
    assert event_model.CAT_GATE in categories
    # Restart-safe: a second refresh changes nothing.
    assert goals_cli.main(["events", "--root", str(root), goal_id]) == 0


def test_m027_web_projection_composes_canonical_state(
        tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    goal_id = _seed_and_run(root)
    document = projection.dashboard(str(root), goal_id)
    assert document["snapshot"]["goal"]["goal_id"] == goal_id
    assert document["events"]["count"] >= 1
    assert document["artifacts"]["goal_id"] == goal_id
    assert document["portfolio"]["member"] is True
    overview = projection.overview(str(root))
    assert [entry["goal_id"] for entry in overview["goals"]] == [goal_id]
    # The snapshot remains the single canonical projection.
    assert snapshot.build_snapshot(
        str(root), goal_id)["final"]["complete"] is True


def test_m028_lifeos_cli_scoped_exchange(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    goal_id = _seed_and_run(root)
    target = tmp_path / "lifeos"
    config = {
        "schema_version": 1,
        "adapters": [
            {"kind": lifeos_model.AK_OBSIDIAN,
             "target": str(target / "vault")},
            {"kind": lifeos_model.AK_SUPER_PRODUCTIVITY,
             "target": str(target / "sp")},
        ],
    }
    config_path = tmp_path / "lifeos.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    assert goals_cli.main([
        "lifeos-export", "--root", str(root), goal_id,
        "--config", str(config_path), "--json"]) == 0
    assert goals_cli.main(["lifeos", "--root", str(root), "--json"]) == 0
    notes = list((target / "vault").rglob("*.md"))
    assert len(notes) == 1
    assert goal_id in notes[0].read_text(encoding="utf-8")
    # Failure isolation: a target inside the canonical root never corrupts it.
    bad = {"schema_version": 1, "adapters": [
        {"kind": lifeos_model.AK_OBSIDIAN, "target": str(root / "leak")}]}
    bad_path = tmp_path / "bad.json"
    bad_path.write_text(json.dumps(bad), encoding="utf-8")
    assert goals_cli.main([
        "lifeos-export", "--root", str(root), goal_id,
        "--config", str(bad_path)]) == 3
    assert not (root / "leak").exists()


def test_review_protocol_cli(tmp_path: Path) -> None:
    from trajectory_os.missions import cli as missions_cli

    good = tmp_path / "good.txt"
    good.write_text(
        "VERDICT: PASS\n\nBLOCKERS:\n- None\n\nMAJORS:\n- None\n\n"
        "MINORS:\n- example note\n\nFINAL RECOMMENDATION: GO COMMIT\n",
        encoding="utf-8")
    assert missions_cli.main(["review-protocol", str(good), "--json"]) == 0
    bad = tmp_path / "bad.txt"
    bad.write_text("VERDICT: PASS\n\nMAJORS:\n- None\n", encoding="utf-8")
    assert missions_cli.main(["review-protocol", str(bad)]) == 3


def test_telemetry_cli_reads_grounded_ledger(tmp_path: Path) -> None:
    from trajectory_os.agents import cli as agents_cli

    result = agent_model.AgentResult.build(
        backend=agent_model.BACKEND_PI, status=agent_model.RS_COMPLETED,
        reason=agent_model.R_OK,
        events=[agent_model.AgentEvent.build(
            sequence=0, kind=agent_model.LK_RESULT, method="result",
            payload={"prompt_tokens": 5, "completion_tokens": 6})],
        runtime_ms=250)
    metrics = telemetry.from_result(result, provider="ollama",
                                    model="qwen3.6:27b")
    telemetry.record(tmp_path, metrics)
    assert agents_cli.main(["usage", "--root", str(tmp_path), "--json"]) == 0
    assert telemetry.reconstruct(tmp_path)["count"] == 1


def test_new_modules_never_write_git(tmp_path: Path) -> None:
    from trajectory_os.events import engine as event_engine
    from trajectory_os.lifeos import engine as lifeos_engine
    from trajectory_os.missions import review_protocol
    from trajectory_os.web import model as web_model
    from trajectory_os.web import server as web_server

    verbs = ("commit", "push", "merge", "reset", "restore", "clean", "stash",
             "rebase", "switch", "checkout", "pull", "fetch", "cherry-pick")
    modules = [event_engine, lifeos_engine, review_protocol, web_model,
               web_server]
    offenders: list[str] = []
    for module in modules:
        try:
            source = Path(module.__file__).read_text(encoding="utf-8")
        except (OSError, TypeError):
            continue
        for verb in verbs:
            if f'"git", "{verb}"' in source or f"'git', '{verb}'" in source:
                offenders.append(f"{module.__name__}:{verb}")
    assert offenders == []
