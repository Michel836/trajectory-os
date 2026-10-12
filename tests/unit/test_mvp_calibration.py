"""Tests for deterministic effort calibration (C1)."""

from __future__ import annotations

import pytest

from trajectory_os.mvp import calibration, model, mutations, store


def _seed(root: str, samples: int) -> None:
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=(model.Project(project_id="p1", name="P1", objective="o"),),
        tasks=(model.Task(task_id="t1", project_id="p1", title="Work",
                          estimated_minutes=60),),
    ))
    for index in range(samples):
        store.append_outcome(root, model.OutcomeRecord(
            task_id="t1", outcome=model.O_COMPLETED,
            recorded_at=f"2026-01-0{index + 1}T10:00:00Z",
            actual_minutes=90))


def test_insufficient_data_reports_no_factor(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root, calibration.MIN_SAMPLES - 1)
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    payload = calibration.summary(portfolio, store.read_outcomes(root))
    assert payload["global"]["sufficient"] is False
    assert payload["global"]["factor"] is None


def test_calibration_factor_and_interval(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root, calibration.MIN_SAMPLES)
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    result = calibration.calibration_for_task(
        portfolio, store.read_outcomes(root), "t1")
    assert result is not None
    assert result.sufficient is True
    assert result.factor == 1.5
    assert result.interval(60) == (90, 90)
    assert result.adjusted(60) == 90


def test_apply_calibrated_estimate_updates_task(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root, calibration.MIN_SAMPLES)
    result = mutations.apply_calibrated_estimate(root, "t1")
    assert result["previous_minutes"] == 60
    assert result["estimated_minutes"] == 90
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    assert portfolio.task_map()["t1"].estimated_minutes == 90


def test_apply_calibrated_estimate_fails_without_data(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root, calibration.MIN_SAMPLES - 1)
    with pytest.raises(model.MvpError) as exc:
        mutations.apply_calibrated_estimate(root, "t1")
    assert exc.value.code == model.E_MALFORMED
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    assert portfolio.task_map()["t1"].estimated_minutes == 60


def test_apply_calibrated_estimate_requires_an_estimate(
        tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root, calibration.MIN_SAMPLES)
    mutations.update_task(root, "t1", {"estimated_minutes": None})
    with pytest.raises(model.MvpError):
        mutations.apply_calibrated_estimate(root, "t1")
