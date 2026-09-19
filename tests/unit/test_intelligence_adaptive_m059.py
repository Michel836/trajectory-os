"""M059 — adaptive scheduler overlay unit tests (advisory only, no Git)."""

from __future__ import annotations

from trajectory_os.intelligence import adaptive


def _candidate(
    identifier: str, *, project_priority: int = 3,
    deadline_s: float | None = None, wait_age_s: float = 0.0,
    dependencies_ready: bool = True, backend_available: bool = True,
    human_priority: int | None = None,
    predicted_success: float | None = None,
    predicted_block_risk: float | None = None,
    predicted_duration_s: float | None = None,
) -> adaptive.ScheduleCandidate:
    return adaptive.ScheduleCandidate(
        candidate_id=identifier, project="p", mission_id=None,
        project_priority=project_priority, deadline_s=deadline_s,
        wait_age_s=wait_age_s, dependencies_ready=dependencies_ready,
        backend_available=backend_available, human_priority=human_priority,
        predicted_success=predicted_success,
        predicted_block_risk=predicted_block_risk,
        predicted_duration_s=predicted_duration_s)


def test_rule_score_is_transparent() -> None:
    score = adaptive.score_candidate(_candidate("a"), mode=adaptive.MODE_RULE)
    assert score.components
    assert score.total == sum(c.value for c in score.components)
    assert score.reason


def test_priority_and_dependencies_affect_ordering() -> None:
    overlay = adaptive.build_overlay(
        (_candidate("low", project_priority=1),
         _candidate("high", project_priority=5),
         _candidate("blocked", project_priority=5, dependencies_ready=False)),
        mode=adaptive.MODE_RULE)
    assert overlay.ordering[0].candidate_id == "high"


def test_ml_mode_falls_back_without_predictions() -> None:
    overlay = adaptive.build_overlay(
        (_candidate("a"), _candidate("b")), mode=adaptive.MODE_ML)
    assert overlay.fallback_used is True
    assert overlay.mode == adaptive.MODE_RULE
    assert overlay.fallback_reason


def test_ml_mode_uses_predictions_when_present() -> None:
    overlay = adaptive.build_overlay(
        (_candidate("risky", predicted_success=0.2, predicted_block_risk=0.8),
         _candidate("safe", predicted_success=0.9, predicted_block_risk=0.1)),
        mode=adaptive.MODE_ML)
    assert overlay.mode == adaptive.MODE_ML
    assert overlay.predictions_used is True
    assert overlay.ordering[0].candidate_id == "safe"


def test_comparison_never_claims_improvement() -> None:
    comparison = adaptive.compare_schedulers(
        (_candidate("a", project_priority=5),
         _candidate("b", project_priority=1),
         _candidate("c", project_priority=3,
                    predicted_success=0.9, predicted_block_risk=0.05)))
    assert comparison.claimed_improvement is False
    assert comparison.improvement_reason
    assert -1.0 <= comparison.rank_agreement <= 1.0


def test_overlay_never_mutates_canonical_or_git() -> None:
    overlay = adaptive.build_overlay((_candidate("a"),))
    assert overlay.mutates_canonical_scheduler is False
    assert overlay.commands_release_git is False


def test_human_priority_is_transparent() -> None:
    overlay = adaptive.build_overlay(
        (_candidate("normal"), _candidate("preferred", human_priority=5)),
        mode=adaptive.MODE_RULE)
    top = overlay.ordering[0]
    assert top.candidate_id == "preferred"
    assert any(c.name == "human_priority" for c in top.components)


def test_prediction_snapshot_records_model_ids(tmp_path) -> None:
    from pathlib import Path

    from trajectory_os.intelligence import dataset, fixtures, ml

    root = tmp_path / "runs"
    fixtures.generate_fixture_runs(root, count=60)
    learning = dataset.build_learning_dataset(
        [root], source_kind="FIXTURE", built_at="2026-01-01T00:00:00Z")
    report = ml.train_all(learning, generated_at="2026-01-01T00:00:00Z")
    snapshot = adaptive.prediction_snapshot(
        report, generated_at="2026-01-01T00:00:00Z")
    assert snapshot.model_ids
    assert Path(root).is_dir()
