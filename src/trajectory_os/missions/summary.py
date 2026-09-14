"""Mission 003 — read-only mission output (status, benchmark, summary).

Every human-readable or machine-readable output here is derived **only**
from the canonical persisted state (``mission.json`` + sub-run/event
evidence).  Nothing in this module mutates state, launches work, or
invents evidence — human output and machine output are therefore always
consistent because they share one representation.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from trajectory_os.missions import model, store
from trajectory_os.missions.store import MissionDoc


def _parse_iso(ts: str) -> datetime.datetime:
    """Parse the canonical timestamp; fail closed on non-canonical input."""
    try:
        dt = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError) as exc:
        raise store.MalformedMissionError(model.R_MALFORMED_STATE, "timestamp",
                                          repr(ts)) from exc
    return dt.replace(tzinfo=datetime.UTC)


def _gpu_gated(phase: store.PhaseDoc) -> bool:
    resources = phase.resources
    if resources is None:
        return False
    return resources.get("gpu") is True


def _phase_flags(phase: store.PhaseDoc) -> dict[str, Any]:
    return {
        "phase_id": phase.phase_id,
        "kind": phase.kind,
        "mode": phase.mode,
        "state": phase.state,
        "reason": phase.reason,
        "round": phase.round,
        "attempts": phase.attempt,
        "max_attempts": phase.max_attempts,
        "subruns": list(phase.subrun_ids),
        "gpu_gated": _gpu_gated(phase),
    }


def _subrun_flags(mission: MissionDoc,
                  paths: dict[str, Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for subrun_id in mission.subruns:
        record = store.load_subrun(paths, subrun_id)
        records.append({
            "subrun_id": record.subrun_id,
            "phase_id": record.phase_id,
            "attempt": record.attempt,
            "round": record.round,
            "exit_code": record.exit_code,
            "classification": record.classification,
            "provider_failure": record.provider_failure,
        })
    return records


def mission_summary(mission: MissionDoc,
                    paths: dict[str, Path]) -> dict[str, Any]:
    """Canonical benchmark/status record for one mission (pure derivation).

    Covers the Mission 003 benchmark fields: wall-clock runtime, phase and
    sub-run counts, jobs created/started/completed, max concurrency,
    GPU-gated phases, automatic retries/repairs, provider failures
    recovered/surfaced, human interventions, and final state/review state.
    """
    duration_seconds: int | None = None
    if mission.started_at is not None and mission.finished_at is not None:
        span = _parse_iso(mission.finished_at) - _parse_iso(mission.started_at)
        duration_seconds = max(0, int(span.total_seconds()))

    phases = [_phase_flags(phase) for phase in mission.phases]
    phase_states = {phase.phase_id: phase.state for phase in mission.phases}
    subruns = _subrun_flags(mission, paths)

    provider_recovered = sum(
        1 for sub in subruns
        if sub["provider_failure"] and phase_states[sub["phase_id"]] == model.PS_PASSED)
    provider_surfaced = sum(
        1 for sub in subruns
        if sub["provider_failure"] and phase_states[sub["phase_id"]] != model.PS_PASSED)
    automatic_retries = sum(max(0, len(phase.subrun_ids) - 1) for phase in mission.phases)

    review = next((flags for flags in reversed(phases)
                   if flags["kind"] == model.PH_REVIEW), None)
    return {
        "schema_version": mission.schema_version,
        "mission_id": mission.mission_id,
        "objective": mission.objective,
        "baseline_revision": mission.baseline_revision,
        "mission_state": mission.mission_state,
        "mission_reason": mission.mission_reason,
        "created_at": mission.created_at,
        "started_at": mission.started_at,
        "finished_at": mission.finished_at,
        "wall_clock": {
            "budget_seconds": mission.time_budget_s,
            "duration_seconds": duration_seconds,
            "deadline": mission.time_budget_deadline,
        },
        "phases": phases,
        "phases_total": len(phases),
        "phases_passed": sum(1 for flags in phases if flags["state"] == model.PS_PASSED),
        "jobs": {
            "created": len(mission.subruns),
            "started": mission.subrun_started,
            "completed": mission.subrun_completed,
            "budget": mission.subrun_budget,
        },
        "subruns": subruns,
        "max_concurrency": 1,  # bounded sequential design (single owner)
        "gpu_gated_phase_count": sum(1 for flags in phases if flags["gpu_gated"]),
        "automatic": {
            "retries": automatic_retries,
            "repairs_used": mission.repairs_used,
            "repair_budget": mission.repair_budget,
        },
        "provider_failures": {
            "recovered": provider_recovered,
            "surfaced": provider_surfaced,
        },
        "human_interventions": len(mission.human_notes),
        "final_state": mission.mission_state,
        "final_reason": mission.mission_reason,
        "final_review_state": None if review is None else review["state"],
        "final_review_reason": None if review is None else review["reason"],
        "event_log_size": len(store.load_events(paths)),
    }


def render_summary(summary: Mapping[str, Any]) -> str:
    """Human-readable rendering derived from the *same* machine summary.

    Fails closed (``MalformedMissionError``) on a malformed payload instead
    of rendering partial/ambiguous data.
    """
    if not isinstance(summary, Mapping):  # pragma: no cover - strict guard
        raise store.MalformedMissionError(model.R_MALFORMED_STATE, "summary",
                                          "mapping required")
    required = ("mission_id", "mission_state", "mission_reason", "jobs",
                "phases", "automatic", "provider_failures")
    missing = [key for key in required if key not in summary]
    if missing:
        raise store.MalformedMissionError(
            model.R_MALFORMED_STATE, "summary", f"missing {sorted(missing)}")

    lines: list[str] = [
        f"mission   : {summary['mission_id']}",
        f"objective : {summary.get('objective', '')}",
        f"state     : {summary['mission_state']} ({summary['mission_reason']})",
    ]
    jobs = summary["jobs"]
    if isinstance(jobs, Mapping):
        lines.append(f"subruns   : {jobs['completed']}/{jobs['created']} started "
                     f"(budget {jobs['budget']})")
    automatic = summary["automatic"]
    if isinstance(automatic, Mapping):
        lines.append(f"automatic : retries={automatic['retries']} "
                     f"repairs={automatic['repairs_used']}/{automatic['repair_budget']}")
    provider = summary["provider_failures"]
    if isinstance(provider, Mapping):
        lines.append(f"provider  : recovered={provider['recovered']} "
                     f"surfaced={provider['surfaced']}")
    lines.append(f"human     : interventions={summary['human_interventions']}")
    phases = summary["phases"]
    if isinstance(phases, list):
        for flags in phases:
            if not isinstance(flags, Mapping):
                raise store.MalformedMissionError(
                    model.R_MALFORMED_STATE, "summary", "phase entry")
            lines.append(f"  - {flags['phase_id']}: {flags['state']} "
                         f"(attempts={flags['attempts']}, {flags['reason']})")
    return "\n".join(lines)


def mission_status(root: str, mission_id: str) -> dict[str, Any]:
    """Operator 'mission status': machine summary + human rendering, both
    derived from the canonical persisted state (single source of truth)."""
    mission, paths = store.load_mission(root, mission_id)
    summary = mission_summary(mission, paths)
    return {
        "status": "OK",
        "summary": summary,
        "rendered": render_summary(summary),
    }
