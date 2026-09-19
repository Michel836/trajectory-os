"""Mission 015 — operator CLI for the bounded agent-backend abstraction.

Commands:

    probe    deterministic capability probe (pi, deepseek-harness)
    canary   one bounded DeepSeek Harness canary with Pi fallback evidence
    compare  run one bounded task on both backends and compare
    qualify  M016 bounded DeepSeek Harness qualification (secret-free)
    version

No backend may commit, push, merge, reset, restore, clean, stash, rebase,
switch or checkout; this CLI performs no Git write at all.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from trajectory_os import __version__
from trajectory_os.agents import model, registry
from trajectory_os.agents.canary import run_canary

EXIT_OK = 0
EXIT_USAGE = 2


def _print(payload: dict[str, Any], as_json: bool) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _request(args: argparse.Namespace) -> model.AgentRequest:
    task = args.task
    if task is None and args.task_file:
        task = Path(args.task_file).read_text(encoding="utf-8")
    if task is None:
        task = "Reply with a single short confirmation and no repository change."
    return model.AgentRequest(
        task=task, workspace=args.workspace or os.getcwd(),
        mode=args.mode, timeout_s=args.timeout,
        reasoning_effort=args.reasoning_effort, provider=args.provider,
        model=args.model).validate()


def _cmd_probe(args: argparse.Namespace) -> int:
    if args.backend and args.backend not in ("all",):
        probe = registry.make_backend(args.backend).probe()
        payload: dict[str, Any] = probe.to_dict()
    else:
        payload = {"status": "OK", "backends": registry.probe_all()}
    _print(payload, args.json)
    return EXIT_OK


def _cmd_canary(args: argparse.Namespace) -> int:
    request = _request(args)
    outcome = run_canary(
        request, primary=args.primary, fallback=args.fallback,
        require_sdk=not args.allow_runtime)
    payload = outcome.to_dict()
    if args.save:
        Path(args.save).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
    _print(payload, args.json)
    return EXIT_OK


def _cmd_compare(args: argparse.Namespace) -> int:
    request = _request(args)
    results: dict[str, Any] = {}
    for name in (model.BACKEND_DEEPSEEK_HARNESS, model.BACKEND_PI):
        result = registry.make_backend(name).run(request)
        results[name] = registry.comparison_summary(result)
    _print({"status": "OK", "comparison": results}, args.json)
    return EXIT_OK


def _cmd_qualify(args: argparse.Namespace) -> int:
    from trajectory_os.agents import qualification

    outcome = qualification.qualify(
        workspace=args.workspace or os.getcwd(), timeout_s=args.timeout,
        allow_runtime=args.allow_runtime)
    payload = outcome.to_dict()
    if args.save:
        Path(args.save).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
    _print(payload, args.json)
    return EXIT_OK


def _cmd_qualify_isolated(args: argparse.Namespace) -> int:
    from trajectory_os.agents import harness_qualification

    outcome = harness_qualification.qualify_isolated(
        workspace=args.workspace or os.getcwd(), timeout_s=args.timeout,
        allow_runtime=args.allow_runtime)
    payload = outcome.to_dict()
    if args.save:
        Path(args.save).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
    _print(payload, args.json)
    return EXIT_OK


def _cmd_usage(args: argparse.Namespace) -> int:
    from trajectory_os.agents import telemetry

    document = telemetry.reconstruct(args.root)
    if args.json:
        _print(document, args.json)
    else:
        print(f"telemetry : records={document['count']}")
        for record in document["records"][-16:]:
            print(f"  - {record.get('backend')} "
                  f"provider={record.get('provider')} "
                  f"model={record.get('model')} "
                  f"prompt={record.get('prompt_tokens')} "
                  f"completion={record.get('completion_tokens')} "
                  f"runtime_ms={record.get('runtime_ms')}")
    return EXIT_OK


def _cmd_version() -> int:
    print(f"trajectory-agent {__version__}")
    return EXIT_OK


def build_parser(prog: str = "trajectory-agent") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="TrajectoryOS agent-backend CLI (bounded, no Git writes).")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("probe", help="deterministic backend capability probe")
    p.add_argument("--backend", default="all",
                   help="backend name or 'all'")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("canary", help="bounded DeepSeek Harness canary")
    p.add_argument("--task", default=None)
    p.add_argument("--task-file", default=None)
    p.add_argument("--workspace", default=None)
    p.add_argument("--mode", default="IMPLEMENT")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--reasoning-effort", dest="reasoning_effort", default=None)
    p.add_argument("--provider", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--primary", default=model.BACKEND_DEEPSEEK_HARNESS)
    p.add_argument("--fallback", default=model.BACKEND_PI)
    p.add_argument("--allow-runtime", action="store_true",
                   help="allow the runtime transport without the SDK")
    p.add_argument("--save", default=None)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("compare", help="compare both backends on one task")
    p.add_argument("--task", default=None)
    p.add_argument("--task-file", default=None)
    p.add_argument("--workspace", default=None)
    p.add_argument("--mode", default="IMPLEMENT")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--reasoning-effort", dest="reasoning_effort", default=None)
    p.add_argument("--provider", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser(
        "qualify", help="M016 bounded DeepSeek Harness qualification")
    p.add_argument("--workspace", default=None)
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--allow-runtime", action="store_true",
                   help="allow the runtime transport without the SDK")
    p.add_argument("--save", default=None)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser(
        "qualify-isolated",
        help="M023 isolated-SDK DeepSeek Harness qualification")
    p.add_argument("--workspace", default=None)
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--allow-runtime", action="store_true",
                   help="allow the runtime transport without the SDK")
    p.add_argument("--save", default=None)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser(
        "usage", help="provider-grounded token/context telemetry ledger")
    p.add_argument("--root", required=True,
                   help="state root holding telemetry/usage.jsonl")
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
    handler = {
        "probe": _cmd_probe,
        "canary": _cmd_canary,
        "compare": _cmd_compare,
        "qualify": _cmd_qualify,
        "qualify-isolated": _cmd_qualify_isolated,
        "usage": _cmd_usage,
    }[args.command]
    try:
        return int(handler(args))
    except model.AgentBackendError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
