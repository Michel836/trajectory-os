"""M036–M039 — operator-facing human-gated release CLI.

Commands::

    handoff       emit the deterministic GO COMMIT handoff (read-only Git)
    go-commit     recheck + authorized stage/commit/push (explicit token)
    bind-pr       create or discover exactly one PR for the exact commit
    watch-ci      read the exact-head CI state (queued/in_progress/...)
    merge-handoff verify every merge precondition and emit go-merge.json
    go-merge      recheck + authorized merge (explicit token, default squash)
    closure       record the release closure after a confirmed merge
    reconstruct   read-only reconstruction of the release chain
    status        read-only release progression view
    acceptance    run the deterministic M036-M039 acceptance matrix
    version

Only ``go-commit`` and ``go-merge`` perform a Git/GitHub write, and only with
an explicit ``--authorize-commit`` / ``--authorize-merge`` token supplied by
the operator. Every other command is read-only with respect to Git.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from trajectory_os import __version__
from trajectory_os.assembly import store as assembly_store
from trajectory_os.release import acceptance as release_acceptance
from trajectory_os.release import closure as release_closure
from trajectory_os.release import handoff as release_handoff
from trajectory_os.release import merge_gate, model, pull_request, store
from trajectory_os.release.authorization import authorize
from trajectory_os.release.git_adapter import LocalGitAdapter
from trajectory_os.release.github_adapter import GhCliGitHubAdapter

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


def _git_adapter(args: argparse.Namespace) -> LocalGitAdapter:
    return LocalGitAdapter(repo=args.repo or os.getcwd())


def _github_adapter(args: argparse.Namespace) -> GhCliGitHubAdapter:
    return GhCliGitHubAdapter(repo=args.repo or os.getcwd())


def _cmd_handoff(args: argparse.Namespace) -> int:
    handoff = release_handoff.build_commit_handoff(
        _default_root(args.root), args.mission_id, git=_git_adapter(args),
        base_branch=args.base_branch, issue=args.issue)
    if args.json:
        _print_json(handoff.to_dict())
    else:
        print(handoff.human_summary)
    return EXIT_OK


def _cmd_go_commit(args: argparse.Namespace) -> int:
    authorization = authorize(model.GATE_GO_COMMIT, args.authorize_commit,
                              actor=args.authorize_actor)
    result = release_handoff.go_commit(
        _default_root(args.root), args.mission_id, git=_git_adapter(args),
        authorization=authorization, remote=args.remote)
    if args.json:
        _print_json(result.to_dict())
    else:
        print(f"GO COMMIT: {result.commit_sha} pushed to {result.remote} "
              f"({result.branch})")
    return EXIT_OK


def _cmd_bind_pr(args: argparse.Namespace) -> int:
    binding = pull_request.bind_pull_request(
        _default_root(args.root), args.mission_id, git=_git_adapter(args),
        github=_github_adapter(args), base_branch=args.base_branch,
        title=args.title, body=args.body)
    if args.json:
        _print_json(binding.to_dict())
    else:
        print(f"PR #{binding.number} bound to {binding.head_sha} "
              f"({binding.base_branch}) {binding.url}")
    return EXIT_OK


def _cmd_watch_ci(args: argparse.Namespace) -> int:
    status = pull_request.watch_ci(
        _default_root(args.root), args.mission_id,
        github=_github_adapter(args), persist=not args.no_persist)
    if args.json:
        _print_json(status.to_dict())
    else:
        print(f"exact-head CI: {status.state} (head {status.head_sha})")
        if status.failed_jobs:
            print(f"  failed jobs: {', '.join(status.failed_jobs)}")
        for ref in status.log_refs:
            print(f"  log: {ref}")
    return EXIT_OK if status.green else EXIT_REJECTED


def _cmd_merge_handoff(args: argparse.Namespace) -> int:
    handoff = merge_gate.build_merge_handoff(
        _default_root(args.root), args.mission_id,
        github=_github_adapter(args), base_branch=args.base_branch,
        merge_method=args.merge_method)
    if args.json:
        _print_json(handoff.to_dict())
    else:
        print(handoff.human_summary)
    return EXIT_OK


def _cmd_go_merge(args: argparse.Namespace) -> int:
    authorization = authorize(model.GATE_GO_MERGE, args.authorize_merge,
                              actor=args.authorize_actor)
    result = merge_gate.go_merge(
        _default_root(args.root), args.mission_id,
        github=_github_adapter(args), authorization=authorization,
        merge_method=args.merge_method)
    if args.json:
        _print_json(result.to_dict())
    else:
        print(f"GO MERGE: {result.merge_sha} -> {result.target_branch} "
              f"(verified={result.verified})")
    return EXIT_OK


def _cmd_closure(args: argparse.Namespace) -> int:
    closure = release_closure.build_release_closure(
        _default_root(args.root), args.mission_id,
        github=_github_adapter(args), base_branch=args.base_branch,
        issue=args.issue)
    if args.json:
        _print_json(closure.to_dict())
    else:
        print(closure.human_summary)
    return EXIT_OK


def _cmd_reconstruct(args: argparse.Namespace) -> int:
    document = release_closure.reconstruct_release(
        _default_root(args.root), args.mission_id)
    _print_json(document)
    return EXIT_OK


def _cmd_status(args: argparse.Namespace) -> int:
    root = _default_root(args.root)
    mission_root = assembly_store.mission_root(root, args.mission_id)
    if not store.exists(mission_root, store.RELEASE_STATE_NAME):
        _print_json({"mission_id": args.mission_id, "stage": None,
                     "reason": "NO_RELEASE_STATE"})
        return EXIT_OK
    state = store.load_state(mission_root)
    _print_json(state.to_dict())
    return EXIT_OK


def _cmd_acceptance(args: argparse.Namespace) -> int:
    report = release_acceptance.run_acceptance(_default_root(args.root))
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
    print(f"trajectory-release {__version__} "
          f"(M036-M039 {model.RELEASE_VERSION})")
    return EXIT_OK


def _add_mission_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=None,
                        help="mission root (default: $TRAJECTORY_MISSION_ROOT "
                             "or ./.trajectory-missions)")
    parser.add_argument("--mission-id", dest="mission_id", required=True)


def build_parser(prog: str = "trajectory-release",
                 ) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog, description="M036-M039 human-gated release bundle.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("handoff", help="emit the GO COMMIT handoff")
    _add_mission_args(p)
    p.add_argument("--repo", default=None)
    p.add_argument("--base-branch", dest="base_branch", default="main")
    p.add_argument("--issue", default=None)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("go-commit", help="authorized commit + push")
    _add_mission_args(p)
    p.add_argument("--repo", default=None)
    p.add_argument("--remote", default="origin")
    p.add_argument("--authorize-commit", dest="authorize_commit",
                   default=None)
    p.add_argument("--authorize-actor", dest="authorize_actor",
                   default="operator")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("bind-pr", help="bind exactly one PR to the commit")
    _add_mission_args(p)
    p.add_argument("--repo", default=None)
    p.add_argument("--base-branch", dest="base_branch", default=None)
    p.add_argument("--title", default=None)
    p.add_argument("--body", default=None)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("watch-ci", help="read exact-head CI (read-only)")
    _add_mission_args(p)
    p.add_argument("--repo", default=None)
    p.add_argument("--no-persist", dest="no_persist", action="store_true",
                   default=False)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("merge-handoff",
                       help="verify merge preconditions and emit the gate")
    _add_mission_args(p)
    p.add_argument("--repo", default=None)
    p.add_argument("--base-branch", dest="base_branch", default=None)
    p.add_argument("--merge-method", dest="merge_method",
                   choices=sorted(model.MERGE_METHODS),
                   default=model.DEFAULT_MERGE_METHOD)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("go-merge", help="authorized merge")
    _add_mission_args(p)
    p.add_argument("--repo", default=None)
    p.add_argument("--authorize-merge", dest="authorize_merge", default=None)
    p.add_argument("--authorize-actor", dest="authorize_actor",
                   default="operator")
    p.add_argument("--merge-method", dest="merge_method",
                   choices=sorted(model.MERGE_METHODS), default=None)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("closure", help="record the release closure")
    _add_mission_args(p)
    p.add_argument("--repo", default=None)
    p.add_argument("--base-branch", dest="base_branch", default=None)
    p.add_argument("--issue", default=None)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("reconstruct", help="read-only release reconstruction")
    _add_mission_args(p)

    p = sub.add_parser("status", help="read-only release stage view")
    _add_mission_args(p)

    p = sub.add_parser("acceptance",
                       help="run the deterministic release acceptance matrix")
    p.add_argument("--root", default=None)
    p.add_argument("--out", default=None)
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
        "handoff": _cmd_handoff, "go-commit": _cmd_go_commit,
        "bind-pr": _cmd_bind_pr, "watch-ci": _cmd_watch_ci,
        "merge-handoff": _cmd_merge_handoff, "go-merge": _cmd_go_merge,
        "closure": _cmd_closure, "reconstruct": _cmd_reconstruct,
        "status": _cmd_status, "acceptance": _cmd_acceptance,
    }[args.command]
    try:
        return int(handler(args))
    except (model.ReleaseError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REJECTED
    except SystemExit as exc:  # pragma: no cover - argparse usage
        code = exc.code if isinstance(exc.code, int) else EXIT_USAGE
        return EXIT_OK if code == 0 else EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
