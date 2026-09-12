"""``trajectory-pi-runs`` — deterministic multi-run tool (V1.85-V1.90).

Subcommands (see ``--help`` for each):

  runs list                    V1.85 read-only run registry
  runs status <run-id>         V1.86 unified state query
  runs admit                   V1.87 bounded admission decision
  runs queue                   V1.89 FIFO queue listing (read-only)
  runs enqueue <id> -- CMD...  V1.89 enqueue (bounded, identity-checked)
  runs start                   V1.88 one authorized start (isolated process group)
  runs cancel                  V1.88 graceful, ownership-proven cancellation
  runs recover                 V1.90 rebuild persisted state, reap finished jobs
  runs orchestrate             V1.90 bounded multi-run progress
  runs version

Exit codes: 0 ok; 2 usage; 3 fail-closed rejection (admission / malformed
state / duplicate identity / queue limits / unproven ownership); 4 unknown
run or job id.  Admission failures and unknown filters never mutate state.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, cast

from trajectory_os.runs import (
    admission,
    model,
    orchestration,
    query,
    registry,
    store,
)

# V1.85-V1.90 tool exit codes (stable, documented).
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REJECTED = 3
EXIT_UNKNOWN = 4


def _default_runs_root() -> Path:
    override = os.environ.get("TRAJECTORY_PI_RUNS_ROOT")
    if override:
        return Path(override)
    return Path.cwd() / ".trajectory-pi" / "runs"


def _default_state_root() -> Path:
    override = os.environ.get("TRAJECTORY_PI_STATE_ROOT")
    if override:
        return Path(override)
    return Path.cwd() / ".trajectory-pi"


class CliError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _parse_capacity(raw: str) -> int:
    token = raw.strip()
    if not token.isdigit():
        raise CliError(EXIT_REJECTED, f"invalid capacity: {raw!r} (reason: CAPACITY_INVALID)")
    try:
        return admission.validate_capacity(int(token))
    except admission.InvalidCapacityError:
        raise CliError(
            EXIT_REJECTED, f"invalid capacity: {raw!r} (reason: CAPACITY_INVALID)"
        ) from None


def _parse_json_arg(raw: str, label: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise CliError(EXIT_USAGE, f"invalid JSON for {label}") from None


# ---------------------------------------------------------------------------
# Command implementations (each returns an exit code + renderable doc).
# ---------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> tuple[int, str]:
    runs_root = Path(args.runs_root)
    document = query.query_payload(
        runs_root, args.filter, None if args.limit is None else int(args.limit)
    )
    return EXIT_OK, (_render_json(document) if args.json else query.render_table(document))


def cmd_status(args: argparse.Namespace) -> tuple[int, str]:
    view = query.find_run(Path(args.runs_root), args.run_id)
    if view is None:
        raise CliError(EXIT_UNKNOWN, f"unknown run id: {args.run_id}")
    document = {
        "schema_version": model.SCHEMA_VERSION,
        "tool": model.CLI_NAME,
        "run": view.to_dict(),
    }
    return EXIT_OK, (_render_json(document) if args.json else _render_run_human(document))


def cmd_admit(args: argparse.Namespace) -> tuple[int, str]:
    runs_root = Path(args.runs_root)
    state_root = Path(args.state_root)
    capacity = _parse_capacity(args.capacity)
    paths = store.state_paths(state_root)
    queue_ok = True
    try:
        queue = store.QueueDoc.load(paths["queue"])
        active = store.load_active_records(paths["active"])
    except store.MalformedStoreError:
        queue, active = store.QueueDoc(seq=0, entries=[]), []
        queue_ok = False
    decision = admission.evaluate_admission(
        capacity_raw=capacity,
        runs=registry.load_runs(runs_root),
        active_records=active,
        queue_entries=queue.entries,
        queue_malformed=not queue_ok,
    )
    rendered = (
        _render_json(decision.to_dict())
        if args.json
        else _render_decision_human(decision)
    )
    if decision.decision != model.DECISION_ALLOWED:
        return EXIT_REJECTED, rendered
    return EXIT_OK, rendered


def cmd_queue(args: argparse.Namespace) -> tuple[int, str]:
    paths = store.state_paths(Path(args.state_root))
    try:
        queue = store.QueueDoc.load(paths["queue"])
        closed = store.load_closed_records(paths["closed"])
        active = store.load_active_records(paths["active"])
    except store.MalformedStoreError as exc:
        document = {
            "schema_version": model.SCHEMA_VERSION,
            "tool": model.CLI_NAME,
            "malformed": True,
            "code": exc.code,
        }
        return EXIT_REJECTED, _render_json(document)
    document = {
        "schema_version": model.SCHEMA_VERSION,
        "tool": model.CLI_NAME,
        "queue": [entry.to_dict() for entry in queue.entries],
        "queue_count": len(queue.entries),
        "active": [record.to_dict() for record in active],
        "active_count": len(active),
        "closed": [record.to_dict() for record in closed][-20:],
        "closed_count": len(closed),
    }
    return EXIT_OK, (_render_json(document) if args.json else _render_queue_human(document))


def cmd_enqueue(args: argparse.Namespace) -> tuple[int, str]:
    command: list[str] = list(args.command or [])
    while command and command[0] == "--":
        command.pop(0)
    if not command:
        raise CliError(EXIT_USAGE, "enqueue requires a command after '--'")
    # Fail closed on flag leakage (REMAINDER swallows trailing options):
    # flags must PRECEDE the job id and command.
    _known_flags = (
        "--state-root",
        "--runs-root",
        "--query-file",
        "--max-attempts",
        "--json",
        "--capacity",
    )
    leaked = [flag for flag in _known_flags if flag in command]
    if leaked:
        raise CliError(
            EXIT_USAGE,
            f"flags {leaked} after the job id are ignored by the parser; "
            "usage: enqueue [flags...] JOB_ID -- CMD [ARGS...]",
        )
    if args.max_attempts is not None:
        if not str(args.max_attempts).strip().isdigit() or not (
            1 <= int(args.max_attempts) <= model.MAX_ATTEMPTS_LIMIT
        ):
            raise CliError(
                EXIT_USAGE,
                (
                    f"invalid max_attempts: {args.max_attempts!r} "
                    f"(allowed 1..{model.MAX_ATTEMPTS_LIMIT})"
                ),
            )
        max_attempts = int(args.max_attempts)
    else:
        max_attempts = model.DEFAULT_MAX_ATTEMPTS

    paths = store.state_paths(Path(args.state_root))
    query_file: str | None = None
    if args.query_file is not None:
        qpath = Path(args.query_file)
        if not qpath.is_file():
            raise CliError(EXIT_USAGE, f"query file not found: {qpath}")
        query_file = str(qpath)
    active_ids: frozenset[str]
    try:
        active = store.load_active_records(paths["active"])
        active_ids = frozenset(rec.job_id for rec in active)
        queue = store.QueueDoc.load(paths["queue"])
    except store.MalformedStoreError as exc:
        return EXIT_REJECTED, _render_json(
            {
                "schema_version": model.SCHEMA_VERSION,
                "tool": model.CLI_NAME,
                "malformed": True,
                "code": exc.code,
            }
        )
    try:
        try:
            entry = queue.enqueue(
                job_id=args.job_id,
                command=command,
                max_attempts=max_attempts,
                query_file=query_file,
                reserved_ids=active_ids,
            )
        except ValueError as exc:
            raise CliError(EXIT_REJECTED, str(exc)) from exc
        queue.save(paths["queue"])
    except store.DuplicateIdentityError as exc:
        return EXIT_REJECTED, f"rejected: {exc}\n"
    except store.QueueFullError as exc:
        return EXIT_REJECTED, f"rejected: {exc}\n"
    document = {
        "schema_version": model.SCHEMA_VERSION,
        "tool": model.CLI_NAME,
        "enqueued": entry.to_dict(),
        "queue_count": len(queue.entries),
    }
    rendered = (
        _render_json(document)
        if args.json
        else f"enqueued {entry.job_id} (seq={entry.seq})\n"
    )
    return EXIT_OK, rendered


def cmd_start(args: argparse.Namespace) -> tuple[int, str]:
    runs_root = Path(args.runs_root)
    state_root = Path(args.state_root)
    capacity = _parse_capacity(args.capacity)
    try:
        report = orchestration.start_one(
            state_root, runs_root, job_id=args.job_id, capacity_raw=capacity
        )
    except (store.QueueEmptyError, store.JobNotFoundError):
        return EXIT_REJECTED, "rejected: QUEUE_EMPTY (nothing queued)\n"
    except store.DuplicateIdentityError as exc:
        return EXIT_REJECTED, f"rejected: {exc}\n"
    except store.MalformedStoreError as exc:
        return EXIT_REJECTED, f"rejected: {exc.code}\n"
    except orchestration.AdmissionRejectedError as exc:
        decision = exc.decision
        rendered = (
            _render_json(decision.to_dict())
            if args.json
            else _render_decision_human(decision) + f"rejected: {'; '.join(decision.reasons)}\n"
        )
        return EXIT_REJECTED, rendered
    document = {"schema_version": model.SCHEMA_VERSION, "tool": model.CLI_NAME, **report}
    message = (
        "started "
        f"{report['job_id']} (pid={report['pid']} "
        f"pgid={report['pgid']} slot={report['slot']})\n"
    )
    return EXIT_OK, (_render_json(document) if args.json else message)


def cmd_cancel(args: argparse.Namespace) -> tuple[int, str]:
    state_root = Path(args.state_root)
    state = orchestration.rebuild_state(state_root)
    report = orchestration.cancel_job(
        state, job_id=args.job_id, grace_seconds=float(args.grace)
    )
    document = {"schema_version": model.SCHEMA_VERSION, "tool": model.CLI_NAME, **report}
    if not report["cancelled"]:
        exit_code = EXIT_OK if (
            str(report.get("outcome")) in (model.TERMINAL_CANCEL_PENDING,)
        ) else EXIT_REJECTED
    else:
        exit_code = EXIT_OK
    rendered = _render_json(document) if args.json else _render_report_human(document)
    return exit_code, rendered


def cmd_recover(args: argparse.Namespace) -> tuple[int, str]:
    state_root = Path(args.state_root)
    state = orchestration.rebuild_state(state_root)
    report = orchestration.reap(state, observe=None)
    document = {"schema_version": model.SCHEMA_VERSION, "tool": model.CLI_NAME, **report,
                "queue_count": len(state.queue.entries), "active_count": len(state.active)}
    return EXIT_OK, (_render_json(document) if args.json else _render_report_human(document))


def cmd_orchestrate(args: argparse.Namespace) -> tuple[int, str]:
    runs_root = Path(args.runs_root)
    state_root = Path(args.state_root)
    cycles = 1
    if args.cycles is not None:
        token = str(args.cycles).strip()
        if not token.isdigit():
            raise CliError(EXIT_USAGE, f"invalid cycles: {args.cycles!r}")
        cycles = int(token)
    capacity = _parse_capacity(args.capacity)
    try:
        report = orchestration.orchestrate(
            state_root, runs_root, cycles=cycles, capacity=capacity
        )
    except ValueError as exc:
        return EXIT_USAGE, f"usage error: {exc}\n"
    return EXIT_OK, (_render_json(report) if args.json else _render_orchestrate_human(report))


def cmd_version(args: argparse.Namespace) -> tuple[int, str]:
    document = {
        "schema_version": model.SCHEMA_VERSION,
        "tool": model.CLI_NAME,
        "version": model.CLI_TOOL_VERSION,
        "capabilities": {
            "V1.85": "read-only run registry",
            "V1.86": "unified state model and queries",
            "V1.87": "bounded admission control",
            "V1.88": "controlled concurrency with proven ownership",
            "V1.89": "durable FIFO queue",
            "V1.90": "bounded multi-run orchestration",
        },
    }
    rendered = _render_json(document) if args.json else (
        f"{model.CLI_NAME} {model.CLI_TOOL_VERSION} (V1.85-V1.90)\n"
    )
    return EXIT_OK, rendered


# ---------------------------------------------------------------------------
# Deterministic human renderers
# ---------------------------------------------------------------------------


def _render_json(document: dict[str, Any]) -> str:
    return query.render_json(document)


def _render_run_human(document: dict[str, Any]) -> str:
    run = document["run"]
    lines = [f"run_id: {run['run_id']}"]
    lines.append(f"state: {run['state']} (lifecycle={run['lifecycle']})")
    if run["state_reasons"]:
        lines.append(f"reasons: {'; '.join(run['state_reasons'])}")
    for key in (
        "run_class",
        "branch",
        "model",
        "workspace",
        "head_before",
        "head_after",
        "started_at",
        "ended_at",
        "duration_seconds",
        "validation",
        "review_status",
        "final_verify_status",
        "patch_sha256",
        "agent_classification",
        "readiness",
    ):
        value = run.get(key)
        if value is not None:
            lines.append(f"{key}: {value}")
    if run.get("repaired", {}).get("requested"):
        rep = run["repaired"]
        lines.append(
            f"repaired: requested (used_attempts={rep.get('used_attempts')} "
            f"convergence={rep.get('convergence')})"
        )
    if run.get("evidence_missing"):
        lines.append(f"evidence_missing: {'; '.join(run['evidence_missing'])}")
    if run.get("evidence_problems"):
        lines.append(f"evidence_problems: {'; '.join(run['evidence_problems'])}")
    return "\n".join(lines) + "\n"


def _render_decision_human(decision: admission.AdmissionDecision) -> str:
    lines = [
        (
            f"decision: {decision.decision} (capacity={decision.capacity} "
            f"free={decision.capacity_free} queued={decision.queued})"
        )
    ]
    for entry in decision.active_unproven:
        lines.append(f"unproven: {entry['id']} ({entry['code']})")
    if decision.active_proven:
        lines.append(f"active_proven: {'; '.join(decision.active_proven)}")
    lines.append(f"reasons: {'; '.join(decision.reasons)}")
    return "\n".join(lines) + "\n"


def _render_queue_human(document: dict[str, Any]) -> str:
    lines = []
    for entry in document.get("queue", []):
        lines.append(
            f"queued  seq={entry['seq']} job={entry['job_id']} "
            f"attempts={entry['attempts']}+{entry['max_attempts']} "
            f"enqueued_at={entry['enqueued_at']} cmd={' '.join(entry['command'])[:60]}"
        )
    for record in document.get("active", []):
        lines.append(
            f"active  job={record['job_id']} slot={record['slot']} "
            f"pid={record['pid']} started_at={record['started_at']}"
        )
    for record in document.get("closed", []):
        lines.append(
            f"closed  job={record['job_id']} terminal={record['terminal']} "
            f"exit_code={record['exit_code']}"
        )
    if not lines:
        lines.append("queue empty (no queued / active / closed records)")
    return "\n".join(lines) + "\n"


def _render_report_human(document: dict[str, Any]) -> str:
    lines = []
    for outcome in document.get("results", []):
        suffix = "requeued" if outcome.get("requeued") else ""
        suffix_part = (
            f" (exit_code={outcome['exit_code']})"
            if outcome.get("exit_code") is not None
            else ""
        )
        lines.append(
            f"{outcome['job_id']}: {outcome['outcome']}"
            + suffix_part
            + (f" [{suffix}]" if suffix else "")
        )
    if document.get("active_count") is not None:
        lines.append(
            f"active_count={document['active_count']} "
            f"queue_count={document.get('queue_count', 0)}"
        )
    if "cancelled" in document:
        state = (
            "cancelled"
            if document["cancelled"]
            else f"NOT cancelled (outcome={document.get('outcome')})"
        )
        lines.insert(0, f"job={document.get('job_id')} -> {state}")
    if not lines:
        lines.append("no records")
    return "\n".join(lines) + "\n"


def _render_orchestrate_human(report: dict[str, Any]) -> str:
    lines = []
    for index, pass_report in enumerate(report.get("passes", []), start=1):
        for outcome in pass_report.get("reap", {}).get("results", []):
            lines.append(
                f"pass{index} reap {outcome['job_id']}: {outcome['outcome']}"
                + (" [requeued]" if outcome.get("requeued") else "")
            )
        for started in pass_report.get("started", []):
            lines.append(f"pass{index} started {started['job_id']} (pid={started['pid']})")
    lines.append(f"total_started={report.get('total_started', 0)}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--runs-root", default=None,
        help="runs evidence root (default <cwd>/.trajectory-pi/runs)",
    )
    common.add_argument(
        "--state-root", default=None,
        help="orchestration state root (default <cwd>/.trajectory-pi)",
    )
    common.add_argument("--json", action="store_true", help="machine-readable JSON output")

    parser = argparse.ArgumentParser(
        prog=model.CLI_NAME,
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{model.CLI_NAME} {model.CLI_TOOL_VERSION}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", parents=[common], help="list runs (read-only, deterministic)")
    p.add_argument("--filter", default=model.FILTER_ALL, choices=list(model.ALL_FILTERS))
    p.add_argument("--limit", default=None)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("status", parents=[common], help="status for one run id")
    p.add_argument("run_id")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("admit", parents=[common], help="bounded admission decision (fail closed)")
    p.add_argument("--capacity", default=str(model.DEFAULT_CAPACITY))
    p.set_defaults(func=cmd_admit)

    p = sub.add_parser("queue", parents=[common], help="list queue/active/closed (read-only)")
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("enqueue", parents=[common], help="enqueue one job (bounded FIFO)")
    p.add_argument("job_id")
    p.add_argument("--max-attempts", default=None)
    p.add_argument("--query-file", default=None, help="per-run PI_FINAL_QUERY content file")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_enqueue)

    p = sub.add_parser(
        "start",
        parents=[common],
        help="start the next job (explicit, admitted, isolated)",
    )
    p.add_argument("--job-id", default=None)
    p.add_argument("--capacity", default=str(model.DEFAULT_CAPACITY))
    p.set_defaults(func=cmd_start)

    p = sub.add_parser(
        "cancel",
        parents=[common],
        help="gracefully cancel one job (ownership-proven only)",
    )
    p.add_argument("--job-id", default=None)
    p.add_argument("--grace", default="3")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser(
        "recover",
        parents=[common],
        help="rebuild orchestration state after wrapper restart",
    )
    p.set_defaults(func=cmd_recover)

    p = sub.add_parser(
        "orchestrate",
        parents=[common],
        help="bounded multi-run progress (cycles-bounded)",
    )
    p.add_argument("--cycles", default="1")
    p.add_argument("--capacity", default=str(model.DEFAULT_CAPACITY))
    p.set_defaults(func=cmd_orchestrate)

    p = sub.add_parser("version", parents=[common], help="tool capability listing")
    p.set_defaults(func=cmd_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "runs_root", None) is None:
        args.runs_root = str(_default_runs_root())
    if getattr(args, "state_root", None) is None:
        args.state_root = str(_default_state_root())
    try:
        if hasattr(args, "grace"):
            grace = float(args.grace)
            if grace < 0 or grace > 30:
                raise CliError(EXIT_USAGE, f"invalid grace: {args.grace!r} (allowed 0..30)")
            args.grace = str(grace)
        if hasattr(args, "limit") and args.limit is not None:
            limit = int(args.limit)
            if limit < 0 or limit > 10000:
                raise CliError(EXIT_USAGE, f"invalid limit: {args.limit!r} (allowed 0..10000)")
            args.limit = str(limit)
        code, rendered = args.func(args)
    except CliError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    except query.UnknownFilterError as exc:
        print(f"unknown filter: {exc.filter_name}", file=sys.stderr)
        return EXIT_USAGE
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        return EXIT_USAGE
    sys.stdout.write(rendered)
    return cast(int, code)


if __name__ == "__main__":
    raise SystemExit(main())
