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

from trajectory_os.missions import identity, model, semantic, store
from trajectory_os.missions.store import MissionDoc

# --- Mission 009: exact-execution-attestation projection -------------------
# The three deterministic operator-visible states, derived ONLY from the
# canonical persisted sub-run evidence (never re-derived, never inferred
# from an exit code):
#
#   VERIFIED  the runner recorded an independently verified exact
#             execution attestation for this exact sub-run;
#   UNPROVEN  an M008 attestation outcome was recorded but is NOT verified
#             (missing/malformed/partial/contradictory/mismatched/stale) —
#             the stable fail-closed code is preserved for the operator;
#   LEGACY    no M008 outcome was ever recorded (legacy M007/pre-M008
#             evidence) — no verification claim is made.
ATT_VERIFIED = "VERIFIED"
ATT_UNPROVEN = "UNPROVEN"
ATT_LEGACY = "LEGACY"


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


def _attestation_identity(record: store.SubrunDoc) -> dict[str, str] | None:
    """Bounded identity for a recorded VERIFIED attestation (or ``None``).

    The identity values (wrapper run id, repository HEAD, exact patch
    digest) are read from the canonical per-sub-run semantic evidence the
    runner already verified and persisted alongside the sub-run. This
    projection never re-derives them, never invents values, and omits the
    identity entirely when that evidence is unavailable or no longer
    carries a complete attestation bound to this exact sub-run.
    """
    evidence = (Path(record.stdout_file).parent
                / f"{record.subrun_id}.semantic.json")
    if not evidence.is_file():
        return None
    doc, error = semantic.read_semantic_file(str(evidence))
    if error is not None or doc is None:
        return None
    att = semantic.extract_attestation(doc)
    if semantic.validate_attestation(att) is not None:
        return None
    assert isinstance(att, dict)
    try:
        semantic.validate_attestation_binding(att, record.subrun_id)
    except semantic.SemanticError:
        return None

    run_id = semantic.attestation_field(att, "run_id")
    head_before = semantic.attestation_field(att, "repo_head_before")
    head_after = semantic.attestation_field(att, "repo_head_after")
    patch_sha = semantic.attestation_field(att, "patch_sha256")
    identity: dict[str, str] = {}
    if run_id:
        identity["run_id"] = run_id
    if head_before and head_after and head_before == head_after:
        identity["repo_head"] = head_after
    if patch_sha:
        identity["patch_sha256"] = patch_sha
    return identity or None


def _attestation_view(record: store.SubrunDoc) -> dict[str, Any]:
    """Deterministic M008 attestation projection for one sub-run record.

    Derived ONLY from the persisted attestation outcome pair on the
    canonical sub-run record (single source of truth). ``identity`` is
    attached only for proven attestations and only where the canonical
    evidence is still readable.
    """
    if record.attestation == semantic.ATTESTATION_VERIFIED:
        return {
            "status": ATT_VERIFIED,
            "error": None,
            "identity": _attestation_identity(record),
        }
    if record.attestation_error is not None:
        return {
            "status": ATT_UNPROVEN,
            "error": record.attestation_error,
            "identity": None,
        }
    return {"status": ATT_LEGACY, "error": None, "identity": None}


def _subrun_flags(mission: MissionDoc,
                  paths: dict[str, Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for subrun_id in mission.subruns:
        record = store.load_subrun(paths, subrun_id)
        attestation = _attestation_view(record)
        records.append({
            "subrun_id": record.subrun_id,
            "phase_id": record.phase_id,
            "attempt": record.attempt,
            "round": record.round,
            "exit_code": record.exit_code,
            "classification": record.classification,
            "provider_failure": record.provider_failure,
            "semantic_require_changes": record.semantic_require_changes,
            # Mission 010: only model-heavy sub-runs carry the wrapper
            # snapshot digest; label its domain explicitly (None for
            # deterministic sub-runs, which have no patch identity).
            "patch_identity_domain": (
                identity.WRAPPER_SNAPSHOT_DOMAIN
                if record.kind in model.MODEL_HEAVY_KINDS else None),

            "attestation": attestation["status"],
            "attestation_error": attestation["error"],
            "attestation_identity": attestation["identity"],
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

    # Mission 004 (deterministic, kind-based): model-heavy vs deterministic
    # accounting + reconstruction/resume lifecycle events (derived from the
    # canonical event log — the same evidence the CLI and harness use).
    kind_by_phase = {phase.phase_id: phase.kind for phase in mission.phases}
    model_heavy_subruns = [
        sub for sub in subruns
        if kind_by_phase.get(sub["phase_id"])
        and model.is_model_heavy(kind_by_phase[sub["phase_id"]])
    ]
    events = store.load_events(paths)
    reconstruction_events = sum(1 for e in events if e.get("event") == "reconstructed")
    resume_events = sum(1 for e in events if e.get("event") == "resumed")
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
        "model_heavy": {
            "phase_count": sum(
                1 for flags in phases if model.is_model_heavy(flags["kind"])),
            "subrun_count": len(model_heavy_subruns),
            "subruns": [
                {"subrun_id": sub["subrun_id"], "phase_id": sub["phase_id"],
                 "classification": sub["classification"],
                 "semantic_require_changes": sub["semantic_require_changes"],
                 "patch_identity_domain": sub["patch_identity_domain"],
                 "attestation": sub["attestation"],
                 "attestation_error": sub["attestation_error"],
                 "attestation_identity": sub["attestation_identity"]}
                for sub in model_heavy_subruns
            ],
        },
        # Mission 010: expose both patch identity domains distinctly. The
        # two digests are independent and MUST never be compared for
        # equality; the domain id + authoritative field make that explicit
        # in operator-visible output.
        "patch_identity": {
            "wrapper_snapshot": dict(identity.DOMAIN_DESCRIPTORS[0]),
            "mission_worktree": dict(identity.DOMAIN_DESCRIPTORS[1]),
        },
        # Mission 009: exact-execution-attestation projection over the
        # model-heavy sub-runs (the only sub-runs that carry an M008
        # outcome). Counts are derived from the same canonical records.
        "attestation": {
            "model_heavy_subruns": len(model_heavy_subruns),
            "verified": sum(
                1 for sub in model_heavy_subruns
                if sub["attestation"] == ATT_VERIFIED),
            "unproven": sum(
                1 for sub in model_heavy_subruns
                if sub["attestation"] == ATT_UNPROVEN),
            "legacy": sum(
                1 for sub in model_heavy_subruns
                if sub["attestation"] == ATT_LEGACY),
        },
        "lifecycle_events": {
            "reconstruction": reconstruction_events,
            "resume": resume_events,
        },
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
    model_heavy = summary.get("model_heavy")
    if isinstance(model_heavy, Mapping):
        lines.append(
            f"model     : heavy_phases={model_heavy['phase_count']} "
            f"heavy_subruns={model_heavy['subrun_count']}")
    lifecycle = summary.get("lifecycle_events")
    if isinstance(lifecycle, Mapping):
        lines.append(
            f"lifecycle : reconstruction={lifecycle['reconstruction']} "
            f"resume={lifecycle['resume']}")
    attestation = summary.get("attestation")
    if isinstance(attestation, Mapping):
        lines.append(
            f"attestation : verified={attestation['verified']} "
            f"unproven={attestation['unproven']} "
            f"legacy={attestation['legacy']}")
    patch_identity = summary.get("patch_identity")
    if isinstance(patch_identity, Mapping):
        wrapper = patch_identity.get("wrapper_snapshot")
        mission_worktree = patch_identity.get("mission_worktree")
        if isinstance(wrapper, Mapping) and isinstance(mission_worktree, Mapping):
            lines.append(
                f"patch-id  : wrapper_snapshot={wrapper['domain']} "
                f"({wrapper['field']}) | mission_worktree="
                f"{mission_worktree['domain']} ({mission_worktree['field']}) "
                "[independent domains; never compared]")
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
