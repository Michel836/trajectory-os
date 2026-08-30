"""V1.26 — per-project portfolio effort-contribution projection tests.

Locks the boundary contract of
``trajectory_os.application.execution_effort_project_contributions``:

* a genuine V1.25 ``PortfolioEffectiveEffortSummary`` is the only accepted
  input;
* the V1.25 payload is freshly and strictly re-validated, including every
  nested V1.24 project summary and its ``PlannedEffortSummary`` — hostile
  ``model_construct`` values are rejected, not trusted;
* the projection traverses ``summary.projects`` exactly once, in order,
  copying the flat contribution fields verbatim (including
  ``total_duration_seconds is None``);
* no percentages, ratios, shares, or rankings are computed or exposed;
* the input is never mutated and no persistence happens.
"""

import copy
from uuid import UUID

import pytest
from pydantic import ValidationError

from trajectory_os.application.execution_effort_effective_summary import (
    WorkBreakdownEffectiveEffortSummary,
)
from trajectory_os.application.execution_effort_portfolio_summary import (
    PortfolioEffectiveEffortSummary,
    summarize_portfolio_effective_effort,
)
from trajectory_os.application.execution_effort_project_contributions import (
    PortfolioProjectEffortContribution,
    PortfolioProjectEffortContributionError,
    PortfolioProjectEffortContributionSummary,
    project_portfolio_effort_contributions,
)
from trajectory_os.domain.execution_effort_planning import PlannedEffortSummary

PORTFOLIO_ID = UUID("11111111-1111-4111-8111-111111111111")
PROJECT_A = UUID("22222222-2222-4222-8222-222222222222")
PROJECT_B = UUID("33333333-3333-4333-8333-333333333333")
PROJECT_C = UUID("44444444-4444-4444-8444-444444444444")


def _fully_estimated_subtree() -> PlannedEffortSummary:
    return PlannedEffortSummary(
        known_duration_seconds=100,
        estimated_entity_count=2,
        unestimated_entity_count=0,
        total_duration_seconds=100,
    )


def _partial_subtree() -> PlannedEffortSummary:
    return PlannedEffortSummary(
        known_duration_seconds=60,
        estimated_entity_count=1,
        unestimated_entity_count=1,
        total_duration_seconds=None,
    )


def _project_summary(
    *,
    project_id: UUID,
    subtree: PlannedEffortSummary,
    ordinary: int,
    calibrated: int,
    portfolio_id: UUID = PORTFOLIO_ID,
) -> WorkBreakdownEffectiveEffortSummary:
    return WorkBreakdownEffectiveEffortSummary(
        portfolio_id=portfolio_id,
        project_id=project_id,
        effort=subtree,
        ordinary_estimate_count=ordinary,
        calibrated_estimate_count=calibrated,
    )


def _v125_summary(
    summaries: list[WorkBreakdownEffectiveEffortSummary],
) -> PortfolioEffectiveEffortSummary:
    payload = (
        summarize_portfolio_effective_effort(PORTFOLIO_ID, summaries)
        if summaries
        else {
            "portfolio_id": PORTFOLIO_ID,
            "project_count": 0,
            "known_duration_seconds": 0,
            "estimated_entity_count": 0,
            "unestimated_entity_count": 0,
            "ordinary_estimate_count": 0,
            "calibrated_estimate_count": 0,
            "total_duration_seconds": 0,
            "projects": (),
        }
    )
    return PortfolioEffectiveEffortSummary.model_validate(payload, strict=True)


def _contribution_kwargs(
    project_id: UUID = PROJECT_A,
    *,
    known: int = 100,
    estimated: int = 2,
    unestimated: int = 0,
    ordinary: int = 1,
    calibrated: int = 1,
    total: int | None = 100,
) -> dict[str, object]:
    return {
        "project_id": project_id,
        "known_duration_seconds": known,
        "estimated_entity_count": estimated,
        "unestimated_entity_count": unestimated,
        "ordinary_estimate_count": ordinary,
        "calibrated_estimate_count": calibrated,
        "total_duration_seconds": total,
    }


# --- contribution model shape invariants ------------------------------------


def test_contribution_model_is_strict_frozen_and_extra_forbid() -> None:
    contribution = PortfolioProjectEffortContribution(**_contribution_kwargs())
    assert contribution.model_config.get("frozen") is True
    assert contribution.model_config.get("extra") == "forbid"

    with pytest.raises(ValidationError, match="frozen"):
        contribution.project_id = PROJECT_B  # type: ignore[misc]

    with pytest.raises(ValidationError, match="bool"):
        PortfolioProjectEffortContribution(
            **_contribution_kwargs(known=True)  # type: ignore[arg-type]
        )

    with pytest.raises(ValidationError, match="Extra inputs"):
        PortfolioProjectEffortContribution(
            **_contribution_kwargs(),
            share_of_effort=0.5,  # type: ignore[call-arg]
        )


def test_contribution_model_rejects_malformed_partition_and_types() -> None:
    with pytest.raises(
        ValidationError,
        match="ordinary_estimate_count \\+ calibrated_estimate_count",
    ):
        PortfolioProjectEffortContribution(
            **_contribution_kwargs(ordinary=2, calibrated=1)
        )
    with pytest.raises(ValidationError, match="greater than or equal"):
        PortfolioProjectEffortContribution(**_contribution_kwargs(known=-1))
    with pytest.raises(ValidationError, match="valid integer"):
        PortfolioProjectEffortContribution(
            **_contribution_kwargs(known=1.5)  # type: ignore[arg-type]
        )


def test_contribution_model_does_not_infer_completeness() -> None:
    # V1.26 MUST NOT infer completeness from unestimated_entity_count. These
    # values are structurally valid copies: the V1.26-owned contribution model
    # must accept them because it only copies fields verbatim. (The authoritative
    # V1.24 PlannedEffortSummary enforces its own completeness semantics and is
    # what the boundary re-validates upstream.)

    # Partially estimated (unestimated > 0) yet carrying a numeric total: the
    # contribution model must NOT reject this on completeness grounds.
    partial_with_total = PortfolioProjectEffortContribution(
        **_contribution_kwargs(
            known=60, estimated=1, unestimated=1, ordinary=1, calibrated=0, total=95
        )
    )
    assert partial_with_total.unestimated_entity_count == 1
    assert partial_with_total.total_duration_seconds == 95

    # Fully estimated (unestimated == 0) yet carrying a total that is not the
    # exact known sum: the contribution model must NOT reject this either.
    full_with_mismatched_total = PortfolioProjectEffortContribution(
        **_contribution_kwargs(ordinary=2, calibrated=0, total=90)
    )
    assert full_with_mismatched_total.unestimated_entity_count == 0
    assert full_with_mismatched_total.total_duration_seconds == 90

    # And a partial (None) total must still be allowed verbatim.
    partial_none_total = PortfolioProjectEffortContribution(
        **_contribution_kwargs(
            known=60, estimated=1, unestimated=1, ordinary=1, calibrated=0, total=None
        )
    )
    assert partial_none_total.total_duration_seconds is None


# --- contribution summary model invariants -----------------------------------


def test_summary_model_requires_matching_project_count() -> None:
    with pytest.raises(
        ValidationError,
        match="project_count=1 does not equal the number of contributions \\(0\\)",
    ):
        PortfolioProjectEffortContributionSummary(
            portfolio_id=PORTFOLIO_ID, project_count=1, projects=()
        )
    with pytest.raises(
        ValidationError,
        match="project_count=1 does not equal the number of contributions \\(2\\)",
    ):
        PortfolioProjectEffortContributionSummary(
            portfolio_id=PORTFOLIO_ID,
            project_count=1,
            projects=(
                PortfolioProjectEffortContribution(**_contribution_kwargs(PROJECT_A)),
                PortfolioProjectEffortContribution(**_contribution_kwargs(PROJECT_B)),
            ),
        )


def test_summary_model_rejects_duplicate_project_ids() -> None:
    with pytest.raises(ValidationError, match="duplicate project IDs"):
        PortfolioProjectEffortContributionSummary(
            portfolio_id=PORTFOLIO_ID,
            project_count=2,
            projects=(
                PortfolioProjectEffortContribution(**_contribution_kwargs(PROJECT_A)),
                PortfolioProjectEffortContribution(**_contribution_kwargs(PROJECT_A)),
            ),
        )


def test_summary_model_rejects_hostile_constructed_contributions() -> None:
    hostile = PortfolioProjectEffortContribution.model_construct(
        project_id=PROJECT_A,
        known_duration_seconds=100,
        estimated_entity_count=1,
        unestimated_entity_count=0,
        ordinary_estimate_count=5,  # bypasses the partition invariant
        calibrated_estimate_count=1,
        total_duration_seconds=100,
    )
    # Rejected by V1.26 re-validation of the contribution's own invariants
    # (Pydantic re-runs the model's validator on the passed instance).
    with pytest.raises(
        ValidationError,
        match="ordinary_estimate_count \\+ calibrated_estimate_count",
    ):
        PortfolioProjectEffortContributionSummary(
            portfolio_id=PORTFOLIO_ID, project_count=1, projects=(hostile,)
        )
    # Rejected because a non-contribution value is not a genuine contribution.
    with pytest.raises(
        ValidationError,
        match="PortfolioProjectEffortContribution",
    ):
        PortfolioProjectEffortContributionSummary(
            portfolio_id=PORTFOLIO_ID,
            project_count=1,
            projects=("not-a-uuid",),  # type: ignore[list-item]
        )


# --- boundary input requirements --------------------------------------------


def test_boundary_requires_genuine_v125_summary() -> None:
    for invalid in (
        None,
        42,
        "summary",
        {"portfolio_id": PORTFOLIO_ID},
        _project_summary(
            project_id=PROJECT_A,
            subtree=_fully_estimated_subtree(),
            ordinary=1,
            calibrated=1,
        ),
    ):
        with pytest.raises(
            PortfolioProjectEffortContributionError,
            match="genuine V1\\.25 PortfolioEffectiveEffortSummary instance is required",
        ):
            project_portfolio_effort_contributions(invalid)  # type: ignore[arg-type]


def test_boundary_requires_revalidated_complete_v125_payload() -> None:
    clean = _project_summary(
        project_id=PROJECT_A,
        subtree=_fully_estimated_subtree(),
        ordinary=1,
        calibrated=1,
    )
    snapshot = copy.deepcopy(clean.model_dump())

    hostile_v125 = PortfolioEffectiveEffortSummary.model_construct(
        portfolio_id=PORTFOLIO_ID,
        project_count=2,  # wrong: only one project present
        known_duration_seconds=100,
        estimated_entity_count=2,
        unestimated_entity_count=0,
        ordinary_estimate_count=1,
        calibrated_estimate_count=1,
        total_duration_seconds=100,
        projects=(clean,),
    )
    with pytest.raises(
        PortfolioProjectEffortContributionError,
        match="strict re-validation",
    ):
        project_portfolio_effort_contributions(hostile_v125)

    # The boundary must not mutate the input, even on a rejected input.
    assert clean.model_dump() == snapshot


def test_boundary_rejects_hostile_nested_v124_project_summaries() -> None:
    hostile_v124 = WorkBreakdownEffectiveEffortSummary.model_construct(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_A,
        effort=_fully_estimated_subtree(),
        # Bypasses the partition invariant: ordinary + calibrated > estimated.
        ordinary_estimate_count=7,
        calibrated_estimate_count=1,
    )
    hostile_v125 = PortfolioEffectiveEffortSummary.model_construct(
        portfolio_id=PORTFOLIO_ID,
        project_count=1,
        known_duration_seconds=100,
        estimated_entity_count=2,
        unestimated_entity_count=0,
        ordinary_estimate_count=7,
        calibrated_estimate_count=1,
        total_duration_seconds=100,
        projects=(hostile_v124,),
    )
    with pytest.raises(
        PortfolioProjectEffortContributionError,
        match="strict re-validation",
    ):
        project_portfolio_effort_contributions(hostile_v125)


def test_boundary_rejects_hostile_nested_planned_effort_subtrees() -> None:
    hostile_effort = PlannedEffortSummary.model_construct(
        known_duration_seconds=60,
        estimated_entity_count=1,
        unestimated_entity_count=1,
        # Bypasses the subtree total rule: partially estimated but with a total.
        total_duration_seconds=95,
    )
    # Use ``model_construct`` to bypass V1.24's own completeness so the boundary's
    # strict re-validation (down through the nested ``PlannedEffortSummary``) is
    # what must reject it — identical to the sibling hostile-tree tests.
    hostile_v124 = WorkBreakdownEffectiveEffortSummary.model_construct(
        portfolio_id=PORTFOLIO_ID,
        project_id=PROJECT_A,
        effort=hostile_effort,
        ordinary_estimate_count=1,
        calibrated_estimate_count=0,
    )
    hostile_v125 = PortfolioEffectiveEffortSummary.model_construct(
        portfolio_id=PORTFOLIO_ID,
        project_count=1,
        known_duration_seconds=60,
        estimated_entity_count=1,
        unestimated_entity_count=1,
        ordinary_estimate_count=1,
        calibrated_estimate_count=0,
        total_duration_seconds=None,
        projects=(hostile_v124,),
    )
    with pytest.raises(
        PortfolioProjectEffortContributionError,
        match="strict re-validation",
    ):
        project_portfolio_effort_contributions(hostile_v125)


def test_boundary_rejects_foreign_project_scoping_in_v125() -> None:
    foreign = WorkBreakdownEffectiveEffortSummary.model_construct(
        portfolio_id=UUID("99999999-9999-4999-8999-999999999999"),
        project_id=PROJECT_A,
        effort=_fully_estimated_subtree(),
        ordinary_estimate_count=1,
        calibrated_estimate_count=0,
    )
    hostile_v125 = PortfolioEffectiveEffortSummary.model_construct(
        portfolio_id=PORTFOLIO_ID,
        project_count=1,
        known_duration_seconds=100,
        estimated_entity_count=2,
        unestimated_entity_count=0,
        ordinary_estimate_count=1,
        calibrated_estimate_count=0,
        total_duration_seconds=100,
        projects=(foreign,),
    )
    with pytest.raises(
        PortfolioProjectEffortContributionError,
        match="strict re-validation",
    ):
        project_portfolio_effort_contributions(hostile_v125)


def test_boundary_rejects_unreadable_or_noniterable_v125_shapes() -> None:
    noniterable_projects = PortfolioEffectiveEffortSummary.model_construct(
        portfolio_id=PORTFOLIO_ID,
        project_count=0,
        known_duration_seconds=0,
        estimated_entity_count=0,
        unestimated_entity_count=0,
        ordinary_estimate_count=0,
        calibrated_estimate_count=0,
        total_duration_seconds=0,
        projects=42,  # type: ignore[arg-type]
    )
    with pytest.raises(PortfolioProjectEffortContributionError):
        project_portfolio_effort_contributions(noniterable_projects)


# --- boundary behaviour ------------------------------------------------------


def test_boundary_preserves_exact_fields_order_and_none_total() -> None:
    v125 = _v125_summary(
        [
            _project_summary(
                project_id=PROJECT_C,
                subtree=_partial_subtree(),
                ordinary=1,
                calibrated=0,
            ),
            _project_summary(
                project_id=PROJECT_A,
                subtree=_fully_estimated_subtree(),
                ordinary=0,
                calibrated=2,
            ),
        ]
    )
    assert [p.project_id for p in v125.projects] == [PROJECT_C, PROJECT_A]

    result = project_portfolio_effort_contributions(v125)
    assert result.portfolio_id == PORTFOLIO_ID
    assert result.project_count == 2
    assert [c.project_id for c in result.projects] == [PROJECT_C, PROJECT_A]

    first = result.projects[0]
    assert first.known_duration_seconds == 60
    assert first.estimated_entity_count == 1
    assert first.unestimated_entity_count == 1
    assert first.ordinary_estimate_count == 1
    assert first.calibrated_estimate_count == 0
    assert first.total_duration_seconds is None

    second = result.projects[1]
    assert second.known_duration_seconds == 100
    assert second.estimated_entity_count == 2
    assert second.unestimated_entity_count == 0
    assert second.ordinary_estimate_count == 0
    assert second.calibrated_estimate_count == 2
    assert second.total_duration_seconds == 100

    # Deterministic: a second call produces an identical projection.
    again = project_portfolio_effort_contributions(v125)
    assert again.model_dump() == result.model_dump()

    # The result is frozen.
    with pytest.raises(ValidationError, match="frozen"):
        result.projects = ()  # type: ignore[misc]


def test_summary_result_is_strict_frozen_and_extra_forbid() -> None:
    result = project_portfolio_effort_contributions(
        _v125_summary(
            [
                _project_summary(
                    project_id=PROJECT_A,
                    subtree=_fully_estimated_subtree(),
                    ordinary=1,
                    calibrated=1,
                )
            ]
        )
    )
    assert result.model_config.get("frozen") is True
    assert result.model_config.get("extra") == "forbid"
    with pytest.raises(ValidationError, match="Extra inputs"):
        PortfolioProjectEffortContributionSummary(
            portfolio_id=PORTFOLIO_ID,
            project_count=1,
            projects=result.projects,
            share_of_portfolio=1.0,  # type: ignore[call-arg]
        )


def test_boundary_empty_portfolio_projects_to_empty_contribution_summary() -> None:
    v125 = _v125_summary([])
    assert v125.project_count == 0
    assert v125.projects == ()

    result = project_portfolio_effort_contributions(v125)
    assert result.portfolio_id == PORTFOLIO_ID
    assert result.project_count == 0
    assert result.projects == ()
    assert result.model_dump() == {
        "portfolio_id": PORTFOLIO_ID,
        "project_count": 0,
        "projects": (),
    }


def test_boundary_does_not_modify_input() -> None:
    v125 = _v125_summary(
        [
            _project_summary(
                project_id=PROJECT_A,
                subtree=_fully_estimated_subtree(),
                ordinary=1,
                calibrated=1,
            ),
            _project_summary(
                project_id=PROJECT_B,
                subtree=_partial_subtree(),
                ordinary=1,
                calibrated=0,
            ),
        ]
    )
    snapshot = copy.deepcopy(v125.model_dump())
    nested_snapshot = [copy.deepcopy(p.model_dump()) for p in v125.projects]

    project_portfolio_effort_contributions(v125)
    project_portfolio_effort_contributions(v125)

    assert v125.model_dump() == snapshot
    assert [p.model_dump() for p in v125.projects] == nested_snapshot


# --- no relative metrics anywhere -------------------------------------------


def test_projected_models_expose_no_relative_metrics() -> None:
    contribution_fields = set(PortfolioProjectEffortContribution.model_fields)
    summary_fields = set(PortfolioProjectEffortContributionSummary.model_fields)
    forbidden = {
        "share",
        "share_of_effort",
        "percentage",
        "percent",
        "ratio",
        "weight",
        "rank",
    }
    assert not (contribution_fields & forbidden)
    assert not (summary_fields & forbidden)
    assert contribution_fields == {
        "project_id",
        "known_duration_seconds",
        "estimated_entity_count",
        "unestimated_entity_count",
        "ordinary_estimate_count",
        "calibrated_estimate_count",
        "total_duration_seconds",
    }
    assert summary_fields == {
        "portfolio_id",
        "project_count",
        "projects",
    }


def test_public_export_surface() -> None:
    import trajectory_os.application as application
    from trajectory_os.application import (
        execution_effort_project_contributions as module,
    )

    expected = [
        "PortfolioProjectEffortContribution",
        "PortfolioProjectEffortContributionError",
        "PortfolioProjectEffortContributionSummary",
        "project_portfolio_effort_contributions",
    ]
    assert module.__all__ == expected
    for name in expected:
        assert getattr(application, name) is getattr(module, name)
