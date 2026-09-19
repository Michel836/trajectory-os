"""M025 — workspace/artifact provenance unit tests (no Git, no hardware)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trajectory_os.artifacts import engine, identity, model, store, summary


def _manager(tmp_path: Path) -> engine.WorkspaceManager:
    return engine.WorkspaceManager(tmp_path)


def test_workspace_identity_and_idempotent_ensure(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    first = manager.ensure_goal_workspace("g", created_at="2026-01-01T00:00:00Z")
    second = manager.ensure_goal_workspace("g", created_at="2026-01-02T00:00:00Z")
    assert first.workspace_id == second.workspace_id
    assert Path(first.path).is_dir()
    mission = manager.ensure_mission_workspace(
        "g", "m", created_at="2026-01-01T00:00:00Z")
    assert mission.kind == "mission"
    assert mission.mission_id == "m"
    assert mission.workspace_id == mission.compute_workspace_id()


def test_artifact_content_identity_and_lineage(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    parent = manager.record_artifact(
        goal_id="g", kind=model.AK_REPORT, name="report.txt",
        content=b"hello", producer=model.PRODUCER_AGENT,
        created_at="2026-01-01T00:00:00Z")
    child = manager.record_artifact(
        goal_id="g", kind=model.AK_DATASET, name="data.bin", content=b"world",
        parent_ids=(parent.artifact_id,), reuse_input_id="reuse-1",
        proof_id="proof-1", criterion_id="ac-1",
        created_at="2026-01-01T00:00:01Z")
    assert child.artifact_id == child.compute_artifact_id()
    # Identical content + provenance deduplicates to the same identity.
    again = manager.record_artifact(
        goal_id="g", kind=model.AK_REPORT, name="report.txt",
        content=b"hello", producer=model.PRODUCER_AGENT,
        created_at="2026-01-01T00:00:09Z")
    assert again.artifact_id == parent.artifact_id
    assert child.reuse_input_id == "reuse-1"
    assert child.proof_id == "proof-1"
    assert child.criterion_id == "ac-1"
    lineage = manager.lineage("g", child.artifact_id)
    assert [record.name for record in lineage] == ["data.bin", "report.txt"]


def test_artifact_from_source_path(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    source = tmp_path / "model.bin"
    source.write_bytes(b"weights")
    record = manager.record_artifact(
        goal_id="g", kind=model.AK_MODEL, name="model.bin",
        source_path=source, producer=model.PRODUCER_RUNNER,
        created_at="2026-01-01T00:00:00Z")
    assert record.size_bytes == len(b"weights")
    assert record.content_sha256 == store.sha256_bytes(b"weights")
    assert (tmp_path / record.rel_path).is_file()


def test_same_name_different_content_never_aliases(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    first = manager.record_artifact(
        goal_id="g", kind=model.AK_FILE, name="report.txt",
        content=b"version-1", created_at="2026-01-01T00:00:00Z")
    second = manager.record_artifact(
        goal_id="g", kind=model.AK_FILE, name="report.txt",
        content=b"version-2", created_at="2026-01-01T00:00:01Z")
    assert first.artifact_id != second.artifact_id
    assert first.rel_path != second.rel_path
    ok, problems = manager.verify("g")
    assert ok is True and problems == []


def test_missing_parent_fails_closed(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    with pytest.raises(model.ArtifactError) as excinfo:
        manager.record_artifact(
            goal_id="g", kind=model.AK_FILE, name="a.txt", content=b"a",
            parent_ids=("0" * 64,), created_at="2026-01-01T00:00:00Z")
    assert excinfo.value.code == model.E_NOT_FOUND


def test_cross_goal_resolution_is_blocked(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    record = manager.record_artifact(
        goal_id="goal-a", kind=model.AK_FILE, name="secret.txt",
        content=b"secret", created_at="2026-01-01T00:00:00Z")
    manager.ensure_goal_workspace("goal-b", created_at="2026-01-01T00:00:00Z")
    with pytest.raises(model.ArtifactError):
        manager.load_artifact("goal-b", record.artifact_id)
    # resolve_record enforces the boundary even for an in-memory record.
    with pytest.raises(model.ArtifactError) as excinfo:
        store.resolve_record("goal-b", record)
    assert excinfo.value.code == model.E_CROSS_GOAL_LEAK
    # An explicit share is honoured.
    shared = model.ArtifactRecord.build(
        goal_id="goal-a", kind=model.AK_FILE, name="shared.txt",
        rel_path="x", size_bytes=1, content_sha256="a" * 64,
        producer=model.PRODUCER_OPERATOR, created_at="2026-01-01T00:00:00Z",
        shared_with=("goal-b",))
    assert store.resolve_record("goal-b", shared) is shared


def test_forged_record_with_foreign_goal_is_rejected(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    record = manager.record_artifact(
        goal_id="goal-a", kind=model.AK_FILE, name="f.txt", content=b"f",
        created_at="2026-01-01T00:00:00Z")
    # Copy a valid goal-a record into goal-b's namespace.
    target = store.artifact_record_path(tmp_path, "goal-b", record.artifact_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record.to_dict()), encoding="utf-8")
    with pytest.raises(model.ArtifactError) as excinfo:
        store.load_artifact(tmp_path, "goal-b", record.artifact_id)
    assert excinfo.value.code == model.E_CROSS_GOAL_LEAK


def test_verify_detects_content_tampering(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    record = manager.record_artifact(
        goal_id="g", kind=model.AK_FILE, name="f.txt", content=b"original",
        created_at="2026-01-01T00:00:00Z")
    assert manager.verify("g") == (True, [])
    (tmp_path / record.rel_path).write_bytes(b"tampered")
    ok, problems = manager.verify("g")
    assert ok is False
    assert any(p.startswith("CONTENT_MISMATCH") for p in problems)


def test_malformed_artifact_name_and_kind_rejected(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    with pytest.raises(model.ArtifactError):
        manager.record_artifact(goal_id="g", kind="NOPE", name="a",
                                content=b"a")
    with pytest.raises(model.ArtifactError):
        manager.record_artifact(goal_id="g", kind=model.AK_FILE,
                                name="../escape", content=b"a")


def test_status_document_and_reconstruction(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    a = manager.record_artifact(
        goal_id="g", kind=model.AK_REPORT, name="r.txt", content=b"r",
        created_at="2026-01-01T00:00:00Z")
    state = manager.reconstruct("g")
    assert state.lineage_id == state.compute_lineage_id()
    assert [x.artifact_id for x in state.artifacts] == [a.artifact_id]
    document = summary.status_document(tmp_path, "g")
    assert document["status"] == "OK"
    assert document["counts"]["artifacts_total"] == 1
    assert document["counts"]["by_kind"][model.AK_REPORT] == 1
    text = summary.render_status(document)
    assert "artifacts : total=1" in text
    lineage_document = summary.lineage_document(tmp_path, "g", a.artifact_id)
    assert lineage_document["lineage"][0]["artifact_id"] == a.artifact_id


def test_identity_domains_are_distinct_and_closed() -> None:
    payload = {"x": 1}
    digests = {identity.digest(domain, payload)
               for domain in identity.DOMAIN_IDS}
    assert len(digests) == len(identity.DOMAIN_IDS)
    with pytest.raises(ValueError):
        identity.digest("trajectory-os.nope.v1", payload)
