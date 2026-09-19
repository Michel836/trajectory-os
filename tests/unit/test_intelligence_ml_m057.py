"""M057 — predictive ML baseline unit tests (deterministic, no Git)."""

from __future__ import annotations

import math
from pathlib import Path

from trajectory_os.intelligence import dataset, features, fixtures, ml, model


def _learning(tmp_path: Path, count: int = 120) -> dataset.LearningDataset:
    root = tmp_path / "runs"
    fixtures.generate_fixture_runs(root, count=count)
    return dataset.build_learning_dataset(
        [root], source_kind=model.SOURCE_FIXTURE,
        built_at="2026-01-01T00:00:00Z")


def test_ridge_recovers_a_linear_signal() -> None:
    model_instance = ml.RidgeRegression(alpha=0.0)
    matrix = [[0.0], [1.0], [2.0], [3.0], [4.0]]
    target = [1.0, 3.0, 5.0, 7.0, 9.0]
    model_instance.fit(matrix, target)
    predictions = model_instance.predict([[5.0], [10.0]])
    assert math.isclose(predictions[0], 11.0, abs_tol=1e-6)
    assert math.isclose(predictions[1], 21.0, abs_tol=1e-6)


def test_logistic_separates_two_clusters() -> None:
    estimator = ml.LogisticRegression(rate=0.5, epochs=600)
    matrix = [[0.0], [0.1], [0.2], [0.3], [10.0], [10.1], [10.2], [10.3]]
    target = [0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0]
    estimator.fit(matrix, target)
    probabilities = estimator.predict_proba([[0.15], [10.15]])
    assert probabilities[0] < 0.5 < probabilities[1]


def test_decision_tree_and_boosting_fit_a_step() -> None:
    matrix = [[float(i)] for i in range(20)]
    target = [0.0 if i < 10 else 1.0 for i in range(20)]
    tree = ml.DecisionTreeRegressor(max_depth=3, min_leaf=1)
    tree.fit(matrix, target)
    assert tree.predict([[1.0]])[0] < 0.25
    assert tree.predict([[18.0]])[0] > 0.75
    boosting = ml.GradientBoostingRegressor(trees=10, max_depth=2)
    boosting.fit(matrix, target)
    assert boosting.predict([[18.0]])[0] > boosting.predict([[1.0]])[0]


def test_training_produces_baselines_and_metadata(tmp_path: Path) -> None:
    learning = _learning(tmp_path)
    report = ml.train_all(learning, generated_at="2026-01-01T00:00:00Z")
    trained = [e for e in report.evaluations if e.status == ml.ST_TRAINED]
    assert trained
    for evaluation in trained:
        assert evaluation.baseline
        assert evaluation.model_id
        assert evaluation.dataset_id == learning.dataset_id
        assert evaluation.uncertainty
    assert report.evaluation("cost_usd") is not None
    assert report.evaluation("cost_usd").status == ml.ST_INSUFFICIENT


def test_duration_beats_naive_mean_on_fixture_signal(tmp_path: Path) -> None:
    learning = _learning(tmp_path)
    evaluation = ml.evaluate_target(learning, "duration_s")
    assert evaluation.status == ml.ST_TRAINED
    selected = next(a for a in evaluation.algorithms
                    if a.algorithm == evaluation.selected_algorithm)
    assert float(selected.metrics["mae"]) < float(
        evaluation.baseline["metrics"]["mae"])


def test_success_classification_reports_calibration(tmp_path: Path) -> None:
    learning = _learning(tmp_path)
    evaluation = ml.evaluate_target(learning, "success")
    assert evaluation.status == ml.ST_TRAINED
    assert evaluation.calibration is not None
    assert evaluation.calibration["method"] == "platt"
    assert "expected_calibration_error" in evaluation.calibration["after"]


def test_insufficient_data_fails_safe(tmp_path: Path) -> None:
    root = tmp_path / "tiny"
    fixtures.generate_fixture_runs(root, count=3)
    learning = dataset.build_learning_dataset(
        [root], source_kind=model.SOURCE_FIXTURE,
        built_at="2026-01-01T00:00:00Z")
    evaluation = ml.evaluate_target(learning, "success")
    assert evaluation.status == ml.ST_INSUFFICIENT
    assert evaluation.reason
    assert evaluation.selected_algorithm is None


def test_feature_schema_excludes_target_leakage() -> None:
    schema = features.build_feature_schema("success")
    assert "readiness" not in schema.categorical
    assert "final_outcome" not in schema.categorical
    assert "repairs" not in schema.numeric
    assert "duration_s" not in schema.numeric


def test_encoder_marks_missing_numeric_values(tmp_path: Path) -> None:
    learning = _learning(tmp_path, count=30)
    schema = features.build_feature_schema(
        "duration_s", feature_set=features.FEATURE_TASK_SIZE)
    encoder = features.fit_encoder(learning.observations, schema)
    names = encoder.feature_names
    assert "missing:changed_files" in names
    row = encoder.transform_row(learning.observations[0])
    assert len(row) == encoder.dimension()


def test_no_fabricated_significance(tmp_path: Path) -> None:
    learning = _learning(tmp_path)
    report = ml.train_all(learning, generated_at="2026-01-01T00:00:00Z")
    serialized = model.canonical_json(report.to_dict())
    assert "p_value" not in serialized
    assert "statistical_significance" not in serialized


def test_training_report_persists(tmp_path: Path) -> None:
    learning = _learning(tmp_path)
    report = ml.train_all(learning, generated_at="2026-01-01T00:00:00Z")
    ml.persist_report(str(tmp_path), report)
    reloaded = ml.load_report(str(tmp_path))
    assert reloaded is not None
    assert reloaded.dataset_id == report.dataset_id
    assert len(reloaded.evaluations) == len(report.evaluations)
