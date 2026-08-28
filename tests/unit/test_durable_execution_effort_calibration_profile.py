"""Unit tests for V1.13 durable calibration-profile orchestration."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

import trajectory_os.application.execution_effort_calibration_profile as profile_app
from trajectory_os.application.execution_effort_calibration_profile import (
    DurableEffortCalibrationProfileError,
    EffortCalibrationProfilePortfolioNotFoundError,
    build_effort_calibration_profile_durably,
)
from trajectory_os.domain.entities import EntityStatus, EntityType, TrajectoryEntity
from trajectory_os.domain.execution_effort import ExecutionEffortObservation
from trajectory_os.domain.execution_effort_estimates import ExecutionEffortEstimate
from trajectory_os.domain.portfolio import Portfolio

PORTFOLIO_ID = UUID("21212121-2121-4121-8121-212121212121")
PROJECT_ID = UUID("22222222-2222-4222-8222-222222222222")
NOW = datetime(2025, 2, 1, tzinfo=UTC)


def _portfolio() -> Portfolio:
    return Portfolio(
        id=PORTFOLIO_ID,
        name="Durable V1.13 Portfolio",
        entities=[
            TrajectoryEntity(
                id=PROJECT_ID,
                entity_type=EntityType.PROJECT,
                title="Project",
                status=EntityStatus.COMPLETED,
                created_at=NOW,
                updated_at=NOW,
            )
        ],
        relations=[],
    )


class FakePortfolioRepository:
    def __init__(self, portfolio: Portfolio | None) -> None:
        self.portfolio = portfolio
        self.load_calls: list[UUID] = []
        self.saved: list[Portfolio] = []

    def load(self, portfolio_id: UUID) -> Portfolio | None:
        self.load_calls.append(portfolio_id)
        return self.portfolio

    def save(self, portfolio: Portfolio) -> None:
        self.saved.append(portfolio)
        raise AssertionError("V1.13 durable profile must not write")


class FakeEstimateReader:
    def __init__(self) -> None:
        self.calls: list[UUID] = []
        self.values: tuple[ExecutionEffortEstimate, ...] = ()

    def list_for_portfolio(
        self, portfolio_id: UUID
    ) -> tuple[ExecutionEffortEstimate, ...]:
        self.calls.append(portfolio_id)
        return self.values

    def list_for_entity(
        self, portfolio_id: UUID, entity_id: UUID
    ) -> tuple[ExecutionEffortEstimate, ...]:
        return ()


class FakeObservationReader:
    def __init__(self) -> None:
        self.calls: list[UUID] = []
        self.values: tuple[ExecutionEffortObservation, ...] = ()

    def list_for_portfolio(
        self, portfolio_id: UUID
    ) -> tuple[ExecutionEffortObservation, ...]:
        self.calls.append(portfolio_id)
        return self.values

    def list_for_entity(
        self, portfolio_id: UUID, entity_id: UUID
    ) -> tuple[ExecutionEffortObservation, ...]:
        return ()


class FailingRepository:
    def load(self, portfolio_id: UUID) -> Portfolio | None:
        raise RuntimeError("repository failure")


class FailingEstimateReader(FakeEstimateReader):
    def list_for_portfolio(
        self, portfolio_id: UUID
    ) -> tuple[ExecutionEffortEstimate, ...]:
        raise RuntimeError("estimate failure")


class FailingObservationReader(FakeObservationReader):
    def list_for_portfolio(
        self, portfolio_id: UUID
    ) -> tuple[ExecutionEffortObservation, ...]:
        raise RuntimeError("observation failure")


def test_non_uuid_portfolio_id_is_rejected_before_repository_access() -> None:
    repo = FakePortfolioRepository(_portfolio())
    estimates = FakeEstimateReader()
    observations = FakeObservationReader()

    with pytest.raises(DurableEffortCalibrationProfileError, match="UUID"):
        build_effort_calibration_profile_durably(
            "bad-id",  # type: ignore[arg-type]
            PROJECT_ID,
            repo,
            estimates,
            observations,
        )

    assert repo.load_calls == []
    assert estimates.calls == []
    assert observations.calls == []


def test_missing_portfolio_prevents_reader_calls() -> None:
    repo = FakePortfolioRepository(None)
    estimates = FakeEstimateReader()
    observations = FakeObservationReader()

    with pytest.raises(
        EffortCalibrationProfilePortfolioNotFoundError,
        match="portfolio not found",
    ):
        build_effort_calibration_profile_durably(
            PORTFOLIO_ID,
            PROJECT_ID,
            repo,
            estimates,
            observations,
        )

    assert repo.load_calls == [PORTFOLIO_ID]
    assert estimates.calls == []
    assert observations.calls == []


def test_exact_sequence_and_same_loaded_portfolio_are_used(monkeypatch: pytest.MonkeyPatch) -> None:
    portfolio = _portfolio()
    repo = FakePortfolioRepository(portfolio)
    estimates = FakeEstimateReader()
    observations = FakeObservationReader()

    measurement = object()
    evidence = object()
    expected_profile = object()
    events: list[str] = []

    def fake_measure(
        current: Portfolio,
        project_id: UUID,
        observations: object,
    ) -> object:
        events.append("measure")
        assert current is portfolio
        assert project_id == PROJECT_ID
        assert observations == ()
        return measurement

    def fake_evidence(
        current: Portfolio,
        measured: object,
        estimate_values: object,
    ) -> object:
        events.append("evidence")
        assert current is portfolio
        assert measured is measurement
        assert estimate_values == ()
        return evidence

    def fake_profile(current: Portfolio, evidence_value: object) -> object:
        events.append("profile")
        assert current is portfolio
        assert evidence_value is evidence
        return expected_profile

    monkeypatch.setattr(profile_app, "measure_work_breakdown_effort", fake_measure)
    monkeypatch.setattr(profile_app, "build_effort_calibration_evidence", fake_evidence)
    monkeypatch.setattr(profile_app, "build_effort_calibration_profile", fake_profile)

    result = build_effort_calibration_profile_durably(
        PORTFOLIO_ID,
        PROJECT_ID,
        repo,
        estimates,
        observations,
    )

    assert result is expected_profile
    assert repo.load_calls == [PORTFOLIO_ID]
    assert estimates.calls == [PORTFOLIO_ID]
    assert observations.calls == [PORTFOLIO_ID]
    assert events == ["measure", "evidence", "profile"]
    assert repo.saved == []


def test_repository_failure_propagates() -> None:
    with pytest.raises(RuntimeError, match="repository failure"):
        build_effort_calibration_profile_durably(
            PORTFOLIO_ID,
            PROJECT_ID,
            FailingRepository(),
            FakeEstimateReader(),
            FakeObservationReader(),
        )


def test_estimate_reader_failure_propagates() -> None:
    with pytest.raises(RuntimeError, match="estimate failure"):
        build_effort_calibration_profile_durably(
            PORTFOLIO_ID,
            PROJECT_ID,
            FakePortfolioRepository(_portfolio()),
            FailingEstimateReader(),
            FakeObservationReader(),
        )


def test_observation_reader_failure_propagates() -> None:
    with pytest.raises(RuntimeError, match="observation failure"):
        build_effort_calibration_profile_durably(
            PORTFOLIO_ID,
            PROJECT_ID,
            FakePortfolioRepository(_portfolio()),
            FakeEstimateReader(),
            FailingObservationReader(),
        )


def test_no_repository_save_occurs() -> None:
    portfolio = _portfolio()
    repo = FakePortfolioRepository(portfolio)

    result = build_effort_calibration_profile_durably(
        PORTFOLIO_ID,
        PROJECT_ID,
        repo,
        FakeEstimateReader(),
        FakeObservationReader(),
    )

    assert result.portfolio_id == PORTFOLIO_ID
    assert result.project_id == PROJECT_ID
    assert result.segments == ()
    assert repo.saved == []
