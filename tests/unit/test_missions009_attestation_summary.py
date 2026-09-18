"""Mission 009 — exact-attestation projection in the mission summary.

The canonical mission summary/status projection must let an operator
distinguish, deterministically and from persisted evidence only:

* exact attestation proven (``VERIFIED``) + bounded identity;
* missing/rejected/unproven attestation (``UNPROVEN`` + stable code);
* legacy/unattested evidence (``LEGACY``).

Everything is derived from the canonical durable mission + sub-run
records (and the existing per-sub-run semantic evidence for identity).
No new source of truth, no inference from process exit codes, and the
M008 fail-closed record validation is preserved (a legacy record still
loads; an unattested ``COMPLETED`` is still rejected by the store).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trajectory_os.missions import model, semantic, store, summary
from trajectory_os.missions.orchestrator import (
    MissionConfig,
    create_mission,
    default_phase_specs,
)

MID = "m009"
_SHA = "a" * 64
_HEAD = "b" * 40


def _commands() -> dict[str, tuple[str, ...]]:
    return {kind: ("true",) for kind in model.CANONICAL_SEQUENCE}


def _record(subrun_id: str, phase_id: str = "implement", **over: Any) -> store.SubrunDoc:
    base: dict[str, Any] = {
        "subrun_id": subrun_id,
        "phase_id": phase_id,
        "kind": model.PH_IMPLEMENT,
        "mode": "IMPLEMENT",
        "round": 0,
        "attempt": 1,
        "command": ["true"],
        "cwd": None,
        "started_at": "2026-09-17T00:00:00Z",
        "finished_at": "2026-09-17T00:00:01Z",
        "exit_code": 0,
        "classification": model.CR_UNPROVEN,
        "stdout_file": "/tmp/does-not-exist.stdout.log",
        "stderr_file": "/tmp/does-not-exist.stderr.log",
        "resources": None,
        "semantic_status": semantic.STATUS_SUCCESS,
        "semantic_error": None,
        "semantic_agent_classification": "TEST_PRODUCER",
        "semantic_readiness": None,
        "semantic_reason": "test",
        "semantic_aware": True,
        "attestation": None,
        "attestation_error": None,
    }
    base.update(over)
    return store.SubrunDoc(**base)  # type: ignore[arg-type]


def _make_mission(tmp_path: Path,
                  records: list[store.SubrunDoc]) -> tuple[store.MissionDoc,
                                                           dict[str, Path]]:
    """Create a canonical mission then persist the supplied exact records."""
    root = str(tmp_path / "root")
    create_mission(root, MissionConfig(
        mission_id=MID,
        objective="attestation projection test",
        phase_specs=default_phase_specs(_commands()),
    ))
    mission, paths = store.load_mission(root, MID)
    for record in records:
        record.stdout_file = str(
            paths["subruns"] / f"{record.subrun_id}.stdout.log")
        record.stderr_file = str(
            paths["subruns"] / f"{record.subrun_id}.stderr.log")
        store.save_subrun(record, paths)
        if record.subrun_id not in mission.subruns:
            mission.subruns.append(record.subrun_id)
        mission.phase(record.phase_id).subrun_ids.append(record.subrun_id)
    store.save_mission(mission, paths)
    return store.load_mission(root, MID)


def _write_semantic_evidence(record: store.SubrunDoc, *,
                             run_id: str = "run-implement-a1",
                             head: str = _HEAD,
                             patch_sha: str = _SHA) -> None:
    """Persist a canonical semantic doc with a complete attestation."""
    evidence = (Path(record.stdout_file).parent
                / f"{record.subrun_id}.semantic.json")
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps({
        "schema_version": 1,
        "subrun_id": record.subrun_id,
        "status": semantic.STATUS_SUCCESS,
        "attestation": {
            "schema_version": 1,
            "subrun_id": record.subrun_id,
            "run_id": run_id,
            "repo_head_before": head,
            "repo_head_after": head,
            "patch_sha256": patch_sha,
        },
    }), encoding="utf-8")


def _model_heavy_subrun(s: dict[str, Any]) -> dict[str, Any]:
    return next(sub for sub in s["model_heavy"]["subruns"]
                if sub["subrun_id"] == "implement-a1")


def test_summary_projects_verified_attestation_with_bounded_identity(
        tmp_path: Path) -> None:
    mission, paths = _make_mission(tmp_path, [_record(
        "implement-a1",
        classification=model.CR_COMPLETED,
        attestation=semantic.ATTESTATION_VERIFIED,
    )])
    record = store.load_subrun(paths, "implement-a1")
    _write_semantic_evidence(record, run_id="run-a1", head=_HEAD,
                             patch_sha=_SHA)

    s = summary.mission_summary(mission, paths)
    assert s["attestation"] == {
        "model_heavy_subruns": 1,
        "verified": 1,
        "unproven": 0,
        "legacy": 0,
    }
    sub = _model_heavy_subrun(s)
    assert sub["attestation"] == summary.ATT_VERIFIED
    assert sub["attestation_error"] is None
    assert sub["attestation_identity"] == {
        "run_id": "run-a1",
        "repo_head": _HEAD,
        "patch_sha256": _SHA,
    }
    rendered = summary.render_summary(s)
    assert "attestation : verified=1 unproven=0 legacy=0" in rendered


def test_summary_omits_identity_when_evidence_is_unavailable(
        tmp_path: Path) -> None:
    """A proven outcome stays visible even if the evidence file is gone."""
    mission, paths = _make_mission(tmp_path, [_record(
        "implement-a1",
        classification=model.CR_COMPLETED,
        attestation=semantic.ATTESTATION_VERIFIED,
    )])
    s = summary.mission_summary(mission, paths)
    sub = _model_heavy_subrun(s)
    assert sub["attestation"] == summary.ATT_VERIFIED
    assert sub["attestation_identity"] is None


@pytest.mark.parametrize("code", [
    semantic.ATT_MISSING,
    semantic.ATT_MISMATCH,
    semantic.ATT_PARTIAL,
    semantic.ATT_STALE,
    semantic.ATT_CONTRADICTORY,
])
def test_summary_projects_rejected_or_missing_attestation_as_unproven(
        tmp_path: Path, code: str) -> None:
    mission, paths = _make_mission(tmp_path, [_record(
        "implement-a1",
        classification=model.CR_UNPROVEN,
        attestation=None,
        attestation_error=code,
    )])
    s = summary.mission_summary(mission, paths)
    assert s["attestation"] == {
        "model_heavy_subruns": 1,
        "verified": 0,
        "unproven": 1,
        "legacy": 0,
    }
    sub = _model_heavy_subrun(s)
    assert sub["attestation"] == summary.ATT_UNPROVEN
    assert sub["attestation_error"] == code
    assert sub["attestation_identity"] is None


def test_summary_projects_legacy_evidence_as_legacy(
        tmp_path: Path) -> None:
    """A legacy M007 record (no M008 outcome) is never read as verified."""
    legacy = store.SubrunDoc(
        subrun_id="implement-a1",
        phase_id="implement",
        kind=model.PH_IMPLEMENT,
        mode="IMPLEMENT",
        round=0,
        attempt=1,
        command=["true"],
        cwd=None,
        started_at="2026-09-17T00:00:00Z",
        finished_at="2026-09-17T00:00:01Z",
        exit_code=0,
        classification=model.CR_COMPLETED,
        stdout_file="/tmp/legacy.stdout.log",
        stderr_file="/tmp/legacy.stderr.log",
        resources=None,
        semantic_status=semantic.STATUS_SUCCESS,
        semantic_error=None,
        semantic_agent_classification="LEGACY_PRODUCER",
        semantic_readiness=None,
        semantic_reason="legacy",
        semantic_aware=True,
    )
    mission, paths = _make_mission(tmp_path, [legacy])
    s = summary.mission_summary(mission, paths)
    assert s["attestation"] == {
        "model_heavy_subruns": 1,
        "verified": 0,
        "unproven": 0,
        "legacy": 1,
    }
    sub = _model_heavy_subrun(s)
    assert sub["attestation"] == summary.ATT_LEGACY
    assert sub["attestation_error"] is None
    assert sub["attestation_identity"] is None


def test_store_still_fails_closed_on_unattested_completed(
        tmp_path: Path) -> None:
    """M008 fail-closed record validation is untouched by the projection."""
    paths = store.mission_paths(str(tmp_path), MID)
    bad = _record(
        "implement-a1",
        classification=model.CR_COMPLETED,
        attestation=None,
        attestation_error=semantic.ATT_MISSING,
    ).to_dict()
    with pytest.raises(store.MalformedMissionError):
        store.SubrunDoc.from_dict(bad, str(paths["root"] / "bad.json"))
