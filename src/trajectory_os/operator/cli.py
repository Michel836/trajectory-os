"""M040–M047 — unified operator CLI (``scripts/trajectory``).

One primary operator-facing surface over mission + release. Observation
commands are read-only; mutation commands are explicit; Git release writes
require the existing human ``GO COMMIT`` / ``GO MERGE`` authorization tokens.

Commands::

    start          resolve policy/routing and start a mission
    status         read-only one-line/lifecycle status
    dashboard      read-only one-screen operator product state
    follow         read-only repeated status (never mutates)
    pause          record a resumable graceful pause request
    request-stop   record a resumable graceful stop request
    cancel         cancel a mission (canonical CANCELLED)
    resume         resume a mission from its safe recovery point
    recover        emit the full-lifecycle recovery decision
    handoff        emit the deterministic GO COMMIT handoff (read-only Git)
    go-commit      authorized stage/commit/push (explicit --authorize-commit)
    bind-pr        create or discover exactly one PR for the exact commit
    pr-status      read-only pull-request status
    watch-ci       read the exact-head CI state (read-only unless --persist)
    merge-handoff  verify merge preconditions and emit the GO MERGE gate
    go-merge       authorized merge (explicit --authorize-merge)
    closure        record the release closure after a confirmed merge
    reconstruct    read-only unified reconstruction (events + release)
    dogfood        run the M040 self-hosting dogfood
    acceptance     run the deterministic M040-M047 acceptance matrix
    version
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from trajectory_os import __version__
from trajectory_os.assembly import orchestrator as assembly_run
from trajectory_os.benchmark import executor as bench_executor
from trajectory_os.benchmark import model as bench_model
from trajectory_os.benchmark import review as bench_review
from trajectory_os.observability import model as obs_model
from trajectory_os.operator import acceptance as operator_acceptance
from trajectory_os.operator import dogfood as operator_dogfood
from trajectory_os.operator import model
from trajectory_os.operator.control_plane import ControlPlane
from trajectory_os.operator.policy import PROFILE_RELEASE, PROFILES
from trajectory_os.platform import cli as platform_cli

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REJECTED = 3


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _default_root(value: str | None) -> str:
    if value:
        return value
    env_root = os.environ.get("TRAJECTORY_MISSION_ROOT")
    if env_root:
        return env_root
    return str(Path.cwd() / ".trajectory-missions")


def _plane(args: argparse.Namespace) -> ControlPlane:
    return ControlPlane(_default_root(args.root),
                        repo=getattr(args, "repo", None))


def _mission_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=None,
                        help="mission root (default: $TRAJECTORY_MISSION_ROOT "
                             "or ./.trajectory-missions)")
    parser.add_argument("--mission-id", dest="mission_id", required=True)


def _live_reviewer_factory() -> Any:
    def factory() -> bench_review.ReviewerClient:
        return bench_review.OllamaReviewerClient(
            model_name=obs_model.FINAL_REVIEWER_MODEL)
    return factory


def _cmd_start(args: argparse.Namespace) -> int:
    plane = _plane(args)
    if args.executor == "live":
        executor: Any = bench_executor.LiveExecutor()
        reviewer = _live_reviewer_factory()
        mode = bench_model.MODE_LIVE
    else:
        executor = bench_executor.FixtureExecutor(interrupt_once=False)
        reviewer = None
        mode = bench_model.MODE_FIXTURE
    outcome = plane.start(
        assembly_run.MissionRequest(
            objective=args.objective, workspace=args.workspace,
            mission_id=args.mission_id, mode=mode,
            workload_id=args.workload),
        profile=args.profile, executor=executor,
        reviewer_factory=reviewer)
    payload = outcome.to_dict()
    if args.json:
        _print_json(payload)
    else:
        status = payload["result"]["status"]
        print(f"mission {payload['mission_id']} "
              f"{status['state']} / {status['readiness']} "
              f"(profile {payload['policy']['profile']})")
    return EXIT_OK


def _cmd_status(args: argparse.Namespace) -> int:
    plane = _plane(args)
    payload = plane.status(args.mission_id)
    if args.json:
        _print_json(payload)
    else:
        print(f"{payload['mission_id']}: {payload['lifecycle']} / "
              f"{payload['readiness']} (gate {payload['pending_human_gate']})")
    return EXIT_OK


def _cmd_dashboard(args: argparse.Namespace) -> int:
    plane = _plane(args)
    payload = plane.dashboard(args.mission_id)
    if args.json:
        _print_json(payload)
    else:
        print(payload["screen"])
    return EXIT_OK


def _cmd_follow(args: argparse.Namespace) -> int:
    plane = _plane(args)
    snapshots = plane.follow(args.mission_id, iterations=args.iterations)
    if args.json:
        _print_json(snapshots)
    else:
        for snapshot in snapshots:
            print(f"{snapshot['lifecycle']} / {snapshot['readiness']} / "
                  f"{snapshot['phase']}")
    return EXIT_OK


def _control_handler(action: str) -> Any:
    def handler(args: argparse.Namespace) -> int:
        plane = _plane(args)
        payload = getattr(plane, action)(args.mission_id,
                                         reason=args.reason)
        _print_json(payload) if args.json else print(
            f"{payload['action']}: {payload['result']} ({payload['reason']})")
        return EXIT_OK

    return handler


def _cmd_resume(args: argparse.Namespace) -> int:
    plane = _plane(args)
    executor: Any = bench_executor.FixtureExecutor(interrupt_once=False)
    reviewer = None
    if args.executor == "live":
        executor = bench_executor.LiveExecutor()
        reviewer = _live_reviewer_factory()
    payload = plane.resume(args.mission_id, executor=executor,
                           reviewer_factory=reviewer)
    status = payload["status"]
    if args.json:
        _print_json(payload)
    else:
        print(f"resumed {payload['mission']['mission_id']}: "
              f"{status['state']} / {status['readiness']}")
    return EXIT_OK


def _cmd_recover(args: argparse.Namespace) -> int:
    plane = _plane(args)
    payload = plane.recover(args.mission_id, record=not args.no_record)
    if args.json:
        _print_json(payload)
    else:
        print(f"recovery {payload['action']} at {payload['stage']}: "
              f"{payload['reason']}")
        print(f"  next: {payload['next_step']}")
    return EXIT_OK


def _cmd_handoff(args: argparse.Namespace) -> int:
    plane = _plane(args)
    payload = plane.handoff(args.mission_id, base_branch=args.base_branch,
                            issue=args.issue)
    _print_json(payload) if args.json else print(
        f"GO COMMIT ready: patch {payload['reviewed_patch_sha256']} "
        f"on {payload['branch']}")
    return EXIT_OK


def _cmd_go_commit(args: argparse.Namespace) -> int:
    plane = _plane(args)
    payload = plane.go_commit(args.mission_id, token=args.authorize_commit,
                              actor=args.authorize_actor,
                              remote=args.remote)
    _print_json(payload) if args.json else print(
        f"GO COMMIT: {payload['commit_sha']} pushed to {payload['remote']}")
    return EXIT_OK


def _cmd_bind_pr(args: argparse.Namespace) -> int:
    plane = _plane(args)
    payload = plane.bind_pr(args.mission_id, base_branch=args.base_branch,
                            title=args.title, body=args.body)
    _print_json(payload) if args.json else print(
        f"PR #{payload['number']} bound to {payload['head_sha']}")
    return EXIT_OK


def _cmd_pr_status(args: argparse.Namespace) -> int:
    _print_json(_plane(args).pr_status(args.mission_id))
    return EXIT_OK


def _cmd_watch_ci(args: argparse.Namespace) -> int:
    plane = _plane(args)
    payload = plane.watch_ci(args.mission_id, persist=args.persist)
    if args.json:
        _print_json(payload)
    else:
        print(f"exact-head CI: {payload['state']} (head {payload['head_sha']})")
    return EXIT_OK if payload.get("green") else EXIT_REJECTED


def _cmd_merge_handoff(args: argparse.Namespace) -> int:
    plane = _plane(args)
    payload = plane.merge_handoff(args.mission_id,
                                  base_branch=args.base_branch,
                                  merge_method=args.merge_method)
    _print_json(payload) if args.json else print(
        f"GO MERGE ready: PR #{payload['pr_number']} "
        f"CI {payload['ci_state']}")
    return EXIT_OK


def _cmd_go_merge(args: argparse.Namespace) -> int:
    plane = _plane(args)
    payload = plane.go_merge(args.mission_id, token=args.authorize_merge,
                             actor=args.authorize_actor,
                             merge_method=args.merge_method)
    _print_json(payload) if args.json else print(
        f"GO MERGE: {payload['merge_sha']} -> {payload['target_branch']}")
    return EXIT_OK


def _cmd_closure(args: argparse.Namespace) -> int:
    plane = _plane(args)
    payload = plane.closure(args.mission_id, base_branch=args.base_branch,
                            issue=args.issue)
    _print_json(payload) if args.json else print(
        f"RELEASE {payload['status']}: merge {payload['merge_sha']}")
    return EXIT_OK


def _cmd_reconstruct(args: argparse.Namespace) -> int:
    _print_json(_plane(args).reconstruct(args.mission_id))
    return EXIT_OK


def _cmd_dogfood(args: argparse.Namespace) -> int:
    payload = operator_dogfood.run_self_hosting_dogfood(
        _default_root(args.root), repo=args.repo, issue=args.issue)
    if args.json:
        _print_json(payload)
    else:
        print(f"self-hosting dogfood: {payload['status']}")
        print(f"  fixture proof : {payload['fixture_proof'].get('ok')}")
        print(f"  live dogfood  : "
              f"{payload['live_dogfood'].get('ready_for_commit')} "
              f"(stopped at {payload['live_dogfood'].get('stopped_at')})")
    return EXIT_OK if payload["status"] == "PASS" else EXIT_REJECTED


def _cmd_acceptance(args: argparse.Namespace) -> int:
    report = operator_acceptance.run_acceptance(_default_root(args.root))
    if args.out:
        Path(args.out).write_text(
            json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
    if args.json:
        _print_json(report.to_dict())
    else:
        print(report.render())
    return EXIT_OK if report.status == "PASS" else EXIT_REJECTED


def _cmd_version() -> int:
    print(f"trajectory {__version__} "
          f"(operator {model.OPERATOR_VERSION})")
    return EXIT_OK


def build_parser(prog: str = "trajectory",
                 ) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="M040-M047 unified self-hosting operator platform.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start", help="resolve policy/routing and start")
    p.add_argument("--root", default=None)
    p.add_argument("--mission-id", dest="mission_id", default=None)
    p.add_argument("--objective", required=True)
    p.add_argument("--workspace", required=True)
    p.add_argument("--profile", choices=sorted(PROFILES),
                   default=PROFILE_RELEASE)
    p.add_argument("--executor", choices=("fixture", "live"),
                   default="fixture")
    p.add_argument("--workload", default="small-targeted-repair")
    p.add_argument("--json", action="store_true")

    for name in ("status", "dashboard"):
        p = sub.add_parser(name, help=f"read-only {name}")
        _mission_args(p)
        p.add_argument("--repo", default=None)
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("follow", help="read-only repeated status")
    _mission_args(p)
    p.add_argument("--repo", default=None)
    p.add_argument("--iterations", type=int, default=1)
    p.add_argument("--json", action="store_true")

    for name in ("pause", "request-stop", "cancel"):
        p = sub.add_parser(name, help=f"explicit {name}")
        _mission_args(p)
        p.add_argument("--reason", default=f"operator {name}")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("resume", help="resume a mission safely")
    _mission_args(p)
    p.add_argument("--executor", choices=("fixture", "live"),
                   default="fixture")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("recover", help="emit the recovery decision")
    _mission_args(p)
    p.add_argument("--no-record", dest="no_record", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("handoff", help="emit the GO COMMIT handoff")
    _mission_args(p)
    p.add_argument("--repo", default=None)
    p.add_argument("--base-branch", dest="base_branch", default="main")
    p.add_argument("--issue", default=None)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("go-commit", help="authorized commit + push")
    _mission_args(p)
    p.add_argument("--repo", default=None)
    p.add_argument("--remote", default="origin")
    p.add_argument("--authorize-commit", dest="authorize_commit",
                   default=None)
    p.add_argument("--authorize-actor", dest="authorize_actor",
                   default="operator")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("bind-pr", help="bind exactly one PR")
    _mission_args(p)
    p.add_argument("--repo", default=None)
    p.add_argument("--base-branch", dest="base_branch", default=None)
    p.add_argument("--title", default=None)
    p.add_argument("--body", default=None)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("pr-status", help="read-only PR status")
    _mission_args(p)
    p.add_argument("--repo", default=None)

    p = sub.add_parser("watch-ci", help="read exact-head CI")
    _mission_args(p)
    p.add_argument("--repo", default=None)
    p.add_argument("--persist", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("merge-handoff", help="emit the GO MERGE gate")
    _mission_args(p)
    p.add_argument("--repo", default=None)
    p.add_argument("--base-branch", dest="base_branch", default=None)
    p.add_argument("--merge-method", dest="merge_method", default="squash")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("go-merge", help="authorized merge")
    _mission_args(p)
    p.add_argument("--repo", default=None)
    p.add_argument("--authorize-merge", dest="authorize_merge", default=None)
    p.add_argument("--authorize-actor", dest="authorize_actor",
                   default="operator")
    p.add_argument("--merge-method", dest="merge_method", default=None)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("closure", help="record the release closure")
    _mission_args(p)
    p.add_argument("--repo", default=None)
    p.add_argument("--base-branch", dest="base_branch", default=None)
    p.add_argument("--issue", default=None)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("reconstruct", help="read-only reconstruction")
    _mission_args(p)
    p.add_argument("--repo", default=None)

    p = sub.add_parser("dogfood", help="run the M040 self-hosting dogfood")
    p.add_argument("--root", default=None)
    p.add_argument("--repo", default=None)
    p.add_argument("--issue", default=None)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("acceptance", help="run the M040-M047 acceptance matrix")
    p.add_argument("--root", default=None)
    p.add_argument("--out", default=None)
    p.add_argument("--json", action="store_true")

    platform_cli.register(sub)

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
    if platform_cli.is_platform_command(args.command):
        return platform_cli.dispatch(args)
    handlers: dict[str, Any] = {
        "start": _cmd_start, "status": _cmd_status,
        "dashboard": _cmd_dashboard, "follow": _cmd_follow,
        "pause": _control_handler("pause"),
        "request-stop": _control_handler("request_stop"),
        "cancel": _control_handler("cancel"), "resume": _cmd_resume,
        "recover": _cmd_recover, "handoff": _cmd_handoff,
        "go-commit": _cmd_go_commit, "bind-pr": _cmd_bind_pr,
        "pr-status": _cmd_pr_status, "watch-ci": _cmd_watch_ci,
        "merge-handoff": _cmd_merge_handoff, "go-merge": _cmd_go_merge,
        "closure": _cmd_closure, "reconstruct": _cmd_reconstruct,
        "dogfood": _cmd_dogfood, "acceptance": _cmd_acceptance,
    }
    handler = handlers[args.command]
    try:
        return int(handler(args))
    except (model.OperatorError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REJECTED
    except SystemExit as exc:  # pragma: no cover - argparse usage
        code = exc.code if isinstance(exc.code, int) else EXIT_USAGE
        return EXIT_OK if code == 0 else EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
