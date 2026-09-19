"""M067 — LifeOS operational intelligence over the canonical project model.

This module answers the operational questions a person actually asks:

* what changed?
* what is blocked?
* what requires attention?
* what can wait?
* what are the next best actions, and why?

It reuses the existing LifeOS-compatible project/decision architecture
(:mod:`trajectory_os.platform.projects`) and never introduces a competing
LifeOS truth model. Every surfaced action carries an explicit rationale,
source, dependencies, uncertainty, urgency basis and — when meaningful —
alternatives. There is no opaque ranking formula: the ordering is a
deterministic, readable priority key.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trajectory_os.platform import model as platform_model
from trajectory_os.platform import projects as project_registry
from trajectory_os.realworld import model

FAMILY = "LIFEOS_OPERATIONAL_INTELLIGENCE"

#: Urgency labels (closed set, most to least urgent).
URGENCY_CRITICAL = "CRITICAL"
URGENCY_HIGH = "HIGH"
URGENCY_MEDIUM = "MEDIUM"
URGENCY_LOW = "LOW"

URGENCIES = (URGENCY_CRITICAL, URGENCY_HIGH, URGENCY_MEDIUM, URGENCY_LOW)

_URGENCY_RANK = {value: index for index, value in enumerate(URGENCIES)}


@dataclass(frozen=True)
class MissionState:
    """One mission's operational state (supplied by the caller/registry)."""

    mission_id: str
    project_id: str
    lifecycle: str = "RUNNING"
    readiness: str = "UNKNOWN"
    phase: str = ""
    priority: int = 3
    changed_at: str | None = None
    blocked_reason: str | None = None
    waiting_reason: str | None = None
    failed_reason: str | None = None
    human_gate: str | None = None
    stale_evidence_since: str | None = None
    outcome_recorded: bool = True
    dependencies: tuple[str, ...] = ()
    next_action: str | None = None

    def validate(self) -> MissionState:
        if not self.mission_id or not self.project_id:
            model.intel_model.fail(model.intel_model.E_MALFORMED,
                                   "mission requires id and project")
        return self

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocked_reason or self.failed_reason)

    @property
    def is_completed(self) -> bool:
        return self.lifecycle.upper() in ("COMPLETE", "COMPLETED", "MERGED")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "project_id": self.project_id,
            "lifecycle": self.lifecycle,
            "readiness": self.readiness,
            "phase": self.phase,
            "priority": self.priority,
            "changed_at": self.changed_at,
            "blocked_reason": self.blocked_reason,
            "waiting_reason": self.waiting_reason,
            "failed_reason": self.failed_reason,
            "human_gate": self.human_gate,
            "stale_evidence_since": self.stale_evidence_since,
            "outcome_recorded": self.outcome_recorded,
            "dependencies": list(self.dependencies),
            "next_action": self.next_action,
        }


@dataclass(frozen=True)
class OperationsReport:
    workflow_id: str
    project_id: str | None
    project: str | None
    changed: tuple[dict[str, Any], ...]
    blocked: tuple[dict[str, Any], ...]
    attention: tuple[dict[str, Any], ...]
    can_wait: tuple[dict[str, Any], ...]
    next_actions: tuple[model.Action, ...]
    generated_at: str
    canonical: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "realworld_version": model.REALWORLD_VERSION,
            "kind": "lifeos_operations_report",
            "family": FAMILY,
            "workflow_id": self.workflow_id,
            "project_id": self.project_id,
            "project": self.project,
            "changed": list(self.changed),
            "blocked": list(self.blocked),
            "attention": list(self.attention),
            "can_wait": list(self.can_wait),
            "next_actions": [action.to_dict()
                             for action in self.next_actions],
            "generated_at": self.generated_at,
            "canonical": self.canonical,
        }

    def render_markdown(self) -> str:
        lines = [
            f"# Operational view — {self.project or 'portfolio'}", "",
            f"- generated: {self.generated_at}",
            f"- canonical runtime truth: {self.canonical}", "",
        ]
        sections = (
            ("What changed", self.changed),
            ("Blocked", self.blocked),
            ("Requires attention", self.attention),
            ("Can wait", self.can_wait),
        )
        for title, items in sections:
            lines += [f"## {title}", ""]
            if not items:
                lines.append("- none")
            for item in items:
                lines.append(f"- **{item['mission_id']}** — {item['reason']}")
            lines.append("")
        lines += ["## Next best actions", ""]
        for action in self.next_actions:
            lines.append(f"- **{action.action}**")
            lines.append(f"  - why: {action.rationale}")
            lines.append(f"  - source: `{action.source}`")
            lines.append(f"  - urgency: {action.urgency} "
                         f"({action.uncertainty} uncertainty)")
            if action.dependencies:
                lines.append("  - dependencies: "
                             + ", ".join(action.dependencies))
            if action.alternatives:
                lines.append("  - alternatives: "
                             + "; ".join(action.alternatives))
        return "\n".join(lines) + "\n"


def _mission_view(mission: MissionState,
                  reason: str) -> dict[str, Any]:
    view = mission.to_dict()
    view["reason"] = reason
    return view


def _attention_items(
    missions: Sequence[MissionState],
) -> tuple[dict[str, Any], ...]:
    items: list[dict[str, Any]] = []
    for mission in missions:
        if mission.is_completed:
            continue
        if mission.human_gate:
            items.append(_mission_view(
                mission, f"human decision required: {mission.human_gate}"))
        if mission.blocked_reason:
            items.append(_mission_view(
                mission, f"blocked: {mission.blocked_reason}"))
        if mission.failed_reason:
            items.append(_mission_view(
                mission, f"failed workflow: {mission.failed_reason}"))
        if mission.stale_evidence_since:
            items.append(_mission_view(
                mission, "stale evidence since "
                         f"{mission.stale_evidence_since}"))
        if not mission.outcome_recorded:
            items.append(_mission_view(
                mission, "missing outcome feedback"))
    return tuple(items)


def _can_wait(missions: Sequence[MissionState]) -> tuple[dict[str, Any], ...]:
    return tuple(
        _mission_view(mission, "low priority and not blocked; safe to defer")
        for mission in missions
        if not mission.is_completed and not mission.is_blocked
        and not mission.human_gate
        and mission.priority <= 2)


def _next_actions(
    attention: Sequence[Mapping[str, Any]],
    changed: Sequence[Mapping[str, Any]],
    can_wait: Sequence[Mapping[str, Any]],
) -> tuple[model.Action, ...]:
    actions: list[model.Action] = []
    seen: set[str] = set()
    for item in attention:
        mission_id = str(item["mission_id"])
        if mission_id in seen:
            continue
        seen.add(mission_id)
        reason = str(item["reason"])
        if reason.startswith("human decision"):
            urgency = URGENCY_CRITICAL
            alternatives: tuple[str, ...] = (
                "escalate to the accountable owner",)
            kind = "HUMAN_DECISION"
        elif reason.startswith("blocked"):
            urgency = URGENCY_HIGH
            alternatives = ("reassign resources",
                            "reduce scope and continue")
            kind = "UNBLOCK"
        elif reason.startswith("failed"):
            urgency = URGENCY_HIGH
            alternatives = ("retry with a different route",
                            "record the failure and stop")
            kind = "RECOVER"
        elif reason.startswith("stale"):
            urgency = URGENCY_MEDIUM
            alternatives = ("re-ingest the source",
                            "mark the evidence as acceptable")
            kind = "REFRESH_EVIDENCE"
        else:
            urgency = URGENCY_MEDIUM
            alternatives = ("record an explicit UNKNOWN outcome",)
            kind = "OUTCOME_FEEDBACK"
        actions.append(model.Action(
            action=str(item.get("next_action")
                       or f"Resolve {mission_id}: {reason}"),
            rationale=reason,
            source=f"mission:{mission_id}",
            urgency=urgency,
            dependencies=tuple(str(dep) for dep in item.get("dependencies",
                                                            [])),
            uncertainty=("HIGH" if reason.startswith("human decision")
                         else "MEDIUM"),
            alternatives=alternatives,
            kind=kind).validate())
    for item in changed:
        mission_id = str(item["mission_id"])
        if mission_id in seen or item.get("is_completed"):
            continue
        seen.add(mission_id)
        actions.append(model.Action(
            action=f"Review the change on {mission_id} and decide next step",
            rationale=f"changed at {item.get('changed_at')}",
            source=f"mission:{mission_id}",
            urgency=URGENCY_MEDIUM,
            dependencies=(),
            uncertainty="LOW",
            alternatives=("defer to the next review window",),
            kind="REVIEW").validate())
    for item in can_wait:
        mission_id = str(item["mission_id"])
        if mission_id in seen:
            continue
        seen.add(mission_id)
        actions.append(model.Action(
            action=f"Defer {mission_id} while higher-priority work proceeds",
            rationale="not blocked, low priority; safe to wait",
            source=f"mission:{mission_id}",
            urgency=URGENCY_LOW, uncertainty="LOW",
            alternatives=("start it now if capacity is free",),
            kind="DEFER").validate())
    return tuple(actions)


def evaluate_operations(
    missions: Sequence[MissionState], *, root: str | None = None,
    project_id: str | None = None, workflow_id: str = "operations",
    generated_at: str = "", persist: bool = True,
) -> OperationsReport:
    """Evaluate the operational view over supplied canonical mission state."""
    stamp = generated_at or model.utc_now()
    for mission in missions:
        mission.validate()
    filtered = [mission for mission in missions
                if project_id is None or mission.project_id == project_id]

    changed = tuple(
        sorted(
            (_mission_view(mission,
                           f"changed at {mission.changed_at}")
             for mission in filtered if mission.changed_at is not None),
            key=lambda item: str(item.get("changed_at")), reverse=True))
    blocked = tuple(
        _mission_view(
            mission,
            mission.failed_reason and f"failed: {mission.failed_reason}"
            or f"blocked: {mission.blocked_reason}")
        for mission in filtered if mission.is_blocked)
    attention = _attention_items(filtered)
    can_wait = _can_wait(filtered)
    actions = _next_actions(attention, changed, can_wait)

    project_name: str | None = None
    if root is not None and project_id is not None:
        try:
            project = project_registry.load_project(root, project_id)
            project_name = project.name
        except platform_model.PlatformError:
            project_name = None

    report = OperationsReport(
        workflow_id=workflow_id, project_id=project_id, project=project_name,
        changed=changed, blocked=blocked, attention=attention,
        can_wait=can_wait, next_actions=actions, generated_at=stamp)
    if persist and root is not None:
        _persist(root, report)
    return report


def evaluate_project_operations(
    root: str, project_id: str, *,
    missions: Sequence[MissionState] = (),
    workflow_id: str = "operations", generated_at: str = "",
) -> OperationsReport:
    """Evaluate operations for one registered LifeOS-compatible project."""
    project = project_registry.load_project(root, project_id)
    derived: list[MissionState] = list(missions)
    known = {mission.mission_id for mission in derived}
    for objective in project.objectives:
        for mission_id in objective.mission_ids:
            if mission_id in known:
                continue
            derived.append(MissionState(
                mission_id=mission_id, project_id=project_id,
                lifecycle="RUNNING", readiness="UNKNOWN",
                priority=3, outcome_recorded=True))
    return evaluate_operations(
        derived, root=root, project_id=project_id, workflow_id=workflow_id,
        generated_at=generated_at)


def _persist(root: str, report: OperationsReport) -> None:
    base = Path(root) / "realworld" / "operations" / report.workflow_id
    base.mkdir(parents=True, exist_ok=True)
    model.write_json(str(base / "operations.json"), report.to_dict())
    (base / "operations.md").write_text(report.render_markdown(),
                                        encoding="utf-8")


def load_operations(root: str,
                    workflow_id: str = "operations"
                    ) -> dict[str, Any] | None:
    return model.read_json(
        str(Path(root) / "realworld" / "operations" / workflow_id
            / "operations.json"))


__all__ = [
    "FAMILY",
    "URGENCIES",
    "URGENCY_CRITICAL",
    "URGENCY_HIGH",
    "URGENCY_LOW",
    "URGENCY_MEDIUM",
    "MissionState",
    "OperationsReport",
    "evaluate_operations",
    "evaluate_project_operations",
    "load_operations",
]
