"""M027 — pure HTML rendering of the web dashboard projections.

Deterministic, dependency-free and side-effect-free: the frame is a pure
function of the projection document. All dynamic text is HTML-escaped, so
authoritative state can never inject markup into the operator dashboard.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from html import escape
from typing import Any

_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TrajectoryOS dashboard</title>
<style>
body {{ font-family: system-ui, monospace; margin: 1.5rem; color: #16181d; }}
h1 {{ font-size: 1.25rem; }}
table {{ border-collapse: collapse; margin: 0.5rem 0 1rem; }}
th, td {{ border: 1px solid #d0d3d9; padding: 0.25rem 0.6rem; text-align: left; }}
th {{ background: #f2f3f5; }}
.ok {{ color: #1a7f37; }} .warn {{ color: #9a6700; }} .bad {{ color: #b42318; }}
.muted {{ color: #6b7280; }}
</style>
</head>
<body>
"""

_FOOT = "</body>\n</html>\n"


def _cell(value: Any) -> str:
    return escape(str(value if value is not None else "-"))


def render_index(root: str, document: Mapping[str, Any]) -> str:
    goals = document.get("goals")
    if not isinstance(goals, Sequence) or isinstance(goals, (str, bytes)):
        goals = []
    rows = []
    for entry in goals:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("status") != "OK":
            rows.append(
                "<tr><td>{}</td><td colspan=6 class=\"bad\">MALFORMED "
                "({})</td></tr>".format(
                    _cell(entry.get("goal_id")), _cell(entry.get("error"))))
            continue
        cls = "ok" if entry.get("complete") else "warn"
        rows.append(
            "<tr><td><a href=\"/goal/{gid}\">{gid}</a></td>"
            "<td class=\"{cls}\">{state}/{reason}</td>"
            "<td>{cp}/{ct}</td><td>{blockers}</td><td>{gate}</td>"
            "<td>{stop}</td></tr>".format(
                gid=_cell(entry.get("goal_id")), cls=cls,
                state=_cell(entry.get("state")),
                reason=_cell(entry.get("reason")),
                cp=_cell(entry.get("criteria_proven")),
                ct=_cell(entry.get("criteria_total")),
                blockers=_cell(entry.get("blockers")),
                gate=_cell(entry.get("gate")),
                stop="yes" if entry.get("stop_requested") else "no"))
    body = [
        "<h1>TrajectoryOS — goals</h1>",
        f"<p class=\"muted\">root: {_cell(root)} "
        f"({_cell(document.get('count'))} goal(s))</p>",
        "<table><thead><tr><th>goal</th><th>state</th><th>criteria</th>"
        "<th>blockers</th><th>gate</th><th>stop</th></tr></thead><tbody>",
        *rows,
        "</tbody></table>",
        "<p class=\"muted\">Read-only projection. Controls: "
        "POST /api/control/{stop,clear-stop,refresh-events,daemon-stop}. "
        "No Git write is ever performed.</p>",
    ]
    return _HEAD + "\n".join(body) + "\n" + _FOOT


def render_goal(document: Mapping[str, Any]) -> str:
    goal_id = document.get("goal_id")
    snapshot = document.get("snapshot")
    if not isinstance(snapshot, Mapping):
        return _HEAD + f"<h1>goal {_cell(goal_id)}</h1><p>no snapshot</p>" \
            + _FOOT
    goal = snapshot.get("goal")
    if not isinstance(goal, Mapping):
        goal = {}
    final = snapshot.get("final")
    if not isinstance(final, Mapping):
        final = {}
    proof = snapshot.get("proof")
    if not isinstance(proof, Mapping):
        proof = {}
    counts = snapshot.get("counts")
    if not isinstance(counts, Mapping):
        counts = {}
    events_doc = document.get("events")
    if not isinstance(events_doc, Mapping):
        events_doc = {}
    event_list = events_doc.get("events")
    if not isinstance(event_list, Sequence) or isinstance(
            event_list, (str, bytes)):
        event_list = []
    body = [
        f"<h1>goal {_cell(goal_id)}</h1>",
        "<table>",
        f"<tr><th>objective</th><td>{_cell(goal.get('objective'))}</td></tr>",
        f"<tr><th>state</th><td>{_cell(final.get('state'))}/"
        f"{_cell(final.get('reason'))} "
        f"(complete={_cell(final.get('complete'))})</td></tr>",
        f"<tr><th>proof</th><td>{_cell(proof.get('proof_id'))} "
        f"stale={_cell(proof.get('stale'))}</td></tr>",
        f"<tr><th>criteria</th><td>{_cell(counts.get('criteria_proven'))}/"
        f"{_cell(counts.get('criteria_total'))}</td></tr>",
        f"<tr><th>generation</th><td>"
        f"{_cell((snapshot.get('generation') or {}).get('generation_number'))}"
        f"</td></tr>",
        "</table>",
        "<h2>Events</h2>",
        f"<p class=\"muted\">{_cell(events_doc.get('count'))} event(s)</p>",
        "<table><thead><tr><th>occurred</th><th>severity</th><th>category</th>"
        "<th>kind</th><th>subject</th><th>reason</th></tr></thead><tbody>",
    ]
    for entry in list(event_list)[-64:]:
        if not isinstance(entry, Mapping):
            continue
        severity = str(entry.get("severity"))
        cls = {"CRITICAL": "bad", "WARNING": "warn"}.get(severity, "ok")
        body.append(
            "<tr><td>{o}</td><td class=\"{c}\">{s}</td><td>{cat}</td>"
            "<td>{k}</td><td>{sub}</td><td>{r}</td></tr>".format(
                o=_cell(entry.get("occurred_at")), c=cls, s=_cell(severity),
                cat=_cell(entry.get("category")), k=_cell(entry.get("kind")),
                sub=_cell(entry.get("subject")), r=_cell(entry.get("reason"))))
    body.extend([
        "</tbody></table>",
        "<p class=\"muted\">Projection only; authoritative state lives in the "
        "canonical stores. No Git write is ever performed.</p>",
        "<p><a href=\"/\">all goals</a></p>",
    ])
    return _HEAD + "\n".join(body) + "\n" + _FOOT
