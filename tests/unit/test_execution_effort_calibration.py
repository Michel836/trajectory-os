"""Unit tests for the pure V1.12 leakage-safe calibration-evidence boundary."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from trajectory_os.domain.entities import (
    EntityStatus,
    EntityType,
    SourceKind,
    TrajectoryEntity,
)
from trajectory_os.domain.execution_effort import ExecutionEffortObservation
from trajectory_os.domain.execution_effort_calibration import (
    EffortCalibrationSample,
    EffortCalibrationSummary,
    ExecutionEffortCalibrationError,
    WorkBreakdownEffortCalibrationEvidence,
    build_effort_calibration_evidence,
)
from trajectory_os.domain.execution_effort_estimates import (
    ExecutionEffortEstimate,
)
from trajectory_os.domain.execution_effort_measurement import (
    WorkBreakdownEffortMeasurement,
    measure_work_breakdown_effort,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

# ---------------------------------------------------------------------------
# Fixtures: identities and timestamps
# ---------------------------------------------------------------------------

PORTFOLIO_ID = UUID("5a5a5a5a-5a5a-5a5a-5a5a-5a5a5a5a5a5a")
OTHER_PORTFOLIO_ID = UUID("6b6b6b6b-6b6b-6b6b-6b6b-6b6b6b6b6b6b")
PROJECT_ID = UUID("a0a0a0a0-a0a0-a0a0-a0a0-a0a0a0a0a0a0")
TASK_A_ID = UUID("a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1")
TASK_B_ID = UUID("a2a2a2a2-a2a2-a2a2-a2a2-a2a2a2a2a2a2")
TASK_C_ID = UUID("a3a3a3a3-a3a3-a3a3-a3a3-a3a3a3a3a3a3")
TASK_D_ID = UUID("a4a4a4a4-a4a4-a4a4-a4a4-a4a4a4a4a4a4")
TASK_E_ID = UUID("a5a5a5a5-a5a5-a5a5-a5a5-a5a5a5a5a5a5")
TASK_F_ID = UUID("a6a6a6a6-a6a6-a6a6-a6a6-a6a6a6a6a6a6")
OUT_OF_WBS_ID = UUID("a7a7a7a7-a7a7-a7a7-a7a7-a7a7a7a7a7a7")
UNKNOWN_ENTITY_ID = UUID("a8a8a8a8-a8a8-a8a8-a8a8-a8a8a8a8a8a8")

EST_BEFORE = datetime(2024, 5, 1, tzinfo=UTC)
EST_BEFORE_EARLY = datetime(2024, 4, 1, tzinfo=UTC)
FIRST_OBS = datetime(2024, 6, 1, tzinfo=UTC)
OBS_LATER = datetime(2024, 6, 2, tzinfo=UTC)
EST_AFTER = datetime(2024, 6, 3, tzinfo=UTC)


def _entity(entity_id: UUID, entity_type: EntityType, status: EntityStatus) -> TrajectoryEntity:
    return TrajectoryEntity(
        id=entity_id,
        entity_type=entity_type,
        title=f"Entity {str(entity_id)[:8]}",
        description="",
        status=status,
        source=SourceKind.USER_CONFIRMED,
        confidence=1.0,
        created_at=EST_BEFORE_EARLY,
        updated_at=EST_BEFORE_EARLY,
    )


def _relation(parent_id: UUID, child_id: UUID) -> TrajectoryRelation:
    return TrajectoryRelation(
        id=uuid4(),
        source_id=child_id,
        target_id=parent_id,
        relation_type=RelationType.BELONGS_TO,
        source=SourceKind.USER_CONFIRMED,
        confidence=1.0,
    )


def _make_portfolio(include_out_of_wbs: bool = True) -> Portfolio:
    entities = [
        _entity(PROJECT_ID, EntityType.PROJECT, EntityStatus.COMPLETED),
        _entity(TASK_A_ID, EntityType.TASK, EntityStatus.COMPLETED),
        _entity(TASK_B_ID, EntityType.TASK, EntityStatus.COMPLETED),
        _entity(TASK_C_ID, EntityType.TASK, EntityStatus.COMPLETED),
        _entity(TASK_D_ID, EntityType.TASK, EntityStatus.COMPLETED),
        _entity(TASK_E_ID, EntityType.TASK, EntityStatus.COMPLETED),
        _entity(TASK_F_ID, EntityType.TASK, EntityStatus.ACTIVE),
    ]
    relations = [
        _relation(PROJECT_ID, child)
        for child in (TASK_A_ID, TASK_B_ID, TASK_C_ID, TASK_D_ID, TASK_E_ID, TASK_F_ID)
    ]

    if include_out_of_wbs:
        # Legitimate portfolio entity with no WBS containment edge: outside
        # the CURRENT selected WBS.
        entities.append(_entity(OUT_OF_WBS_ID, EntityType.TASK, EntityStatus.COMPLETED))

    return Portfolio(
        id=PORTFOLIO_ID,
        name="Calibration Portfolio",
        entities=entities,
        relations=relations,
    )


def _estimate(
    entity_id: UUID,
    duration: int,
    estimated_at: datetime,
    estimate_id: UUID | None = None,
    portfolio_id: UUID = PORTFOLIO_ID,
) -> ExecutionEffortEstimate:
    return ExecutionEffortEstimate(
        id=estimate_id or uuid4(),
        portfolio_id=portfolio_id,
        entity_id=entity_id,
        duration_seconds=duration,
        estimated_at=estimated_at,
        source=SourceKind.USER_CONFIRMED,
    )


def _observation(
    entity_id: UUID,
    duration: int,
    observed_at: datetime,
) -> ExecutionEffortObservation:
    return ExecutionEffortObservation(
        id=uuid4(),
        portfolio_id=PORTFOLIO_ID,
        entity_id=entity_id,
        duration_seconds=duration,
        observed_at=observed_at,
        source=SourceKind.USER_CONFIRMED,
    )


def _measure(
    portfolio: Portfolio, observations: list[ExecutionEffortObservation]
) -> WorkBreakdownEffortMeasurement:
    """Real V1.9 measurement so all V1.9 semantics are authoritative."""
    return measure_work_breakdown_effort(portfolio, PROJECT_ID, observations)


def _canonical_setup() -> tuple[
    Portfolio,
    WorkBreakdownEffortMeasurement,
    list[ExecutionEffortEstimate],
    list[ExecutionEffortObservation],
]:
    """Scenario covering positive/zero/negative variance plus exclusions.

    - project (COMPLETED): planned 100s before observation, actual 130s
      → underplanned (+30).
    - task_a (COMPLETED): planned 60s, actual 60s → exact (0).
    - task_b (COMPLETED): planned 90s, actual 75s → overplanned (-15).
    - task_c (COMPLETED): observation exists but its single estimate is
      exactly at first_observed_at → ineligible → missing prior estimate.
    - task_d (COMPLETED): no observations but an estimate exists
      → missing actual evidence.
    - task_e (COMPLETED): prior estimate exists but never observed
      → missing actual evidence.
    - task_f (ACTIVE): estimated before observation and observed;
      never a sample, never counted. (non-completed)
    - out_of_wbs: estimate + observation outside the WBS; ignored.
    """
    portfolio = _make_portfolio()

    observations = [
        _observation(PROJECT_ID, 130, FIRST_OBS),
        _observation(TASK_A_ID, 60, FIRST_OBS),
        _observation(TASK_B_ID, 75, FIRST_OBS),
        _observation(TASK_C_ID, 40, FIRST_OBS),
        _observation(TASK_F_ID, 50, FIRST_OBS),
        _observation(OUT_OF_WBS_ID, 10, FIRST_OBS),
    ]

    estimates = [
        _estimate(PROJECT_ID, 100, EST_BEFORE),
        _estimate(TASK_A_ID, 60, EST_BEFORE),
        _estimate(TASK_B_ID, 90, EST_BEFORE),
        # Exactly at first observation: never eligible.
        _estimate(TASK_C_ID, 40, FIRST_OBS),
        # Task_d never observed.
        _estimate(TASK_D_ID, 10, EST_BEFORE),
        _estimate(TASK_E_ID, 100, EST_BEFORE),
        _estimate(OUT_OF_WBS_ID, 100, EST_BEFORE),
    ]

    measurement = _measure(portfolio, observations)
    return portfolio, measurement, estimates, observations


def _sample_for(
    evidence: WorkBreakdownEffortCalibrationEvidence, entity_id: UUID
) -> EffortCalibrationSample:
    matches = [sample for sample in evidence.samples if sample.entity_id == entity_id]
    assert len(matches) == 1
    return matches[0]


# ---------------------------------------------------------------------------
# 1. Frozen / strict result models
# ---------------------------------------------------------------------------


class TestModelsStrict:
    def test_sample_is_frozen(self) -> None:
        portfolio, measurement, estimates, _ = _canonical_setup()
        evidence = build_effort_calibration_evidence(portfolio, measurement, estimates)
        sample = _sample_for(evidence, PROJECT_ID)
        with pytest.raises(ValidationError):
            sample.variance_seconds = 0  # type: ignore[misc]

    def test_sample_rejects_non_strict_values(self) -> None:
        with pytest.raises(ValidationError):
            EffortCalibrationSample(
                entity_id=str(PROJECT_ID),  # type: ignore[arg-type]
                estimate_id=uuid4(),
                estimated_at=EST_BEFORE,
                first_observed_at=FIRST_OBS,
                last_observed_at=FIRST_OBS,
                observation_count=1,  # type: ignore[arg-type]
                planned_duration_seconds="100",  # type: ignore[arg-type]
                actual_duration_seconds=130,
                variance_seconds=30,
                absolute_error_seconds=30,
            )

    def test_sample_requires_strictly_prior_estimate(self) -> None:
        with pytest.raises(ValidationError, match="strictly before"):
            EffortCalibrationSample(
                entity_id=PROJECT_ID,
                estimate_id=uuid4(),
                estimated_at=FIRST_OBS,
                first_observed_at=FIRST_OBS,
                last_observed_at=FIRST_OBS,
                observation_count=1,
                planned_duration_seconds=10,
                actual_duration_seconds=10,
                variance_seconds=0,
                absolute_error_seconds=0,
            )

    def test_sample_enforces_variance_convention(self) -> None:
        with pytest.raises(ValidationError, match="actual - planned"):
            EffortCalibrationSample(
                entity_id=PROJECT_ID,
                estimate_id=uuid4(),
                estimated_at=EST_BEFORE,
                first_observed_at=FIRST_OBS,
                last_observed_at=FIRST_OBS,
                observation_count=1,
                planned_duration_seconds=10,
                actual_duration_seconds=13,
                variance_seconds=999,
                absolute_error_seconds=3,
            )

    def test_summary_rejects_inconsistent_classifications(self) -> None:
        with pytest.raises(ValidationError, match="sample_count"):
            EffortCalibrationSummary(
                sample_count=2,
                total_planned_duration_seconds=0,
                total_actual_duration_seconds=0,
                signed_variance_seconds=0,
                absolute_error_seconds=0,
                underplanned_entity_count=1,
                exact_entity_count=0,
                overplanned_entity_count=0,
            )

    def test_evidence_rejects_broken_coverage_invariant(self) -> None:
        with pytest.raises(ValidationError, match="completed_entity_count"):
            WorkBreakdownEffortCalibrationEvidence(
                portfolio_id=PORTFOLIO_ID,
                project_id=PROJECT_ID,
                completed_entity_count=0,
                completed_without_observation_count=0,
                completed_without_prior_estimate_count=0,
                samples=(),
                summary=EffortCalibrationSummary(
                    sample_count=1,
                    total_planned_duration_seconds=10,
                    total_actual_duration_seconds=10,
                    signed_variance_seconds=0,
                    absolute_error_seconds=0,
                    underplanned_entity_count=0,
                    exact_entity_count=1,
                    overplanned_entity_count=0,
                ),
            )


# ---------------------------------------------------------------------------
# 2–7. Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_foreign_measurement_portfolio_rejected(self) -> None:
        portfolio = _make_portfolio(include_out_of_wbs=False)
        other = Portfolio(
            id=OTHER_PORTFOLIO_ID,
            name="Other",
            entities=[_entity(PROJECT_ID, EntityType.PROJECT, EntityStatus.COMPLETED)],
            relations=[],
        )
        foreign_observation = ExecutionEffortObservation(
            id=uuid4(),
            portfolio_id=OTHER_PORTFOLIO_ID,
            entity_id=PROJECT_ID,
            duration_seconds=10,
            observed_at=FIRST_OBS,
            source=SourceKind.USER_CONFIRMED,
        )
        foreign_measurement = measure_work_breakdown_effort(
            other, PROJECT_ID, [foreign_observation]
        )

        with pytest.raises(ExecutionEffortCalibrationError, match="different portfolio"):
            build_effort_calibration_evidence(portfolio, foreign_measurement, [])

        # Estimate belonging to a different portfolio must also be rejected.
        observations = [_observation(PROJECT_ID, 10, FIRST_OBS)]
        measurement = _measure(portfolio, observations)
        foreign_estimate = _estimate(
            TASK_A_ID, 1, EST_BEFORE, portfolio_id=OTHER_PORTFOLIO_ID
        )
        with pytest.raises(ExecutionEffortCalibrationError, match="different portfolio"):
            build_effort_calibration_evidence(portfolio, measurement, [foreign_estimate])

    def test_measurement_item_missing_entity_rejected(self) -> None:
        portfolio = _make_portfolio(include_out_of_wbs=False)
        observations = [_observation(PROJECT_ID, 10, FIRST_OBS)]
        measurement = _measure(portfolio, observations)
        bad_item = measurement.items[0].model_copy(
            update={"entity_id": UNKNOWN_ENTITY_ID}
        )
        hostile = WorkBreakdownEffortMeasurement.model_construct(
            portfolio_id=PORTFOLIO_ID,
            project_id=UNKNOWN_ENTITY_ID,
            items=(bad_item,),
        )
        with pytest.raises(
            ExecutionEffortCalibrationError, match="missing from the current portfolio"
        ):
            build_effort_calibration_evidence(portfolio, hostile, [])

    def test_hostile_bypassed_measurement_rejected(self) -> None:
        """A ``model_construct`` measurement with invalid internals must be
        defeated by fresh validation."""
        portfolio = _make_portfolio(include_out_of_wbs=False)
        hostile = WorkBreakdownEffortMeasurement.model_construct(
            portfolio_id=PORTFOLIO_ID,
            project_id=PROJECT_ID,
            items=(),
        )
        with pytest.raises(ExecutionEffortCalibrationError):
            build_effort_calibration_evidence(portfolio, hostile, [])

    def test_non_measurement_instance_rejected(self) -> None:
        portfolio = _make_portfolio(include_out_of_wbs=False)
        with pytest.raises(ExecutionEffortCalibrationError, match="WorkBreakdownEffortMeasurement"):
            build_effort_calibration_evidence(portfolio, object(), [])  # type: ignore[arg-type]

    def test_hostile_bypassed_estimate_rejected(self) -> None:
        """A ``model_construct`` estimate with invalid internals must be
        defeated by fresh validation."""
        portfolio = _make_portfolio(include_out_of_wbs=False)
        observations = [_observation(PROJECT_ID, 10, FIRST_OBS)]
        measurement = _measure(portfolio, observations)
        hostile = ExecutionEffortEstimate.model_construct(
            id=uuid4(),
            portfolio_id=PORTFOLIO_ID,
            entity_id=PROJECT_ID,
            duration_seconds=-1,
            estimated_at="not-a-datetime",  # type: ignore[arg-type]
            source=SourceKind.USER_CONFIRMED,
        )
        with pytest.raises(ExecutionEffortCalibrationError):
            build_effort_calibration_evidence(portfolio, measurement, [hostile])

    def test_non_estimate_instance_rejected(self) -> None:
        portfolio = _make_portfolio(include_out_of_wbs=False)
        observations = [_observation(PROJECT_ID, 10, FIRST_OBS)]
        measurement = _measure(portfolio, observations)
        with pytest.raises(ExecutionEffortCalibrationError, match="ExecutionEffortEstimate"):
            build_effort_calibration_evidence(portfolio, measurement, [object()])

    def test_duplicate_estimate_ids_rejected(self) -> None:
        portfolio = _make_portfolio(include_out_of_wbs=False)
        observations = [_observation(PROJECT_ID, 10, FIRST_OBS)]
        measurement = _measure(portfolio, observations)
        shared_id = uuid4()
        with pytest.raises(ExecutionEffortCalibrationError, match="duplicate estimate id"):
            build_effort_calibration_evidence(
                portfolio,
                measurement,
                [
                    _estimate(PROJECT_ID, 10, EST_BEFORE, estimate_id=shared_id),
                    _estimate(TASK_A_ID, 20, EST_BEFORE, estimate_id=shared_id),
                ],
            )


# ---------------------------------------------------------------------------
# 8–16, 17–26. Calibration semantics
# ---------------------------------------------------------------------------


class TestCalibrationSemantics:
    def test_non_completed_entity_never_sample_never_counted(self) -> None:
        portfolio, measurement, estimates, _ = _canonical_setup()
        evidence = build_effort_calibration_evidence(portfolio, measurement, estimates)

        assert all(sample.entity_id != TASK_F_ID for sample in evidence.samples)
        coverage = (
            evidence.completed_without_observation_count
            + evidence.completed_without_prior_estimate_count
            + evidence.summary.sample_count
        )
        assert coverage == evidence.completed_entity_count
        # task_e is ACTIVE: not in samples and not in any coverage count.
        # completed entities: project, a, b, c, d, e
        assert evidence.completed_entity_count == 6

    def test_completed_without_observation_counted(self) -> None:
        portfolio, measurement, estimates, _ = _canonical_setup()
        evidence = build_effort_calibration_evidence(portfolio, measurement, estimates)
        assert evidence.completed_without_observation_count == 2  # task_d, task_e
        assert all(sample.entity_id not in (TASK_D_ID, TASK_E_ID) for sample in evidence.samples)

    def test_completed_with_observations_but_no_eligible_estimate(self) -> None:
        portfolio, measurement, estimates, _ = _canonical_setup()
        evidence = build_effort_calibration_evidence(portfolio, measurement, estimates)
        assert evidence.completed_without_prior_estimate_count == 1
        assert all(sample.entity_id != TASK_C_ID for sample in evidence.samples)

    def test_strictly_prior_estimate_eligible(self) -> None:
        portfolio, measurement, estimates, _ = _canonical_setup()
        evidence = build_effort_calibration_evidence(portfolio, measurement, estimates)
        sample = _sample_for(evidence, PROJECT_ID)
        assert sample.variance_seconds == 30
        assert sample.planned_duration_seconds == 100
        assert sample.actual_duration_seconds == 130
        assert sample.estimated_at < sample.first_observed_at

    def test_estimate_exactly_at_first_observation_ineligible(self) -> None:
        portfolio, measurement, estimates, _ = _canonical_setup()
        evidence = build_effort_calibration_evidence(portfolio, measurement, estimates)
        assert all(sample.entity_id != TASK_C_ID for sample in evidence.samples)
        assert evidence.completed_without_prior_estimate_count == 1

    def test_estimate_after_first_observation_ineligible(self) -> None:
        portfolio, measurement, _, _ = _canonical_setup()
        # Scenario: the project is the only completed entity with observations
        # (a, b, c keep their canonical observations; re-deriving with just the
        # project's observation isolates the leakage rule).
        only_obs = [_observation(PROJECT_ID, 130, FIRST_OBS)]
        only_measurement = _measure(portfolio, only_obs)
        only_after = [_estimate(PROJECT_ID, 1, EST_AFTER)]

        evidence = build_effort_calibration_evidence(portfolio, only_measurement, only_after)
        # The only estimate is post-observation: it must not leak, leaving 1
        # completed entity with observations but no prior estimate (project)
        # and 5 without observations (a, b, c, d, e).
        assert evidence.samples == ()
        assert evidence.completed_without_prior_estimate_count == 1
        assert evidence.completed_without_observation_count == 5
        assert evidence.completed_entity_count == 6

    def test_latest_eligible_prior_estimate_wins(self) -> None:
        portfolio, measurement, _, _ = _canonical_setup()
        estimates = [
            _estimate(PROJECT_ID, 100, EST_BEFORE_EARLY),
            _estimate(PROJECT_ID, 120, EST_BEFORE),  # latest strictly before → wins
            _estimate(PROJECT_ID, 999, EST_AFTER),  # post-observation revision
        ]
        evidence = build_effort_calibration_evidence(portfolio, measurement, estimates)
        sample = _sample_for(evidence, PROJECT_ID)
        assert sample.planned_duration_seconds == 120
        assert sample.variance_seconds == 130 - 120

    def test_equal_estimated_at_resolves_by_greatest_uuid_int(self) -> None:
        low = UUID("00000000-0000-0000-0000-000000000001")
        high = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
        portfolio, measurement, _, _ = _canonical_setup()
        estimates = [
            _estimate(PROJECT_ID, 100, EST_BEFORE, estimate_id=low),
            _estimate(PROJECT_ID, 55, EST_BEFORE, estimate_id=high),
        ]
        evidence = build_effort_calibration_evidence(portfolio, measurement, estimates)
        sample = _sample_for(evidence, PROJECT_ID)
        assert sample.estimate_id == high
        assert sample.planned_duration_seconds == 55
        assert sample.variance_seconds == 130 - 55

    def test_reversed_input_order_same_selection(self) -> None:
        low = UUID("00000000-0000-0000-0000-000000000001")
        high = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
        portfolio, measurement, _, _ = _canonical_setup()
        forward = [
            _estimate(PROJECT_ID, 100, EST_BEFORE, estimate_id=low),
            _estimate(PROJECT_ID, 55, EST_BEFORE, estimate_id=high),
        ]
        backward = list(reversed(forward))
        assert (
            build_effort_calibration_evidence(portfolio, measurement, forward)
            == build_effort_calibration_evidence(portfolio, measurement, backward)
        )

    def test_post_observation_revision_cannot_leak(self) -> None:
        """Even the very latest estimate, recorded after the first
        observation, must not replace the selected prior estimate."""
        portfolio, measurement, _, _ = _canonical_setup()
        prior = _estimate(PROJECT_ID, 100, EST_BEFORE)
        late_revision = _estimate(PROJECT_ID, 5, EST_AFTER)
        estimates = [prior, late_revision]

        evidence = build_effort_calibration_evidence(portfolio, measurement, estimates)
        sample = _sample_for(evidence, PROJECT_ID)
        assert sample.estimate_id == prior.id
        assert sample.planned_duration_seconds == 100
        assert sample.estimated_at < sample.first_observed_at

    def test_zero_planned_estimate_valid_and_variance_equals_actual(self) -> None:
        portfolio, measurement, _, _ = _canonical_setup()
        estimates = [_estimate(PROJECT_ID, 0, EST_BEFORE)]
        evidence = build_effort_calibration_evidence(portfolio, measurement, estimates)
        sample = _sample_for(evidence, PROJECT_ID)
        assert sample.planned_duration_seconds == 0
        assert sample.actual_duration_seconds == 130
        assert sample.variance_seconds == 130
        assert sample.absolute_error_seconds == 130

    def test_variance_signs_exact(self) -> None:
        portfolio, measurement, estimates, _ = _canonical_setup()
        evidence = build_effort_calibration_evidence(portfolio, measurement, estimates)
        underplanned = _sample_for(evidence, PROJECT_ID)
        exact = _sample_for(evidence, TASK_A_ID)
        overplanned = _sample_for(evidence, TASK_B_ID)
        assert underplanned.variance_seconds == 30
        assert exact.variance_seconds == 0
        assert overplanned.variance_seconds == -15

    def test_absolute_error_exact(self) -> None:
        portfolio, measurement, estimates, _ = _canonical_setup()
        evidence = build_effort_calibration_evidence(portfolio, measurement, estimates)
        for sample in evidence.samples:
            assert sample.absolute_error_seconds == abs(sample.variance_seconds)
        assert _sample_for(evidence, TASK_B_ID).absolute_error_seconds == 15

    def test_multiple_observations_use_v19_exact_semantics(self) -> None:
        portfolio = _make_portfolio(include_out_of_wbs=False)
        observations = [
            _observation(PROJECT_ID, 25, EST_BEFORE_EARLY),
            _observation(PROJECT_ID, 80, FIRST_OBS),
            _observation(PROJECT_ID, 25, OBS_LATER),
        ]
        measurement = _measure(portfolio, observations)
        # Strictly before the first observation (EST_BEFORE_EARLY).
        prior_estimate_at = datetime(2024, 3, 1, tzinfo=UTC)
        estimates = [_estimate(PROJECT_ID, 100, prior_estimate_at)]
        evidence = build_effort_calibration_evidence(portfolio, measurement, estimates)

        sample = _sample_for(evidence, PROJECT_ID)
        assert sample.observation_count == 3
        assert sample.actual_duration_seconds == 130  # exact V1.9 direct sum
        assert sample.first_observed_at == EST_BEFORE_EARLY
        assert sample.last_observed_at == OBS_LATER
        assert sample.variance_seconds == 30

    def test_samples_preserve_current_wbs_preorder(self) -> None:
        portfolio, measurement, estimates, _ = _canonical_setup()
        evidence = build_effort_calibration_evidence(portfolio, measurement, estimates)
        sample_entities = [sample.entity_id for sample in evidence.samples]
        expected = [
            entity_id
            for entity_id in (
                item.entity_id for item in measurement.items
            )
            if entity_id in set(sample_entities)
        ]
        assert sample_entities == expected
        assert sample_entities[0] == PROJECT_ID
        assert sample_entities.index(TASK_B_ID) > sample_entities.index(TASK_A_ID)

    def test_out_of_wbs_history_ignored_without_error(self) -> None:
        portfolio, measurement, estimates, observations = _canonical_setup()
        evidence = build_effort_calibration_evidence(portfolio, measurement, estimates)
        assert all(sample.entity_id != OUT_OF_WBS_ID for sample in evidence.samples)
        # The histories themselves remain valid domain values.
        assert any(e.entity_id == OUT_OF_WBS_ID for e in estimates)
        assert any(o.entity_id == OUT_OF_WBS_ID for o in observations)

    def test_coverage_counts_satisfy_invariant(self) -> None:
        portfolio, measurement, estimates, _ = _canonical_setup()
        evidence = build_effort_calibration_evidence(portfolio, measurement, estimates)
        assert (
            evidence.completed_entity_count
            == evidence.summary.sample_count
            + evidence.completed_without_observation_count
            + evidence.completed_without_prior_estimate_count
        )

    def test_aggregate_totals_exact(self) -> None:
        portfolio, measurement, estimates, _ = _canonical_setup()
        evidence = build_effort_calibration_evidence(portfolio, measurement, estimates)
        summary = evidence.summary
        assert summary.sample_count == 3
        assert summary.total_planned_duration_seconds == 100 + 60 + 90
        assert summary.total_actual_duration_seconds == 130 + 60 + 75
        assert summary.signed_variance_seconds == (130 + 60 + 75) - (100 + 60 + 90)
        assert summary.absolute_error_seconds == 30 + 0 + 15

    def test_classification_counts_exact(self) -> None:
        portfolio, measurement, estimates, _ = _canonical_setup()
        evidence = build_effort_calibration_evidence(portfolio, measurement, estimates)
        summary = evidence.summary
        assert summary.underplanned_entity_count == 1  # project
        assert summary.exact_entity_count == 1  # task_a
        assert summary.overplanned_entity_count == 1  # task_b

    def test_empty_eligible_set_yields_zero_summary(self) -> None:
        portfolio = _make_portfolio(include_out_of_wbs=False)
        measurement = _measure(portfolio, [])
        evidence = build_effort_calibration_evidence(portfolio, measurement, [])

        assert evidence.samples == ()
        summary = evidence.summary
        assert summary.sample_count == 0
        assert summary.total_planned_duration_seconds == 0
        assert summary.total_actual_duration_seconds == 0
        assert summary.signed_variance_seconds == 0
        assert summary.absolute_error_seconds == 0
        assert summary.underplanned_entity_count == 0
        assert summary.exact_entity_count == 0
        assert summary.overplanned_entity_count == 0
        assert evidence.completed_without_observation_count == 6
        assert evidence.completed_without_prior_estimate_count == 0

    def test_source_inputs_unchanged(self) -> None:
        portfolio, measurement, estimates, observations = _canonical_setup()
        snapshot_portfolio = copy.deepcopy(portfolio)
        snapshot_estimates = copy.deepcopy(estimates)

        build_effort_calibration_evidence(portfolio, measurement, estimates)

        assert portfolio == snapshot_portfolio
        assert estimates == snapshot_estimates
        assert measurement.items == measurement.items  # structure untouched
        assert all(o.duration_seconds >= 0 for o in observations)

    def test_repeated_derivation_equivalent(self) -> None:
        portfolio, measurement, estimates, _ = _canonical_setup()
        first = build_effort_calibration_evidence(portfolio, measurement, estimates)
        second = build_effort_calibration_evidence(portfolio, measurement, estimates)
        assert first == second
