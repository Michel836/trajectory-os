"""Mission 010 — writable semantic promotion contract & patch identity domains.

Covers:

* the mandatory writable-mode promotion contract (green readiness promotes,
  known non-green readiness fails closed, unrecognized readiness is
  UNKNOWN, absent readiness stays legacy-compatible);
* the defense-in-depth consumer rule at classification time and on the
  durable record read path;
* the optional ``require_changes`` provenance (producer emission,
  validation, persistence round-trip, malformed values fail closed);
* the two independent patch identity domains and their operator-visible
  labels;
* that the M008 attestation algorithms/shape checks and the M009
  attestation projection remain unchanged.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from trajectory_os.missions import (
    identity,
    model,
    runner,
    semantic,
    store,
    summary,
)
from trajectory_os.missions.orchestrator import (
    MissionConfig,
    create_mission,
    default_phase_specs,
    worktree_identity,
)

MID = "m010"
_SHA_A = "a" * 64
_HEAD_B = "b" * 40


# ---------------------------------------------------------------------------
# 1-5, 9: the writable-mode promotion contract (classification, pure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("readiness", ["READY_FOR_COMMIT", "READY_FOR_REVIEW"])
def test_writable_success_green_readiness_completes(readiness: str) -> None:
    result = runner.classify_subrun(
        0,
        semantic_status=semantic.STATUS_SUCCESS,
        semantic_agent_classification=semantic.AGENT_COMPLETED,
        semantic_readiness=readiness,
        semantic_mode="IMPLEMENT",
        attestation=semantic.ATTESTATION_VERIFIED,
    )
    assert result.classification == model.CR_COMPLETED


@pytest.mark.parametrize("readiness", ["NEEDS_REVIEW", "BLOCKED"])
def test_writable_success_non_green_readiness_fails_closed(readiness: str) -> None:
    result = runner.classify_subrun(
        0,
        semantic_status=semantic.STATUS_SUCCESS,
        semantic_agent_classification=semantic.AGENT_COMPLETED,
        semantic_readiness=readiness,
        semantic_mode="IMPLEMENT",
        attestation=semantic.ATTESTATION_VERIFIED,
    )
    assert result.classification == model.CR_FAILED
    assert result.classification != model.CR_COMPLETED


def test_writable_success_unrecognized_readiness_is_unproven() -> None:
    result = runner.classify_subrun(
        0,
        semantic_status=semantic.STATUS_SUCCESS,
        semantic_agent_classification=semantic.AGENT_COMPLETED,
        semantic_readiness="NOT_GREEN",
        semantic_mode="IMPLEMENT",
        attestation=semantic.ATTESTATION_VERIFIED,
    )
    assert result.classification == model.CR_UNPROVEN


def test_writable_success_absent_readiness_is_legacy_compatible() -> None:
    """A pre-Mission-010 record has no readiness claim and stays readable."""
    result = runner.classify_subrun(
        0,
        semantic_status=semantic.STATUS_SUCCESS,
        semantic_agent_classification="TEST_PRODUCER",
        semantic_readiness=None,
        semantic_mode="IMPLEMENT",
        attestation=semantic.ATTESTATION_VERIFIED,
    )
    assert result.classification == model.CR_COMPLETED


@pytest.mark.parametrize("readiness", ["NEEDS_REVIEW", "BLOCKED"])
def test_non_writable_mode_known_non_green_readiness_still_fails_closed(
        readiness: str) -> None:
    """Defense-in-depth applies to every mode, not only writable ones."""
    result = runner.classify_subrun(
        0,
        semantic_status=semantic.STATUS_SUCCESS,
        semantic_readiness=readiness,
        semantic_mode="REVIEW",
        attestation=semantic.ATTESTATION_VERIFIED,
    )
    assert result.classification == model.CR_FAILED


def test_non_success_status_is_never_upgraded_by_the_contract() -> None:
    result = runner.classify_subrun(
        0,
        semantic_status=semantic.STATUS_INCOMPLETE,
        semantic_readiness="READY_FOR_COMMIT",
        semantic_mode="IMPLEMENT",
        attestation=semantic.ATTESTATION_VERIFIED,
    )
    assert result.classification == model.CR_UNPROVEN


def test_pure_promotion_helper_documents_the_contract() -> None:
    assert semantic.promote_writable_status(
        "IMPLEMENT", semantic.STATUS_SUCCESS, semantic.AGENT_COMPLETED,
        "READY_FOR_COMMIT") == semantic.STATUS_SUCCESS
    assert semantic.promote_writable_status(
        "IMPLEMENT", semantic.STATUS_SUCCESS, semantic.AGENT_COMPLETED,
        "NEEDS_REVIEW") == semantic.STATUS_FAILED
    assert semantic.promote_writable_status(
        "IMPLEMENT", semantic.STATUS_SUCCESS, semantic.AGENT_COMPLETED,
        None) == semantic.STATUS_UNKNOWN
    # Non-writable / non-success inputs are returned unchanged.
    assert semantic.promote_writable_status(
        "REVIEW", semantic.STATUS_SUCCESS, semantic.AGENT_COMPLETED,
        "NEEDS_REVIEW") == semantic.STATUS_SUCCESS
    assert semantic.promote_writable_status(
        "IMPLEMENT", semantic.STATUS_FAILED, semantic.AGENT_COMPLETED,
        "NEEDS_REVIEW") == semantic.STATUS_FAILED


# ---------------------------------------------------------------------------
# 9: end-to-end runner path — a green-looking doc with a non-green readiness
# cannot be promoted even with a verified attestation.
# ---------------------------------------------------------------------------

_PRODUCER = r'''
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
repo = Path(os.getcwd()).resolve()
head = subprocess.run(
    ["git", "-C", str(repo), "rev-parse", "HEAD"],
    capture_output=True, text=True, check=True).stdout.strip()
subrun = os.environ["TRAJECTORY_SUBRUN_ID"]
run_id = "run-" + subrun
run_dir = repo / ".trajectory-pi" / "runs" / run_id
run_dir.mkdir(parents=True, exist_ok=True)
(run_dir / "meta.txt").write_text(
    "run_id=" + run_id + "\n"
    "workspace=" + str(repo) + "\n"
    "head_before=" + head + "\n",
    encoding="utf-8")
patch = b""
(run_dir / "worktree.patch").write_bytes(patch)
doc = {
    "schema_version": 1,
    "subrun_id": subrun,
    "status": cfg.get("status", "SUCCESS"),
    "agent_classification": "AGENT_COMPLETED",
    "readiness": cfg.get("readiness", "READY_FOR_COMMIT"),
    "require_changes": cfg.get("require_changes", "NOT_REQUIRED"),
    "reason": "producer",
    "attestation": {
        "schema_version": 1,
        "subrun_id": subrun,
        "run_id": run_id,
        "repo_head_before": head,
        "repo_head_after": head,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
    },
}
Path(os.environ["TRAJECTORY_SUBRUN_RESULT_FILE"]).write_text(
    json.dumps(doc), encoding="utf-8")
'''


def _git_repo(tmp_path: Path) -> str:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t.t",
         "-c", "user.name=t", "commit", "-q", "--allow-empty",
         "-m", "baseline"],
        check=True,
    )
    return str(repo)


def _producer(tmp_path: Path, **cfg: Any) -> tuple[str, ...]:
    script = tmp_path / "producer.py"
    script.write_text(_PRODUCER, encoding="utf-8")
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    return ("python3", str(script), str(cfg_path))


def _req(tmp: Path, command: tuple[str, ...], cwd: str,
         mode: str = "IMPLEMENT") -> runner.SubrunRequest:
    base = tmp / "ev"
    return runner.SubrunRequest(
        mission_id=MID,
        subrun_id="implement-a1",
        phase_id="implement",
        kind="IMPLEMENT",
        mode=mode,
        round=0,
        attempt=1,
        command=command,
        cwd=cwd,
        timeout_s=30,
        stdout_file=f"{base}/implement-a1.stdout.log",
        stderr_file=f"{base}/implement-a1.stderr.log",
        resources=None,
        semantic_required=True,
    )


def test_e2e_writable_non_green_readiness_cannot_promote(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    req = _req(tmp_path, _producer(tmp_path, readiness="NEEDS_REVIEW"), repo)
    result = runner.ProcessPhaseRunner().run(req)
    assert result.semantic_status == semantic.STATUS_SUCCESS
    assert result.attestation == semantic.ATTESTATION_VERIFIED
    assert result.classification == model.CR_FAILED


def test_e2e_writable_green_readiness_promotes(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    req = _req(tmp_path, _producer(tmp_path, readiness="READY_FOR_COMMIT"), repo)
    result = runner.ProcessPhaseRunner().run(req)
    assert result.classification == model.CR_COMPLETED
    assert result.semantic_require_changes == "NOT_REQUIRED"


# ---------------------------------------------------------------------------
# 10: malformed require_changes values fail closed
# ---------------------------------------------------------------------------


def test_malformed_require_changes_fails_closed(tmp_path: Path) -> None:
    base = {
        "schema_version": 1,
        "subrun_id": "s1",
        "status": "SUCCESS",
        "require_changes": "MAYBE",
    }
    with pytest.raises(semantic.SemanticError) as excinfo:
        semantic.validate_semantic(base)
    assert excinfo.value.code == "REQUIRE_CHANGES_INVALID"
    assert semantic.interpret_semantic(base, "s1") == (
        None, "REQUIRE_CHANGES_INVALID")

    for bad in (5, None, "", "SATISFIED ", "unsatisfied"):
        doc = dict(base, require_changes=bad)
        status, error = semantic.interpret_semantic(doc, "s1")
        assert status is None
        assert error == "REQUIRE_CHANGES_INVALID"


def test_valid_require_changes_values_accepted() -> None:
    for value in sorted(semantic.REQUIRE_CHANGES_VALUES):
        doc = {"schema_version": 1, "subrun_id": "s1", "status": "SUCCESS",
               "require_changes": value}
        assert semantic.interpret_semantic(doc, "s1") == ("SUCCESS", None)
    # Absent stays absent (legacy readable).
    legacy = {"schema_version": 1, "subrun_id": "s1", "status": "SUCCESS"}
    assert semantic.interpret_semantic(legacy, "s1") == ("SUCCESS", None)


# ---------------------------------------------------------------------------
# 11: durable round-trip for semantic_require_changes
# ---------------------------------------------------------------------------


def _record(**over: Any) -> store.SubrunDoc:
    base: dict[str, Any] = {
        "subrun_id": "implement-a1",
        "phase_id": "implement",
        "kind": model.PH_IMPLEMENT,
        "mode": "IMPLEMENT",
        "round": 0,
        "attempt": 1,
        "command": ["true"],
        "cwd": None,
        "started_at": "2026-09-18T00:00:00Z",
        "finished_at": "2026-09-18T00:00:01Z",
        "exit_code": 0,
        "classification": model.CR_FAILED,
        "stdout_file": "stdout.log",
        "stderr_file": "stderr.log",
        "resources": None,
        "semantic_status": semantic.STATUS_SUCCESS,
        "semantic_error": None,
        "semantic_agent_classification": semantic.AGENT_COMPLETED,
        "semantic_readiness": "NEEDS_REVIEW",
        "semantic_reason": "unsatisfied",
        "semantic_require_changes": semantic.REQUIRE_CHANGES_UNSATISFIED,
        "semantic_aware": True,
    }
    base.update(over)
    return store.SubrunDoc(**base)  # type: ignore[arg-type]


def test_subrun_doc_round_trips_semantic_require_changes() -> None:
    doc = _record().to_dict()
    assert doc["semantic_require_changes"] == "UNSATISFIED"
    loaded = store.SubrunDoc.from_dict(doc, "implement-a1.json")
    assert loaded.semantic_require_changes == "UNSATISFIED"

    for value in sorted(semantic.REQUIRE_CHANGES_VALUES):
        loaded = store.SubrunDoc.from_dict(
            _record(semantic_require_changes=value).to_dict(), "x.json")
        assert loaded.semantic_require_changes == value


def test_legacy_require_changes_shape_remains_absent() -> None:
    """A pre-Mission-010 aware record keeps its exact old shape."""
    doc = _record(semantic_require_changes=None).to_dict()
    assert "semantic_require_changes" not in doc
    loaded = store.SubrunDoc.from_dict(doc, "legacy.json")
    assert loaded.semantic_require_changes is None


def test_malformed_persisted_require_changes_fails_closed() -> None:
    doc = _record().to_dict()
    doc["semantic_require_changes"] = "MAYBE"
    with pytest.raises(store.MalformedMissionError) as excinfo:
        store.SubrunDoc.from_dict(doc, "bad.json")
    assert excinfo.value.code == model.R_MALFORMED_STATE


def test_require_changes_without_semantic_evidence_fails_closed() -> None:
    doc = _record().to_dict()
    for key in ("semantic_status", "semantic_error",
                "semantic_agent_classification", "semantic_readiness",
                "semantic_reason"):
        del doc[key]
    with pytest.raises(store.MalformedMissionError) as excinfo:
        store.SubrunDoc.from_dict(doc, "bad.json")
    assert excinfo.value.code == model.R_MALFORMED_STATE


# ---------------------------------------------------------------------------
# Defense-in-depth on the durable read path
# ---------------------------------------------------------------------------


def test_persisted_completed_with_non_green_readiness_fails_closed() -> None:
    for readiness in ("NEEDS_REVIEW", "BLOCKED"):
        doc = _record(
            classification=model.CR_COMPLETED,
            semantic_readiness=readiness,
            semantic_require_changes=None,
        ).to_dict()
        with pytest.raises(store.MalformedMissionError) as excinfo:
            store.SubrunDoc.from_dict(doc, "contradiction.json")
        assert excinfo.value.code == model.R_CONTRADICTION
    # Green readiness stays valid.
    green = _record(
        classification=model.CR_COMPLETED,
        semantic_readiness="READY_FOR_COMMIT",
        semantic_require_changes=None,
        attestation=semantic.ATTESTATION_VERIFIED,
        attestation_error=None,
    ).to_dict()
    assert store.SubrunDoc.from_dict(green, "ok.json").classification \
        == model.CR_COMPLETED


def test_legacy_absent_readiness_completed_stays_readable() -> None:
    doc = _record(
        classification=model.CR_COMPLETED,
        semantic_readiness=None,
        semantic_require_changes=None,
        attestation=semantic.ATTESTATION_VERIFIED,
        attestation_error=None,
    ).to_dict()
    loaded = store.SubrunDoc.from_dict(doc, "legacy.json")
    assert loaded.classification == model.CR_COMPLETED
    assert loaded.semantic_readiness is None


# ---------------------------------------------------------------------------
# 12: both patch identity domains exposed distinctly
# ---------------------------------------------------------------------------


def test_patch_identity_domains_are_distinct() -> None:
    assert identity.WRAPPER_SNAPSHOT_DOMAIN == "trajectory-pi.worktree.snapshot.v1"
    assert identity.WRAPPER_SNAPSHOT_FIELD == "attestation.patch_sha256"
    assert identity.MISSION_WORKTREE_DOMAIN == "mission.worktree.diff.v1"
    assert identity.MISSION_WORKTREE_FIELD == "worktree.worktree_patch_sha256"
    assert identity.WRAPPER_SNAPSHOT_DOMAIN != identity.MISSION_WORKTREE_DOMAIN
    assert identity.WRAPPER_SNAPSHOT_FIELD != identity.MISSION_WORKTREE_FIELD
    assert set(identity.DOMAIN_IDS) == {
        identity.WRAPPER_SNAPSHOT_DOMAIN, identity.MISSION_WORKTREE_DOMAIN}


def test_worktree_identity_carries_mission_domain(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    (Path(repo) / "file.txt").write_text("x\n", encoding="utf-8")
    worktree = worktree_identity(repo)
    assert worktree["domain"] == identity.MISSION_WORKTREE_DOMAIN
    assert worktree["field"] == identity.MISSION_WORKTREE_FIELD
    assert worktree["worktree_patch_sha256"] == hashlib.sha256(
        subprocess.run(["git", "-C", repo, "diff", "HEAD"],
                       check=True, capture_output=True).stdout).hexdigest()


def _make_mission(tmp_path: Path,
                  records: list[store.SubrunDoc]) -> tuple[store.MissionDoc,
                                                           dict[str, Path]]:
    root = str(tmp_path / "root")
    commands = {kind: ("true",) for kind in model.CANONICAL_SEQUENCE}
    create_mission(root, MissionConfig(
        mission_id=MID,
        objective="mission 010 identity/summary test",
        phase_specs=default_phase_specs(commands),
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


def test_summary_exposes_both_patch_domains_and_labels(tmp_path: Path) -> None:
    mission, paths = _make_mission(tmp_path, [_record(
        classification=model.CR_UNPROVEN,
        semantic_readiness="NEEDS_REVIEW",
        semantic_require_changes=semantic.REQUIRE_CHANGES_UNSATISFIED,
        attestation=semantic.ATTESTATION_VERIFIED,
        attestation_error=None,
    )])
    doc = summary.mission_summary(mission, paths)
    patch_identity = doc["patch_identity"]
    assert patch_identity["wrapper_snapshot"]["domain"] \
        == identity.WRAPPER_SNAPSHOT_DOMAIN
    assert patch_identity["mission_worktree"]["domain"] \
        == identity.MISSION_WORKTREE_DOMAIN
    heavy = doc["model_heavy"]["subruns"][0]
    assert heavy["patch_identity_domain"] == identity.WRAPPER_SNAPSHOT_DOMAIN
    assert heavy["semantic_require_changes"] == "UNSATISFIED"
    rendered = summary.render_summary(doc)
    assert identity.WRAPPER_SNAPSHOT_DOMAIN in rendered
    assert identity.MISSION_WORKTREE_DOMAIN in rendered


# ---------------------------------------------------------------------------
# 13: patch hashing algorithms remain unchanged
# ---------------------------------------------------------------------------


def test_patch_hashing_algorithms_are_unchanged(tmp_path: Path) -> None:
    # The wrapper snapshot digest remains SHA-256 over the exact bytes.
    payload = b"mission-010-algorithm-proof\n"
    path = tmp_path / "worktree.patch"
    path.write_bytes(payload)
    assert runner._sha256_file(path) == hashlib.sha256(payload).hexdigest()
    # The frozen known-answer for SHA-256("abc") proves the algorithm was
    # not swapped for a different digest function.
    abc = tmp_path / "abc.bin"
    abc.write_bytes(b"abc")
    assert runner._sha256_file(abc) == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
    # The attestation contract still requires an exact 64-hex SHA-256 digest.
    att = {
        "schema_version": 1,
        "run_id": "r1",
        "repo_head_before": "a" * 40,
        "repo_head_after": "a" * 40,
        "patch_sha256": "b" * 64,
    }
    assert semantic.validate_attestation(att) is None
    assert semantic.validate_attestation(dict(att, patch_sha256="b" * 63)) \
        == semantic.ATT_MALFORMED


# ---------------------------------------------------------------------------
# 14: M008 rejection codes still fail closed (with a green readiness)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [
    semantic.ATT_MISSING,
    semantic.ATT_MALFORMED,
    semantic.ATT_PARTIAL,
    semantic.ATT_MISMATCH,
    semantic.ATT_STALE,
    semantic.ATT_CONTRADICTORY,
])
def test_m008_rejection_codes_still_fail_closed(code: str) -> None:
    result = runner.classify_subrun(
        0,
        semantic_status=semantic.STATUS_SUCCESS,
        semantic_readiness="READY_FOR_COMMIT",
        semantic_mode="IMPLEMENT",
        attestation=None,
        attestation_error=code,
    )
    assert result.classification == model.CR_UNPROVEN
    assert result.attestation_error == code


# ---------------------------------------------------------------------------
# 15: M009 attestation projection remains backward compatible
# ---------------------------------------------------------------------------


def test_m009_attestation_projection_unchanged_for_legacy_records(
        tmp_path: Path) -> None:
    legacy = store.SubrunDoc(
        subrun_id="implement-a1",
        phase_id="implement",
        kind=model.PH_IMPLEMENT,
        mode="IMPLEMENT",
        round=0,
        attempt=1,
        command=["true"],
        cwd=None,
        started_at="2026-09-18T00:00:00Z",
        finished_at="2026-09-18T00:00:01Z",
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
    doc = summary.mission_summary(mission, paths)
    assert doc["attestation"] == {
        "model_heavy_subruns": 1,
        "verified": 0,
        "unproven": 0,
        "legacy": 1,
    }
    sub = doc["model_heavy"]["subruns"][0]
    assert sub["attestation"] == summary.ATT_LEGACY
    assert sub["attestation_identity"] is None
    assert sub["semantic_require_changes"] is None
    assert "attestation : verified=0 unproven=0 legacy=1" in \
        summary.render_summary(doc)
