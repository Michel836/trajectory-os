"""Unit evidence for the V1.25 portfolio-level effective-effort summary.

All tests are pure/in-memory: no SQLite repository, wall-clock, or provider
interaction occurs. Covered areas include:

1.  ``PortfolioEffectiveEffortSummary`` exposes flat, frozen, strict summary
    fields and must not expose a separate total model;
2.  a single summary must be strictly self-consistent: ``project_count`` and
    all aggregate values are exact sums over the included projects, estimates
    partition exactly into ordinary versus calibrated, and the complete total
    is exposed only when every project is completely estimated;
3.  malformed project tuples (hostile ``model_construct`` summaries, foreign
    portfolio ownership, duplicate project IDs, non-strict payloads) are
    rejected;
4.  the pure boundary requires a genuine UUID, consumes any ``Iterable`` of
    V1.24 project summaries exactly once, rejects elements that fail strict
    re-validation as well as foreign and duplicate identities, and is
    read-only with respect to its inputs;
5.  the empty and full/partial aggregate semantics are exact and deterministic;
6.  the durable boundary requires a genuine UUID, loads the CURRENT Portfolio
    through the supplied repository exactly once, freshly revalidates the
    loaded Portfolio (rejecting hostile ``model_construct`` instances), rejects
    a missing Portfolio with ``ExecutionEffortPlanningPortfolioNotFoundError``
    and a different-identity Portfolio with a summary error, delegates each
    PROJECT in canonical order to the authoritative V1.24 durable boundary
    with the supplied readers, never writes, propagates per-project failures
    unchanged, and produces a result equivalent to the pure boundary for the
    same per-project summaries.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from trajectory_os.application import execution_effort_portfolio_summary as v125
from trajectory_os.application.execution_effort_effective_summary import (
    WorkBreakdownEffectiveEffortSummary,
)
from trajectory_os.application.execution_effort_planning import (
    ExecutionEffortPlanningPortfolioNotFoundError,
)
from trajectory_os.application.execution_effort_portfolio_summary import (
    PortfolioEffectiveEffortSummary,
    PortfolioEffectiveEffortSummaryError,
    build_portfolio_effective_effort_summary_durably,
    summarize_portfolio_effective_effort,
)
from trajectory_os.domain.entities import EntityType, TrajectoryEntity
from trajectory_os.domain.execution_effort_planning import PlannedEffortSummary
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

PORTFOLIO_ID = UUID("bb000000-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
PROJECTA = UUID("bb000001-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
PROJECTB = UUID("bb000002-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
PROJECTC = UUID("bb000003-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
TASK_ID = UUID("bb000010-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
FOREIGN_ID = UUID("cc000000-cccc-4ccc-8ccc-cccccccccccc")


# ---------------------------------------------------------------------------
# In-memory construction helpers (no persistence).
# ---------------------------------------------------------------------------


def _project_summary(
    project_id: UUID,
    *,
    portfolio_id: UUID = PORTFOLIO_ID,
    known: int = 0,
    estimated: int = 0,
    unestimated: int = 0,
    complete_total: int | None = None,
    ordinary: int = 0,
    calibrated: int = 0,
) -> WorkBreakdownEffectiveEffortSummary:
    """Build one authoritative-looking V1.24 project summary in memory."""
    return WorkBreakdownEffectiveEffortSummary(
        portfolio_id=portfolio_id,
        project_id=project_id,
        effort=PlannedEffortSummary(
            known_duration_seconds=known,
            estimated_entity_count=estimated,
            unestimated_entity_count=unestimated,
            total_duration_seconds=complete_total,
        ),
        ordinary_estimate_count=ordinary,
        calibrated_estimate_count=calibrated,
    )


def _complete_summary(
    project_id: UUID,
    *,
    portfolio_id: UUID = PORTFOLIO_ID,
    known: int = 900,
    ordinary: int = 1,
    calibrated: int = 1,
) -> WorkBreakdownEffectiveEffortSummary:
    """A fully estimated project summary (total == known)."""
    return _project_summary(
        project_id,
        portfolio_id=portfolio_id,
        known=known,
        estimated=ordinary + calibrated,
        unestimated=0,
        complete_total=known,
        ordinary=ordinary,
        calibrated=calibrated,
    )


def _hostile_constructed_summary(**overrides: object) -> WorkBreakdownEffectiveEffortSummary:
    """Bypass V1.24 construction invariants to test hostile re-validation."""
    effort_data: dict[str, object] = {
        "known_duration_seconds": 100,
        "estimated_entity_count": 1,
        "unestimated_entity_count": 0,
        "total_duration_seconds": 100,
    }
    effort_override = overrides.pop("effort", None)
    if effort_override is not None:
        assert isinstance(effort_override, dict)
        effort_data.update(effort_override)
    base: dict[str, object] = {
        "portfolio_id": PORTFOLIO_ID,
        "project_id": PROJECTA,
        "effort": PlannedEffortSummary.model_construct(**effort_data),
        "ordinary_estimate_count": 0,
        "calibrated_estimate_count": 1,
    }
    base.update(overrides)
    return WorkBreakdownEffectiveEffortSummary.model_construct(**base)  # type: ignore[arg-type]


def _hostile_constructed_project(
    known: int,
    *,
    ordinary: int,
    calibrated: int,
    estimated: int,
    portfolio_id: UUID = PORTFOLIO_ID,
    complete_total: int | None = None,
) -> WorkBreakdownEffectiveEffortSummary:
    """A model-constructed project summary that bypasses V1.24's invariants."""
    return WorkBreakdownEffectiveEffortSummary.model_construct(  # type: ignore[arg-type]
        portfolio_id=portfolio_id,
        project_id=PROJECTA,
        effort=PlannedEffortSummary(
            known_duration_seconds=known,
            estimated_entity_count=estimated,
            unestimated_entity_count=0,
            total_duration_seconds=complete_total,
        ),
        ordinary_estimate_count=ordinary,
        calibrated_estimate_count=calibrated,
    )


def _hostile_portable_portfolio() -> Portfolio:
    """A Portfolio bypassing all construction invariants (malicious content)."""
    return Portfolio.model_construct(  # type: ignore[arg-type]
        id=PORTFOLIO_ID,
        name="malicious",
        entities=[
            {
                "id": "not-a-uuid",
                "entity_type": EntityType.PROJECT,
                "title": "malicious entity",
                "description": "",
            }
        ],
        relations=[],
    )


def _portfolio_with_projects(
    *project_ids: UUID,
    include_task: bool = False,
) -> Portfolio:
    """An in-memory Portfolio with the given PROJECT entities (and optionally a TASK)."""
    entities = [
        TrajectoryEntity(
            id=project_id,
            entity_type=EntityType.PROJECT,
            title=f"project {project_id.hex[:8]}",
            description="",
        )
        for project_id in project_ids
    ]
    relations: list[TrajectoryRelation] = []
    if include_task:
        entities.append(
            TrajectoryEntity(
                id=TASK_ID,
                entity_type=EntityType.TASK,
                title="non-project member",
                description="",
            )
        )
        for project_id in project_ids:
            relations.append(
                TrajectoryRelation(
                    source_id=TASK_ID,
                    target_id=project_id,
                    relation_type=RelationType.BELONGS_TO,
                )
            )
    return Portfolio(id=PORTFOLIO_ID, name="portfolio", entities=entities, relations=relations)


class _PortfoliosPortfolio:
    """A Portfolio whose identity differs from any requested identifier."""

    def __init__(self, portfolio: Portfolio) -> None:
        self._portfolio = portfolio

    def load(self, _portfolio_id: UUID) -> Portfolio:
        return self._portfolio


class _RecordingPortfolioRepository:
    """Read-only in-memory repository with call recording and a write guard."""

    def __init__(self, portfolio: Portfolio | None) -> None:
        self._portfolio = portfolio
        self.loaded_portfolio_id: UUID | None = None
        self.load_calls = 0
        self.save_calls = 0

    def load(self, portfolio_id: UUID) -> Portfolio | None:
        self.load_calls += 1
        self.loaded_portfolio_id = portfolio_id
        if self._portfolio is not None and self._portfolio.id == portfolio_id:
            return self._portfolio
        return None

    def save(self, portfolio: object) -> None:
        self.save_calls += 1
        raise PortfolioEffectiveEffortSummaryError("summary boundary must never write")


def _install_spy(
    monkeypatch: pytest.MonkeyPatch,
    summaries: dict[UUID, WorkBreakdownEffectiveEffortSummary],
) -> list[tuple[UUID, UUID, object, object, object]]:
    """Monkeypatch the V1.24 durable boundary with a recording spy."""
    calls: list[tuple[UUID, UUID, object, object, object]] = []

    def _spy(
        portfolio_id: UUID,
        project_id: UUID,
        portfolio_repository: object,
        estimate_reader: object,
        provenance_reader: object,
    ) -> WorkBreakdownEffectiveEffortSummary:
        calls.append(
            (portfolio_id, project_id, portfolio_repository, estimate_reader, provenance_reader)
        )
        return summaries[project_id]

    monkeypatch.setattr(
        v125,
        "build_effective_work_breakdown_effort_summary_durably",
        _spy,
    )
    return calls


def _durable_kwargs(
    repository: object,
    estimate_reader: object = None,
    provenance_reader: object = None,
) -> dict[str, object]:
    return {
        "portfolio_repository": repository,
        "estimate_reader": estimate_reader,
        "provenance_reader": provenance_reader,
    }


# ---------------------------------------------------------------------------
# 1. The flat, strict, frozen summary model replaces the nested total model.
# ---------------------------------------------------------------------------


def test_summary_model_exposes_flat_fields_and_no_separate_total_type() -> None:
    suma = _complete_summary(PROJECTA, known=300, ordinary=1, calibrated=1)
    sumb = _complete_summary(PROJECTB, known=450, ordinary=0, calibrated=2)

    summary = summarize_portfolio_effective_effort(PORTFOLIO_ID, [suma, sumb])

    assert isinstance(summary, PortfolioEffectiveEffortSummary)
    assert summary.portfolio_id == PORTFOLIO_ID
    assert summary.project_count == 2
    assert summary.known_duration_seconds == 750
    assert summary.estimated_entity_count == 4
    assert summary.unestimated_entity_count == 0
    assert summary.ordinary_estimate_count == 1
    assert summary.calibrated_estimate_count == 3
    assert summary.total_duration_seconds == 750
    assert summary.projects == (suma, sumb)
    assert summary.projects[0].project_id == PROJECTA
    assert summary.projects[1].project_id == PROJECTB

    # The V1.25 surface must not reintroduce a separate total type.
    assert not hasattr(summary, "effort")
    assert not hasattr(summary, "totals")
    assert not hasattr(v125, "PortfolioEffectiveEffortTotal")
    assert v125.__all__ == [  # type: ignore[attr-defined]
        "PortfolioEffectiveEffortSummary",
        "PortfolioEffectiveEffortSummaryError",
        "build_portfolio_effective_effort_summary_durably",
        "summarize_portfolio_effective_effort",
    ]


def test_summary_model_is_strict_and_frozen() -> None:
    suma = _complete_summary(PROJECTA)

    summary = summarize_portfolio_effective_effort(PORTFOLIO_ID, [suma])

    with pytest.raises(ValidationError):
        summary.project_count = 1  # type: ignore[misc]
    with pytest.raises(ValidationError):
        summary.total_duration_seconds = None  # type: ignore[misc]

    # Strict typing: booleans are not integers, negatives are not durations/counts.
    with pytest.raises(ValidationError):
        PortfolioEffectiveEffortSummary(
            portfolio_id=PORTFOLIO_ID,
            project_count=True,  # type: ignore[arg-type]
            known_duration_seconds=0,
            estimated_entity_count=0,
            unestimated_entity_count=0,
            ordinary_estimate_count=0,
            calibrated_estimate_count=0,
            total_duration_seconds=0,
            projects=(),
        )
    with pytest.raises(ValidationError):
        PortfolioEffectiveEffortSummary(
            portfolio_id=PORTFOLIO_ID,
            project_count=0,
            known_duration_seconds=-1,
            estimated_entity_count=0,
            unestimated_entity_count=0,
            ordinary_estimate_count=0,
            calibrated_estimate_count=0,
            total_duration_seconds=0,
            projects=(),
        )
    with pytest.raises(ValidationError, match="extra"):
        PortfolioEffectiveEffortSummary(
            portfolio_id=PORTFOLIO_ID,
            project_count=0,
            known_duration_seconds=0,
            estimated_entity_count=0,
            unestimated_entity_count=0,
            ordinary_estimate_count=0,
            calibrated_estimate_count=0,
            total_duration_seconds=0,
            projects=(),
            invented="field",  # type: ignore[call-overload]
        )


def test_summary_rejects_non_tuple_project_sequences() -> None:
    with pytest.raises(ValidationError, match="tuple"):
        PortfolioEffectiveEffortSummary(
            portfolio_id=PORTFOLIO_ID,
            project_count=1,
            known_duration_seconds=900,
            estimated_entity_count=2,
            unestimated_entity_count=0,
            ordinary_estimate_count=1,
            calibrated_estimate_count=1,
            total_duration_seconds=900,
            projects=[_complete_summary(PROJECTA)],  # type: ignore[list-item]
        )


# ---------------------------------------------------------------------------
# 2. A single summary must be strictly self-consistent with its projects.
# ---------------------------------------------------------------------------


def test_summary_requires_project_count_to_match_the_tuple() -> None:
    suma = _complete_summary(PROJECTA)
    with pytest.raises(ValidationError, match="project_count"):
        PortfolioEffectiveEffortSummary(
            portfolio_id=PORTFOLIO_ID,
            project_count=2,
            known_duration_seconds=900,
            estimated_entity_count=2,
            unestimated_entity_count=0,
            ordinary_estimate_count=1,
            calibrated_estimate_count=1,
            total_duration_seconds=900,
            projects=(suma,),
        )


def test_summary_rejects_non_exact_aggregate_sums() -> None:
    suma = _complete_summary(PROJECTA, known=300, ordinary=1, calibrated=2)

    with pytest.raises(ValidationError, match="known_duration_seconds"):
        PortfolioEffectiveEffortSummary(
            portfolio_id=PORTFOLIO_ID,
            project_count=1,
            known_duration_seconds=301,
            estimated_entity_count=3,
            unestimated_entity_count=0,
            ordinary_estimate_count=1,
            calibrated_estimate_count=2,
            total_duration_seconds=750,
            projects=(suma,),
        )
    with pytest.raises(ValidationError, match="ordinary_estimate_count"):
        PortfolioEffectiveEffortSummary(
            portfolio_id=PORTFOLIO_ID,
            project_count=1,
            known_duration_seconds=300,
            estimated_entity_count=3,
            unestimated_entity_count=0,
            ordinary_estimate_count=2,
            calibrated_estimate_count=2,
            total_duration_seconds=750,
            projects=(suma,),
        )
    with pytest.raises(ValidationError, match="estimated_entity_count"):
        PortfolioEffectiveEffortSummary(
            portfolio_id=PORTFOLIO_ID,
            project_count=1,
            known_duration_seconds=300,
            estimated_entity_count=4,
            unestimated_entity_count=0,
            ordinary_estimate_count=1,
            calibrated_estimate_count=2,
            total_duration_seconds=750,
            projects=(suma,),
        )


def test_summary_enforces_strict_total_exposure_rules() -> None:
    sum_complete = _complete_summary(PROJECTA, known=300, ordinary=1, calibrated=1)
    sum_partial = _project_summary(
        PROJECTA,
        known=120,
        estimated=1,
        unestimated=1,
        complete_total=None,
        ordinary=1,
    )

    # A complete portfolio must expose the exact complete sum.
    with pytest.raises(ValidationError, match="total_duration_seconds"):
        PortfolioEffectiveEffortSummary(
            portfolio_id=PORTFOLIO_ID,
            project_count=1,
            known_duration_seconds=300,
            estimated_entity_count=2,
            unestimated_entity_count=0,
            ordinary_estimate_count=1,
            calibrated_estimate_count=1,
            total_duration_seconds=301,
            projects=(sum_complete,),
        )
    with pytest.raises(ValidationError, match="total_duration_seconds"):
        PortfolioEffectiveEffortSummary(
            portfolio_id=PORTFOLIO_ID,
            project_count=1,
            known_duration_seconds=300,
            estimated_entity_count=2,
            unestimated_entity_count=0,
            ordinary_estimate_count=1,
            calibrated_estimate_count=1,
            total_duration_seconds=None,
            projects=(sum_complete,),
        )

    # A partially estimated portfolio must never expose a complete total.
    with pytest.raises(ValidationError, match="must not be exposed"):
        PortfolioEffectiveEffortSummary(
            portfolio_id=PORTFOLIO_ID,
            project_count=1,
            known_duration_seconds=120,
            estimated_entity_count=1,
            unestimated_entity_count=1,
            ordinary_estimate_count=1,
            calibrated_estimate_count=0,
            total_duration_seconds=120,
            projects=(sum_partial,),
        )


def test_summary_requires_the_partition_to_cover_all_estimates() -> None:
    suma = _complete_summary(PROJECTA)
    with pytest.raises(ValidationError):
        PortfolioEffectiveEffortSummary(
            portfolio_id=PORTFOLIO_ID,
            project_count=1,
            known_duration_seconds=900,
            estimated_entity_count=1,
            unestimated_entity_count=0,
            ordinary_estimate_count=0,
            calibrated_estimate_count=2,
            total_duration_seconds=900,
            projects=(suma,),
        )


def test_summary_rejects_foreign_project_summaries() -> None:
    foreign = _complete_summary(PROJECTA, portfolio_id=FOREIGN_ID)
    with pytest.raises(ValidationError, match="different portfolio"):
        PortfolioEffectiveEffortSummary(
            portfolio_id=PORTFOLIO_ID,
            project_count=1,
            known_duration_seconds=900,
            estimated_entity_count=2,
            unestimated_entity_count=0,
            ordinary_estimate_count=1,
            calibrated_estimate_count=1,
            total_duration_seconds=900,
            projects=(foreign,),
        )


def test_summary_rejects_duplicate_project_ids() -> None:
    duplicates = (_complete_summary(PROJECTA), _complete_summary(PROJECTA))
    with pytest.raises(ValidationError, match="duplicate project"):
        PortfolioEffectiveEffortSummary(
            portfolio_id=PORTFOLIO_ID,
            project_count=2,
            known_duration_seconds=1800,
            estimated_entity_count=4,
            unestimated_entity_count=0,
            ordinary_estimate_count=2,
            calibrated_estimate_count=2,
            total_duration_seconds=1800,
            projects=duplicates,
        )


def test_summary_rejects_hostile_constructed_project_summaries() -> None:
    """A model-constructed project summary must not be trusted at all."""
    hostile = _hostile_constructed_project(
        known=100,
        ordinary=7,
        calibrated=1,
        estimated=1,
        complete_total=100,
    )
    with pytest.raises(ValidationError):
        PortfolioEffectiveEffortSummary(
            portfolio_id=PORTFOLIO_ID,
            project_count=1,
            known_duration_seconds=100,
            estimated_entity_count=8,
            unestimated_entity_count=0,
            ordinary_estimate_count=7,
            calibrated_estimate_count=1,
            total_duration_seconds=100,
            projects=(hostile,),
        )


# ---------------------------------------------------------------------------
# 3. Pure boundary: strict identity, one-shot Iterable consumption,
#    hostile re-validation, order, determinism, and read-only inputs.
# ---------------------------------------------------------------------------


def test_pure_boundary_requires_genuine_uuid_inputs() -> None:
    with pytest.raises(PortfolioEffectiveEffortSummaryError, match="portfolio_id"):
        summarize_portfolio_effective_effort(str(PORTFOLIO_ID), [])  # type: ignore[arg-type]
    with pytest.raises(PortfolioEffectiveEffortSummaryError, match="portfolio_id"):
        summarize_portfolio_effective_effort(42, [])  # type: ignore[arg-type]
    with pytest.raises(PortfolioEffectiveEffortSummaryError, match="portfolio_id"):
        summarize_portfolio_effective_effort(None, [])  # type: ignore[arg-type]


def test_pure_boundary_consumes_any_iterable_exactly_once() -> None:
    suma = _complete_summary(PROJECTA, known=300, ordinary=1, calibrated=1)
    sumb = _complete_summary(PROJECTB, known=450, ordinary=0, calibrated=2)

    list_result = summarize_portfolio_effective_effort(PORTFOLIO_ID, [suma, sumb])
    tuple_result = summarize_portfolio_effective_effort(PORTFOLIO_ID, (suma, sumb))
    assert list_result == tuple_result

    pulled: list[WorkBreakdownEffectiveEffortSummary] = []

    def _generator():
        for summary in (suma, sumb):
            pulled.append(summary)
            yield summary

    generator = _generator()
    generator_result = summarize_portfolio_effective_effort(PORTFOLIO_ID, generator)
    assert generator_result == list_result
    assert pulled == [suma, sumb], "the iterable must be pulled, not copied or replayed"
    assert next(generator, ...) is ...


def test_pure_boundary_rejects_non_iterable_sequences() -> None:
    good = _complete_summary(PROJECTA)
    for invalid in (None, 42):
        with pytest.raises(PortfolioEffectiveEffortSummaryError, match="Iterable"):
            summarize_portfolio_effective_effort(  # type: ignore[arg-type]
                PORTFOLIO_ID,
                invalid,
            )
    assert good.project_id == PROJECTA


@pytest.mark.parametrize(
    "invalid_element",
    [
        {"portfolio_id": PORTFOLIO_ID, "project_id": PROJECTA, "effort": 7},
        42,
        "summary",
        3.5,
    ],
)
def test_pure_boundary_rejects_elements_that_are_not_genuine_summaries(
    invalid_element: object,
) -> None:
    with pytest.raises(PortfolioEffectiveEffortSummaryError, match="WorkBreakdown"):
        summarize_portfolio_effective_effort(  # type: ignore[list-item]
            PORTFOLIO_ID,
            [invalid_element],
        )


def test_pure_boundary_rejects_hostile_constructed_partitions() -> None:
    # Counters that disagree with the authoritative root estimated count.
    with pytest.raises(PortfolioEffectiveEffortSummaryError, match="re-validation"):
        summarize_portfolio_effective_effort(
            PORTFOLIO_ID,
            [_hostile_constructed_summary(ordinary_estimate_count=7)],
        )
    # Negative counters must never corrupt the totals.
    with pytest.raises(PortfolioEffectiveEffortSummaryError, match="re-validation"):
        summarize_portfolio_effective_effort(
            PORTFOLIO_ID,
            [_hostile_constructed_summary(calibrated_estimate_count=-1)],
        )


def test_pure_boundary_rejects_hostile_constructed_coverage() -> None:
    # A fabricated complete total for partial effort must not aggregate.
    with pytest.raises(PortfolioEffectiveEffortSummaryError, match="re-validation"):
        summarize_portfolio_effective_effort(
            PORTFOLIO_ID,
            [
                _hostile_constructed_summary(
                    effort={
                        "known_duration_seconds": 100,
                        "estimated_entity_count": 1,
                        "unestimated_entity_count": 1,
                        "total_duration_seconds": 100,
                    }
                )
            ],
        )
    # Negative known duration must not aggregate.
    with pytest.raises(PortfolioEffectiveEffortSummaryError, match="re-validation"):
        summarize_portfolio_effective_effort(
            PORTFOLIO_ID,
            [
                _hostile_constructed_summary(
                    effort={
                        "known_duration_seconds": -5,
                        "estimated_entity_count": 0,
                        "unestimated_entity_count": 0,
                        "total_duration_seconds": 0,
                    },
                    ordinary_estimate_count=0,
                    calibrated_estimate_count=0,
                )
            ],
        )


def test_pure_boundary_rejects_foreign_portfolio_summaries() -> None:
    foreign = _complete_summary(PROJECTA, portfolio_id=FOREIGN_ID)
    with pytest.raises(PortfolioEffectiveEffortSummaryError, match="different portfolio"):
        summarize_portfolio_effective_effort(PORTFOLIO_ID, [foreign])


def test_pure_boundary_rejects_duplicate_project_ids() -> None:
    first = _complete_summary(PROJECTA)
    double = _complete_summary(PROJECTA)
    with pytest.raises(PortfolioEffectiveEffortSummaryError, match="duplicates project"):
        summarize_portfolio_effective_effort(PORTFOLIO_ID, [first, double])


# ---------------------------------------------------------------------------
# 4. Empty, exact-sum, and partial-total semantics; determinism; read-only.
# ---------------------------------------------------------------------------


def test_empty_sequence_aggregates_to_exact_zero_total() -> None:
    result = summarize_portfolio_effective_effort(PORTFOLIO_ID, [])
    assert isinstance(result, PortfolioEffectiveEffortSummary)
    assert result.portfolio_id == PORTFOLIO_ID
    assert result.project_count == 0
    assert result.known_duration_seconds == 0
    assert result.estimated_entity_count == 0
    assert result.unestimated_entity_count == 0
    assert result.ordinary_estimate_count == 0
    assert result.calibrated_estimate_count == 0
    assert result.total_duration_seconds == 0
    assert result.projects == ()


def test_exact_sum_aggregate_when_every_project_is_complete() -> None:
    suma = _complete_summary(PROJECTA, known=300, ordinary=1, calibrated=2)
    sumb = _complete_summary(PROJECTB, known=450, ordinary=0, calibrated=3)

    result = summarize_portfolio_effective_effort(PORTFOLIO_ID, [suma, sumb])

    assert result.project_count == 2
    assert result.known_duration_seconds == 750
    assert result.estimated_entity_count == 6
    assert result.unestimated_entity_count == 0
    assert result.ordinary_estimate_count == 1
    assert result.calibrated_estimate_count == 5
    assert result.total_duration_seconds == 750
    assert result.projects == (suma, sumb)


def test_partial_project_suppresses_total_but_keeps_known_totals_exact() -> None:
    suma = _complete_summary(PROJECTA, known=300, ordinary=1, calibrated=1)
    partial = _project_summary(
        PROJECTB,
        known=120,
        estimated=1,
        unestimated=1,
        complete_total=None,
        ordinary=1,
    )

    result = summarize_portfolio_effective_effort(PORTFOLIO_ID, [suma, partial])

    assert result.known_duration_seconds == 420
    assert result.estimated_entity_count == 3
    assert result.unestimated_entity_count == 1
    assert result.ordinary_estimate_count == 2
    assert result.calibrated_estimate_count == 1
    assert result.total_duration_seconds is None


def test_aggregate_is_deterministic_and_preserves_input_order() -> None:
    suma = _complete_summary(PROJECTA, known=300, ordinary=1, calibrated=1)
    sumb = _complete_summary(PROJECTB, known=450, ordinary=0, calibrated=2)
    sumc = _project_summary(
        PROJECTC,
        known=60,
        estimated=1,
        unestimated=1,
        complete_total=None,
        ordinary=0,
        calibrated=1,
    )

    forward = summarize_portfolio_effective_effort(PORTFOLIO_ID, [suma, sumb, sumc])
    again = summarize_portfolio_effective_effort(PORTFOLIO_ID, [suma, sumb, sumc])
    reversed_input = summarize_portfolio_effective_effort(PORTFOLIO_ID, [sumc, sumb, suma])

    assert forward == again, "identical ordered input must be deterministic"
    assert [project.project_id for project in forward.projects] == [
        PROJECTA,
        PROJECTB,
        PROJECTC,
    ]
    assert [project.project_id for project in reversed_input.projects] == [
        PROJECTC,
        PROJECTB,
        PROJECTA,
    ]
    assert forward.known_duration_seconds == 810
    assert forward.estimated_entity_count == 5
    assert forward.unestimated_entity_count == 1
    assert forward.total_duration_seconds is None
    assert forward.project_count == 3


def test_pure_boundary_does_not_rewrite_the_supplied_summaries() -> None:
    suma = _complete_summary(PROJECTA, known=300, ordinary=1, calibrated=1)
    snapshot = (
        suma.portfolio_id,
        suma.project_id,
        suma.effort,
        suma.ordinary_estimate_count,
        suma.calibrated_estimate_count,
    )

    summarize_portfolio_effective_effort(PORTFOLIO_ID, [suma])

    assert (
        suma.portfolio_id,
        suma.project_id,
        suma.effort,
        suma.ordinary_estimate_count,
        suma.calibrated_estimate_count,
    ) == snapshot


# ---------------------------------------------------------------------------
# 5. Durable boundary: strict inputs, single load, fresh revalidation,
#    canonical delegation, no writes, failure propagation, equivalence.
# ---------------------------------------------------------------------------


def test_durable_requires_genuine_uuid_inputs() -> None:
    repository = _RecordingPortfolioRepository(_portfolio_with_projects(PROJECTA))
    with pytest.raises(PortfolioEffectiveEffortSummaryError, match="portfolio_id"):
        build_portfolio_effective_effort_summary_durably(
            portfolio_id=str(PORTFOLIO_ID),  # type: ignore[arg-type]
            **_durable_kwargs(repository),
        )
    with pytest.raises(PortfolioEffectiveEffortSummaryError, match="portfolio_id"):
        build_portfolio_effective_effort_summary_durably(
            portfolio_id=42,  # type: ignore[arg-type]
            **_durable_kwargs(repository),
        )
    assert repository.load_calls == 0


def test_durable_rejects_a_missing_portfolio() -> None:
    repository = _RecordingPortfolioRepository(_portfolio_with_projects(PROJECTA))
    with pytest.raises(ExecutionEffortPlanningPortfolioNotFoundError):
        build_portfolio_effective_effort_summary_durably(
            portfolio_id=PORTFOLIO_ID,
            **_durable_kwargs(_RecordingPortfolioRepository(None)),
        )
    assert repository.load_calls == 0


def test_durable_rejects_a_foreign_portfolio_identity() -> None:
    foreign_repository = _PortfoliosPortfolio(
        Portfolio(id=FOREIGN_ID, name="foreign", entities=[], relations=[])
    )
    with pytest.raises(PortfolioEffectiveEffortSummaryError, match="foreign portfolio"):
        build_portfolio_effective_effort_summary_durably(
            portfolio_id=PORTFOLIO_ID,
            portfolio_repository=foreign_repository,
            estimate_reader=None,  # must never be reached
            provenance_reader=None,
        )


def test_durable_rejects_a_hostile_constructed_portfolio() -> None:
    with pytest.raises(PortfolioEffectiveEffortSummaryError, match="re-validation"):
        build_portfolio_effective_effort_summary_durably(
            portfolio_id=PORTFOLIO_ID,
            **_durable_kwargs(_PortfoliosPortfolio(_hostile_portable_portfolio())),
        )


def test_durable_discovers_projects_via_the_loaded_portfolio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suma = _complete_summary(PROJECTA, known=300, ordinary=1, calibrated=1)
    sumb = _complete_summary(PROJECTB, known=450, ordinary=0, calibrated=2)
    calls = _install_spy(monkeypatch, {PROJECTA: suma, PROJECTB: sumb})
    repository = _RecordingPortfolioRepository(
        _portfolio_with_projects(PROJECTB, PROJECTA, include_task=True)
    )
    estimate_reader = object.__new__(object)
    provenance_reader = object.__new__(object)

    result = build_portfolio_effective_effort_summary_durably(
        portfolio_id=PORTFOLIO_ID,
        **_durable_kwargs(repository, estimate_reader, provenance_reader),
    )

    # Exactly one V1.25-owned load...
    assert repository.load_calls == 1
    assert repository.loaded_portfolio_id == PORTFOLIO_ID
    # ...and one delegated V1.24 build per discovered PROJECT, in canonical
    # (Portfolio.entities) order, ignoring non-project members.
    assert [project_id for _portfolio, project_id, *_rest in calls] == [
        PROJECTB,
        PROJECTA,
    ]
    assert all(portfolio == PORTFOLIO_ID for portfolio, _project, *_rest in calls)
    # The supplied readers are passed through untouched.
    assert all(calls[_][2] is repository for _ in range(len(calls)))
    assert all(calls[_][3] is estimate_reader for _ in range(len(calls)))
    assert all(calls[_][4] is provenance_reader for _ in range(len(calls)))
    # Never writes.
    assert repository.save_calls == 0
    # The aggregate is the flat, exact combination of the delegated results.
    assert result.project_count == 2
    assert result.known_duration_seconds == 750
    assert result.total_duration_seconds == 750
    assert result.unestimated_entity_count == 0


def test_durable_on_an_empty_project_portfolio_aggregates_to_exact_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_spy(monkeypatch, {})
    repository = _RecordingPortfolioRepository(_portfolio_with_projects(include_task=True))

    result = build_portfolio_effective_effort_summary_durably(
        portfolio_id=PORTFOLIO_ID,
        **_durable_kwargs(repository),
    )

    assert calls == []
    assert result.project_count == 0
    assert result.known_duration_seconds == 0
    assert result.total_duration_seconds == 0
    assert result.projects == ()
    assert repository.load_calls == 1
    assert repository.save_calls == 0


def test_durable_is_equivalent_to_the_pure_boundary_for_the_same_summaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suma = _complete_summary(PROJECTA, known=300, ordinary=1, calibrated=1)
    sumb = _project_summary(
        PROJECTB,
        known=120,
        estimated=1,
        unestimated=1,
        complete_total=None,
        ordinary=1,
    )
    _install_spy(monkeypatch, {PROJECTA: suma, PROJECTB: sumb})
    repository = _RecordingPortfolioRepository(_portfolio_with_projects(PROJECTA, PROJECTB))

    durable = build_portfolio_effective_effort_summary_durably(
        portfolio_id=PORTFOLIO_ID,
        **_durable_kwargs(repository),
    )
    pure = summarize_portfolio_effective_effort(PORTFOLIO_ID, [suma, sumb])

    assert durable == pure
    assert durable.known_duration_seconds == 420
    assert durable.total_duration_seconds is None


def test_durable_propagates_per_project_failures_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _RecordingPortfolioRepository(_portfolio_with_projects(PROJECTA))

    class _V124Failure(Exception):
        """Standalone failure type to prove unchanged propagation."""

    def _failing_spy(
        portfolio_id: UUID,
        project_id: UUID,
        portfolio_repository: object,
        estimate_reader: object,
        provenance_reader: object,
    ) -> WorkBreakdownEffectiveEffortSummary:
        raise _V124Failure("per-project failure must propagate unchanged")

    monkeypatch.setattr(
        v125,
        "build_effective_work_breakdown_effort_summary_durably",
        _failing_spy,
    )

    with pytest.raises(_V124Failure):
        build_portfolio_effective_effort_summary_durably(
            portfolio_id=PORTFOLIO_ID,
            **_durable_kwargs(repository),
        )
    assert repository.load_calls == 1
    assert repository.save_calls == 0
