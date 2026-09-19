"""M067 — LifeOS operational intelligence unit tests."""

from __future__ import annotations

from pathlib import Path

from trajectory_os.platform import projects as project_registry
from trajectory_os.realworld import lifeos_ops


def test_operations_answers_the_five_questions(tmp_path: Path) -> None:
    report = lifeos_ops.evaluate_operations((
        lifeos_ops.MissionState("m-changed", "p",
                                changed_at="2026-01-02T00:00:00Z"),
        lifeos_ops.MissionState("m-blocked", "p",
                                blocked_reason="dependency"),
        lifeos_ops.MissionState("m-gate", "p", human_gate="GO COMMIT"),
        lifeos_ops.MissionState("m-stale", "p",
                                stale_evidence_since="2025-12-01"),
        lifeos_ops.MissionState("m-outcome", "p", outcome_recorded=False),
        lifeos_ops.MissionState("m-wait", "p", priority=1),
    ), root=str(tmp_path), generated_at="2026-01-03T00:00:00Z")
    assert report.changed
    assert report.blocked
    assert any("human decision" in item["reason"]
               for item in report.attention)
    assert any("stale evidence" in item["reason"]
               for item in report.attention)
    assert any("missing outcome" in item["reason"]
               for item in report.attention)
    assert report.can_wait
    assert report.next_actions
    assert all(action.rationale and action.urgency
               for action in report.next_actions)
    assert any(action.alternatives for action in report.next_actions)
    assert (tmp_path / "realworld" / "operations" / "operations" /
            "operations.json").is_file()


def test_project_operations_uses_the_registry(tmp_path: Path) -> None:
    project = project_registry.create_project(
        tmp_path, name="Registry project", workspace=str(tmp_path / "ws"),
        clock=lambda: "2026-01-01T00:00:00Z")
    project_registry.add_objective(
        tmp_path, project.project_id, objective_id="obj-1",
        title="Objective", clock=lambda: "2026-01-01T00:00:00Z")
    project_registry.link_mission(
        tmp_path, project.project_id, objective_id="obj-1",
        mission_id="m-1", clock=lambda: "2026-01-01T00:00:00Z")
    report = lifeos_ops.evaluate_project_operations(
        str(tmp_path), project.project_id, generated_at="2026-01-01T00:00:00Z")
    assert report.project == "Registry project"
    assert report.project_id == project.project_id
    assert isinstance(report.changed, tuple)
