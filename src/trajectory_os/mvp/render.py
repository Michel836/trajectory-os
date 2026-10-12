"""MVP — human-readable cockpit rendering (text and HTML).

The text renderer is a read-only projection. The HTML renderer is the visual
execution & decision cockpit: a framework-free, multi-view interface whose
single source of truth is the validated portfolio (see
:mod:`trajectory_os.mvp.visualization`). Every mutation is sent to the
validated backend endpoints — the browser never rewrites ``portfolio.json``
and never re-implements business rules.
"""

from __future__ import annotations

from trajectory_os.mvp import engine


def _fmt_minutes(minutes: int) -> str:
    hours, remainder = divmod(minutes, 60)
    if hours and remainder:
        return f"{hours}h{remainder:02d}m"
    if hours:
        return f"{hours}h"
    return f"{remainder}m"


def render_text(cockpit: engine.Cockpit) -> str:
    lines: list[str] = [
        "TrajectoryOS — daily cockpit",
        f"generated: {cockpit.generated_at}",
        f"today: {cockpit.today}",
        "",
        "PORTFOLIO",
    ]
    status = cockpit.portfolio["project_status"]
    lines.append("  projects: " + ", ".join(
        f"{label.lower()}={status[label]}" for label in status))
    lines += ["", "TOP READY TASKS"]
    for item in cockpit.ready[:8]:
        effort = (f" ({_fmt_minutes(item['effort_minutes'])})"
                  if item["effort_minutes"] else "")
        lines.append(f"  {item['rank']}. {item['title']}{effort}")
        for reason in item["reasons"]:
            lines.append(f"       - {reason}")
    if not cockpit.ready:
        lines.append("  nothing ready")
    lines += ["", "TODAY"]
    day = cockpit.today_plan
    lines.append(
        f"  capacity {_fmt_minutes(day['total_capacity'])} | "
        f"calendar {_fmt_minutes(day['calendar_minutes'])} | "
        f"buffer {_fmt_minutes(day['buffer_minutes'])} | "
        f"planned {_fmt_minutes(day['planned_minutes'])} / "
        f"{_fmt_minutes(day['plan_capacity'])}")
    for item in day["planned"]:
        lines.append(
            f"  - {item['title']} ({_fmt_minutes(item['estimated_minutes'])})")
        for reason in item["reasons"][:3]:
            lines.append(f"      - {reason}")
    if day["unknown_effort"]:
        lines.append("  ready, effort unknown:")
        for item in day["unknown_effort"][:5]:
            lines.append(f"    - {item['title']}")
    if day["deferred"]:
        lines.append("  deferred (over capacity):")
        for item in day["deferred"][:5]:
            lines.append(f"    - {item['title']}")
    lines += ["", "BLOCKED / WAITING"]
    if not cockpit.blocked and not cockpit.waiting:
        lines.append("  nothing blocked or waiting")
    for item in cockpit.blocked:
        lines.append(f"  [BLOCKED] {item['title']} ({item['project']})")
        lines.append(f"      reason: {item['reason']}")
        lines.append(f"      do: {item['suggested_action']}")
    for item in cockpit.waiting:
        lines.append(f"  [WAITING] {item['title']} ({item['project']})")
        lines.append(f"      waiting for: {', '.join(item['waiting_for'])}")
        lines.append(f"      do: {item['suggested_action']}")
    lines += ["", "PROJECTS"]
    for project in cockpit.projects:
        progress = (f"{int(project['progress'] * 100)}%"
                    if project["progress"] is not None else "-")
        lines.append(
            f"  [{project['status']}] {project['name']} — {progress} "
            f"({project['completed']}/{project['tasks']} tasks)")
        if project["next_action"]:
            lines.append(f"      next: {project['next_action']}")
    lines += ["", "WHAT CHANGED"]
    for record in cockpit.recent_outcomes[:8]:
        minutes = (f" ({record['actual_minutes']}m)"
                   if record["actual_minutes"] is not None else "")
        lines.append(
            f"  - {record['task_id']}: {record['outcome']}{minutes} "
            f"@ {record['recorded_at']}")
    if not cockpit.recent_outcomes:
        lines.append("  no outcomes recorded yet")
    lines += ["", "PLANNED VS ACTUAL"]
    pva = cockpit.planned_vs_actual
    lines.append(
        f"  {pva['completed_with_actual']} completed with actual effort; "
        f"planned {_fmt_minutes(pva['planned_minutes'])}, "
        f"actual {_fmt_minutes(pva['actual_minutes'])}")
    if pva["mean_absolute_error_minutes"] is not None:
        lines.append("  mean estimate error: "
                     f"{_fmt_minutes(int(pva['mean_absolute_error_minutes']))}")
    return "\n".join(lines) + "\n"


def _escape(value: object) -> str:
    text = str(value)
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def render_html(cockpit: engine.Cockpit) -> str:
    from trajectory_os.mvp import cockpit_ui

    return cockpit_ui.render_page(cockpit)


__all__ = ["render_html", "render_text"]
