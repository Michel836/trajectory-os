"""M070 — human-facing decision cockpit over the existing control plane.

This is a *projection*, not a competing dashboard. It reads the canonical
LifeOS-compatible project registry, the operational intelligence view and the
outcome ledger, and renders a default view that a non-developer can read:

PROJECTS     active / blocked / waiting / completed
NEXT ACTIONS recommended, why, urgency, expected effort, confidence,
             alternatives
ATTENTION    human decisions required, failed/blocked workflows, stale
             evidence, missing outcome feedback
INTELLIGENCE predictions, uncertainty, source, evidence and
             actual-vs-predicted where available

Git/agent internals are available only as a drill-down evidence reference;
they are never the primary UI. The view is strictly read-only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trajectory_os.platform import model as platform_model
from trajectory_os.platform import projects as project_registry
from trajectory_os.realworld import lifeos_ops, model, outcomes

FAMILY = "DECISION_COCKPIT"

#: Project statuses shown in the default view.
PROJECT_ACTIVE = "ACTIVE"
PROJECT_BLOCKED = "BLOCKED"
PROJECT_WAITING = "WAITING"
PROJECT_COMPLETED = "COMPLETED"


@dataclass(frozen=True)
class CockpitView:
    workflow_id: str
    generated_at: str
    projects: tuple[dict[str, Any], ...]
    next_actions: tuple[dict[str, Any], ...]
    attention: tuple[dict[str, Any], ...]
    intelligence: Mapping[str, Any]
    drilldown: Mapping[str, Any]
    read_only: bool = True
    canonical: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "realworld_version": model.REALWORLD_VERSION,
            "kind": "decision_cockpit_view",
            "family": FAMILY,
            "workflow_id": self.workflow_id,
            "generated_at": self.generated_at,
            "projects": [dict(item) for item in self.projects],
            "next_actions": [dict(item) for item in self.next_actions],
            "attention": [dict(item) for item in self.attention],
            "intelligence": dict(self.intelligence),
            "drilldown": dict(self.drilldown),
            "read_only": self.read_only,
            "canonical": self.canonical,
        }

    def render_text(self) -> str:
        lines = [
            "TrajectoryOS — decision cockpit",
            f"generated: {self.generated_at}",
            "",
            "PROJECTS",
        ]
        for project in self.projects:
            lines.append(
                f"  [{project['status']}] {project['name']} — "
                f"{project['summary']}")
        if not self.projects:
            lines.append("  none")
        lines += ["", "NEXT ACTIONS"]
        for action in self.next_actions:
            lines.append(f"  • {action['action']}")
            lines.append(f"      why: {action['rationale']}")
            lines.append(f"      urgency: {action['urgency']} | "
                         f"confidence: {action['confidence']}")
            if action.get("expected_effort"):
                lines.append("      expected effort: "
                             f"{action['expected_effort']}")
            if action.get("alternatives"):
                lines.append("      alternatives: "
                             + "; ".join(action["alternatives"]))
        if not self.next_actions:
            lines.append("  none")
        lines += ["", "ATTENTION"]
        for item in self.attention:
            lines.append(f"  ! {item['reason']}")
        if not self.attention:
            lines.append("  nothing requires attention")
        lines += ["", "INTELLIGENCE"]
        for prediction in self.intelligence.get("predictions", []):
            lines.append(
                f"  ~ {prediction.get('target')}: "
                f"{prediction.get('prediction')} "
                f"(uncertainty {prediction.get('uncertainty')}; "
                f"source {prediction.get('source')})")
        reconciliation = self.intelligence.get("reconciliation", {})
        lines.append(
            "  outcomes captured: "
            f"{reconciliation.get('reconciled', 0)}/"
            f"{reconciliation.get('link_count', 0)} reconciled")
        if not self.intelligence.get("predictions"):
            lines.append("  no predictions available")
        return "\n".join(lines) + "\n"

    def render_html(self) -> str:
        def escape(value: object) -> str:
            text = str(value)
            return (text.replace("&", "&amp;").replace("<", "&lt;")
                    .replace(">", "&gt;"))

        parts = [
            "<!doctype html><html><head><meta charset='utf-8'>",
            "<title>TrajectoryOS cockpit</title>",
            "<style>body{font-family:system-ui,sans-serif;margin:2rem;"
            "color:#1a1a1a}h2{border-bottom:1px solid #ddd}li{margin:.35rem 0}"
            ".muted{color:#666}</style></head><body>",
            "<h1>TrajectoryOS — decision cockpit</h1>",
            f"<p class='muted'>Generated {escape(self.generated_at)} · "
            "read-only</p>",
            "<h2>Projects</h2><ul>",
        ]
        for project in self.projects:
            parts.append(
                f"<li><strong>[{escape(project['status'])}] "
                f"{escape(project['name'])}</strong> — "
                f"{escape(project['summary'])}</li>")
        parts.append("</ul><h2>Next actions</h2><ul>")
        for action in self.next_actions:
            parts.append(
                f"<li><strong>{escape(action['action'])}</strong><br>"
                f"<span class='muted'>Why: {escape(action['rationale'])} · "
                f"urgency {escape(action['urgency'])} · confidence "
                f"{escape(action['confidence'])}</span></li>")
        parts.append("</ul><h2>Attention</h2><ul>")
        for item in self.attention:
            parts.append(f"<li>{escape(item['reason'])}</li>")
        parts.append("</ul><h2>Intelligence</h2><ul>")
        for prediction in self.intelligence.get("predictions", []):
            parts.append(
                f"<li>{escape(prediction.get('target'))}: "
                f"{escape(prediction.get('prediction'))} "
                f"(uncertainty {escape(prediction.get('uncertainty'))})</li>")
        parts.append("</ul></body></html>")
        return "".join(parts)


def _project_status(
    project_id: str, missions: Sequence[lifeos_ops.MissionState],
) -> tuple[str, str]:
    related = [mission for mission in missions
               if mission.project_id == project_id]
    if not related:
        return PROJECT_ACTIVE, "no mission state supplied"
    if all(mission.is_completed for mission in related):
        return PROJECT_COMPLETED, f"{len(related)} mission(s) completed"
    if any(mission.is_blocked for mission in related):
        blocked = sum(1 for mission in related if mission.is_blocked)
        return PROJECT_BLOCKED, f"{blocked} blocked/failed mission(s) out of "\
            f"{len(related)}"
    if any(mission.human_gate or mission.waiting_reason
           for mission in related):
        waiting = sum(1 for mission in related
                      if mission.human_gate or mission.waiting_reason)
        return PROJECT_WAITING, f"{waiting} mission(s) waiting on a human"
    active = sum(1 for mission in related if not mission.is_completed)
    return PROJECT_ACTIVE, f"{active} active mission(s)"


def build_cockpit(
    root: str, missions: Sequence[lifeos_ops.MissionState], *,
    project_ids: Sequence[str] = (), workflow_id: str = "cockpit",
    predictions: Sequence[Mapping[str, Any]] = (),
    generated_at: str = "", persist: bool = True,
) -> CockpitView:
    """Build the read-only cockpit projection and persist it."""
    stamp = generated_at or model.utc_now()
    if project_ids:
        selected = list(project_ids)
    else:
        selected = [project.project_id
                    for project in project_registry.list_projects(root)
                    if project.active]

    project_entries: list[dict[str, Any]] = []
    for project_id in selected:
        try:
            project = project_registry.load_project(root, project_id)
            name = project.name
            lifecycle = project.lifecycle
        except platform_model.PlatformError:
            name = project_id
            lifecycle = "UNKNOWN"
        status, summary = _project_status(project_id, missions)
        if lifecycle.upper() == "ARCHIVED":
            status = PROJECT_COMPLETED
        project_entries.append({
            "project_id": project_id,
            "name": name,
            "lifecycle": lifecycle,
            "status": status,
            "summary": summary,
        })

    operations = lifeos_ops.evaluate_operations(
        missions, root=root, workflow_id=workflow_id,
        generated_at=stamp, persist=persist)
    reconciliation = outcomes.reconciliation_summary(root)
    intelligence = {
        "predictions": [dict(item) for item in predictions],
        "reconciliation": reconciliation,
        "note": "predictions are advisory; actual outcomes are measured or "
                "explicitly entered",
    }
    drilldown = {
        "operations": f"realworld/operations/{workflow_id}/operations.json",
        "outcome_ledger": "realworld/outcomes/ledger.jsonl",
        "note": "Git/agent internals are drill-down evidence, not primary UI",
    }
    view = CockpitView(
        workflow_id=workflow_id, generated_at=stamp,
        projects=tuple(project_entries),
        next_actions=tuple(action.to_dict()
                           for action in operations.next_actions),
        attention=operations.attention, intelligence=intelligence,
        drilldown=drilldown)
    if persist:
        _persist(root, view)
    return view


def _persist(root: str, view: CockpitView) -> None:
    base = Path(root) / "realworld" / "cockpit" / view.workflow_id
    base.mkdir(parents=True, exist_ok=True)
    model.write_json(str(base / "cockpit.json"), view.to_dict())
    (base / "cockpit.txt").write_text(view.render_text(), encoding="utf-8")
    (base / "cockpit.html").write_text(view.render_html(), encoding="utf-8")


def load_cockpit(root: str,
                 workflow_id: str = "cockpit"
                 ) -> dict[str, Any] | None:
    return model.read_json(
        str(Path(root) / "realworld" / "cockpit" / workflow_id
            / "cockpit.json"))


__all__ = [
    "FAMILY",
    "PROJECT_ACTIVE",
    "PROJECT_BLOCKED",
    "PROJECT_COMPLETED",
    "PROJECT_WAITING",
    "CockpitView",
    "build_cockpit",
    "load_cockpit",
]
