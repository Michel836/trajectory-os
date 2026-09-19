"""M069 — guarded champion/challenger model refresh unit tests."""

from __future__ import annotations

from pathlib import Path

from trajectory_os.intelligence import dataset as dataset_module
from trajectory_os.intelligence import fixtures, ml
from trajectory_os.intelligence import model as intel_model
from trajectory_os.realworld import learning


def _dataset(root: Path, count: int) -> dataset_module.LearningDataset:
    fixtures.generate_fixture_runs(root, count=count)
    return dataset_module.build_learning_dataset(
        [root], source_kind=intel_model.SOURCE_FIXTURE,
        built_at="2026-01-01T00:00:00Z")


def test_insufficient_data_is_fail_closed(tmp_path: Path) -> None:
    learning_dataset = _dataset(tmp_path / "tiny", 4)
    report = learning.refresh_model(
        learning_dataset, "success", generated_at="2026-01-01T00:00:00Z")
    assert report.state == learning.STATE_INSUFFICIENT_DATA
    assert report.sample_count < report.minimum_samples


def test_champion_challenger_no_promotion_and_rollback(
        tmp_path: Path) -> None:
    learning_dataset = _dataset(tmp_path / "runs", 120)
    evaluation = ml.evaluate_target(learning_dataset, "success")
    champion = learning.model_record_from_evaluation(
        evaluation, registered_at="2026-01-01T00:00:00Z")
    assert champion is not None
    report = learning.refresh_model(
        learning_dataset, "success", champion=champion,
        generated_at="2026-01-02T00:00:00Z")
    assert report.state in (learning.STATE_PROMOTE,
                            learning.STATE_NO_PROMOTION)
    assert report.snapshot.snapshot_id
    assert report.rollback["champion_model_id"] == champion.model_id
    assert report.policy_mutation is False
    assert report.route_authority_change is False
    assert report.scheduler_authority_change is False
    assert learning.registry_entry(report)["registered"] is False


def test_calibration_and_quality_are_reported(tmp_path: Path) -> None:
    learning_dataset = _dataset(tmp_path / "runs", 120)
    report = learning.refresh_model(
        learning_dataset, "success", generated_at="2026-01-01T00:00:00Z")
    assert "status" in report.calibration_comparison
    assert report.quality_checks["leakage_free"] is True
    assert "status" in report.drift


def test_persist_report(tmp_path: Path) -> None:
    learning_dataset = _dataset(tmp_path / "runs", 120)
    report = learning.refresh_model(
        learning_dataset, "success", generated_at="2026-01-01T00:00:00Z")
    path = learning.persist_report(str(tmp_path), report)
    assert Path(path).is_file()
