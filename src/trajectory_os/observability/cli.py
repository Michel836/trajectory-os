"""M030 — operator CLI for canonical live-run observability.

Commands::

    run         run one observable implementation/validation/review/repair loop
    status      render the canonical status (CLI / TUI / Web)
    follow      follow a run until a terminal lifecycle/readiness outcome
    telemetry   print the canonical run telemetry document
    preflight   run the fail-fast preflight check
    project     render a canonical document for a named frontend surface
    version

No command performs a Git trust-boundary write. Observation commands are
strictly read-only.
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
from trajectory_os.benchmark import review as bench_review
from trajectory_os.benchmark import workloads as bench_workloads
from trajectory_os.observability import follow as obs_follow
from trajectory_os.observability import model, preflight, projection, store
from trajectory_os.observability import run as obs_run

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
    env_root = os.environ.get("TRAJECTORY_OBSERVABILITY_ROOT")
    if env_root:
        return env_root
    return str(Path.cwd() / ".trajectory-observability")


def _load_document(root: str, run_id: str) -> dict[str, Any]:
    return store.load_status(store.run_root(root, run_id))


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _cmd_run(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    workload = bench_workloads.by_id(args.workload)
    workspace = args.workspace or str(
        store.run_root(root, args.run_id) / "workspace")
    Path(workspace).mkdir(parents=True, exist_ok=True)
    from trajectory_os.benchmark import executor as bench_executor

    if args.mode == "fixture":
        executor: Any = bench_executor.FixtureExecutor(
            interrupt_once=False)
    else:
        executor = bench_executor.LiveExecutor()
    config = obs_run.LiveRunConfig(
        run_id=args.run_id, root=root, workspace=workspace,
        backend=args.backend, provider=args.provider, model=args.model,
        workload=workload, telemetry_mode=args.telemetry,
        max_repairs=args.max_repairs,
        require_review=not args.no_review,
        final_review_enabled=not args.no_review,
        final_reviewer_model=args.reviewer_model,
        timeout_s=args.timeout, mission_id=args.mission)
    reviewer_factory = _reviewer_factory(args)
    sampler = _resource_sampler(args)
    coordinator = obs_run.RunCoordinator(
        config, executor=executor, reviewer_factory=reviewer_factory,
        resource_sampler=sampler)
    result = coordinator.run()
    if args.json:
        _print_json(result.to_dict())
    else:
        print(projection.render_projection(result.status.to_dict(),
                                           target=projection.PROJECTION_CLI))
    return (EXIT_OK if result.status.ready_for_commit else EXIT_REJECTED)


def _reviewer_factory(args: argparse.Namespace) -> Any:
    if args.no_review:
        return None
    from trajectory_os.benchmark import review as bench_review

    if args.reviewer == "pass":
        return obs_run.scripted_reviewer_factory([FIXTURE_PASS])
    if args.reviewer == "reject-then-pass":
        return obs_run.scripted_reviewer_factory([FIXTURE_REJECT, FIXTURE_PASS])
    if args.reviewer == "inactive":
        def inactive() -> bench_review.ReviewerClient:
            return bench_review.InactiveReviewerClient(
                model_name=args.reviewer_model)

        return inactive

    def ollama() -> bench_review.ReviewerClient:
        return bench_review.OllamaReviewerClient(
            model_name=args.reviewer_model)

    return ollama


def _resource_sampler(args: argparse.Namespace) -> Any:
    if args.telemetry != model.TELEMETRY_BENCHMARK:
        return None
    from trajectory_os.benchmark import metrics as bench_metrics

    return bench_metrics.local_resource_sampler(args.model or "")


def _cmd_status(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    document = _load_document(root, args.run_id)
    if args.json:
        _print_json(document)
    else:
        print(projection.render_projection(document, target=args.target))
    return EXIT_OK


def _cmd_follow(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    notifications: list[str] = []

    def reader() -> dict[str, Any]:
        return _load_document(root, args.run_id)

    def on_frame(frame: str) -> None:
        print(frame)

    outcome = obs_follow.follow(
        reader, interval_s=args.interval, max_polls=args.max_polls,
        on_frame=on_frame)
    print("=" * 68)
    if outcome.exited:
        print(f"RUN TERMINÉ — {outcome.document.get('readiness')} "
              f"({outcome.reason})")
    else:
        print("FOLLOW BOUND REACHED — run still active")
    print("=" * 68)
    notifications.extend(outcome.notifications)
    return EXIT_OK if outcome.exited else EXIT_REJECTED


def _cmd_telemetry(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    document = store.load_telemetry(store.run_root(root, args.run_id))
    _print_json(document)
    return EXIT_OK


def _cmd_preflight(args: argparse.Namespace) -> int:
    request = preflight.PreflightRequest(
        run_id=args.run_id, backend=args.backend, provider=args.provider,
        model=args.model, workspace=args.workspace or os.getcwd(),
        reviewer_model=args.reviewer_model,
        review_enabled=not args.no_review, telemetry_mode=args.telemetry)
    outcome = preflight.preflight(request)
    if args.json:
        _print_json(outcome.to_dict())
    else:
        print(f"PREFLIGHT {'PASS' if outcome.ok else 'REJECT'}: "
              f"{outcome.reason}")
        print(f"  {outcome.detail}")
    return EXIT_OK if outcome.ok else EXIT_REJECTED


def _cmd_project(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    document = _load_document(root, args.run_id)
    print(projection.render_projection(document, target=args.target))
    return EXIT_OK


def _cmd_version() -> int:
    print(f"trajectory-observability {__version__} "
          f"(M030 {model.OBSERVABILITY_VERSION})")
    return EXIT_OK


def build_parser(prog: str = "trajectory-observability",
                 ) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog, description="M030 canonical live-run observability.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="run one observable run")
    p.add_argument("--root", default=None)
    p.add_argument("--run-id", dest="run_id", required=True)
    p.add_argument("--workspace", default=None)
    p.add_argument("--mission", default=None)
    p.add_argument("--workload", default="small-targeted-repair")
    p.add_argument("--mode", choices=("fixture", "live"), default="fixture")
    p.add_argument("--backend", default=agent_model.BACKEND_PI)
    p.add_argument("--provider", default="deepseek")
    p.add_argument("--model", default="deepseek-flash")
    p.add_argument("--telemetry", choices=sorted(model.TELEMETRY_MODES),
                   default=model.TELEMETRY_STANDARD)
    p.add_argument("--max-repairs", dest="max_repairs", type=int, default=2)
    p.add_argument("--reviewer",
                   choices=("pass", "reject-then-pass", "inactive", "ollama"),
                   default="pass")
    p.add_argument("--reviewer-model", dest="reviewer_model",
                   default=model.FINAL_REVIEWER_MODEL)
    p.add_argument("--no-review", action="store_true", default=False)
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("status", help="render canonical status")
    p.add_argument("--root", default=None)
    p.add_argument("--run-id", dest="run_id", required=True)
    p.add_argument("--target", choices=sorted(projection.PROJECTIONS),
                   default=projection.PROJECTION_CLI)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("follow", help="follow a run to a terminal outcome")
    p.add_argument("--root", default=None)
    p.add_argument("--run-id", dest="run_id", required=True)
    p.add_argument("--interval", type=float,
                   default=obs_follow.DEFAULT_INTERVAL_S)
    p.add_argument("--max-polls", dest="max_polls", type=int,
                   default=obs_follow.DEFAULT_MAX_POLLS)

    p = sub.add_parser("telemetry", help="print run telemetry (JSON)")
    p.add_argument("--root", default=None)
    p.add_argument("--run-id", dest="run_id", required=True)

    p = sub.add_parser("preflight", help="run fail-fast preflight")
    p.add_argument("--run-id", dest="run_id", default="preflight")
    p.add_argument("--backend", default=agent_model.BACKEND_PI)
    p.add_argument("--provider", default="deepseek")
    p.add_argument("--model", default="deepseek-flash")
    p.add_argument("--workspace", default=None)
    p.add_argument("--reviewer-model", dest="reviewer_model",
                   default=model.FINAL_REVIEWER_MODEL)
    p.add_argument("--no-review", action="store_true", default=False)
    p.add_argument("--telemetry", choices=sorted(model.TELEMETRY_MODES),
                   default=model.TELEMETRY_STANDARD)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("project", help="render a canonical document")
    p.add_argument("--root", default=None)
    p.add_argument("--run-id", dest="run_id", required=True)
    p.add_argument("--target", choices=sorted(projection.PROJECTIONS),
                   default=projection.PROJECTION_CLI)

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
        "run": _cmd_run, "status": _cmd_status, "follow": _cmd_follow,
        "telemetry": _cmd_telemetry, "preflight": _cmd_preflight,
        "project": _cmd_project,
    }[args.command]
    try:
        return int(handler(args))
    except (model.ObservabilityError, store.CanonicalStoreError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
