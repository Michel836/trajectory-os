"""Mission 012 — operator CLI for the bounded goal decomposition graph.

Canonical operator surface (deterministic, fail-closed, read-only except
for ``create``):

    create    create one goal graph from a bounded declarative JSON spec
    show      full graph state: goal, size, order, readiness, provenance
    nodes     list normalized nodes with readiness state
    edges     list explicit dependency edges
    order     deterministic topological order
    ready     dependency-ready / eligible nodes
    blocked   dependency-blocked nodes with explicit reasons
    why       explain one node's dependency-block state
    project   machine-readable M013 scheduler-facing projection
    validate  reconstruct + revalidate persisted graph state (read-only)
    list      all goal graphs under the root with state
    version   CLI version

No command performs a Git trust-boundary write. ``create`` validates the
required mission references read-only and never launches, schedules or
mutates a mission. No operator micro-gate is introduced for ordinary graph
operations.

Exit codes:
    0  OK
    2  usage error
    3  graph rejected (malformed / references / identity mismatch)
    4  graph not found

Root resolution: ``--root PATH`` > ``$TRAJECTORY_GOALS_ROOT`` >
``$TRAJECTORY_MISSIONS_ROOT`` > ``<cwd>/.trajectory-pi`` (graphs and
missions share one root so references resolve).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from trajectory_os import __version__
from trajectory_os.graph import evidence, identity, model, readiness, store, summary
from trajectory_os.missions import orchestrator

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REJECTED = 3
EXIT_NOT_FOUND = 4


class UsageError(Exception):
    """Operator usage error (rejected before any state is written)."""


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


def load_spec(path: str) -> object:
    """Read a bounded declarative JSON spec (fail closed before parsing)."""
    p = Path(path)
    if not p.is_file():
        raise UsageError(f"spec missing or not a regular file: {path}")
    try:
        with p.open("rb") as handle:
            raw = handle.read(model.MAX_SPEC_BYTES + 1)
    except OSError as exc:
        raise UsageError(f"spec unreadable: {exc}") from exc
    if len(raw) > model.MAX_SPEC_BYTES:
        raise UsageError(
            f"spec exceeds the hard cap of {model.MAX_SPEC_BYTES} bytes "
            "(before JSON parsing)")
    try:
        obj: object = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise UsageError("spec must be UTF-8 encoded JSON") from exc
    except json.JSONDecodeError as exc:
        raise UsageError(
            f"spec is not valid JSON: {exc.msg} (line {exc.lineno}, "
            f"column {exc.colno})") from exc
    if not isinstance(obj, dict):
        raise UsageError("spec must be a JSON object")
    return obj


# --- commands -----------------------------------------------------------------


def _cmd_create(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        spec = load_spec(args.spec)
    except UsageError as exc:
        return _fail(f"spec: {exc}")
    repo = args.repo or os.getcwd()
    head = args.head
    if head is None:
        head = orchestrator.git_head(repo)
    try:
        graph = store.create_graph(
            root, spec, repo_root=repo, baseline_revision=head)
    except store.GraphExists as exc:
        return _fail(str(exc), EXIT_REJECTED)
    except model.GraphValidationError as exc:
        return _fail(f"graph rejected: {exc}", EXIT_REJECTED)
    except OSError as exc:
        return _fail(f"graph persistence failed: {exc}", EXIT_REJECTED)
    projection = readiness.project_with_store(root, graph)
    payload = {
        "status": "CREATED",
        "goal_id": graph.goal_id,
        "objective": graph.objective,
        "graph_id": graph.graph_id,
        "spec_sha256": graph.spec_sha256,
        "goal_root": str(store.graph_paths(root, graph.goal_id)["root"]),
        "repo_root": repo,
        "baseline_revision": head,
        "size": {"nodes": len(graph.nodes), "edges": len(graph.edges)},
        "topological_order": list(projection.topological_order),
        "ready": list(projection.ready()),
        "blocked": list(projection.blocked()),
        "counts": projection.counts(),
    }
    if args.json:
        _print_json(payload)
        return EXIT_OK
    print(f"created  : {graph.goal_id} at {payload['goal_root']}")
    print(f"graph    : {graph.graph_id}")
    print(f"repo     : {repo} baseline={head}")
    print(f"size     : nodes={len(graph.nodes)} edges={len(graph.edges)}")
    print(f"order    : {' '.join(projection.topological_order) or '-'}")
    print(f"ready    : {len(projection.ready())} "
          f"[{' '.join(projection.ready()) or '-'}]")
    print(f"blocked  : {len(projection.blocked())} "
          f"[{' '.join(projection.blocked()) or '-'}]")
    return EXIT_OK


def _load_document(root: str, goal_id: str) -> dict[str, Any]:
    return summary.inspect(root, goal_id)


def _cmd_show(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        document = _load_document(root, args.goal_id)
    except store.GraphNotFound as exc:
        return _fail(str(exc), EXIT_NOT_FOUND)
    except model.GraphValidationError as exc:
        return _fail(f"error(rejected): {exc}", EXIT_REJECTED)
    if args.json:
        _print_json(document)
        return EXIT_OK
    print(summary.render_inspect(document))
    return EXIT_OK


def _cmd_nodes(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        document = _load_document(root, args.goal_id)
    except store.GraphNotFound as exc:
        return _fail(str(exc), EXIT_NOT_FOUND)
    except model.GraphValidationError as exc:
        return _fail(f"error(rejected): {exc}", EXIT_REJECTED)
    if args.json:
        _print_json({
            "status": "OK", "goal_id": document["goal_id"],
            "graph_id": document["graph_id"], "nodes": document["nodes"],
        })
    else:
        print(summary.render_nodes(document))
    return EXIT_OK


def _cmd_edges(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        document = _load_document(root, args.goal_id)
    except store.GraphNotFound as exc:
        return _fail(str(exc), EXIT_NOT_FOUND)
    except model.GraphValidationError as exc:
        return _fail(f"error(rejected): {exc}", EXIT_REJECTED)
    if args.json:
        _print_json({
            "status": "OK", "goal_id": document["goal_id"],
            "graph_id": document["graph_id"], "edges": document["edges"],
        })
    else:
        print(summary.render_edges(document))
    return EXIT_OK


def _cmd_order(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        document = _load_document(root, args.goal_id)
    except store.GraphNotFound as exc:
        return _fail(str(exc), EXIT_NOT_FOUND)
    except model.GraphValidationError as exc:
        return _fail(f"error(rejected): {exc}", EXIT_REJECTED)
    if args.json:
        _print_json({
            "status": "OK", "goal_id": document["goal_id"],
            "graph_id": document["graph_id"],
            "topological_order": document["topological_order"],
        })
    else:
        print(summary.render_order(document))
    return EXIT_OK


def _cmd_ready(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        document = _load_document(root, args.goal_id)
    except store.GraphNotFound as exc:
        return _fail(str(exc), EXIT_NOT_FOUND)
    except model.GraphValidationError as exc:
        return _fail(f"error(rejected): {exc}", EXIT_REJECTED)
    if args.json:
        ready_nodes = [
            node for node in document["nodes"]
            if node["state"] == readiness.RS_READY
        ]
        _print_json({
            "status": "OK", "goal_id": document["goal_id"],
            "graph_id": document["graph_id"],
            "ready": document["ready"], "nodes": ready_nodes,
        })
    else:
        print(summary.render_ready(document))
    return EXIT_OK


def _cmd_blocked(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        document = _load_document(root, args.goal_id)
    except store.GraphNotFound as exc:
        return _fail(str(exc), EXIT_NOT_FOUND)
    except model.GraphValidationError as exc:
        return _fail(f"error(rejected): {exc}", EXIT_REJECTED)
    if args.json:
        blocked_nodes = [
            node for node in document["nodes"]
            if node["state"] == readiness.RS_BLOCKED
        ]
        _print_json({
            "status": "OK", "goal_id": document["goal_id"],
            "graph_id": document["graph_id"],
            "blocked": document["blocked"], "nodes": blocked_nodes,
        })
    else:
        print(summary.render_blocked(document))
    return EXIT_OK


def _cmd_why(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        document = summary.explain(root, args.goal_id, args.node_id)
    except store.GraphNotFound as exc:
        return _fail(str(exc), EXIT_NOT_FOUND)
    except model.GraphValidationError as exc:
        return _fail(f"error(rejected): {exc}", EXIT_REJECTED)
    if args.json:
        _print_json(document)
        return EXIT_OK
    if document["node"] is None:
        return _fail(f"node not found: {args.node_id}", EXIT_USAGE)
    print(summary.render_explain(document))
    return EXIT_OK


def _cmd_project(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        document = _load_document(root, args.goal_id)
    except store.GraphNotFound as exc:
        return _fail(str(exc), EXIT_NOT_FOUND)
    except model.GraphValidationError as exc:
        return _fail(f"error(rejected): {exc}", EXIT_REJECTED)
    scheduler = dict(document["scheduler"])
    if args.json:
        _print_json(scheduler)
    else:
        print(json.dumps(scheduler, indent=2, sort_keys=True))
    return EXIT_OK


def _cmd_validate(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        graph, _ = store.load_graph(root, args.goal_id)
        projection = readiness.project_with_store(root, graph)
    except store.GraphNotFound as exc:
        return _fail(str(exc), EXIT_NOT_FOUND)
    except model.GraphValidationError as exc:
        return _fail(f"error(rejected): {exc}", EXIT_REJECTED)
    reference_ids = sorted({
        node.mission_ref.mission_id
        for node in graph.nodes if node.mission_ref is not None
    })
    records = evidence.collect_evidence(root, reference_ids)
    payload = {
        "status": "VALID",
        "goal_id": graph.goal_id,
        "graph_id": graph.graph_id,
        "spec_sha256": graph.spec_sha256,
        "identity": {
            "graph": identity.graph_identity_ref(graph.graph_id),
            "spec": identity.spec_identity_ref(graph.spec_sha256),
        },
        "size": {"nodes": len(graph.nodes), "edges": len(graph.edges)},
        "topological_order": list(projection.topological_order),
        "counts": projection.counts(),
        "references": [records[mid].to_dict() for mid in reference_ids],
    }
    if args.json:
        _print_json(payload)
        return EXIT_OK
    print(f"valid    : {graph.goal_id} ({graph.graph_id})")
    print(f"spec     : {graph.spec_sha256}")
    print(f"size     : nodes={len(graph.nodes)} edges={len(graph.edges)}")
    print(f"order    : {' '.join(projection.topological_order) or '-'}")
    print("counts   : "
          + " ".join(f"{key}={value}"
                     for key, value in sorted(projection.counts().items())))
    for mid in reference_ids:
        record = records[mid]
        print(f"ref      : {mid} resolved={record.resolved} "
              f"state={record.state} error={record.error} "
              f"proven={record.proven_complete}")
    return EXIT_OK


def _cmd_list(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    entries: list[dict[str, Any]] = []
    for goal_id in store.list_goal_ids(root):
        try:
            graph, _ = store.load_graph(root, goal_id)
            projection = readiness.project_with_store(root, graph)
            entries.append({
                "goal_id": graph.goal_id,
                "graph_id": graph.graph_id,
                "objective": graph.objective,
                "nodes": len(graph.nodes),
                "edges": len(graph.edges),
                "ready": len(projection.ready()),
                "blocked": len(projection.blocked()),
                "counts": projection.counts(),
                "state": "OK",
            })
        except model.GraphValidationError as exc:
            entries.append({
                "goal_id": goal_id,
                "graph_id": None,
                "objective": None,
                "nodes": 0,
                "edges": 0,
                "ready": 0,
                "blocked": 0,
                "counts": {},
                "state": "MALFORMED",
                "error": exc.code,
            })
    if args.json:
        _print_json({"status": "OK", "root": root, "goals": entries})
        return EXIT_OK
    if not entries:
        print(f"no goal graphs under {root}")
        return EXIT_OK
    for entry in entries:
        print(f"{entry['goal_id']}  {entry['state']} "
              f"nodes={entry['nodes']} edges={entry['edges']} "
              f"ready={entry['ready']} blocked={entry['blocked']}")
    return EXIT_OK


def _cmd_version() -> int:
    print(f"trajectory-pi-goals {__version__}")
    return EXIT_OK


# --- parser -------------------------------------------------------------------


def _add_shared_flags(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--root", default=None, dest="sub_root",
                           help="goals/missions root for this command "
                                "(overrides the global --root; default: "
                                "$TRAJECTORY_GOALS_ROOT, "
                                "$TRAJECTORY_MISSIONS_ROOT or "
                                "<cwd>/.trajectory-pi)")
    subparser.add_argument("--json", action="store_true", default=False,
                           dest="sub_json",
                           help="machine-readable output (global or "
                                "per-subcommand position)")


def build_parser(prog: str = "trajectory-pi-goals") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="TrajectoryOS operator CLI — bounded goal decomposition "
                    "graph (deterministic, fail-closed, read-only except "
                    "create).")
    parser.add_argument("--root", default=None,
                        help="goals/missions root (default: "
                             "$TRAJECTORY_GOALS_ROOT, "
                             "$TRAJECTORY_MISSIONS_ROOT or "
                             "<cwd>/.trajectory-pi)")
    parser.add_argument("--json", action="store_true", default=False,
                        help="machine-readable output (accepted globally or "
                             "per-subcommand)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create",
                       help="create a goal graph from a declarative spec")
    _add_shared_flags(p)
    p.add_argument("--spec", required=True,
                   help="bounded declarative JSON spec file")
    p.add_argument("--repo", default=None,
                   help="repository worktree (default: cwd)")
    p.add_argument("--head", default=None,
                   help="baseline revision (default: read-only HEAD probe)")

    for name, help_text in (
        ("show", "full graph state (goal, size, order, readiness, provenance)"),
        ("nodes", "list normalized nodes with readiness state"),
        ("edges", "list explicit dependency edges"),
        ("order", "deterministic topological order"),
        ("ready", "dependency-ready / eligible nodes"),
        ("blocked", "dependency-blocked nodes with reasons"),
        ("project", "machine-readable M013 scheduler-facing projection"),
        ("validate", "reconstruct + revalidate persisted graph state"),
    ):
        p = sub.add_parser(name, help=help_text)
        _add_shared_flags(p)
        p.add_argument("goal_id")

    p = sub.add_parser("why", help="explain one node's dependency-block state")
    _add_shared_flags(p)
    p.add_argument("goal_id")
    p.add_argument("node_id")

    p = sub.add_parser("list", help="all goal graphs under the root")
    _add_shared_flags(p)
    sub.add_parser("version", help="CLI version")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point (canonical exit codes; deterministic)."""
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
        "create": _cmd_create,
        "show": _cmd_show,
        "nodes": _cmd_nodes,
        "edges": _cmd_edges,
        "order": _cmd_order,
        "ready": _cmd_ready,
        "blocked": _cmd_blocked,
        "why": _cmd_why,
        "project": _cmd_project,
        "validate": _cmd_validate,
        "list": _cmd_list,
    }[args.command]
    return int(handler(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
