"""M018 — unified operator CLI for the visible goal-execution product layer.

One coherent production surface over the canonical persisted state. Read
commands project canonical state (never a second source of truth); control
commands drive the existing canonical engines (mission orchestrator +
portfolio scheduler) and never perform a Git trust-boundary write.

    start      create the goal graph (provisioning missions first) and run
    resume     clear a safe-stop request and continue a goal (bounded)
    stop       request a safe stop (never kills an in-flight sub-run)
    status     concise canonical state (human + JSON)
    inspect    full canonical snapshot (human + JSON)
    explain    deterministic why/explain for the goal or one node (alias why)
    dashboard  compact goal-proof dashboard
    evidence   acceptance criteria plus the exact per-phase mission evidence
    proof      derived goal proof + reconstruction validation
    tui        live one-screen terminal dashboard
    list       all goal graphs under the root
    version    CLI version

Exit codes: 0 ok, 2 usage, 3 rejected/blocked, 4 unknown goal.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from trajectory_os import __version__
from trajectory_os.artifacts import model as artifact_model
from trajectory_os.artifacts import summary as artifact_summary
from trajectory_os.daemon import engine as daemon_engine
from trajectory_os.daemon import model as daemon_model
from trajectory_os.daemon import summary as daemon_summary
from trajectory_os.events import model as event_model
from trajectory_os.events import summary as event_summary
from trajectory_os.goals import launch as goal_launch
from trajectory_os.goals import runner as goal_runner
from trajectory_os.goals import snapshot as goal_snapshot
from trajectory_os.goals import tui as goal_tui
from trajectory_os.graph import model as graph_model
from trajectory_os.graph import store as graph_store
from trajectory_os.graph.proof import engine as proof_engine
from trajectory_os.graph.proof import summary as proof_summary
from trajectory_os.graph.scheduler import engine as sched_engine
from trajectory_os.graph.scheduler import model as sched_model
from trajectory_os.lifeos import engine as lifeos_engine
from trajectory_os.lifeos import model as lifeos_model
from trajectory_os.lifeos import summary as lifeos_summary
from trajectory_os.missions import adapter
from trajectory_os.missions import model as mission_model
from trajectory_os.missions import store as mission_store
from trajectory_os.portfolio import engine as portfolio_engine
from trajectory_os.portfolio import model as portfolio_model
from trajectory_os.portfolio import summary as portfolio_summary
from trajectory_os.resources import model as resource_model
from trajectory_os.resources import summary as resource_summary
from trajectory_os.web import model as web_model
from trajectory_os.web import server as web_server

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REJECTED = 3
EXIT_NOT_FOUND = 4

MAX_POLICY_BYTES = 65536


class UsageError(Exception):
    """Operator usage error (rejected before any consequential state)."""


def _root_from(argv_root: str | None) -> str:
    if argv_root:
        return argv_root
    env_root = (os.environ.get("TRAJECTORY_GOALS_ROOT")
                or os.environ.get("TRAJECTORY_MISSIONS_ROOT"))
    if env_root:
        return env_root
    return str(Path.cwd() / ".trajectory-pi")


def _fail(message: str, code: int = EXIT_USAGE) -> int:
    print(f"error: {message}", file=sys.stderr)
    return code


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _load_spec(path: str) -> object:
    p = Path(path)
    if not p.is_file():
        raise UsageError(f"spec missing or not a regular file: {path}")
    try:
        raw = p.read_bytes()[: graph_model.MAX_SPEC_BYTES + 1]
    except OSError as exc:
        raise UsageError(f"spec unreadable: {exc}") from exc
    if len(raw) > graph_model.MAX_SPEC_BYTES:
        raise UsageError(
            f"spec exceeds the hard cap of {graph_model.MAX_SPEC_BYTES} bytes")
    try:
        obj: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UsageError(f"spec is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise UsageError("spec must be a JSON object")
    return obj


def _load_policy(path: str | None) -> sched_model.SchedulerPolicy:
    if path is None:
        return sched_model.DEFAULT_POLICY
    p = Path(path)
    if not p.is_file():
        raise UsageError(f"policy missing or not a regular file: {path}")
    try:
        raw = p.read_bytes()[: MAX_POLICY_BYTES + 1]
    except OSError as exc:
        raise UsageError(f"policy unreadable: {exc}") from exc
    if len(raw) > MAX_POLICY_BYTES:
        raise UsageError(f"policy exceeds {MAX_POLICY_BYTES} bytes")
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UsageError(f"policy is not valid JSON: {exc}") from exc
    try:
        return sched_model.SchedulerPolicy.from_dict(obj)
    except sched_model.SchedulerValidationError as exc:
        raise UsageError(f"policy rejected: {exc}") from exc


def _load_json_object(path: str, *, label: str) -> object:
    p = Path(path)
    if not p.is_file():
        raise UsageError(f"{label} missing or not a regular file: {path}")
    try:
        raw = p.read_bytes()[: MAX_POLICY_BYTES + 1]
    except OSError as exc:
        raise UsageError(f"{label} unreadable: {exc}") from exc
    if len(raw) > MAX_POLICY_BYTES:
        raise UsageError(f"{label} exceeds {MAX_POLICY_BYTES} bytes")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UsageError(f"{label} is not valid JSON: {exc}") from exc


def _load_portfolio_policy(path: str | None) -> portfolio_model.PortfolioPolicy:
    if path is None:
        return portfolio_model.DEFAULT_POLICY
    obj = _load_json_object(path, label="portfolio policy")
    try:
        return portfolio_model.PortfolioPolicy.from_dict(obj)
    except portfolio_model.PortfolioError as exc:
        raise UsageError(f"portfolio policy rejected: {exc}") from exc


def _load_resource_policy(path: str | None) -> resource_model.ResourcePolicy:
    if path is None:
        return resource_model.ResourcePolicy()
    obj = _load_json_object(path, label="resource policy")
    try:
        return resource_model.ResourcePolicy.from_dict(obj)
    except resource_model.ResourceOrchestrationError as exc:
        raise UsageError(f"resource policy rejected: {exc}") from exc


def _load_dependencies(
        path: str | None) -> portfolio_model.PortfolioDependencies:
    if path is None:
        return portfolio_model.PortfolioDependencies()
    obj = _load_json_object(path, label="portfolio dependencies")
    try:
        return portfolio_model.PortfolioDependencies.normalize(obj)
    except portfolio_model.PortfolioError as exc:
        raise UsageError(f"dependencies rejected: {exc}") from exc


def _resolve_goals(root: str,
                   explicit: list[str] | None) -> list[str]:
    if explicit:
        return sorted(set(explicit))
    goals = graph_store.list_goal_ids(root)
    if not goals:
        raise UsageError(f"no goal graphs under {root}; pass --goal")
    return goals


def _split_command(value: str | None) -> tuple[str, ...] | None:
    try:
        return adapter.split_command(value)
    except adapter.AdapterError as exc:
        raise UsageError(f"command: {exc}") from exc


def _launch_defaults(args: argparse.Namespace) -> goal_launch.MissionLaunchDefaults:
    repo = args.repo or os.getcwd()
    head = args.head
    return goal_launch.MissionLaunchDefaults(
        repo_root=repo,
        baseline_revision=head,
        model=args.model or adapter.DEFAULT_MODEL,
        pi_wrapper=args.pi_wrapper or adapter.DEFAULT_PI_WRAPPER,
        validate_command=(_split_command(args.validate)
                          or adapter.DEFAULT_VALIDATE_COMMAND),
        consolidate_command=_split_command(args.consolidate),
        gpu=bool(args.gpu),
        gpu_mem_bytes=(args.gpu_mem if args.gpu_mem is not None
                       else 0),
        time_budget_s=(args.time_budget
                       if args.time_budget is not None
                       else mission_model.DEFAULT_TIME_BUDGET_S),
        repair_budget=(args.repair_budget
                       if args.repair_budget is not None
                       else mission_model.MAX_REPAIR_ROUNDS),
        subrun_budget=args.subrun_budget,
    )


def _run_config(args: argparse.Namespace,
                defaults: goal_launch.MissionLaunchDefaults,
                ) -> goal_runner.GoalRunConfig:
    try:
        policy = _load_policy(args.policy)
        config = goal_runner.GoalRunConfig(
            policy=policy,
            session_subruns=(args.session_subruns
                             if args.session_subruns is not None
                             else sched_engine.DEFAULT_SESSION_SUBRUNS),
            max_cycles=(args.max_cycles
                        if args.max_cycles is not None
                        else goal_runner.DEFAULT_MAX_CYCLES),
            launch=defaults,
        )
        config.validate()
    except ValueError as exc:
        raise UsageError(str(exc)) from exc
    return config


def _emit_run_report(report: goal_runner.GoalRunReport,
                     as_json: bool) -> None:
    if as_json:
        _print_json(report.to_dict())
        return
    print(f"goal      : {report.goal_id} -> {report.status} "
          f"({report.reason})")
    print(f"final     : {report.final_state}/{report.final_reason} "
          f"complete={report.complete}")
    print(f"proof     : {report.proof_id}")
    print(f"cycles    : {report.cycles}")
    for mission_id, outcome in sorted(report.missions.items()):
        print(f"  mission : {mission_id} [{outcome}]")
    for transition in report.transitions:
        if transition.get("action") == "dispatch":
            admitted = transition.get("admitted", [])
            dispatched = transition.get("dispatched", [])
            print(f"  cycle {transition['cycle']}: "
                  f"admitted={admitted} "
                  f"dispatched="
                  f"{[d['mission_id'] for d in dispatched]} "
                  f"goal={transition.get('goal_state')}")


def _snapshot_errors(exc: Exception) -> int:
    if isinstance(exc, graph_store.GraphNotFound):
        return _fail(str(exc), EXIT_NOT_FOUND)
    code = getattr(exc, "code", None)
    return _fail(f"error(rejected): {code or exc}", EXIT_REJECTED)


# --- commands ------------------------------------------------------------------


def _cmd_start(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        spec = _load_spec(args.spec)
    except UsageError as exc:
        return _fail(f"spec: {exc}")
    try:
        defaults = _launch_defaults(args)
    except UsageError as exc:
        return _fail(str(exc))
    try:
        normalized = graph_model.normalize_spec(spec)
    except graph_model.GraphValidationError as exc:
        return _fail(f"graph rejected: {exc}", EXIT_REJECTED)
    goal_id = normalized.goal_id
    if graph_store.graph_paths(root, goal_id)["graph"].is_file():
        return _fail(f"goal already exists: {goal_id} (use resume)",
                     EXIT_REJECTED)
    try:
        config = _run_config(args, defaults)
    except UsageError as exc:
        return _fail(str(exc))
    try:
        outcomes = goal_launch.ensure_missions(
            root, normalized.nodes, normalized.objective, defaults)
        graph = graph_store.create_graph(
            root, spec, repo_root=defaults.repo_root,
            baseline_revision=defaults.baseline_revision)
    except Exception as exc:  # noqa: BLE001 - fail closed, machine-readable
        return _fail(f"launch rejected: {exc}", EXIT_REJECTED)
    try:
        report = goal_runner.run_goal(root, goal_id, config=config)
    except (graph_store.GraphNotFound, graph_model.GraphValidationError) as exc:
        return _snapshot_errors(exc)
    if args.json:
        _print_json({
            "status": "STARTED",
            "goal_id": goal_id,
            "graph_id": graph.graph_id,
            "missions": outcomes,
            "run": report.to_dict(),
        })
    else:
        print(f"created   : {goal_id} graph={graph.graph_id}")
        for mission_id, outcome in sorted(outcomes.items()):
            print(f"  mission : {mission_id} [{outcome}]")
        _emit_run_report(report, as_json=False)
    return _run_exit(report)


def _cmd_resume(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        graph, _ = graph_store.load_graph(root, args.goal_id)
    except graph_store.GraphNotFound as exc:
        return _fail(str(exc), EXIT_NOT_FOUND)
    except graph_model.GraphValidationError as exc:
        return _fail(f"error(rejected): {exc}", EXIT_REJECTED)
    try:
        defaults = _launch_defaults(args)
        config = _run_config(args, defaults)
    except UsageError as exc:
        return _fail(str(exc))
    try:
        report = goal_runner.run_goal_resume(
            root, graph.goal_id, config=config)
    except (graph_store.GraphNotFound, graph_model.GraphValidationError) as exc:
        return _snapshot_errors(exc)
    if args.json:
        _print_json(report.to_dict())
    else:
        _emit_run_report(report, as_json=False)
    return _run_exit(report)


def _cmd_stop(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        graph, _ = graph_store.load_graph(root, args.goal_id)
    except graph_store.GraphNotFound as exc:
        return _fail(str(exc), EXIT_NOT_FOUND)
    except graph_model.GraphValidationError as exc:
        return _fail(f"error(rejected): {exc}", EXIT_REJECTED)
    document = goal_runner.request_stop(
        root, graph.goal_id, reason=args.reason)
    if args.json:
        _print_json({"status": "STOP_REQUESTED", **document})
    else:
        print(f"stop requested : {graph.goal_id} "
              f"reason={document['reason']}")
        print("safe stop      : current bounded step completes; no new work "
              "is launched; use resume to continue")
    return EXIT_OK


def _cmd_status(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        snapshot = goal_snapshot.build_snapshot(root, args.goal_id)
    except (graph_store.GraphNotFound, graph_model.GraphValidationError) as exc:
        return _snapshot_errors(exc)
    except Exception as exc:  # noqa: BLE001 - bounded operator error surface
        return _snapshot_errors(exc)
    if args.json:
        _print_json(snapshot)
    else:
        print(goal_snapshot.render_status(snapshot))
    return EXIT_OK


def _cmd_inspect(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        snapshot = goal_snapshot.build_snapshot(root, args.goal_id)
    except (graph_store.GraphNotFound, graph_model.GraphValidationError) as exc:
        return _snapshot_errors(exc)
    if args.json:
        _print_json(snapshot)
    else:
        print(goal_snapshot.render_inspect(snapshot))
    return EXIT_OK


def _cmd_explain(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        document = proof_summary.explain_document(
            root, args.goal_id, args.subject or "goal")
    except (graph_store.GraphNotFound, graph_model.GraphValidationError) as exc:
        return _snapshot_errors(exc)
    if args.json:
        _print_json(document)
        return EXIT_OK
    if document.get("status") == "UNKNOWN_NODE":
        return _fail(f"node not found: {args.subject}", EXIT_USAGE)
    print(proof_summary.render_explain(document))
    return EXIT_OK


def _cmd_dashboard(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        document = proof_summary.dashboard_document(root, args.goal_id)
    except (graph_store.GraphNotFound, graph_model.GraphValidationError) as exc:
        return _snapshot_errors(exc)
    if args.json:
        _print_json(document)
    else:
        print(proof_summary.render_dashboard(document))
    return EXIT_OK


def _evidence_document(root: str, goal_id: str) -> dict[str, Any]:
    proof = proof_engine.build_proof(root, goal_id)
    graph, _ = graph_store.load_graph(root, goal_id)
    missions: list[dict[str, Any]] = []
    for node in graph.nodes:
        ref = node.mission_ref
        if ref is None:
            continue
        entry: dict[str, Any] = {
            "node_id": node.node_id,
            "mission_id": ref.mission_id,
            "state": None,
            "reason": None,
            "phases": [],
        }
        try:
            mission, paths = mission_store.load_mission(root, ref.mission_id)
        except (mission_store.MissionNotFound,
                mission_store.MalformedMissionError) as exc:
            entry["state"] = "MISSING"
            entry["reason"] = type(exc).__name__
            missions.append(entry)
            continue
        entry["state"] = mission.mission_state
        entry["reason"] = mission.mission_reason
        phases: list[dict[str, Any]] = []
        for phase in mission.phases:
            evidence: dict[str, Any] | None = None
            if phase.state == mission_model.PS_PASSED:
                try:
                    evidence = mission_store.load_phase_evidence(
                        paths, phase.phase_id)
                except mission_store.MalformedMissionError:
                    evidence = None
            phases.append({
                "phase_id": phase.phase_id,
                "kind": phase.kind,
                "state": phase.state,
                "reason": phase.reason,
                "subruns": list(phase.subrun_ids),
                "evidence": evidence,
            })
        entry["phases"] = phases
        missions.append(entry)
    return {
        "status": "OK",
        "goal_id": proof.goal_id,
        "graph_id": proof.graph_id,
        "proof_id": proof.proof_id,
        "final_state": proof.final_state,
        "final_reason": proof.final_reason,
        "criteria": [c.to_dict() for c in proof.criteria],
        "missions": missions,
    }


def _cmd_evidence(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        document = _evidence_document(root, args.goal_id)
    except Exception as exc:  # noqa: BLE001 - bounded operator error surface
        return _snapshot_errors(exc)
    if args.json:
        _print_json(document)
        return EXIT_OK
    print(f"goal     : {document['goal_id']} "
          f"[{document['final_state']}/{document['final_reason']}]")
    print(f"proof    : {document['proof_id']}")
    for criterion in document["criteria"]:
        print(proof_summary.criterion_line(criterion))
    print("mission evidence:")
    for mission in document["missions"]:
        print(f"  - {mission['node_id']} {mission['mission_id']} "
              f"state={mission['state']}")
        for phase in mission["phases"]:
            print(f"      {phase['phase_id']} {phase['state']} "
                  f"subruns={phase['subruns']}")
    return EXIT_OK


def _cmd_proof(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        read = proof_engine.reconstruct(root, args.goal_id)
    except (graph_store.GraphNotFound, graph_model.GraphValidationError) as exc:
        return _snapshot_errors(exc)
    payload = read.to_dict()
    payload["status"] = "OK"
    payload["identity"] = proof_engine.identity_refs(read.live)
    if args.json:
        _print_json(payload)
        return EXIT_OK
    print(f"goal      : {args.goal_id}")
    print(f"final     : {read.persisted.final_state} "
          f"({read.persisted.final_reason}) complete="
          f"{read.persisted.complete}")
    print(f"proof     : {read.live.proof_id}")
    print(f"persisted : {read.persisted.proof_id} "
          f"reconstructed={read.reconstructed}")
    return EXIT_OK


def _cmd_tui(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        return goal_tui.run_tui(
            root, args.goal_id,
            interval_s=args.interval,
            iterations=args.iterations,
            width=args.width,
            height=args.height,
            resource_probe=lambda: resource_summary.status_document(root=root))
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return EXIT_OK


def _cmd_resource_status(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        policy = _load_resource_policy(args.policy)
    except UsageError as exc:
        return _fail(str(exc))
    try:
        document = resource_summary.status_document(root=root, policy=policy)
    except Exception as exc:  # noqa: BLE001 - bounded operator error surface
        return _fail(f"resource probe failed: {type(exc).__name__}",
                     EXIT_REJECTED)
    if args.json:
        _print_json(document)
    else:
        print(resource_summary.render_status(document))
    return EXIT_OK


def _cmd_artifacts(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    if args.lineage:
        try:
            document = artifact_summary.lineage_document(
                root, args.goal_id, args.lineage)
        except artifact_model.ArtifactError as exc:
            return _fail(f"artifact rejected: {exc.code}", EXIT_REJECTED)
        if args.json:
            _print_json(document)
        else:
            print(artifact_summary.render_lineage(document))
        return EXIT_OK
    try:
        document = artifact_summary.status_document(root, args.goal_id)
    except artifact_model.ArtifactError as exc:
        return _fail(f"artifact rejected: {exc.code}", EXIT_REJECTED)
    if args.json:
        _print_json(document)
    else:
        print(artifact_summary.render_status(document))
    return EXIT_OK


def _cmd_events(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        document = event_summary.document(
            root, args.goal_id, refresh=args.refresh)
    except event_model.EventError as exc:
        return _fail(f"event projection rejected: {exc.code}", EXIT_REJECTED)
    except (graph_store.GraphNotFound, graph_model.GraphValidationError) as exc:
        return _snapshot_errors(exc)
    if args.json:
        _print_json(document)
    else:
        print(event_summary.render(document))
    return EXIT_OK


def _cmd_web(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        config = web_model.WebConfig(
            root=root, default_goal_id=args.goal_id, host=args.host,
            port=args.port, token=args.token).validate()
    except web_model.WebError as exc:
        return _fail(f"web config rejected: {exc.code}")
    app = web_server.WebApp(config)

    def _ready(server: Any) -> None:
        host, port = server.server_address[:2]
        print(f"trajectory web dashboard: http://{host}:{port}/ "
              f"(loopback only; goal={config.default_goal_id or '-'})")
        print("controls: " + ", ".join(sorted(web_model.CONTROLS)))
        print("no Git trust-boundary write is ever performed")

    try:
        web_server.serve(app, on_ready=_ready)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return EXIT_OK
    return EXIT_OK


def _cmd_lifeos_status(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    document = lifeos_summary.status_document(root)
    if args.json:
        _print_json(document)
    else:
        print(lifeos_summary.render(document))
    return EXIT_OK


def _cmd_lifeos_export(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        raw = _load_json_object(args.config, label="lifeos config")
        config = lifeos_model.LifeOSConfig.from_dict(raw)
        report = lifeos_engine.run_exchange(root, args.goal_id, config)
    except UsageError as exc:
        return _fail(str(exc))
    except lifeos_model.LifeOSError as exc:
        return _fail(f"lifeos rejected: {exc.code}", EXIT_REJECTED)
    if args.json:
        _print_json(report.to_dict())
    else:
        print(f"lifeos     : {report.status} "
              f"exchange={report.exchange_id}")
        for record in report.records:
            print(f"  - {record.adapter} {record.status} "
                  f"outputs={len(record.outputs)}"
                  + (f" error={record.error}" if record.error else ""))
    return EXIT_OK if report.status in ("OK", "UNCHANGED") \
        else EXIT_REJECTED


def _cmd_list(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    entries: list[dict[str, Any]] = []
    for goal_id in graph_store.list_goal_ids(root):
        try:
            snapshot = goal_snapshot.build_snapshot(root, goal_id)
            entries.append({
                "goal_id": goal_id,
                "state": snapshot["final"]["state"],
                "reason": snapshot["final"]["reason"],
                "complete": snapshot["final"]["complete"],
                "criteria_proven": snapshot["counts"]["criteria_proven"],
                "criteria_total": snapshot["counts"]["criteria_total"],
                "stop": snapshot["stop"] is not None,
                "state_status": "OK",
            })
        except Exception as exc:  # noqa: BLE001 - list must not crash
            entries.append({
                "goal_id": goal_id,
                "state_status": "MALFORMED",
                "error": type(exc).__name__,
            })
    if args.json:
        _print_json({"status": "OK", "root": root, "goals": entries})
        return EXIT_OK
    if not entries:
        print(f"no goal graphs under {root}")
        return EXIT_OK
    for entry in entries:
        if entry["state_status"] != "OK":
            print(f"{entry['goal_id']}  MALFORMED ({entry.get('error')})")
            continue
        stop = " STOP" if entry["stop"] else ""
        print(f"{entry['goal_id']}  {entry['state']}/{entry['reason']} "
              f"criteria={entry['criteria_proven']}/"
              f"{entry['criteria_total']}{stop}")
    return EXIT_OK


def _cmd_version() -> int:
    print(f"trajectory-pi-goal {__version__}")
    return EXIT_OK


def _cmd_portfolio_status(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    document = portfolio_summary.status_document(root)
    if args.json:
        _print_json(document)
    else:
        print(portfolio_summary.render_status(document))
    return EXIT_OK


def _cmd_portfolio_run(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        policy = _load_portfolio_policy(args.policy)
        dependencies = _load_dependencies(args.dependencies)
        goals = _resolve_goals(root, args.goal)
        result = portfolio_engine.run_cycle(
            root, goals, policy, dependencies=dependencies,
            dispatch=not args.no_dispatch,
            session_subruns=args.session_subruns)
    except UsageError as exc:
        return _fail(str(exc))
    except (portfolio_model.PortfolioError, graph_store.GraphNotFound,
            graph_model.GraphValidationError) as exc:
        return _fail(f"portfolio rejected: {getattr(exc, 'code', exc)}",
                     EXIT_REJECTED)
    if args.json:
        _print_json(result.to_dict())
    else:
        print(portfolio_summary.render_status(
            portfolio_summary.status_document(root)))
    return EXIT_OK


def _daemon_config(args: argparse.Namespace) -> daemon_model.DaemonConfig:
    policy = _load_portfolio_policy(args.policy)
    dependencies = _load_dependencies(args.dependencies)
    return daemon_model.DaemonConfig(
        max_cycles=(args.max_cycles if args.max_cycles is not None
                    else daemon_model.DEFAULT_MAX_CYCLES),
        session_subruns=(args.session_subruns
                         if args.session_subruns is not None else 1),
        policy=policy,
        dependencies=dependencies,
    ).validate()


def _cmd_daemon_status(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    document = daemon_summary.status_document(root)
    if args.json:
        _print_json(document)
    else:
        print(daemon_summary.render_status(document))
    return EXIT_OK


def _run_daemon_command(args: argparse.Namespace, *,
                        resume: bool) -> int:
    root = _root_from(args.root)
    try:
        config = _daemon_config(args)
        goals = _resolve_goals(root, args.goal)
        if resume:
            report = daemon_engine.resume_daemon(root, goals, config=config)
        else:
            report = daemon_engine.run_daemon(
                root, goals, config=config,
                clear_stop_request=bool(getattr(args, "clear_stop", False)))
    except UsageError as exc:
        return _fail(str(exc))
    except (daemon_model.DaemonError, portfolio_model.PortfolioError,
            graph_store.GraphNotFound, graph_model.GraphValidationError) as exc:
        return _fail(f"daemon rejected: {getattr(exc, 'code', exc)}",
                     EXIT_REJECTED)
    if args.json:
        _print_json(report.to_dict())
    else:
        print(daemon_summary.render_status(
            daemon_summary.status_document(root)))
        print(f"session   : {report.status} ({report.reason}) "
              f"cycles={report.session_cycles} "
              f"complete={list(report.complete_goals)}")
    return EXIT_OK


def _cmd_daemon_start(args: argparse.Namespace) -> int:
    return _run_daemon_command(args, resume=False)


def _cmd_daemon_resume(args: argparse.Namespace) -> int:
    return _run_daemon_command(args, resume=True)


def _cmd_daemon_stop(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    document = daemon_engine.request_stop(root, reason=args.reason)
    if args.json:
        _print_json({"status": "STOP_REQUESTED", **document})
    else:
        print(f"stop requested : {document['reason']}")
        print("safe stop      : the current bounded cycle completes; no new "
              "work is launched; use daemon-resume to continue")
    return EXIT_OK


def _run_exit(report: goal_runner.GoalRunReport) -> int:
    if report.complete:
        return EXIT_OK
    if report.status in (goal_runner.GS_STOPPED, goal_runner.GS_STALLED,
                         goal_runner.GS_CYCLE_BOUND):
        return EXIT_REJECTED
    return EXIT_REJECTED


# --- parser --------------------------------------------------------------------


def _add_shared_flags(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--root", default=None, dest="sub_root",
                           help="goals/missions root for this command "
                                "(overrides the global --root)")
    subparser.add_argument("--json", action="store_true", default=False,
                           dest="sub_json",
                           help="machine-readable output")


def _add_launch_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", default=None, help="repository worktree")
    p.add_argument("--head", default=None,
                   help="baseline revision (default: read-only HEAD probe)")
    p.add_argument("--pi-wrapper", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--validate", default=None)
    p.add_argument("--consolidate", default=None)
    p.add_argument("--gpu", action="store_true", default=False)
    p.add_argument("--gpu-mem", type=int, default=None)
    p.add_argument("--time-budget", type=int, default=None)
    p.add_argument("--subrun-budget", type=int, default=None)
    p.add_argument("--repair-budget", type=int, default=None)


def _add_run_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--policy", default=None, help="capacity policy JSON file")
    p.add_argument("--max-cycles", type=int, default=None)
    p.add_argument("--session-subruns", type=int, default=None)


def _add_portfolio_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--goal", action="append", default=None,
                   help="member goal id (repeatable; default: all goals)")
    p.add_argument("--policy", default=None,
                   help="portfolio capacity policy JSON file")
    p.add_argument("--dependencies", default=None,
                   help="cross-goal dependencies JSON file")
    p.add_argument("--session-subruns", type=int, default=None)


def _add_daemon_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--max-cycles", type=int, default=None,
                   help="bounded daemon session cycles")


def build_parser(prog: str = "trajectory-pi-goal") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="TrajectoryOS unified goal operator CLI (visible "
                    "end-to-end execution; no Git trust-boundary write).")
    parser.add_argument("--root", default=None)
    parser.add_argument("--json", action="store_true", default=False)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start", help="create the goal and run it end to end")
    _add_shared_flags(p)
    p.add_argument("--spec", required=True, help="declarative goal spec JSON")
    _add_launch_options(p)
    _add_run_options(p)

    p = sub.add_parser("resume", help="clear safe-stop and continue a goal")
    _add_shared_flags(p)
    p.add_argument("goal_id")
    _add_launch_options(p)
    _add_run_options(p)

    p = sub.add_parser("stop", help="request a safe stop")
    _add_shared_flags(p)
    p.add_argument("goal_id")
    p.add_argument("--reason", default=None)

    for name, help_text in (
        ("status", "concise canonical state"),
        ("inspect", "full canonical snapshot"),
        ("dashboard", "compact goal-proof dashboard"),
        ("evidence", "acceptance criteria + exact mission evidence"),
        ("proof", "derived goal proof + reconstruction validation"),
    ):
        p = sub.add_parser(name, help=help_text)
        _add_shared_flags(p)
        p.add_argument("goal_id")

    p = sub.add_parser("explain", help="deterministic why/explain")
    _add_shared_flags(p)
    p.add_argument("goal_id")
    p.add_argument("subject", nargs="?", default="goal")

    p = sub.add_parser("why", help="deterministic why/explain (alias)")
    _add_shared_flags(p)
    p.add_argument("goal_id")
    p.add_argument("subject", nargs="?", default="goal")

    p = sub.add_parser("tui", help="live one-screen terminal dashboard")
    _add_shared_flags(p)
    p.add_argument("goal_id")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--iterations", type=int, default=None)
    p.add_argument("--width", type=int, default=None)
    p.add_argument("--height", type=int, default=None)

    p = sub.add_parser("list", help="all goal graphs under the root")
    _add_shared_flags(p)

    p = sub.add_parser("portfolio", help="multi-goal portfolio status")
    _add_shared_flags(p)

    p = sub.add_parser("portfolio-run", help="run one portfolio cycle")
    _add_shared_flags(p)
    _add_portfolio_options(p)
    p.add_argument("--no-dispatch", action="store_true", default=False,
                   help="plan only; never advance a selected goal")

    p = sub.add_parser("daemon", help="persistent daemon status")
    _add_shared_flags(p)

    p = sub.add_parser("daemon-start",
                       help="run a bounded persistent daemon session")
    _add_shared_flags(p)
    _add_portfolio_options(p)
    _add_daemon_options(p)
    p.add_argument("--clear-stop", action="store_true", default=False,
                   help="clear a prior safe-stop request before starting")

    p = sub.add_parser("daemon-resume",
                       help="clear safe-stop and resume the daemon")
    _add_shared_flags(p)
    _add_portfolio_options(p)
    _add_daemon_options(p)

    p = sub.add_parser("daemon-stop", help="request a daemon safe stop")
    _add_shared_flags(p)
    p.add_argument("--reason", default=None)

    p = sub.add_parser("resource-status",
                       help="live local CPU/GPU/VRAM resource state")
    _add_shared_flags(p)
    p.add_argument("--policy", default=None,
                   help="resource reservation policy JSON file")

    p = sub.add_parser("artifacts",
                       help="workspace + artifact provenance/lineage")
    _add_shared_flags(p)
    p.add_argument("goal_id")
    p.add_argument("--lineage", default=None,
                   help="show ancestor lineage for one artifact id")

    p = sub.add_parser("events",
                       help="authoritative event/notification projection")
    _add_shared_flags(p)
    p.add_argument("goal_id")
    p.add_argument("--refresh", action="store_true", default=False,
                   help="durably re-derive and persist the projection")

    p = sub.add_parser("web",
                       help="local/private operator web dashboard")
    _add_shared_flags(p)
    p.add_argument("--goal", default=None, dest="goal_id",
                   help="default goal for controls")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int,
                   default=web_model.DEFAULT_PORT)
    p.add_argument("--token", default=None,
                   help="bearer token (default: TRAJECTORY_WEB_TOKEN)")

    p = sub.add_parser("lifeos", help="LifeOS integration exchange ledger")
    _add_shared_flags(p)

    p = sub.add_parser("lifeos-export",
                       help="run a scoped LifeOS exchange")
    _add_shared_flags(p)
    p.add_argument("goal_id")
    p.add_argument("--config", required=True,
                   help="explicit LifeOS adapter config JSON")

    sub.add_parser("version", help="CLI version")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_USAGE
        return EXIT_OK if code in (0, EXIT_OK) else EXIT_USAGE
    if args.command == "version":
        return _cmd_version()
    if getattr(args, "sub_root", None) is not None:
        args.root = args.sub_root
        args.sub_root = None
    args.json = bool(args.json or getattr(args, "sub_json", False))
    handler = {
        "start": _cmd_start,
        "resume": _cmd_resume,
        "stop": _cmd_stop,
        "status": _cmd_status,
        "inspect": _cmd_inspect,
        "explain": _cmd_explain,
        "why": _cmd_explain,
        "dashboard": _cmd_dashboard,
        "evidence": _cmd_evidence,
        "proof": _cmd_proof,
        "tui": _cmd_tui,
        "list": _cmd_list,
        "portfolio": _cmd_portfolio_status,
        "portfolio-run": _cmd_portfolio_run,
        "daemon": _cmd_daemon_status,
        "daemon-start": _cmd_daemon_start,
        "daemon-resume": _cmd_daemon_resume,
        "daemon-stop": _cmd_daemon_stop,
        "resource-status": _cmd_resource_status,
        "artifacts": _cmd_artifacts,
        "events": _cmd_events,
        "web": _cmd_web,
        "lifeos": _cmd_lifeos_status,
        "lifeos-export": _cmd_lifeos_export,
    }[args.command]
    return int(handler(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
