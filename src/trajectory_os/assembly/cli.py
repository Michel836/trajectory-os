"""M031 — one mission-level operator entry point (start/status/follow/resume).

Commands::

    start       create a mission from one objective and drive it to a gate
    status      render the canonical mission status (CLI / TUI / Web)
    follow      follow a mission until a terminal lifecycle/readiness outcome
    resume      resume an interrupted mission (same mission_id, same evidence)
    reconstruct reconstruct the mission from durable artifacts
    plan        print the durable mission plan (JSON)
    closure     print the durable closure evidence (JSON)
    version

Observation commands are strictly read-only. The human gate is authoritative:
no command performs a Git trust-boundary write (commit/push/merge/reset/
restore/clean/stash/rebase/checkout/switch).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from trajectory_os import __version__
from trajectory_os.agents import model as agent_model
from trajectory_os.assembly import model, store
from trajectory_os.assembly import orchestrator as assembly_run
from trajectory_os.benchmark import model as bench_model
from trajectory_os.benchmark import review as bench_review
from trajectory_os.benchmark.executor import FixtureExecutor, LiveExecutor
from trajectory_os.observability import follow as obs_follow
from trajectory_os.observability import model as obs_model
from trajectory_os.observability import projection
from trajectory_os.observability import run as obs_run
from trajectory_os.observability import store as obs_store

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REJECTED = 3

FIXTURE_PASS = (
    "VERDICT: PASS\nBLOCKERS:\n- none\nMAJORS:\n- none\nMINORS:\n- none\n"
    "FINAL RECOMMENDATION: GO COMMIT\n")
FIXTURE_REJECT = (
    "VERDICT: REJECT\nBLOCKERS:\n- missing edge-case handling\n"
    "MAJORS:\n- none\nMINORS:\n- none\nFINAL RECOMMENDATION: REPAIR\n")


def _root_from(value: str | None) -> str:
    if value:
        return value
    env_root = os.environ.get("TRAJECTORY_MISSION_ROOT")
    if env_root:
        return env_root
    return str(Path.cwd() / ".trajectory-missions")


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _executor(mode: str) -> Any:
    if mode == bench_model.MODE_FIXTURE:
        return FixtureExecutor(interrupt_once=False)
    return LiveExecutor()


def _reviewer_factory(reviewer: str, reviewer_model: str) -> Any:
    if reviewer == "inactive":
        def inactive() -> bench_review.ReviewerClient:
            return bench_review.InactiveReviewerClient(
                model_name=reviewer_model)

        return inactive
    if reviewer == "reject-then-pass":
        return obs_run.scripted_reviewer_factory(
            [FIXTURE_REJECT, FIXTURE_PASS])
    if reviewer == "ollama":
        def ollama() -> bench_review.ReviewerClient:
            return bench_review.OllamaReviewerClient(
                model_name=reviewer_model)

        return ollama

    def passing() -> bench_review.ReviewerClient:
        return bench_review.FixtureReviewerClient(
            FIXTURE_PASS, model_name=reviewer_model)

    return passing


def _trust_policy(args: argparse.Namespace) -> model.TrustPolicy:
    return model.TrustPolicy(
        require_review=not args.no_review,
        final_reviewer_model=args.reviewer_model,
        inline_review_enabled=False,
        max_repairs=args.max_repairs,
        stop_at=obs_model.RD_READY_FOR_COMMIT,
        allow_git_trust_writes=False,
    ).validate()


def _cmd_start(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    mission_id = args.mission_id or model.generate_mission_id(
        args.objective, model.utc_now())
    workspace = args.workspace or str(
        store.mission_root(root, mission_id) / "workspace")
    Path(workspace).mkdir(parents=True, exist_ok=True)
    request = assembly_run.MissionRequest(
        objective=args.objective,
        workspace=workspace,
        constraints=tuple(args.constraint),
        definition_of_done=(tuple(args.dod)
                            or assembly_run.DEFAULT_DEFINITION_OF_DONE),
        backend=args.backend,
        provider=args.provider,
        model=args.model,
        workload_id=args.workload,
        trust_policy=_trust_policy(args),
        mission_id=mission_id,
        mode=args.mode,
        telemetry_mode=args.telemetry,
        timeout_s=args.timeout,
    )
    orchestrator = assembly_run.MissionOrchestrator(
        root, executor=_executor(args.mode),
        reviewer_factory=(None if args.no_review
                          else _reviewer_factory(args.reviewer,
                                                 args.reviewer_model)))
    result = orchestrator.start(request)
    if args.json:
        _print_json(result.to_dict())
    else:
        print(projection.render_projection(dict(result.status),
                                           target=projection.PROJECTION_CLI))
    return (EXIT_OK
            if result.status.get("readiness") == obs_model.RD_READY_FOR_COMMIT
            else EXIT_REJECTED)


def _cmd_resume(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    orchestrator = assembly_run.MissionOrchestrator(
        root, executor=_executor(args.mode),
        reviewer_factory=(None if args.no_review
                          else _reviewer_factory(args.reviewer,
                                                 args.reviewer_model)))
    result = orchestrator.resume(args.mission_id)
    if args.json:
        _print_json(result.to_dict())
    else:
        print(projection.render_projection(dict(result.status),
                                           target=projection.PROJECTION_CLI))
    return (EXIT_OK
            if result.status.get("readiness") == obs_model.RD_READY_FOR_COMMIT
            else EXIT_REJECTED)


def _cmd_status(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    document = obs_store.load_status(store.mission_root(root, args.mission_id))
    if args.json:
        _print_json(document)
    else:
        print(projection.render_projection(document, target=args.target))
    return EXIT_OK


def _cmd_follow(args: argparse.Namespace) -> int:
    root = _root_from(args.root)

    def reader() -> dict[str, Any]:
        return obs_store.load_status(
            store.mission_root(root, args.mission_id))

    outcome = obs_follow.follow(
        reader, interval_s=args.interval, max_polls=args.max_polls,
        on_frame=lambda frame: print(frame))
    print("=" * 68)
    if outcome.exited:
        print(f"MISSION TERMINÉE — {outcome.document.get('readiness')} "
              f"({outcome.reason})")
    else:
        print("FOLLOW BOUND REACHED — mission still active")
    print("=" * 68)
    return EXIT_OK if outcome.exited else EXIT_REJECTED


def _cmd_reconstruct(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    document = assembly_closure_reconstruct(root, args.mission_id)
    _print_json(document)
    return EXIT_OK


def assembly_closure_reconstruct(root: str, mission_id: str,
                                 ) -> dict[str, Any]:
    from trajectory_os.assembly import closure as assembly_closure

    return assembly_closure.reconstruct_mission(root, mission_id)


def _cmd_plan(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    _print_json(store.load_plan(root, args.mission_id).to_dict())
    return EXIT_OK


def _cmd_closure(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    _print_json(store.load_closure(root, args.mission_id).to_dict())
    return EXIT_OK


def _cmd_version() -> int:
    print(f"trajectory-mission {__version__} "
          f"(M031 {model.ASSEMBLY_VERSION})")
    return EXIT_OK


def _add_policy_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--reviewer",
                        choices=("pass", "reject-then-pass", "inactive",
                                 "ollama"),
                        default="pass")
    parser.add_argument("--reviewer-model", dest="reviewer_model",
                        default=obs_model.FINAL_REVIEWER_MODEL)
    parser.add_argument("--max-repairs", dest="max_repairs", type=int,
                        default=2)
    parser.add_argument("--no-review", action="store_true", default=False)


def build_parser(prog: str = "trajectory-mission",
                 ) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog, description="M031 end-to-end mission assembly.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start", help="start a mission from one objective")
    p.add_argument("--root", default=None)
    p.add_argument("--mission-id", dest="mission_id", default=None)
    p.add_argument("--objective", required=True)
    p.add_argument("--workspace", default=None)
    p.add_argument("--constraint", action="append", default=[])
    p.add_argument("--dod", action="append", default=[])
    p.add_argument("--workload", default="small-targeted-repair")
    p.add_argument("--mode", choices=sorted(bench_model.EXECUTION_MODES),
                   default=bench_model.MODE_FIXTURE)
    p.add_argument("--backend", default=agent_model.BACKEND_PI)
    p.add_argument("--provider", default="deepseek")
    p.add_argument("--model", default="deepseek-flash")
    p.add_argument("--telemetry", choices=sorted(obs_model.TELEMETRY_MODES),
                   default=obs_model.TELEMETRY_STANDARD)
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--json", action="store_true")
    _add_policy_flags(p)

    p = sub.add_parser("resume", help="resume an interrupted mission")
    p.add_argument("--root", default=None)
    p.add_argument("--mission-id", dest="mission_id", required=True)
    p.add_argument("--mode", choices=sorted(bench_model.EXECUTION_MODES),
                   default=bench_model.MODE_FIXTURE)
    p.add_argument("--json", action="store_true")
    _add_policy_flags(p)

    p = sub.add_parser("status", help="render canonical mission status")
    p.add_argument("--root", default=None)
    p.add_argument("--mission-id", dest="mission_id", required=True)
    p.add_argument("--target", choices=sorted(projection.PROJECTIONS),
                   default=projection.PROJECTION_CLI)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("follow", help="follow a mission to a terminal outcome")
    p.add_argument("--root", default=None)
    p.add_argument("--mission-id", dest="mission_id", required=True)
    p.add_argument("--interval", type=float,
                   default=obs_follow.DEFAULT_INTERVAL_S)
    p.add_argument("--max-polls", dest="max_polls", type=int,
                   default=obs_follow.DEFAULT_MAX_POLLS)

    p = sub.add_parser("reconstruct",
                       help="reconstruct a mission from durable artifacts")
    p.add_argument("--root", default=None)
    p.add_argument("--mission-id", dest="mission_id", required=True)

    p = sub.add_parser("plan", help="print the durable mission plan")
    p.add_argument("--root", default=None)
    p.add_argument("--mission-id", dest="mission_id", required=True)

    p = sub.add_parser("closure", help="print the durable closure evidence")
    p.add_argument("--root", default=None)
    p.add_argument("--mission-id", dest="mission_id", required=True)

    sub.add_parser("version", help="CLI version")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_USAGE
        return EXIT_OK if code == 0 else EXIT_USAGE
    if args.command == "version":
        return _cmd_version()
    handler = {
        "start": _cmd_start, "resume": _cmd_resume, "status": _cmd_status,
        "follow": _cmd_follow, "reconstruct": _cmd_reconstruct,
        "plan": _cmd_plan, "closure": _cmd_closure,
    }[args.command]
    try:
        return int(handler(args))
    except (model.AssemblyError, obs_model.ObservabilityError,
            obs_store.CanonicalStoreError,
            bench_model.BenchmarkError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
