"""M026 — authoritative event projection unit tests (no Git, no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from trajectory_os.events import engine, model, store


def _event(**overrides: object) -> model.EventRecord:
    base: dict[str, object] = {
        "kind": model.K_ARTIFACT_RECORDED,
        "severity": model.SEV_INFO,
        "goal_id": "g-1",
        "source": "artifact",
        "subject": "a-1",
        "reason": "REPORT",
        "detail": "report.json",
        "occurred_at": "2026-01-01T00:00:00Z",
        "payload": {"artifact_id": "a-1"},
    }
    base.update(overrides)
    return model.EventRecord.build(**base)  # type: ignore[arg-type]


def test_event_identity_is_content_addressed_and_clock_free() -> None:
    first = _event(occurred_at="2026-01-01T00:00:00Z")
    later = _event(occurred_at="2026-06-06T06:06:06Z")
    # The observation clock is excluded from identity.
    assert first.event_id == later.event_id
    changed = _event(subject="a-2")
    assert changed.event_id != first.event_id
    assert first.event_id == first.compute_event_id()


def test_event_round_trip_and_identity_mismatch() -> None:
    record = _event()
    restored = model.EventRecord.from_dict(record.to_dict())
    assert restored == record
    with pytest.raises(model.EventError) as exc:
        model.EventRecord.from_dict({**record.to_dict(), "event_id": "0" * 64})
    assert exc.value.code == model.E_IDENTITY_MISMATCH


def test_unknown_kind_and_oversized_payload_fail_closed() -> None:
    with pytest.raises(model.EventError):
        _event(kind="not_a_kind")
    with pytest.raises(model.EventError):
        _event(payload={"blob": "x" * (model.MAX_PAYLOAD_BYTES + 1)})


def test_persist_dedupe_and_reconstruct(tmp_path: Path) -> None:
    a = _event(subject="a-1")
    b = _event(subject="a-2", occurred_at="2026-01-01T00:00:01Z")
    assert store.append_events(tmp_path, "g-1", [a, b],
                               updated_at="t0") == [a, b]
    # Re-appending identical events is an idempotent no-op.
    assert store.append_events(tmp_path, "g-1", [a, b],
                               updated_at="t1") == []
    document, records = store.reconstruct(tmp_path, "g-1")
    assert document["count"] == 2
    assert [record.event_id for record in records] == [a.event_id, b.event_id]
    assert store.load_projection(tmp_path, "g-1") is not None


def test_reconstruct_detects_log_divergence(tmp_path: Path) -> None:
    a = _event()
    store.save_projection(tmp_path, "g-1", [a], updated_at="t0")
    paths = store.event_paths(tmp_path, "g-1")
    paths["events"].write_text("", encoding="utf-8")
    with pytest.raises(model.EventError) as exc:
        store.reconstruct(tmp_path, "g-1")
    assert exc.value.code == model.E_IDENTITY_MISMATCH


def test_projection_is_bounded() -> None:
    events = [_event(subject=f"a-{i:05d}", occurred_at="2026-01-01T00:00:00Z")
              for i in range(model.MAX_EVENTS + 50)]
    bounded = store.dedupe_sorted(events)
    assert len(bounded) == model.MAX_EVENTS


def test_changed_reports_added_and_removed() -> None:
    a = _event(subject="a-1")
    b = _event(subject="a-2")
    added, removed = store.changed([a], [a, b])
    assert added == (b,)
    assert removed == ()


# --- engine projection over a real seeded goal --------------------------------


def _seed_goal(root: Path) -> str:
    from trajectory_os.goals import launch
    from trajectory_os.graph import store as graph_store

    goal_id = "g-events"
    spec = {
        "schema_version": 1,
        "goal_id": goal_id,
        "objective": "Prove authoritative events.",
        "nodes": [{
            "node_id": "n-events",
            "title": "Events mission",
            "priority": 50,
            "depends_on": [],
            "acceptance_criteria": [{
                "criterion_id": "ac-events",
                "statement": "events are authoritative"}],
            "mission_ref": {"mission_id": "m-events", "required": True},
            "resources": {"cpu_slots": 1},
            "budgets": {"repair_budget": 0, "max_attempts": 1},
        }],
    }
    defaults = launch.MissionLaunchDefaults(
        repo_root=None, baseline_revision="base", repair_budget=0)
    launch.provision_from_spec(str(root), spec, defaults)
    graph_store.create_graph(str(root), spec, repo_root=defaults.repo_root,
                             baseline_revision=defaults.baseline_revision)
    return goal_id


def test_engine_projects_authoritative_events(tmp_path: Path) -> None:
    goal_id = _seed_goal(tmp_path)
    events = engine.project_events(tmp_path, goal_id)
    kinds = {event.kind for event in events}
    assert model.K_GOAL_PROVISIONED in kinds
    assert model.K_MISSION_STATE in kinds
    assert model.K_HUMAN_GATE in kinds
    # Every event derives from an authoritative store (never model prose).
    assert {event.category for event in events} <= model.CATEGORIES
    assert engine.refresh(tmp_path, goal_id, clock=lambda: "t0").count \
        == len(events)


def test_engine_refresh_is_restart_safe_and_idempotent(tmp_path: Path) -> None:
    goal_id = _seed_goal(tmp_path)
    first = engine.refresh(tmp_path, goal_id, clock=lambda: "t0")
    second = engine.refresh(tmp_path, goal_id, clock=lambda: "t1")
    assert first.count == second.count
    assert second.added == ()
    assert second.removed == ()
    document, records = store.reconstruct(tmp_path, goal_id)
    assert document["count"] == len(records) == first.count
