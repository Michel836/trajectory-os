"""M029 — operator CLI for the Pi vs DeepSeek Harness benchmark.

Commands::

    run         run (or resume) the benchmark and persist all artifacts
    status      canonical machine/human status of one run
    follow      follow a run until a terminal state
    report      print the human-readable decision report
    reconstruct strictly reconstruct one run from durable artifacts
    version

No command performs a Git trust-boundary write.
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
from trajectory_os.benchmark import engine, model, store
from trajectory_os.benchmark import metrics as bench_metrics
from trajectory_os.benchmark import status as bench_status
from trajectory_os.benchmark import workloads as bench_workloads

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REJECTED = 3


def _preflight_or_block(root: str, benchmark_run_id: str,
                        args: argparse.Namespace) -> int | None:
    """Fail-fast preflight before any expensive phase (M030).

    A knowable configuration error (including an invalid provider/model
    combination) stops the run before validation/review/repair and preserves
    explicit canonical evidence.
    """
    from trajectory_os.observability import adapter, preflight, store

    backend = _backends(args.backend)[0]
    outcome = preflight.preflight(preflight.PreflightRequest(
        run_id=benchmark_run_id, backend=backend, provider=args.provider,
        model=args.model, workspace=args.repo or os.getcwd(),
        reviewer_model=args.reviewer_model,
        review_enabled=not args.allow_inactive_reviewer,
        telemetry_mode=args.telemetry))
    if outcome.ok:
        return None
    canonical = adapter.blocked_status(
        run_id=benchmark_run_id, backend=backend, provider=args.provider,
        model_name=args.model, reason=outcome.reason, detail=outcome.detail,
        telemetry_mode=args.telemetry)
    run_root = store.ensure_run_root(root, benchmark_run_id)
    store.write_json(run_root / store.STATUS_NAME, canonical)
    if args.json:
        _print_json(canonical)
    else:
        print(f"PREFLIGHT REJECTED: {outcome.reason} ({outcome.detail})")
    return EXIT_REJECTED


def _root_from(argv_root: str | None) -> str:
    if argv_root:
        return argv_root
    env_root = os.environ.get("TRAJECTORY_BENCHMARK_ROOT")
    if env_root:
        return env_root
    return str(Path.cwd() / ".trajectory-benchmark")


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _resource_sampler(model_name: str) -> Any:
    return bench_metrics.local_resource_sampler(model_name)


def _backends(value: str) -> tuple[str, ...]:
    if value == "both":
        return (agent_model.BACKEND_PI,
                agent_model.BACKEND_DEEPSEEK_HARNESS)
    if value == agent_model.BACKEND_PI:
        return (agent_model.BACKEND_PI,)
    if value == agent_model.BACKEND_DEEPSEEK_HARNESS:
        return (agent_model.BACKEND_DEEPSEEK_HARNESS,)
    raise model.BenchmarkError("UNKNOWN_BACKEND", value)


def _cmd_run(args: argparse.Namespace) -> int:
    from trajectory_os.benchmark import executor as bench_executor

    root = _root_from(args.root)
    mode = args.mode
    created = engine.utc_now()
    benchmark_run_id = args.run_id or engine.make_run_id(created, mode)
    blocked = _preflight_or_block(root, benchmark_run_id, args)
    if blocked is not None:
        return blocked
    selected = bench_workloads.select(tuple(args.workload or ()))
    config = engine.RunConfig(
        root=root, benchmark_run_id=benchmark_run_id, mode=mode,
        workloads=selected, backends=_backends(args.backend),
        repetitions=args.repetitions, target_provider=args.provider,
        target_model=args.model, final_reviewer_model=args.reviewer_model,
        timeout_s=args.timeout, thinking=args.thinking,
        baseline_revision=engine.detect_repository_revision(args.repo),
        baseline_patch=None, environment=engine.environment_snapshot(),
        require_review=not args.allow_inactive_reviewer)
    if mode == model.MODE_FIXTURE:
        executor: Any = bench_executor.FixtureExecutor()
    else:
        executor = bench_executor.LiveExecutor()
    if args.reviewer == "fixture":
        reviewer_factory: Any = engine.fixture_reviewer_factory(
            engine.default_passing_review())
    elif args.reviewer == "inactive":
        reviewer_factory = engine.default_reviewer_factory(
            live=False, model_name=args.reviewer_model)
    else:
        reviewer_factory = engine.default_reviewer_factory(
            live=True, model_name=args.reviewer_model)
    runner = engine.BenchmarkEngine(
        config, executor=executor, reviewer_factory=reviewer_factory,
        resource_sampler=_resource_sampler(args.model))
    result = runner.run(resume=args.resume)
    from trajectory_os.observability import adapter as obs_adapter

    obs_adapter.write_run_artifacts(
        root, benchmark_run_id, telemetry_mode=args.telemetry)
    if args.json:
        _print_json(result.to_dict())
    else:
        print("=" * 64)
        print(f"RUN TERMINÉ — M029 benchmark {result.state}")
        print(f"  run_id : {result.benchmark_run_id}")
        print(f"  root   : {result.root}")
        print(f"  ran    : {result.ran} skipped: {result.skipped}")
        print(f"  trials : {len(result.trials)}")
        print(bench_status.render_status(bench_status.status_document(
            root, benchmark_run_id)))
    return (EXIT_OK if result.state in
            (model.RUN_READY_FOR_COMMIT, model.RUN_COMPLETE)
            else EXIT_REJECTED)


def _cmd_status(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    document = bench_status.status_document(root, args.run_id)
    if args.json:
        _print_json(document)
    else:
        print(bench_status.render_status(document))
    return (EXIT_OK if document["terminal"] else EXIT_REJECTED)


def _cmd_follow(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    result = bench_status.follow(
        root, args.run_id, interval_s=args.interval,
        max_polls=args.max_polls)
    print(bench_status.render_status(result.document))
    print("=" * 64)
    if result.exited:
        print("RUN TERMINÉ — M029 benchmark complete")
        print(f"  final state: {result.document.get('state')}")
    else:
        print("FOLLOW BOUND REACHED — benchmark still running")
    print("=" * 64)
    return EXIT_OK if result.exited else EXIT_REJECTED


def _cmd_report(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    run_root = store.run_root(root, args.run_id)
    path = run_root / store.REPORT_NAME
    if not path.is_file():
        print(f"error: no report for run {args.run_id}", file=sys.stderr)
        return EXIT_USAGE
    if args.json:
        _print_json(store.reconstruct(run_root))
    else:
        print(path.read_text(encoding="utf-8"))
    return EXIT_OK


def _cmd_reconstruct(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    document = store.reconstruct(store.run_root(root, args.run_id))
    _print_json(document)
    return EXIT_OK


def _cmd_qualify(args: argparse.Namespace) -> int:
    from trajectory_os.agents import harness_qualification as hq

    outcome = hq.qualify_isolated(
        workspace=args.workspace or os.getcwd(), timeout_s=args.timeout,
        allow_runtime=args.allow_runtime)
    document = outcome.to_dict()
    if args.save:
        Path(args.save).write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
    if args.json:
        _print_json(document)
    else:
        print(f"harness qualification: {document['status']} "
              f"({document['reason']})")
        print(f"  environment: {document['environment']['status']} "
              f"{document['environment']['root']}")
        print(f"  isolation  : pythonpath_dropped="
              f"{document['isolation']['pythonpath_dropped']}")
    return EXIT_OK


def _cmd_version() -> int:
    print(f"trajectory-pi-benchmark {__version__} (M029 {model.BENCHMARK_VERSION})")
    return EXIT_OK


def build_parser(prog: str = "trajectory-pi-benchmark",
                 ) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog, description="M029 Pi vs DeepSeek Harness benchmark.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="run or resume the benchmark")
    p.add_argument("--root", default=None)
    p.add_argument("--run-id", dest="run_id", default=None)
    p.add_argument("--mode", type=str.upper,
                   choices=sorted(model.EXECUTION_MODES),
                   default=model.MODE_LIVE)
    p.add_argument("--backend",
                   choices=("pi", "deepseek-harness", "both"), default="both")
    p.add_argument("--workload", action="append", default=None)
    p.add_argument("--repetitions", type=int, default=1)
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--thinking", default="medium")
    p.add_argument("--provider", default="deepseek")
    p.add_argument("--model", default="deepseek-flash")
    p.add_argument("--reviewer-model", dest="reviewer_model",
                   default=model.FINAL_REVIEWER_MODEL)
    p.add_argument("--reviewer", choices=("ollama", "inactive", "fixture"),
                   default=None)
    p.add_argument("--allow-inactive-reviewer", action="store_true",
                   default=False)
    p.add_argument("--repo", default=None,
                   help="repository to capture the read-only baseline from")
    p.add_argument("--resume", action="store_true", default=False)
    p.add_argument("--telemetry",
                   choices=("off", "standard", "benchmark"),
                   default="standard")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("status", help="canonical run status")
    p.add_argument("--root", default=None)
    p.add_argument("--run-id", dest="run_id", required=True)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("follow", help="follow a run to a terminal state")
    p.add_argument("--root", default=None)
    p.add_argument("--run-id", dest="run_id", required=True)
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--max-polls", dest="max_polls", type=int, default=600)

    p = sub.add_parser("report", help="print the decision report")
    p.add_argument("--root", default=None)
    p.add_argument("--run-id", dest="run_id", required=True)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("reconstruct",
                       help="strictly reconstruct one run (JSON)")
    p.add_argument("--root", default=None)
    p.add_argument("--run-id", dest="run_id", required=True)

    p = sub.add_parser(
        "qualify", help="M023 isolated DeepSeek Harness qualification")
    p.add_argument("--workspace", default=None)
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--allow-runtime", action="store_true", default=False)
    p.add_argument("--save", default=None)
    p.add_argument("--json", action="store_true")

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
    if args.command == "run" and args.reviewer is None:
        args.reviewer = ("fixture" if args.mode == model.MODE_FIXTURE
                         else "ollama")
    handler = {
        "run": _cmd_run, "status": _cmd_status, "follow": _cmd_follow,
        "report": _cmd_report, "reconstruct": _cmd_reconstruct,
        "qualify": _cmd_qualify,
    }[args.command]
    try:
        return int(handler(args))
    except (model.BenchmarkError, store.StoreError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
