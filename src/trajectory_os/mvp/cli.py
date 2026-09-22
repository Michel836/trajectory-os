"""MVP — practical CLI surface for the personal execution system.

Commands (all read-only except ``init``, ``load``, ``record`` and the explicit
``export-sp`` write, which all write only under the chosen data root or the
chosen export path):

    mvp init [--root DIR] [--today DATE]     write the representative seed
    mvp load [--root DIR] FILE               load/validate a portfolio JSON
    mvp plan [--root DIR] [--today DATE]     full daily cockpit (text)
    mvp today | week | ready | blocked | projects | wbs
    mvp record --task ID --outcome O [--minutes N] [--note TEXT]
    mvp export-sp [--out FILE] [--limit N]
    mvp dashboard [--port N]                 serve the local cockpit
    mvp demo [--root DIR]                    seed + plan + HTML + demo

The dashboard is the recommended non-developer surface; the CLI coexists with
it for scripting.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

from trajectory_os.mvp import (
    capture,
    dataset,
    engine,
    model,
    outcomes,
    priority,
    readiness,
    render,
    store,
    superproductivity,
    wbs,
)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_ERROR = 3

DEFAULT_ROOT = "local_data/mvp"
DEFAULT_DEMO_ROOT = ".artifacts/mvp/demo"


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _parse_today(value: str | None) -> date:
    if value is None:
        return date.today()
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise SystemExit(
            f"invalid date {value!r} (expected YYYY-MM-DD)") from None


def _load_or_fail(root: str) -> model.Portfolio:
    portfolio = store.load_portfolio(root)
    if portfolio is None:
        print(f"error: no portfolio at {root}/portfolio.json "
              f"(run `mvp init --root {root}` first)", file=sys.stderr)
        raise SystemExit(EXIT_ERROR)
    return portfolio


def _cmd_init(args: argparse.Namespace) -> int:
    path = dataset.write_seed(args.root, today=_parse_today(args.today))
    print(f"portfolio seed written: {path}")
    print("replace it with your real projects before daily use.")
    return EXIT_OK


def _cmd_load(args: argparse.Namespace) -> int:
    source = Path(args.file)
    raw = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit("error: portfolio must be a JSON object")
    portfolio = model.Portfolio.from_dict(raw)
    path = store.save_portfolio(args.root, portfolio)
    print(f"portfolio loaded and validated: {path}")
    return EXIT_OK


def _cmd_plan(args: argparse.Namespace) -> int:
    root = args.root
    _load_or_fail(root)
    cockpit = engine.build_cockpit(root, today=_parse_today(args.today))
    if args.json:
        _print_json(cockpit.to_dict())
    else:
        print(render.render_text(cockpit))
    return EXIT_OK


def _cmd_view(args: argparse.Namespace) -> int:
    root = args.root
    name = args.view
    portfolio = _load_or_fail(root)
    today = _parse_today(args.today)
    if name == "today":
        ranked = priority.rank_ready_tasks(portfolio, today=today)
        day = scheduler_plan(portfolio, ranked, today)
        payload = day.to_dict()
        text = _render_day(day)
    elif name == "week":
        ranked = priority.rank_ready_tasks(portfolio, today=today)
        week = scheduler_week(portfolio, ranked, today)
        payload = week.to_dict()
        text = _render_week(week)
    elif name == "ready":
        ranked = priority.rank_ready_tasks(portfolio, today=today)
        payload = {"ready": [item.to_dict() for item in ranked]}
        text = "\n".join(f"{item.rank}. {item.title}"
                         + (f" ({item.effort_minutes}m)"
                            if item.effort_minutes else "")
                         + "\n   " + "; ".join(item.reasons)
                         for item in ranked) or "nothing ready"
    elif name == "blocked":
        blocked = readiness.blocked_tasks(portfolio)
        waiting = readiness.waiting_tasks(portfolio)
        payload = {"blocked": [item.to_dict() for item in blocked],
                   "waiting": [item.to_dict() for item in waiting]}
        text = _render_blocked(blocked, waiting)
    elif name == "projects":
        cockpit = engine.build_cockpit(root, today=today, persist=False)
        payload = {"projects": list(cockpit.projects)}
        project_lines: list[str] = []
        for p in cockpit.projects:
            if p["progress"] is not None:
                project_lines.append(
                    f"[{p['status']}] {p['name']} — "
                    f"{int(p['progress'] * 100)}% "
                    f"({p['completed']}/{p['tasks']} tasks)")
            else:
                project_lines.append(f"[{p['status']}] {p['name']}")
        text = "\n".join(project_lines)
    else:  # wbs
        payload = {"wbs": wbs.tree(portfolio)}
        text = wbs.render_text(portfolio)
    if args.json:
        _print_json(payload)
    else:
        print(text)
    return EXIT_OK


def scheduler_plan(portfolio: model.Portfolio,
                   ranked: tuple[priority.PrioritizedTask, ...],
                   today: date) -> Any:
    from trajectory_os.mvp import scheduler
    return scheduler.plan_day(portfolio, ranked, today)


def scheduler_week(portfolio: model.Portfolio,
                   ranked: tuple[priority.PrioritizedTask, ...],
                   today: date) -> Any:
    from trajectory_os.mvp import scheduler
    return scheduler.plan_week(portfolio, ranked, today)


def _render_day(day: Any) -> str:
    lines = [
        f"Today {day.day}",
        f"capacity {day.total_capacity}m | calendar {day.calendar_minutes}m | "
        f"buffer {day.buffer_minutes}m | planned {day.planned_minutes}m / "
        f"{day.plan_capacity}m",
    ]
    for item in day.planned:
        lines.append(f"- {item.title} ({item.estimated_minutes}m)")
        lines.extend(f"    - {r}" for r in item.reasons[:3])
    if day.unknown_effort:
        lines.append("ready but effort unknown: "
                     + ", ".join(i.title for i in day.unknown_effort))
    if day.deferred:
        lines.append("deferred: " + ", ".join(i.title for i in day.deferred))
    return "\n".join(lines)


def _render_week(week: Any) -> str:
    lines = [f"Week starting {week.start_day}"]
    for day in week.days:
        lines.append(f"- {day.day}: {day.planned_minutes}m planned")
        for item in day.planned[:4]:
            lines.append(f"    * {item.title} ({item.estimated_minutes}m)")
    if week.at_risk:
        lines.append("at risk (deadline may be missed): "
                     + ", ".join(i.title for i in week.at_risk))
    return "\n".join(lines)


def _render_blocked(blocked: tuple[readiness.TaskReadiness, ...],
                    waiting: tuple[readiness.TaskReadiness, ...]) -> str:
    lines: list[str] = []
    for item in blocked:
        lines.append(f"[BLOCKED] {item.task_id}: {'; '.join(item.reasons)}")
    for item in waiting:
        lines.append(f"[WAITING] {item.task_id}: {'; '.join(item.reasons)}")
    return "\n".join(lines) or "nothing blocked or waiting"


def _cmd_record(args: argparse.Namespace) -> int:
    _load_or_fail(args.root)
    result = outcomes.record_and_save(
        args.root, task_id=args.task, outcome=args.outcome,
        actual_minutes=args.minutes, note=args.note or "")
    if args.json:
        _print_json(result)
    else:
        print(f"recorded {result['outcome']} for {result['task_id']} "
              f"-> task {result['task_status']}, "
              f"project {result['project_status']}")
    return EXIT_OK


def _cmd_export_sp(args: argparse.Namespace) -> int:
    _load_or_fail(args.root)
    result = superproductivity.export_ready(
        args.root, target=args.out, limit=args.limit,
        project_title=args.project, dry_run=args.dry_run)
    if args.json:
        _print_json(result)
    else:
        if result.get("written"):
            print(f"exported {result['exported']} task(s) to "
                  f"{result['target']}")
        else:
            print(f"dry run: {result['exported']} task(s) would be exported "
                  f"(nothing written)")
            for task_id in result["task_ids"]:
                print(f"- {task_id}")
        print(result["note"])
    return EXIT_OK


def _cmd_capture(args: argparse.Namespace) -> int:
    _load_or_fail(args.root)
    text = (Path(args.file).read_text(encoding="utf-8")
            if args.file else args.text or "")
    if not text.strip():
        raise SystemExit("error: provide --text or --file to capture")
    preview = capture.preview(args.root, text)
    if not args.confirm:
        if args.json:
            _print_json(preview.to_dict())
        else:
            counts = preview.counts
            print(f"capture preview: {counts['items']} item(s) · "
                  f"{counts['matched_existing']} matched · "
                  f"{counts['duplicates']} duplicate(s) · "
                  f"{counts['new_tasks']} new task(s) · "
                  f"{counts['possible_projects']} possible project(s)")
            for item in preview.items:
                suffix = (f" -> {item.matched_title}" if item.matched_title
                          else f" -> {item.project_name}"
                          if item.project_name else "")
                print(f"- [{item.category}] {item.title} "
                      f"({item.suggested_action}){suffix}")
            print("no portfolio change: preview only (use --confirm to apply)")
        return EXIT_OK
    decisions: list[dict[str, object]] = []
    needs_project: list[str] = []
    for item in preview.items:
        decision: dict[str, object] = {
            "capture_id": item.capture_id,
            "action": item.suggested_action,
        }
        if item.suggested_action in (capture.ACT_ACCEPT,
                                     capture.ACT_ATTACH):
            project = args.project or item.project_id or (
                item.matched_id if item.suggested_action == capture.ACT_ATTACH
                else "")
            if item.kind == "task" and not project:
                decision["action"] = capture.ACT_SKIP
                needs_project.append(item.title)
            elif project:
                decision["project_id"] = project
        decisions.append(decision)
    summary = capture.confirm(args.root, text, decisions)
    if args.json:
        _print_json(summary)
    else:
        print(f"capture confirmed: created {summary['created_tasks']} "
              f"task(s), {summary['created_projects']} project(s); "
              f"skipped {summary['skipped']}, merged {summary['merged']}")
        if needs_project:
            print("skipped (no project selected; pass --project): "
                  + ", ".join(needs_project))
    return EXIT_OK


def _cmd_dashboard(args: argparse.Namespace) -> int:
    from trajectory_os.mvp import dashboard

    _load_or_fail(args.root)
    token = os.environ.get("TRAJECTORY_MVP_TOKEN")
    # Engine routing is explicit per job: Local Ollama (qwen3.8:27b-q4_K_M)
    # is the default; DeepSeek Flash is opt-in and never selected silently.
    # The dashboard resolves the selected engine for each import job and
    # falls back to deterministic factual extraction only per-chunk.
    app = dashboard.Dashboard(args.root, token=token)
    port = args.port
    print(f"cockpit: http://127.0.0.1:{port}/  (Ctrl-C to stop)")

    def on_ready(server: Any) -> None:
        host, bound_port = server.server_address[:2]
        print(f"listening on http://{host}:{bound_port}/")

    try:
        dashboard.serve(app, port=port, on_ready=on_ready)
    except KeyboardInterrupt:
        print("\nstopped.")
    return EXIT_OK


def _cmd_demo(args: argparse.Namespace) -> int:
    root = args.root
    today = _parse_today(args.today)
    dataset.write_seed(root, today=today)
    cockpit = engine.build_cockpit(root, today=today)
    html_path = Path(root) / "cockpit.html"
    html_path.write_text(render.render_html(cockpit), encoding="utf-8")
    if args.json:
        _print_json(cockpit.to_dict())
    else:
        print(render.render_text(cockpit))
        print(f"\nHTML cockpit: {html_path}")
        print(f"dashboard: mvp dashboard --root {root}")
    return EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mvp",
        description="TrajectoryOS personal execution & decision system (MVP)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_root(p: argparse.ArgumentParser) -> None:
        p.add_argument("--root", default=DEFAULT_ROOT,
                       help="data root (default: %(default)s)")
        p.add_argument("--today", default=None,
                       help="evaluation date YYYY-MM-DD (default: today)")
        p.add_argument("--json", action="store_true",
                       help="emit JSON instead of text")

    p = sub.add_parser("init", help="write the representative seed portfolio")
    p.add_argument("--root", default=DEFAULT_ROOT)
    p.add_argument("--today", default=None)
    p.set_defaults(func=_cmd_init)

    p = sub.add_parser("load", help="load and validate a portfolio JSON file")
    p.add_argument("--root", default=DEFAULT_ROOT)
    p.add_argument("file")
    p.set_defaults(func=_cmd_load)

    p = sub.add_parser("plan", help="full daily cockpit")
    add_root(p)
    p.set_defaults(func=_cmd_plan)

    for name in ("today", "week", "ready", "blocked", "projects", "wbs"):
        p = sub.add_parser(name, help=f"show the {name} view")
        add_root(p)
        p.set_defaults(func=_cmd_view, view=name)

    p = sub.add_parser("record", help="record an execution outcome")
    p.add_argument("--root", default=DEFAULT_ROOT)
    p.add_argument("--task", required=True)
    p.add_argument("--outcome", required=True,
                   choices=sorted(model.OUTCOMES))
    p.add_argument("--minutes", type=int, default=None)
    p.add_argument("--note", default="")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_record)

    p = sub.add_parser("export-sp", help="export ready tasks to Super "
                                          "Productivity import JSON")
    p.add_argument("--root", default=DEFAULT_ROOT)
    p.add_argument("--out", default="local_data/mvp/super-productivity.json")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--project", default="TrajectoryOS")
    p.add_argument("--dry-run", action="store_true",
                   help="build the export document but write nothing")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_export_sp)

    p = sub.add_parser("capture", help="quick capture: preview then confirm")
    p.add_argument("--root", default=DEFAULT_ROOT)
    p.add_argument("--text", default=None)
    p.add_argument("--file", default=None)
    p.add_argument("--confirm", action="store_true",
                   help="apply the suggested actions (default: preview only)")
    p.add_argument("--project", default=None,
                   help="default project id for new tasks")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_capture)

    p = sub.add_parser("dashboard", help="serve the local cockpit")
    p.add_argument("--root", default=DEFAULT_ROOT)
    p.add_argument("--port", type=int, default=8787)
    p.set_defaults(func=_cmd_dashboard)

    p = sub.add_parser("demo", help="run the reproducible demo scenario")
    p.add_argument("--root", default=DEFAULT_DEMO_ROOT)
    p.add_argument("--today", default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except model.MvpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
