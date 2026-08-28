"""Durable application tests for V1.16 human decisions over V1.15 proposals.

The V1.16 application boundary delegates the ENTIRE factor derivation to the
existing durable V1.15 boundary (monkeypatched here with a fake), validates
the command strictly before ANY I/O, locates the exact V1.15 segment,
enforces the human decision rule (ACCEPT only over AVAILABLE), appends the
immutable record exactly ONCE, and appends NOTHING on any failure path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from trajectory_os.application import (
    execution_effort_calibration_factor_decisions as decisions_app,
)
from trajectory_os.application.execution_effort_calibration_factor_decisions import (
    DurableEffortCalibrationFactorDecisionError,
    EffortCalibrationFactorDecisionRejectedForSegmentError,
    EffortCalibrationFactorDecisionSegmentNotFoundError,
    record_effort_calibration_factor_decision,
)
from trajectory_os.application.execution_effort_calibration_factor_proposals import (
    DurableEffortCalibrationFactorProposalError,
)
from trajectory_os.domain.entities import EntityType
from trajectory_os.domain.execution_effort_calibration_factor_decisions import (
    EffortCalibrationDecision,
    EffortCalibrationFactorDecision,
)
from trajectory_os.domain.execution_effort_calibration_factor_proposals import (
    EffortCalibrationFactorProposalReason,
    EffortCalibrationTypeFactorProposal,
    WorkBreakdownEffortCalibrationFactorProposalSet,
)
from trajectory_os.domain.portfolio import Portfolio

PORTFOLIO_ID = UUID("61616161-6161-4161-8161-616161616161")
PROJECT_ID = UUID("62626262-6262-4262-8262-626262626262")
DECISION_ID = UUID("63636363-6363-4363-8363-636363636363")
DECIDED_AT = datetime(2025, 7, 1, 8, 30, tzinfo=UTC)


def _task_segment() -> EffortCalibrationTypeFactorProposal:
    return EffortCalibrationTypeFactorProposal(
        entity_type=EntityType.TASK,
        sample_count=5,
        total_planned_duration_seconds=200,
        total_actual_duration_seconds=150,
        proposal_available=True,
        reason=EffortCalibrationFactorProposalReason.AVAILABLE,
        factor_numerator=3,
        factor_denominator=4,
    )


def _deliverable_segment() -> EffortCalibrationTypeFactorProposal:
    return EffortCalibrationTypeFactorProposal(
        entity_type=EntityType.DELIVERABLE,
        sample_count=1,
        total_planned_duration_seconds=100,
        total_actual_duration_seconds=120,
        proposal_available=False,
        reason=EffortCalibrationFactorProposalReason.INSUFFICIENT_SAMPLES,
        factor_numerator=None,
        factor_denominator=None,
    )


def _project_segment() -> EffortCalibrationTypeFactorProposal:
    return EffortCalibrationTypeFactorProposal(
        entity_type=EntityType.PROJECT,
        sample_count=3,
        total_planned_duration_seconds=0,
        total_actual_duration_seconds=5,
        proposal_available=False,
        reason=(
            EffortCalibrationFactorProposalReason.ZERO_TOTAL_PLANNED_DURATION
        ),
        factor_numerator=None,
        factor_denominator=None,
    )


def _v115_set() -> WorkBreakdownEffortCalibrationFactorProposalSet:
    return WorkBreakdownEffortCalibrationFactorProposalSet(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        minimum_required_sample_count=3,
        available_proposal_count=1,
        unavailable_proposal_count=2,
        segments=(
            _task_segment(),
            _deliverable_segment(),
            _project_segment(),
        ),
    )


@dataclass
class FakeV115Boundary:
    """Stands in for build_effort_calibration_factor_proposals_durably."""

    result: WorkBreakdownEffortCalibrationFactorProposalSet
    error: type[BaseException] | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    def __call__(
        self, **kwargs: object
    ) -> WorkBreakdownEffortCalibrationFactorProposalSet:
        self.calls.append(dict(kwargs))  # type: ignore[arg-type]
        if self.error is not None:
            raise self.error("v1.15 boundary failed")
        return self.result


@dataclass
class FakePortfolioRepository:
    save_calls: list[Portfolio] = field(default_factory=list)

    def save(self, portfolio: Portfolio) -> Portfolio:
        self.save_calls.append(portfolio)
        return portfolio

    def load(self, portfolio_id: UUID) -> Portfolio:
        raise AssertionError(
            "portfolio load is not part of the V1.16 recording path"
        )


@dataclass
class FakeReader:
    calls: int = 0

    def list_for_portfolio(self, portfolio_id: UUID) -> tuple[object, ...]:
        del portfolio_id
        self.calls += 1
        return ()


@dataclass
class FakeDecisionRepository:
    added: list[EffortCalibrationFactorDecision] = field(default_factory=list)
    error: type[BaseException] | None = None

    def add(self, decision: EffortCalibrationFactorDecision) -> None:
        if self.error is not None:
            raise self.error("record insert failed")
        self.added.append(decision)

    def list_history(
        self,
        portfolio_id: UUID,
        project_id: UUID,
        entity_type: EntityType,
    ) -> tuple[EffortCalibrationFactorDecision, ...]:
        return tuple(
            decision
            for decision in self.added
            if (
                decision.portfolio_id == portfolio_id
                and decision.project_id == project_id
                and decision.entity_type == entity_type
            )
        )


@dataclass
class Harness:
    v115: FakeV115Boundary
    portfolio_repository: FakePortfolioRepository
    estimate_reader: FakeReader
    observation_reader: FakeReader
    decision_repository: FakeDecisionRepository

    def record(self, **overrides: object) -> EffortCalibrationFactorDecision:
        base: dict[str, object] = {
            "portfolio_id": PORTFOLIO_ID,
            "project_id": PROJECT_ID,
            "entity_type": EntityType.TASK,
            "minimum_sample_count": 3,
            "decision": EffortCalibrationDecision.ACCEPT,
            "decision_id": DECISION_ID,
            "decided_at": DECIDED_AT,
            "portfolio_repository": self.portfolio_repository,
            "estimate_reader": self.estimate_reader,
            "observation_reader": self.observation_reader,
            "decision_repository": self.decision_repository,
        }
        base.update(overrides)
        return record_effort_calibration_factor_decision(**base)  # type: ignore[arg-type]


def make_harness(
    monkeypatch: pytest.MonkeyPatch,
    v115_error: type[BaseException] | None = None,
) -> Harness:
    harness = Harness(
        v115=FakeV115Boundary(result=_v115_set(), error=v115_error),
        portfolio_repository=FakePortfolioRepository(),
        estimate_reader=FakeReader(),
        observation_reader=FakeReader(),
        decision_repository=FakeDecisionRepository(),
    )
    monkeypatch.setattr(
        decisions_app,
        "build_effort_calibration_factor_proposals_durably",
        harness.v115,
    )
    return harness


# Exact V1.15 snapshot expectations per entity type (from _v115_set()).
SEGMENT_SNAPSHOTS = {
    EntityType.TASK: {
        "sample_count": 5,
        "total_planned_duration_seconds": 200,
        "total_actual_duration_seconds": 150,
        "proposal_available": True,
        "proposal_reason": EffortCalibrationFactorProposalReason.AVAILABLE,
        "factor_numerator": 3,
        "factor_denominator": 4,
    },
    EntityType.DELIVERABLE: {
        "sample_count": 1,
        "total_planned_duration_seconds": 100,
        "total_actual_duration_seconds": 120,
        "proposal_available": False,
        "proposal_reason": (
            EffortCalibrationFactorProposalReason.INSUFFICIENT_SAMPLES
        ),
        "factor_numerator": None,
        "factor_denominator": None,
    },
    EntityType.PROJECT: {
        "sample_count": 3,
        "total_planned_duration_seconds": 0,
        "total_actual_duration_seconds": 5,
        "proposal_available": False,
        "proposal_reason": (
            EffortCalibrationFactorProposalReason.ZERO_TOTAL_PLANNED_DURATION
        ),
        "factor_numerator": None,
        "factor_denominator": None,
    },
}

VALID_DECISIONS = {
    EntityType.TASK: (
        EffortCalibrationDecision.ACCEPT,
        EffortCalibrationDecision.REJECT,
        EffortCalibrationDecision.DEFER,
    ),
    EntityType.DELIVERABLE: (
        EffortCalibrationDecision.REJECT,
        EffortCalibrationDecision.DEFER,
    ),
    EntityType.PROJECT: (
        EffortCalibrationDecision.REJECT,
        EffortCalibrationDecision.DEFER,
    ),
}


def test_invalid_command_values_fail_before_io_or_append_without_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness(monkeypatch)

    invalid_commands = [
        {"decision_id": str(DECISION_ID)},
        {"decision_id": "not-a-uuid"},
        {"portfolio_id": str(PORTFOLIO_ID)},
        {"project_id": "a project name"},
        {"entity_type": "task"},
        {"minimum_sample_count": 0},
        {"minimum_sample_count": -1},
        {"minimum_sample_count": True},
        {"minimum_sample_count": 1.0},
        {"minimum_sample_count": "3"},
        {"minimum_sample_count": None},
        {"decision": "accept"},
        {"decision": None},
        {"decision": 1},
        {"decided_at": datetime(2025, 7, 1, 8, 30)},
        {"decided_at": "2025-07-01T08:30:00+00:00"},
        {"decided_at": None},
    ]
    for command in invalid_commands:
        with pytest.raises(DurableEffortCalibrationFactorDecisionError):
            harness.record(**command)

    assert harness.v115.calls == []
    assert harness.decision_repository.added == []
    assert harness.estimate_reader.calls == 0
    assert harness.observation_reader.calls == 0
    assert harness.portfolio_repository.save_calls == []


def test_delegation_is_exactly_to_the_existing_v115_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness(monkeypatch)
    harness.record(decision=EffortCalibrationDecision.DEFER)

    assert len(harness.v115.calls) == 1
    call = harness.v115.calls[0]
    assert call["portfolio_id"] == PORTFOLIO_ID
    assert call["project_id"] == PROJECT_ID
    assert call["minimum_sample_count"] == 3
    assert call["portfolio_repository"] is harness.portfolio_repository
    assert call["estimate_reader"] is harness.estimate_reader
    assert call["observation_reader"] is harness.observation_reader


def test_accept_records_the_exact_v115_available_snapshot_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness(monkeypatch)
    record = harness.record(
        entity_type=EntityType.TASK,
        decision=EffortCalibrationDecision.ACCEPT,
    )

    # Exactly one append of exactly this record object.
    assert len(harness.decision_repository.added) == 1
    assert harness.decision_repository.added[0] is record

    # Exact snapshot, copied from the V1.15 set/segment: no recombination,
    # no recomputation, no float.
    assert record.decision_id is DECISION_ID
    assert record.portfolio_id is PORTFOLIO_ID
    assert record.project_id is PROJECT_ID
    assert record.entity_type is EntityType.TASK
    assert record.sample_count == 5
    assert record.minimum_required_sample_count == 3
    assert record.total_planned_duration_seconds == 200
    assert record.total_actual_duration_seconds == 150
    assert record.proposal_available is True
    assert (
        record.proposal_reason is EffortCalibrationFactorProposalReason.AVAILABLE
    )
    assert record.factor_numerator == 3
    assert record.factor_denominator == 4
    assert type(record.factor_numerator) is int
    assert type(record.factor_denominator) is int

    # Human decision identity and caller-supplied exact timestamp.
    assert record.decision is EffortCalibrationDecision.ACCEPT
    assert record.decided_at == DECIDED_AT
    assert record.decided_at is DECIDED_AT


@pytest.mark.parametrize(
    ("entity_type", "decision"),
    [
        (EntityType.TASK, EffortCalibrationDecision.REJECT),
        (EntityType.TASK, EffortCalibrationDecision.DEFER),
        (EntityType.DELIVERABLE, EffortCalibrationDecision.REJECT),
        (EntityType.DELIVERABLE, EffortCalibrationDecision.DEFER),
        (EntityType.PROJECT, EffortCalibrationDecision.REJECT),
        (EntityType.PROJECT, EffortCalibrationDecision.DEFER),
    ],
)
def test_reject_and_defer_snapshot_any_valid_v115_segment_exactly(
    monkeypatch: pytest.MonkeyPatch,
    entity_type: EntityType,
    decision: EffortCalibrationDecision,
) -> None:
    harness = make_harness(monkeypatch)
    record = harness.record(entity_type=entity_type, decision=decision)

    assert len(harness.decision_repository.added) == 1
    assert record.entity_type == entity_type
    assert record.decision == decision
    snapshot = SEGMENT_SNAPSHOTS[entity_type]
    assert record.sample_count == snapshot["sample_count"]
    assert record.total_planned_duration_seconds == (
        snapshot["total_planned_duration_seconds"]
    )
    assert record.total_actual_duration_seconds == (
        snapshot["total_actual_duration_seconds"]
    )
    assert record.proposal_available is snapshot["proposal_available"]
    assert record.proposal_reason is snapshot["proposal_reason"]
    assert record.factor_numerator is snapshot["factor_numerator"]
    assert record.factor_denominator is snapshot["factor_denominator"]


def test_accept_over_insufficient_samples_fails_before_append(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness(monkeypatch)
    with pytest.raises(EffortCalibrationFactorDecisionRejectedForSegmentError):
        harness.record(entity_type=EntityType.DELIVERABLE)
    assert harness.decision_repository.added == []
    assert len(harness.v115.calls) == 1  # derived once, still rejected before append


def test_accept_over_zero_total_planned_fails_before_append(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness(monkeypatch)
    with pytest.raises(EffortCalibrationFactorDecisionRejectedForSegmentError):
        harness.record(entity_type=EntityType.PROJECT)
    assert harness.decision_repository.added == []


def test_requesting_a_missing_segment_fails_before_append(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness(monkeypatch)
    with pytest.raises(EffortCalibrationFactorDecisionSegmentNotFoundError):
        harness.record(entity_type=EntityType.WORK_PACKAGE)
    assert harness.decision_repository.added == []


def test_v115_boundary_failure_propagates_and_appends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness(monkeypatch, v115_error=(DurableEffortCalibrationFactorProposalError))
    with pytest.raises(DurableEffortCalibrationFactorProposalError):
        harness.record(entity_type=EntityType.TASK)
    assert harness.decision_repository.added == []
    assert harness.estimate_reader.calls == 0
    assert harness.observation_reader.calls == 0


def test_decision_repository_failure_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness(monkeypatch)
    harness.decision_repository.error = RuntimeError
    with pytest.raises(RuntimeError, match="record insert failed"):
        harness.record(entity_type=EntityType.TASK)


def test_returned_record_is_immutable_and_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness(monkeypatch)
    record = harness.record(entity_type=EntityType.TASK)
    with pytest.raises(ValidationError):
        record.proposal_reason = EffortCalibrationFactorProposalReason.INSUFFICIENT_SAMPLES  # type: ignore[misc]
    with pytest.raises(ValidationError):
        record.decision = EffortCalibrationDecision.DEFER  # type: ignore[misc]
    # Explicit caller values are preserved, never inferred.
    assert record.decision_id == DECISION_ID
    assert record.decided_at == DECIDED_AT


def test_explicit_aware_timestamp_is_preserved_even_outside_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness(monkeypatch)
    decided_at = datetime(2027, 3, 4, 5, 6, 7, tzinfo=timezone(timedelta(hours=2)))
    record = harness.record(decided_at=decided_at)
    assert record.decided_at == decided_at
    assert record.decided_at.utcoffset() == timedelta(hours=2)
    assert record.decided_at is decided_at


def test_history_reader_returns_only_matching_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness(monkeypatch)
    harness.record(entity_type=EntityType.TASK)
    harness.record(
        entity_type=EntityType.DELIVERABLE,
        decision=EffortCalibrationDecision.REJECT,
        decision_id=UUID("64646464-6464-4464-8464-646464646464"),
    )

    tasks = harness.decision_repository.list_history(
        PORTFOLIO_ID, PROJECT_ID, EntityType.TASK
    )
    assert len(tasks) == 1
    assert tasks[0].entity_type is EntityType.TASK

    deliverables = harness.decision_repository.list_history(
        PORTFOLIO_ID, PROJECT_ID, EntityType.DELIVERABLE
    )
    assert len(deliverables) == 1
    assert deliverables[0].entity_type is EntityType.DELIVERABLE
    assert deliverables[0].decision is EffortCalibrationDecision.REJECT

    work_packages = harness.decision_repository.list_history(
        PORTFOLIO_ID, PROJECT_ID, EntityType.WORK_PACKAGE
    )
    assert work_packages == ()
