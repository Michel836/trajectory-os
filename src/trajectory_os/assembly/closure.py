"""M031 — durable closure evidence and mission reconstruction.

Closure is the mission-level aggregate that lets an operator reconstruct
*what happened* without reading prose logs. It is derived from the canonical
durable artifacts (never from transient process state):

* ``mission.json`` / ``plan.json`` — identity, intent and bounded plan;
* ``status.json`` — the single canonical M030 lifecycle/readiness truth;
* ``events.jsonl`` — the canonical append-only phase timeline;
* ``telemetry.json`` / ``summary.json`` — canonical run aggregates.

Reconstructing current *status* never uses this module: operators read
``status.json``. This module only summarises durable history for the closure
artifact and for an explicit ``reconstruct`` command.
"""

from __future__ import annotations

import platform
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from trajectory_os.assembly import model, store
from trajectory_os.observability import model as obs_model
from trajectory_os.observability import store as obs_store

#: Event kind -> the plan step id it evidences.
_KIND_TO_STEP: Mapping[str, str] = {
    "MISSION_CREATED": "intake",
    "PREFLIGHT_COMPLETED": "preflight",
    "PLAN_PERSISTED": "plan",
    "PATCH_CAPTURED": "implement",
    "VALIDATION_COMPLETED": "validate",
    "REVIEW_COMPLETED": "review",
    "HUMAN_GATE_REACHED": "human_gate",
    "MISSION_CLOSED": "closure",
}

#: Telemetry metrics carried into the compact closure summary.
_SUMMARY_METRICS = (
    "trial_count", "total_tokens", "cost_usd", "total_duration_ms",
    "review_rejects", "repairs", "validation_failures",
)


def build_closure(
    root: str | Path,
    mission: model.MissionDefinition,
    *,
    plan: model.MissionPlan | None,
    status: Mapping[str, Any],
    created_at: str,
) -> model.MissionClosure:
    """Build the durable closure from the canonical mission-root artifacts."""
    mission_root = store.mission_root(root, mission.mission_id)
    events = obs_store.load_events(mission_root)
    telemetry = _load_optional(obs_store.load_telemetry, mission_root)
    summary = _load_optional(obs_store.load_summary, mission_root)

    steps_executed = _steps_executed(events)
    validation_results = _validation_results(
        events, plan.expected_validation_gates if plan is not None else ())
    review_results = _review_results(events)
    attempts = _attempts(events, summary, status)
    repairs = max(0, attempts - 1) if attempts else 0

    readiness = str(status.get("readiness", obs_model.RD_UNKNOWN))
    lifecycle = str(status.get("state", obs_model.LC_RUNNING))
    artifacts = _artifacts(mission_root)
    closure = model.MissionClosure(
        mission_id=mission.mission_id,
        objective=mission.objective,
        definition_of_done=mission.definition_of_done,
        constraints=mission.constraints,
        baseline=mission.baseline.to_dict(),
        plan=(plan.to_dict() if plan is not None else None),
        steps_executed=steps_executed,
        attempts=attempts,
        repairs=repairs,
        validation_results=validation_results,
        review_results=review_results,
        current_patch=_opt_str(status.get("current_patch")),
        reviewed_patch=_opt_str(status.get("reviewed_patch")),
        telemetry_summary=_telemetry_summary(telemetry),
        lifecycle=lifecycle,
        readiness=readiness,
        terminal_reason=_opt_str(status.get("terminal_reason")),
        terminal_reason_code=_opt_str(status.get("terminal_reason_code")),
        next_action=str(status.get("next_action", "")),
        artifacts=artifacts,
        created_at=created_at,
    )
    return closure.validate()


def reconstruct_mission(root: str | Path,
                        mission_id: str) -> dict[str, Any]:
    """Reconstruct one mission from durable artifacts (fail closed)."""
    mission = store.load_mission(root, mission_id)
    mission_root = store.mission_root(root, mission_id)
    plan = (store.load_plan(root, mission_id)
            if store.plan_exists(root, mission_id) else None)
    status = _load_optional(obs_store.load_status, mission_root)
    telemetry = _load_optional(obs_store.load_telemetry, mission_root)
    summary = _load_optional(obs_store.load_summary, mission_root)
    closure = (store.load_closure(root, mission_id)
               if store.closure_exists(root, mission_id) else None)
    events = obs_store.load_events(mission_root)
    return {
        "schema_version": model.SCHEMA_VERSION,
        "assembly_version": model.ASSEMBLY_VERSION,
        "mission": mission.to_dict(),
        "plan": (plan.to_dict() if plan is not None else None),
        "status": status,
        "telemetry": telemetry,
        "summary": summary,
        "closure": (closure.to_dict() if closure is not None else None),
        "events": events,
        "counts": {"events": len(events)},
        "artifacts": _artifacts(mission_root),
        "machine": platform.node(),
    }


# --- helpers ------------------------------------------------------------------


def _load_optional(loader: Any, mission_root: Path) -> dict[str, Any] | None:
    try:
        document = loader(mission_root)
    except obs_store.CanonicalStoreError:
        return None
    return document if isinstance(document, dict) else None


def _steps_executed(events: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    seen: list[str] = []
    for event in events:
        kind = str(event.get("kind", ""))
        step = _KIND_TO_STEP.get(kind)
        if step is not None and step not in seen:
            seen.append(step)
        if (step in ("implement", "validate")
                and int(event.get("attempt", 0))
                and "repair" not in seen):
            seen.append("repair")
    return tuple(seen)


def _validation_results(
    events: Sequence[Mapping[str, Any]],
    commands: Sequence[str] = (),
) -> tuple[Mapping[str, Any], ...]:
    out: list[Mapping[str, Any]] = []
    for event in events:
        if event.get("kind") != "VALIDATION_COMPLETED":
            continue
        detail = event.get("detail")
        detail = detail if isinstance(detail, Mapping) else {}
        out.append({
            "attempt": int(event.get("attempt", 0)),
            "at": event.get("at"),
            "command": list(commands),
            "result": event.get("result"),
            "reason": detail.get("reason"),
            "exit_code": detail.get("exit_code"),
            "patch": event.get("patch"),
        })
    return tuple(out)


def _review_results(
    events: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    out: list[Mapping[str, Any]] = []
    for event in events:
        if event.get("kind") != "REVIEW_COMPLETED":
            continue
        detail = event.get("detail")
        detail = detail if isinstance(detail, Mapping) else {}
        out.append({
            "attempt": int(event.get("attempt", 0)),
            "at": event.get("at"),
            "result": event.get("result"),
            "outcome": detail.get("outcome"),
            "reason": detail.get("reason"),
            "reviewer": detail.get("reviewer"),
            "active": detail.get("active"),
            "patch": event.get("patch"),
        })
    return tuple(out)


def _attempts(events: Sequence[Mapping[str, Any]],
              summary: Mapping[str, Any] | None,
              status: Mapping[str, Any]) -> int:
    if summary is not None and isinstance(summary.get("attempts"), int):
        return int(summary["attempts"])
    event_attempts = [
        int(event.get("attempt", 0)) for event in events
        if event.get("attempt") is not None
        and event.get("kind") in ("PATCH_CAPTURED", "VALIDATION_COMPLETED",
                                  "REVIEW_COMPLETED")]
    if event_attempts:
        return max(event_attempts) + 1
    attempt = status.get("attempt")
    return int(attempt) + 1 if isinstance(attempt, int) else 0


def _telemetry_summary(
    telemetry: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    if telemetry is None:
        return None
    metrics = telemetry.get("metrics")
    metrics = metrics if isinstance(metrics, Mapping) else {}
    compact: dict[str, Any] = {}
    for name in _SUMMARY_METRICS:
        metric = metrics.get(name)
        if isinstance(metric, Mapping):
            compact[name] = {
                "value": metric.get("value"),
                "source": metric.get("source"),
            }
    return {
        "mode": telemetry.get("mode"),
        "metrics": compact,
        "waste": telemetry.get("waste"),
    }


def _artifacts(mission_root: Path) -> dict[str, str]:
    names = (
        store.MISSION_NAME, store.PLAN_NAME, obs_store.EVENTS_NAME,
        obs_store.STATUS_NAME, obs_store.TELEMETRY_NAME,
        obs_store.SUMMARY_NAME, store.CLOSURE_NAME,
    )
    artifacts: dict[str, str] = {}
    for name in names:
        path = mission_root / name
        if path.is_file() or name == store.CLOSURE_NAME:
            artifacts[name] = str(path)
    return artifacts


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


__all__ = ["build_closure", "reconstruct_mission"]
