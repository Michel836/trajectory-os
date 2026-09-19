"""M068 — outcome tracking unit tests."""

from __future__ import annotations

from pathlib import Path

from trajectory_os.realworld import outcomes


def _predictions() -> tuple[outcomes.MetricPrediction, ...]:
    return (
        outcomes.MetricPrediction("duration_s", 100.0, 0.2,
                                  outcomes.PRED_MODEL, "model-1"),
        outcomes.MetricPrediction("repairs", 1.0, 0.3,
                                  outcomes.PRED_MODEL, "model-1"),
    )


def _link(actual: tuple[outcomes.ActualMetric, ...],
          *, selected: str = "route-a") -> outcomes.OutcomeLink:
    return outcomes.OutcomeLink(
        link_id=outcomes.link_id_for("pred-1", "dec-1", "exec-1"),
        prediction_id="pred-1", decision_id="dec-1", execution_id="exec-1",
        recommendation="route-a", selected_action=selected,
        predicted=_predictions(), actual=actual, errors={}, status="",
        revision=0, recorded_at="2026-01-04T00:00:00Z")


def _actual() -> tuple[outcomes.ActualMetric, ...]:
    return (
        outcomes.ActualMetric("duration_s", 120.0, outcomes.ACTUAL_MEASURED,
                              "run-1"),
        outcomes.ActualMetric("repairs", 2.0, outcomes.ACTUAL_ENTERED,
                              "user"))


def test_error_calculation_and_reconciliation(tmp_path: Path) -> None:
    recorded = outcomes.record_outcome(_link(_actual()), root=str(tmp_path))
    assert recorded.status == outcomes.STATUS_RECONCILED
    assert recorded.errors["duration_s"]["absolute_error"] == 20.0
    assert recorded.followed_recommendation() is True
    summary = outcomes.reconciliation_summary(str(tmp_path))
    assert summary["mean_absolute_error"] == 10.5


def test_unknown_actual_stays_unknown(tmp_path: Path) -> None:
    actual = (
        outcomes.ActualMetric("duration_s", 130.0, outcomes.ACTUAL_MEASURED,
                              "run-1"),
        outcomes.ActualMetric("repairs", None, outcomes.ACTUAL_UNKNOWN, "",
                              reason="not recorded"))
    recorded = outcomes.record_outcome(_link(actual), root=str(tmp_path))
    assert recorded.status == outcomes.STATUS_PARTIAL
    assert recorded.errors["repairs"]["status"] == "UNKNOWN"
    assert recorded.errors["repairs"]["absolute_error"] is None


def test_delayed_update_is_idempotent_and_append_only(
        tmp_path: Path) -> None:
    first = outcomes.record_outcome(_link(_actual()), root=str(tmp_path))
    again = outcomes.record_outcome(_link(_actual()), root=str(tmp_path))
    assert first.revision == again.revision == 1
    delayed = outcomes.record_outcome(
        _link(_actual(), selected="route-b"), root=str(tmp_path),
        recorded_at="2026-01-05T00:00:00Z")
    assert delayed.revision == 2
    history = outcomes.history(str(tmp_path), first.link_id)
    assert [revision.revision for revision in history] == [1, 2]
    assert history[0].selected_action == "route-a"
    assert history[1].selected_action == "route-b"


def test_missing_actual_is_not_a_success(tmp_path: Path) -> None:
    link = outcomes.OutcomeLink(
        link_id="unlinked", prediction_id="pred-x", decision_id=None,
        execution_id=None, recommendation=None, selected_action=None,
        predicted=_predictions(), actual=(), errors={}, status="", revision=0,
        recorded_at="2026-01-04T00:00:00Z")
    recorded = outcomes.record_outcome(link, root=str(tmp_path))
    assert recorded.status == outcomes.STATUS_UNKNOWN
