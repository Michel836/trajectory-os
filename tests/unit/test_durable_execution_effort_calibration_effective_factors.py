"""Unit tests for the V1.17 durable read-only effective-factor orchestration.

Uses a spy V1.16-compatible decision repository to pin:

* strict UUID scope validation BEFORE any repository access;
* read-only access to the closed EntityType vocabulary through the
  EXISTING per-entity-type ``list_history`` semantics (no new repository
  surface, no add/write calls);
* exact pass-through of the reconstructed V1.16 records into the pure
  V1.17 resolver;
* unchanged repository/reader failure propagation;
* valid empty-history results.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

import trajectory_os.application.execution_effort_calibration_effective_factors as effective_app
from trajectory_os.application.execution_effort_calibration_effective_factors import (
    DurableEffectiveEffortCalibrationFactorError,
    resolve_effective_effort_calibration_factors_durably,
)
from trajectory_os.domain.entities import EntityType
from trajectory_os.domain.execution_effort_calibration_effective_factors import (
    EffectiveEffortCalibrationFactorSet,
)
from trajectory_os.domain.execution_effort_calibration_factor_decisions import (
    EffortCalibrationDecision,
    EffortCalibrationFactorDecision,
)
from trajectory_os.domain.execution_effort_calibration_factor_proposals import (
    EffortCalibrationFactorProposalReason,
)

PORTFOLIO_ID = UUID("85858585-8585-4585-8585-858585858585")
PROJECT_ID = UUID("86868686-8686-4686-8686-868686868686")


@dataclass
class FakeDecisionRepository:
    """V1.16-shaped decision repository spy (read-only for V1.17)."""

    histories: dict[EntityType, tuple[EffortCalibrationFactorDecision, ...]]
    list_calls: list[tuple[UUID, UUID, EntityType]] = field(default_factory=list)
    list_history_impl: (
        Callable[[UUID, UUID, EntityType], tuple[EffortCalibrationFactorDecision, ...]] | None
    ) = None

    def list_history(
        self,
        portfolio_id: UUID,
        project_id: UUID,
        entity_type: EntityType,
    ) -> tuple[EffortCalibrationFactorDecision, ...]:
        if self.list_history_impl is not None:
            return self.list_history_impl(portfolio_id, project_id, entity_type)
        self.list_calls.append((portfolio_id, project_id, entity_type))
        return self.histories.get(entity_type, ())

    def add(self, decision: EffortCalibrationFactorDecision) -> None:
        raise AssertionError("V1.17 durable resolution must never write through the repository")


def _accept(
    entity_type: EntityType,
    decided_at: object = None,
    numerator: int | None = 3,
    denominator: int | None = 2,
    planned: int = 100,
    actual: int = 150,
) -> EffortCalibrationFactorDecision:
    from datetime import UTC, datetime

    return EffortCalibrationFactorDecision(
        decision_id=uuid4(),
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_ID,
        entity_type=entity_type,
        sample_count=5,
        minimum_required_sample_count=1,
        total_planned_duration_seconds=planned,
        total_actual_duration_seconds=actual,
        proposal_available=True,
        proposal_reason=EffortCalibrationFactorProposalReason.AVAILABLE,
        factor_numerator=numerator,
        factor_denominator=denominator,
        decision=EffortCalibrationDecision.ACCEPT,
        decided_at=decided_at or datetime(2025, 7, 1, 8, 30, tzinfo=UTC),
    )


# --- Boundary integrity ------------------------------------------------------


@pytest.mark.parametrize(
    ("bad_portfolio", "bad_project"),
    [
        (str(PORTFOLIO_ID), PROJECT_ID),
        (PORTFOLIO_ID, str(PROJECT_ID)),
        (None, PROJECT_ID),
        (PORTFOLIO_ID, None),
    ],
)
@pytest.mark.parametrize("expected_error", [DurableEffectiveEffortCalibrationFactorError])
def test_invalid_scope_rejected_before_any_repository_access(
    bad_portfolio: object,
    bad_project: object,
    expected_error: type[BaseException],
) -> None:
    spy = FakeDecisionRepository(histories={})

    def explode(*_args: object) -> tuple[EffortCalibrationFactorDecision, ...]:
        raise AssertionError("repository must not be touched for bad scope")

    spy.list_history_impl = explode

    with pytest.raises(expected_error, match="UUID"):
        resolve_effective_effort_calibration_factors_durably(bad_portfolio, bad_project, spy)
    assert spy.list_calls == []


@pytest.mark.parametrize("expected_error", [AssertionError])
def test_repository_failure_propagates_unchanged(
    expected_error: type[BaseException],
) -> None:
    spy = FakeDecisionRepository(histories={})

    def boom(
        *args: object,
    ) -> tuple[EffortCalibrationFactorDecision, ...]:
        raise expected_error("repository boom")

    spy.list_history_impl = boom

    with pytest.raises(expected_error, match="repository boom"):
        resolve_effective_effort_calibration_factors_durably(PORTFOLIO_ID, PROJECT_ID, spy)


# --- Read-only semantics ------------------------------------------------------


def test_reads_closed_entity_type_vocabulary_in_deterministic_order() -> None:
    spy = FakeDecisionRepository(histories={})
    resolve_effective_effort_calibration_factors_durably(PORTFOLIO_ID, PROJECT_ID, spy)

    vocabulary = list(EntityType)
    assert len(spy.list_calls) == len(vocabulary)
    assert [entity_type for _, _, entity_type in spy.list_calls] == vocabulary
    assert all(
        (portfolio_id, project_id) == (PORTFOLIO_ID, PROJECT_ID)
        for portfolio_id, project_id, _ in spy.list_calls
    )


def test_performs_no_writes_and_preserves_read_only_repository_access() -> None:
    spy = FakeDecisionRepository(
        histories={
            EntityType.TASK: (_accept(EntityType.TASK),),
        }
    )
    result = resolve_effective_effort_calibration_factors_durably(PORTFOLIO_ID, PROJECT_ID, spy)
    assert len(result.factors) == 1
    # ``add`` would raise AssertionError if ever called; reaching here with
    # a valid result proves the durable path is read-only.
    assert len(spy.list_calls) == len(list(EntityType))


def test_passes_exact_reconstructed_records_to_pure_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_accept = _accept(EntityType.TASK)
    project_defer = _accept(
        EntityType.PROJECT,
        numerator=1,
        denominator=1,
        planned=100,
        actual=100,
    )
    project_defer = project_defer.model_copy(update={"decision": EffortCalibrationDecision.DEFER})
    spy = FakeDecisionRepository(
        histories={
            EntityType.TASK: (task_accept,),
            EntityType.PROJECT: (project_defer,),
        }
    )

    captured: dict[str, object] = {}
    real_resolver = effective_app.resolve_effective_effort_calibration_factors

    def spy_resolver(
        decisions: object, portfolio_id: UUID, project_id: UUID
    ) -> EffectiveEffortCalibrationFactorSet:
        captured["decisions"] = list(decisions)  # type: ignore[arg-type]
        return real_resolver(
            decisions,
            portfolio_id,
            project_id,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(effective_app, "resolve_effective_effort_calibration_factors", spy_resolver)

    resolve_effective_effort_calibration_factors_durably(PORTFOLIO_ID, PROJECT_ID, spy)

    supplied = captured["decisions"]
    assert isinstance(supplied, list) and len(supplied) == 2

    expected = sorted(
        [task_accept, project_defer],
        key=lambda record: (
            record.decided_at,
            record.decision_id.int,
        ),
    )

    assert [
        record.model_dump(mode="python")
        for record in supplied  # type: ignore[union-attr]
    ] == [record.model_dump(mode="python") for record in expected]
    assert all(
        isinstance(record, EffortCalibrationFactorDecision)  # type: ignore[union-attr]
        for record in supplied
    )


def test_empty_history_yields_empty_effective_set() -> None:
    spy = FakeDecisionRepository(histories={})
    result = resolve_effective_effort_calibration_factors_durably(PORTFOLIO_ID, PROJECT_ID, spy)
    assert result.factors == ()
    assert result.portfolio_id == PORTFOLIO_ID
    assert result.project_id == PROJECT_ID


def test_resolves_exactly_from_persisted_v116_decisions_only() -> None:
    earlier_accept = _accept(
        EntityType.TASK,
        numerator=3,
        denominator=4,
        planned=200,
        actual=150,
    )
    from datetime import UTC, datetime

    later_reject = _accept(EntityType.TASK)
    later_reject = later_reject.model_copy(
        update={
            "decision": EffortCalibrationDecision.REJECT,
            "decided_at": datetime(2025, 8, 1, 8, 30, tzinfo=UTC),
        }
    )
    spy = FakeDecisionRepository(
        histories={
            EntityType.TASK: (earlier_accept, later_reject),
        }
    )
    result = resolve_effective_effort_calibration_factors_durably(PORTFOLIO_ID, PROJECT_ID, spy)
    assert len(result.factors) == 1
    factor = result.factors[0]
    assert factor.decision_id == earlier_accept.decision_id
    assert factor.factor_denominator == 4
    assert factor.entity_type is EntityType.TASK


def test_durable_output_order_follows_global_history_not_entity_enum_order() -> None:
    """Enum iteration must not become effective-factor output ordering."""
    task_accept = _accept(EntityType.TASK).model_copy(
        update={
            "decision_id": UUID("00000000-0000-4000-8000-000000000001"),
            "decided_at": datetime(
                2026,
                8,
                28,
                10,
                0,
                tzinfo=UTC,
            ),
        }
    )
    project_accept = _accept(EntityType.PROJECT).model_copy(
        update={
            "decision_id": UUID("00000000-0000-4000-8000-000000000002"),
            "decided_at": datetime(
                2026,
                8,
                28,
                10,
                1,
                tzinfo=UTC,
            ),
        }
    )

    repository = FakeDecisionRepository(
        histories={
            EntityType.PROJECT: (project_accept,),
            EntityType.TASK: (task_accept,),
        }
    )

    result = resolve_effective_effort_calibration_factors_durably(
        PORTFOLIO_ID,
        PROJECT_ID,
        repository,
    )

    assert tuple(factor.entity_type for factor in result.factors) == (
        EntityType.TASK,
        EntityType.PROJECT,
    )
