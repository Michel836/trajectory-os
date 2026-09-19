#!/usr/bin/env python3
"""Program block D (M026-M028) — final visible-product dogfood.

Drives the REAL production code paths (portfolio coordinator -> goal runner ->
mission orchestrator + scheduler + goal proof) with a deterministic
exact-attestation runner substituted for the model subprocess — exactly as the
M016-M025 harnesses do. It proves the fifteen required product steps end to
end, plus one fail-closed incomplete case, with no network and no Git
trust-boundary write:

 1. goal launch            9. adaptive replan
 2. decomposition         10. restart/reconstruction
 3. scheduling            11. events/notifications
 4. visible execution      12. CLI/TUI
 5. agent/model identity   13. web dashboard
 6. resource attribution   14. LifeOS handoff
 7. artifacts              15. final M016 proof
 8. controlled failure     + fail-closed incomplete case

All state lives in an isolated temp root (or ``--root``).

Exit codes: 0 = all checks proven; 3 = proof blocked; 2 = usage error.
"""

# ruff: noqa: E402
# (the repository src/ layout is added to sys.path below)

from __future__ import annotations

import argparse
import datetime
import http.client
import json
import pathlib
import shutil
import sys
import tempfile
import threading
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from trajectory_os.agents import model as agent_model
from trajectory_os.agents import telemetry
from trajectory_os.artifacts import engine as artifact_engine
from trajectory_os.artifacts import model as artifact_model
from trajectory_os.events import engine as event_engine
from trajectory_os.events import model as event_model
from trajectory_os.events import store as event_store
from trajectory_os.goals import cli as goals_cli
from trajectory_os.goals import launch
from trajectory_os.goals import snapshot as goal_snapshot
from trajectory_os.goals import tui as goal_tui
from trajectory_os.graph import store as graph_store
from trajectory_os.graph.proof import engine as proof_engine
from trajectory_os.graph.replan import engine as replan_engine
from trajectory_os.graph.scheduler import engine as sched_engine
from trajectory_os.lifeos import adapters as lifeos_adapters
from trajectory_os.lifeos import engine as lifeos_engine
from trajectory_os.lifeos import model as lifeos_model
from trajectory_os.missions import model as mission_model
from trajectory_os.missions import semantic
from trajectory_os.missions import store as mission_store
from trajectory_os.missions.runner import SubrunResult
from trajectory_os.portfolio import engine as portfolio_engine
from trajectory_os.portfolio import model as portfolio_model
from trajectory_os.web import model as web_model
from trajectory_os.web import projection as web_projection
from trajectory_os.web import server as web_server

GOAL = "g-program-d-dogfood"
MISSION = "m-program-d-dogfood"
EXTRA_MISSION = "m-program-d-extra"
INCOMPLETE_GOAL = "g-program-d-incomplete"
INCOMPLETE_MISSION = "m-program-d-incomplete"

CHECK_LOG: list[dict[str, Any]] = []


def _check(step: str, description: str, ok: bool) -> bool:
    CHECK_LOG.append({"step": step, "description": description,
                      "ok": bool(ok)})
    return bool(ok)


class AttestedRunner:
    """Deterministic exact-attestation success for every phase."""

    def run(self, request: Any) -> SubrunResult:
        return SubrunResult(
            0, mission_model.CR_COMPLETED,
            semantic_status=semantic.STATUS_SUCCESS,
            attestation=semantic.ATTESTATION_VERIFIED)


class FailingRunner:
    """Deterministic controlled failure for the fail-closed scenario."""

    def run(self, request: Any) -> SubrunResult:
        return SubrunResult(1, mission_model.CR_FAILED,
                            semantic_status=semantic.STATUS_FAILED)


def _node(node_id: str, mission_id: str, priority: int,
          depends_on: list[str] | None = None) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "title": f"Mission {node_id}",
        "priority": priority,
        "depends_on": list(depends_on or []),
        "acceptance_criteria": [{
            "criterion_id": f"ac-{node_id}",
            "statement": f"{node_id} work is proven"}],
        "mission_ref": {"mission_id": mission_id, "required": True},
        "resources": {"cpu_slots": 1},
        "budgets": {"repair_budget": 0, "max_attempts": 1},
    }


def _spec(goal_id: str, nodes: list[dict[str, Any]],
          objective: str) -> dict[str, Any]:
    return {"schema_version": 1, "goal_id": goal_id,
            "objective": objective, "nodes": nodes}


def _defaults() -> launch.MissionLaunchDefaults:
    return launch.MissionLaunchDefaults(
        repo_root=None, baseline_revision="base", repair_budget=0)


def _seed(root: str, goal_id: str, mission_ids: list[str],
          objective: str) -> dict[str, Any]:
    spec = _spec(goal_id, [
        _node("n1", mission_ids[0], 50),
    ], objective)
    launch.provision_from_spec(root, spec, _defaults())
    graph_store.create_graph(root, spec, repo_root=None,
                             baseline_revision="base")
    return spec


def _model_identity(root: str,
                    mission_ids: tuple[str, ...],
                    ) -> tuple[str | None, str | None, str | None]:
    """Return the (model, provider, locality) persisted in a sub-run command."""
    from trajectory_os import live_status

    for mission_id in mission_ids:
        try:
            _mission, paths = mission_store.load_mission(root, mission_id)
        except (mission_store.MissionNotFound,
                mission_store.MalformedMissionError):
            continue
        for subrun_id in _mission.subruns:
            try:
                record = mission_store.load_subrun(paths, subrun_id)
            except (mission_store.MissionNotFound,
                    mission_store.MalformedMissionError):
                continue
            command = list(record.command)
            if command.count("--model") != 1:
                continue
            index = command.index("--model")
            if index + 1 >= len(command):
                continue
            value = command[index + 1]
            if not isinstance(value, str):
                continue
            provider, locality = live_status.classify_provider(value)
            return value, provider, locality
    return None, None, None


def _run_cycles(root: str, goal_id: str, *, cycles: int = 4) -> list[Any]:
    return [
        portfolio_engine.run_cycle(
            root, [goal_id], portfolio_model.PortfolioPolicy(),
            runner_factory=AttestedRunner, session_subruns=32,
            created_at=f"2026-01-01T00:00:0{index}Z")
        for index in range(cycles)
    ]


# --- S1: the fifteen visible-product steps -----------------------------------


def _scenario_visible(root: str, out_dir: pathlib.Path) -> dict[str, Any]:
    spec = _seed(root, GOAL, [MISSION],
                 "Dogfood the visible persistent product.")
    _check("1-launch", "goal created from a declarative spec",
           graph_store.graph_exists(root, GOAL))
    graph, _ = graph_store.load_graph(root, GOAL)
    _check("2-decomposition", "decomposition graph materialized",
           len(graph.nodes) == 1 and len(spec["nodes"]) == 1)

    _run_cycles(root, GOAL)
    proof = proof_engine.build_proof(root, GOAL)
    decision = sched_engine.latest_decision(root, GOAL)
    _check("3-scheduling", "scheduler admitted the ready node",
           decision is not None and len(decision.admitted) == 1)
    _check("3-scheduling", "scheduler records declared resources",
           decision is not None and decision.admitted[0].demand.cpu_slots == 1)

    snapshot = goal_snapshot.build_snapshot(root, GOAL,
                                            now_iso="2026-01-01T00:00:09Z")
    _check("4-execution", "goal proof is complete after visible execution",
           proof.complete is True)
    _check("4-execution", "mission produced sub-runs",
           any(phase.subrun_ids for phase in
               mission_store.load_mission(root, MISSION)[0].phases))

    activity_last = snapshot["activity"]["last"]
    model_value, provider, locality = _model_identity(
        root, (MISSION, EXTRA_MISSION))
    _check("5-agent-identity", "agent/model/provider identity is persisted",
           activity_last is not None and model_value is not None
           and provider is not None and locality is not None)
    _check("6-resource-attribution",
           "resource attribution is exposed by the scheduler",
           snapshot["resources"]["cpu_limit"] is not None
           and snapshot["scheduler"]["admitted"] is not None)

    # Artifacts with lineage.
    manager = artifact_engine.WorkspaceManager(root)
    report = manager.record_artifact(
        goal_id=GOAL, kind=artifact_model.AK_REPORT, name="dogfood-report.md",
        content=b"# dogfood report\n", mission_id=MISSION,
        producer=artifact_model.PRODUCER_AGENT,
        created_at="2026-01-01T00:00:05Z")
    dataset = manager.record_artifact(
        goal_id=GOAL, kind=artifact_model.AK_DATASET, name="dogfood.bin",
        content=b"0123456789", mission_id=MISSION,
        parent_ids=(report.artifact_id,),
        created_at="2026-01-01T00:00:06Z")
    lineage = manager.lineage(GOAL, dataset.artifact_id)
    ok, problems = manager.verify(GOAL)
    _check("7-artifacts", "content-addressed artifacts with lineage",
           len(lineage) == 2 and ok is True and problems == [])

    # Token/context telemetry is provider grounded and never invented.
    result = agent_model.AgentResult.build(
        backend=agent_model.BACKEND_PI, status=agent_model.RS_COMPLETED,
        reason=agent_model.R_OK,
        events=[agent_model.AgentEvent.build(
            sequence=0, kind=agent_model.LK_RESULT, method="result",
            payload={"prompt_tokens": 100, "completion_tokens": 25,
                     "cache_read_input_tokens": 32})],
        runtime_ms=1234,
        completion=agent_model.CompletionEvidence.build(
            source=agent_model.CS_EXIT_CODE_MARKER, reliable=True,
            detail="ok"))
    metrics = telemetry.from_result(result, provider="ollama",
                                    model="qwen3.6:27b")
    telemetry.record(root, metrics)
    _check("5-agent-identity",
           "provider-grounded token/context telemetry is recorded",
           metrics.total_tokens == 125
           and metrics.sources["total_tokens"] == telemetry.SRC_DERIVED
           and metrics.cache_hit is True)

    # Adaptive replan: add a second node/mission and execute it.
    extra_node = _node("n2", EXTRA_MISSION, 40)
    from trajectory_os.graph import model as graph_model
    extra = graph_model.GraphNode.from_dict(extra_node, "extra")
    launch.ensure_missions(root, [extra], graph.objective, _defaults())
    replan_spec = {
        "schema_version": 1,
        "trigger": {"kind": "OPERATOR_REQUEST",
                    "detail": "dogfood adaptive replan"},
        "changes": [{"op": "ADD_NODE", "reason": "add a follow-up mission",
                     "node_id": "n2", "node_spec": extra_node}],
    }
    apply_result = replan_engine.apply_spec(
        root, GOAL, replan_spec, created_at="2026-01-01T00:00:07Z")
    _check("9-replan", "adaptive replan activated a new generation",
           apply_result.status == "ACCEPTED"
           and apply_result.generation is not None)
    _run_cycles(root, GOAL)
    proof_after = proof_engine.build_proof(root, GOAL)
    _check("9-replan", "replanned graph is proven end to end",
           proof_after.complete is True
           and {node.node_id for node in proof_after.nodes}
           == {"n1", "n2"})

    # Events / notifications from the refreshed projection.
    refresh = event_engine.refresh(root, GOAL, clock=lambda:
                                   "2026-01-01T00:00:09Z")
    document, records = event_store.reconstruct(root, GOAL)
    categories = {record.category for record in records}
    expected = {event_model.CAT_GOAL, event_model.CAT_MISSION,
                event_model.CAT_SCHEDULER, event_model.CAT_PROOF,
                event_model.CAT_GATE, event_model.CAT_ARTIFACT}
    _check("11-events", "authoritative event projection covers the surfaces",
           expected <= categories)
    _check("11-events", "event projection is deduplicated and reconstructible",
           document["count"] == len(records) == refresh.count
           and refresh.added and not refresh.removed)

    # Restart / reconstruction.
    read = proof_engine.reconstruct(root, GOAL)
    _check("10-reconstruction", "M016 proof reconstructs exactly",
           read.reconstructed and read.persisted.complete)
    again = event_engine.refresh(root, GOAL, clock=lambda:
                                 "2026-01-01T00:00:10Z")
    _check("10-reconstruction",
           "event projection is restart-safe (idempotent refresh)",
           again.added == () and again.removed == ()
           and again.count == refresh.count)

    # CLI / TUI.
    cli_ok = (goals_cli.main(["status", "--root", root, GOAL, "--json"]) == 0
              and goals_cli.main(["events", "--root", root, GOAL]) == 0
              and goals_cli.main(["list", "--root", root]) == 0)
    frame = goal_tui.render_frame(snapshot, width=100, height=30)
    _check("12-cli-tui", "CLI and TUI render the same canonical snapshot",
           cli_ok and GOAL in frame)

    # Web dashboard (loopback only).
    web_root = pathlib.Path(root) / "web"
    web_root.mkdir(exist_ok=True)
    config = web_model.WebConfig(root=root, default_goal_id=GOAL,
                                 port=0).validate()
    httpd = web_server.create_server(web_server.WebApp(config))
    thread = threading.Thread(target=httpd.serve_forever,
                              kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        port = httpd.server_address[1]

        def _get(path: str) -> tuple[int, str]:
            connection = http.client.HTTPConnection("127.0.0.1", port,
                                                    timeout=10)
            connection.request("GET", path)
            response = connection.getresponse()
            text = response.read().decode("utf-8")
            connection.close()
            return response.status, text

        overview_status, overview_body = _get("/api/overview")
        goal_status, goal_body = _get(f"/api/goals/{GOAL}")
        html_status, html_body = _get("/")
        web_ok = (overview_status == 200 and goal_status == 200
                  and html_status == 200
                  and json.loads(goal_body)["snapshot"]["final"]["complete"]
                  and "TrajectoryOS" in html_body)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
    _check("13-web", "web dashboard projects the same authoritative state",
           web_ok)
    _check("13-web", "web is a projection, not a second source of truth",
           web_projection.dashboard(root, GOAL)["snapshot"]["final"]["complete"])

    # LifeOS handoff.
    lifeos_target = out_dir / "lifeos"
    lifeos_config = lifeos_model.LifeOSConfig(adapters=(
        lifeos_model.AdapterConfig(
            kind=lifeos_model.AK_OBSIDIAN,
            target=str(lifeos_target / "vault")),
        lifeos_model.AdapterConfig(
            kind=lifeos_model.AK_SUPER_PRODUCTIVITY,
            target=str(lifeos_target / "sp")),
        lifeos_model.AdapterConfig(
            kind=lifeos_model.AK_JSON_MANIFEST,
            target=str(lifeos_target / "json")),
    )).validate()
    exchange = lifeos_engine.run_exchange(
        root, GOAL, lifeos_config, clock=lambda: "2026-01-01T00:00:11Z")
    handoff_files = [p for p in lifeos_target.rglob("*") if p.is_file()]
    again_exchange = lifeos_engine.run_exchange(
        root, GOAL, lifeos_config, clock=lambda: "2026-01-01T00:00:12Z")
    _check("14-lifeos", "LifeOS exchange produced scoped artifacts",
           exchange.status == "OK" and len(handoff_files) == 3
           and all(record.status == lifeos_model.XS_OK
                   for record in exchange.records))
    _check("14-lifeos", "LifeOS exchange is idempotent",
           again_exchange.status == "UNCHANGED"
           and all(record.status == lifeos_model.XS_SKIPPED
                   for record in again_exchange.records))
    bad_config = lifeos_model.LifeOSConfig(adapters=(
        lifeos_model.AdapterConfig(kind=lifeos_model.AK_OBSIDIAN,
                                   target=str(pathlib.Path(root) / "leak")),
        lifeos_model.AdapterConfig(kind=lifeos_model.AK_JSON_MANIFEST,
                                   target=str(lifeos_target / "json2")),
    )).validate()
    isolated = lifeos_engine.run_exchange(
        root, GOAL, bad_config, clock=lambda: "2026-01-01T00:00:13Z")
    _check("14-lifeos", "adapter failure is isolated and cannot corrupt "
           "canonical state",
           isolated.canonical_unchanged is True
           and not (pathlib.Path(root) / "leak").exists()
           and any(record.status == lifeos_model.XS_FAILED
                   for record in isolated.records)
           and any(record.status == lifeos_model.XS_OK
                   for record in isolated.records))

    # Final M016 proof.
    final_read = proof_engine.reconstruct(root, GOAL)
    _check("15-final-proof", "final M016 goal proof is complete and exact",
           final_read.reconstructed and final_read.persisted.complete
           and final_read.live.proof_id == final_read.persisted.proof_id)

    return {
        "proof_id": final_read.live.proof_id,
        "events": document["count"],
        "artifacts": 2,
        "exchange_id": exchange.exchange_id,
        "telemetry_id": metrics.telemetry_id,
    }


# --- S2: fail-closed incomplete case -----------------------------------------


def _scenario_incomplete(root: str) -> dict[str, Any]:
    spec = _spec(INCOMPLETE_GOAL,
                 [_node("n1", INCOMPLETE_MISSION, 50)],
                 "Prove the fail-closed incomplete path.")
    launch.provision_from_spec(root, spec, _defaults())
    graph_store.create_graph(root, spec, repo_root=None,
                             baseline_revision="base")
    portfolio_engine.run_cycle(
        root, [INCOMPLETE_GOAL], portfolio_model.PortfolioPolicy(),
        runner_factory=FailingRunner, session_subruns=32,
        created_at="2026-01-01T00:00:00Z")
    proof = proof_engine.build_proof(root, INCOMPLETE_GOAL)
    read = proof_engine.reconstruct(root, INCOMPLETE_GOAL)
    _check("8-failure", "controlled failure leaves the goal unproven",
           proof.complete is False and read.persisted.complete is False)
    _check("8-failure", "controlled failure is machine-readable",
           proof.final_state in ("BLOCKED", "FAILED")
           or proof.final_reason != "ALL_CRITERIA_PROVEN")
    refresh = event_engine.refresh(root, INCOMPLETE_GOAL, clock=lambda:
                                   "2026-01-01T00:00:01Z")
    severities = {record.severity for record in refresh.records}
    _check("8-failure", "failure surfaces as a warning event",
           event_model.SEV_WARNING in severities)
    snapshot = goal_snapshot.build_snapshot(root, INCOMPLETE_GOAL)
    _check("8-failure", "snapshot never claims completion",
           snapshot["final"]["complete"] is False)
    return {"final_state": proof.final_state,
            "final_reason": proof.final_reason,
            "events": refresh.count}


# --- no Git trust-boundary write ---------------------------------------------


def _scenario_no_git_write() -> dict[str, Any]:
    verbs = ("commit", "push", "merge", "reset", "restore", "clean", "stash",
             "rebase", "switch", "checkout", "pull", "fetch", "cherry-pick")
    modules = [event_engine, lifeos_engine, lifeos_adapters, web_server,
               web_model, web_projection]
    offenders: list[str] = []
    for module in modules:
        try:
            source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        except (OSError, TypeError):  # pragma: no cover - defensive
            continue
        for verb in verbs:
            if f'"git", "{verb}"' in source or f"'git', '{verb}'" in source:
                offenders.append(f"{module.__name__}:{verb}")
    _check("16-git-safety", "no module performs a Git trust-boundary write",
           not offenders)
    return {"offenders": offenders,
            "checked": [module.__name__ for module in modules]}


def _step_result(step: str) -> dict[str, Any]:
    checks = [c for c in CHECK_LOG if c["step"] == step]
    return {"step": step, "checks": len(checks),
            "passed": sum(1 for c in checks if c["ok"]),
            "status": "PASS" if checks and all(c["ok"] for c in checks)
            else "FAIL" if checks else "MISSING"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mission026_028_dogfood",
        description="Final M026-M028 visible persistent product dogfood.")
    parser.add_argument("--root", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--keep", action="store_true", default=False)
    args = parser.parse_args(argv)

    temporary = args.root is None
    root = args.root or tempfile.mkdtemp(prefix="trajectory-m026-m028-")
    pathlib.Path(root).mkdir(parents=True, exist_ok=True)
    # Adapter targets must live OUTSIDE the canonical root (that structural
    # guard is itself part of the M028 contract).
    if args.out:
        out_parent = pathlib.Path(args.out).parent
        remove_out = False
    else:
        out_parent = pathlib.Path(
            tempfile.mkdtemp(prefix="trajectory-m026-m028-out-"))
        remove_out = True
    out_dir = out_parent / "dogfood-output"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        visible = _scenario_visible(root, out_dir)
        incomplete = _scenario_incomplete(root)
        git = _scenario_no_git_write()
        steps = sorted({c["step"] for c in CHECK_LOG})
        report = {
            "program": "M026-M028",
            "generated_at": datetime.datetime.now(datetime.UTC)
            .replace(microsecond=0, tzinfo=None).isoformat() + "Z",
            "root": root,
            "visible": visible,
            "incomplete": incomplete,
            "git_safety": git,
            "steps": [_step_result(step) for step in steps],
            "checks": CHECK_LOG,
            "status": "PASS" if all(c["ok"] for c in CHECK_LOG) else "FAIL",
        }
        out = pathlib.Path(args.out or pathlib.Path(root) /
                           "m026-m028-dogfood.json")
        out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        print(f"M026-M028 dogfood {report['status']}: {out}")
        for step in steps:
            print(f"  {step}: {_step_result(step)['status']}")
        return 0 if report["status"] == "PASS" else 3
    finally:
        if temporary and not args.keep:
            shutil.rmtree(root, ignore_errors=True)
        if remove_out and not args.keep:
            shutil.rmtree(out_parent, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
