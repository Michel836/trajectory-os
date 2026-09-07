"""V1.37 — CURRENT task work-unit projection from an accepted project focus.

Covers:
* the ``FocusedProjectTaskWorkUnits`` and
  ``PortfolioProjectFocusTaskWorkUnitProjection`` models (strict / frozen /
  extra-forbid, exact field set, bool rejection, count/tuple mismatch,
  duplicate project / task rejection);
* the ``project_current_task_work_units_from_focus_binding`` boundary:
  - genuine V1.36 binding and genuine Portfolio required;
  - hostile ``model_construct`` bindings rejected by fresh strict
    re-validation and the validated copy as authoritative;
  - exact portfolio identity, missing / non-PROJECT selected identity;
  - V1.1 ``build_work_breakdown`` as sole WBS authority,
    ``WorkBreakdownError`` translated with ``__cause__``, no independent
    containment grammar;
  - exact selected-project and V1.1 pre-order preservation (no sorting),
    nested / same-title TASK capture, empty-row and non-TASK exclusion;
  - TASK retention across every lifecycle status (status is policy-free);
  - zero-selection returns an empty projection without any WBS call;
  - discipline: determinism, input immutability, no repository /
    persistence / provider surface, no clock / UUID generation, public
    exports.
"""

from __future__ import annotations

import types
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

import trajectory_os.application as app
import trajectory_os.application.execution_effort_project_focus_task_work_units as v137_mod  # noqa: E501
from trajectory_os.application import (
    FocusedProjectTaskWorkUnits,
    PortfolioProjectEffortFocusBinding,
    PortfolioProjectFocusTaskWorkUnitProjection,
    PortfolioProjectFocusTaskWorkUnitProjectionError,
    project_current_task_work_units_from_focus_binding,
)
from trajectory_os.domain.entities import EntityStatus, EntityType, TrajectoryEntity
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation
from trajectory_os.domain.work_breakdown import WorkBreakdownError

PORTFOLIO_ID = uuid.UUID("61616161-6161-4161-8161-616161616161")
OTHER_PORTFOLIO_ID = uuid.UUID("61616161-6161-4161-8161-616161616162")
DECISION_ID = uuid.UUID("62626262-6262-4262-8262-626262626262")
DECIDED_AT = datetime(2025, 7, 1, 8, 30, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures — deterministic identities and portfolio construction helpers.
# ---------------------------------------------------------------------------


# Fixed identities: T1B > T1A lexicographically, but portfolio entity order
# deliberately lists T1B BEFORE T1A so any UUID-sorted output would differ
# from the authoritative V1.1 pre-order.
P_ALPHA = uuid.UUID("a0000000-0000-4000-8000-000000000001")
P_BETA = uuid.UUID("b0000000-0000-4000-8000-000000000002")
D_GATE = uuid.UUID("c0000000-0000-4000-8000-000000000003")
WP_BACKEND = uuid.UUID("d0000000-0000-4000-8000-000000000004")
T1A = uuid.UUID("a1a1a1a1-0000-4000-8000-000000000001")
T1B = uuid.UUID("b1b1b1b1-0000-4000-8000-000000000002")
T2_TASK3 = uuid.UUID("c3c3c3c3-0000-4000-8000-000000000003")
T2_TASK4 = uuid.UUID("d4d4d4d4-0000-4000-8000-000000000004")


def _binding(
    *,
    portfolio_id: uuid.UUID = PORTFOLIO_ID,
    selected: tuple[uuid.UUID, ...] = (P_ALPHA,),
    source_count: int | None = None,
    count: int | None = None,
    decision_id: uuid.UUID = DECISION_ID,
    decided_at: datetime = DECIDED_AT,
) -> PortfolioProjectEffortFocusBinding:
    """A valid V1.36 binding shape for boundary tests."""
    if count is None:
        count = len(selected)
    if source_count is None:
        source_count = max(1, count)
    return PortfolioProjectEffortFocusBinding(
        decision_id=decision_id,
        decided_at=decided_at,
        portfolio_id=portfolio_id,
        accepted_requested_limit=max(1, count),
        source_project_count=source_count,
        selected_project_count=count,
        total_duration_seconds=3600,
        selected_duration_seconds=1800,
        remaining_duration_seconds=1800,
        selected_project_ids=tuple(selected),
    )


def _entity(
    entity_uuid: uuid.UUID,
    entity_type: EntityType,
    title: str,
    status: EntityStatus = EntityStatus.ACTIVE,
) -> TrajectoryEntity:
    return TrajectoryEntity(
        id=entity_uuid,
        entity_type=entity_type,
        title=title,
        status=status,
    )


def _belongs_to(child: TrajectoryEntity, parent: TrajectoryEntity) -> TrajectoryRelation:
    return TrajectoryRelation(
        source_id=child.id,
        target_id=parent.id,
        relation_type=RelationType.BELONGS_TO,
    )


def _portfolio(
    entities: list[TrajectoryEntity],
    relations: list[TrajectoryRelation],
    portfolio_id: uuid.UUID = PORTFOLIO_ID,
) -> Portfolio:
    return Portfolio(
        id=portfolio_id,
        name="V1.37 test",
        entities=entities,
        relations=relations,
    )


def _main_portfolio() -> Portfolio:
    """Two selected projects with CURRENT tasks at different depths.

    P_ALPHA: project -> deliverable (D_GATE) -> TASK T1B, T1A
             (entity order lists T1B before T1A: same title "Ship";
             lexicographically T1A < T1B, so no-sorted output is
             (T1B, T1A)).

    P_BETA:  project -> WORK_PACKAGE (WP_BACKEND) -> TASK T2_TASK3
             and project -> TASK T2_TASK4 directly.
             Entity order lists T2_TASK3 before T2_TASK4.
    """
    alpha_project = _entity(P_ALPHA, EntityType.PROJECT, "Alpha")
    beta_project = _entity(P_BETA, EntityType.PROJECT, "Beta")
    gate = _entity(D_GATE, EntityType.DELIVERABLE, "Gate deliverable")
    backend = _entity(WP_BACKEND, EntityType.WORK_PACKAGE, "Backend")
    t1a = _entity(T1A, EntityType.TASK, "Ship")
    t1b = _entity(T1B, EntityType.TASK, "Ship")
    t3 = _entity(T2_TASK3, EntityType.TASK, "Wire API")
    t4 = _entity(T2_TASK4, EntityType.TASK, "Ship")

    return _portfolio(
        entities=[beta_project, t3, backend, t4, alpha_project, gate, t1b, t1a],
        relations=[
            _belongs_to(t3, beta_project),
            _belongs_to(backend, beta_project),
            _belongs_to(t4, beta_project),
            _belongs_to(gate, alpha_project),
            _belongs_to(t1b, gate),
            _belongs_to(t1a, gate),
        ],
    )


def _projection(
    projects: tuple[FocusedProjectTaskWorkUnits, ...],
    *,
    count: int | None = None,
    decision_id: uuid.UUID = DECISION_ID,
    decided_at: datetime = DECIDED_AT,
    portfolio_id: uuid.UUID = PORTFOLIO_ID,
) -> PortfolioProjectFocusTaskWorkUnitProjection:
    if count is None:
        count = len(projects)
    return PortfolioProjectFocusTaskWorkUnitProjection(
        decision_id=decision_id,
        decided_at=decided_at,
        portfolio_id=portfolio_id,
        selected_project_count=count,
        projects=projects,
    )


def _row(
    project_id: uuid.UUID, task_ids: tuple[uuid.UUID, ...] = ()
) -> FocusedProjectTaskWorkUnits:
    return FocusedProjectTaskWorkUnits(project_id=project_id, task_ids=tuple(task_ids))


class _ForeignModel(BaseModel):
    """A different model; must never be accepted."""

    model_config = {"frozen": True}

    field: str = "foreign"


# ---------------------------------------------------------------------------
# Model strictness.
# ---------------------------------------------------------------------------


class TestFocusedProjectTaskWorkUnitsModel:
    def test_strict_frozen_and_extra_forbid(self) -> None:
        row = _row(P_ALPHA, (T1A,))
        assert row.model_config["strict"] is True
        assert row.model_config["frozen"] is True
        assert row.model_config["extra"] == "forbid"

        with pytest.raises(ValidationError):
            FocusedProjectTaskWorkUnits(
                project_id=P_ALPHA,
                task_ids=(T1A,),
                unexpected_extra="not-allowed",
            )

        with pytest.raises(ValidationError):
            row.project_id = P_BETA  # type: ignore[misc]

        # strict UUID: a string identity is rejected
        with pytest.raises(ValidationError):
            FocusedProjectTaskWorkUnits(
                project_id="not-a-uuid",  # type: ignore[arg-type]
                task_ids=(),
            )

    def test_duplicate_task_ids_within_row_rejected(self) -> None:
        with pytest.raises(ValidationError, match="unique within a project row"):
            _row(P_ALPHA, (T1A, T1A))

    def test_empty_task_ids_valid(self) -> None:
        assert _row(P_ALPHA, ()).task_ids == ()


class TestProjectionModel:
    def test_strict_frozen_and_extra_forbid(self) -> None:
        projection = _projection(())
        assert projection.model_config["strict"] is True
        assert projection.model_config["frozen"] is True
        assert projection.model_config["extra"] == "forbid"

        with pytest.raises(ValidationError):
            projection.selected_project_count = 1  # type: ignore[misc]

        with pytest.raises(ValidationError):
            PortfolioProjectFocusTaskWorkUnitProjection(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO_ID,
                selected_project_count=0,
                projects=(),
                unexpected_extra="not-allowed",
            )

    def test_bool_rejected_for_selected_project_count(self) -> None:
        with pytest.raises(ValidationError, match="selected_project_count"):
            _projection((), count=True)  # type: ignore[arg-type]

    def test_selected_count_projects_tuple_mismatch_rejected(self) -> None:
        with pytest.raises(ValidationError, match="selected_project_count"):
            _projection((_row(P_ALPHA),), count=2)
        with pytest.raises(ValidationError, match="selected_project_count"):
            _projection((_row(P_ALPHA), _row(P_BETA)), count=1)

    def test_zero_count_only_valid_with_empty_projects(self) -> None:
        assert _projection((), count=0).projects == ()
        with pytest.raises(ValidationError, match="selected_project_count"):
            _projection((_row(P_ALPHA),), count=0)

    def test_duplicate_project_ids_rejected(self) -> None:
        with pytest.raises(ValidationError, match="unique across project rows"):
            _projection((_row(P_ALPHA), _row(P_ALPHA)), count=2)

    def test_duplicate_task_ids_across_rows_rejected(self) -> None:
        with pytest.raises(ValidationError, match="globally unique"):
            _projection(
                (_row(P_ALPHA, (T1A,)), _row(P_BETA, (T1A,))),
                count=2,
            )

    def test_unique_tasks_across_rows_valid(self) -> None:
        projection = _projection(
            (_row(P_ALPHA, (T1A,)), _row(P_BETA, (T1B, T2_TASK3))),
            count=2,
        )
        assert projection.selected_project_count == 2


# ---------------------------------------------------------------------------
# Boundary type / hostility.
# ---------------------------------------------------------------------------


class TestBoundaryTypes:
    def test_genuine_binding_required(self) -> None:
        wrong = [
            None,
            "a binding",
            {"decision_id": str(DECISION_ID)},
            [_row(P_ALPHA)],
            _ForeignModel(),
            types.SimpleNamespace(
                portfolio_id=PORTFOLIO_ID, selected_project_ids=(P_ALPHA,)
            ),
        ]
        portfolio = _main_portfolio()
        for item in wrong:
            with pytest.raises(PortfolioProjectFocusTaskWorkUnitProjectionError):
                project_current_task_work_units_from_focus_binding(  # type: ignore[arg-type]
                    item, portfolio
                )

    def test_hostile_model_construct_bad_scalar_rejected(self) -> None:
        hostile = PortfolioProjectEffortFocusBinding.model_construct(
            decision_id=DECISION_ID,
            decided_at=DECIDED_AT,
            portfolio_id=PORTFOLIO_ID,
            accepted_requested_limit=True,  # bool where StrictInt required
            source_project_count=1,
            selected_project_count="one",  # string where StrictInt required
            total_duration_seconds=3600,
            selected_duration_seconds=1800,
            remaining_duration_seconds=1800,
            selected_project_ids=(P_ALPHA,),
        )
        with pytest.raises(PortfolioProjectFocusTaskWorkUnitProjectionError):
            project_current_task_work_units_from_focus_binding(
                hostile, _main_portfolio()
            )

    def test_hostile_model_construct_malformed_selected_project_ids_rejected(
        self,
    ) -> None:
        hostile = PortfolioProjectEffortFocusBinding.model_construct(
            decision_id=DECISION_ID,
            decided_at=DECIDED_AT,
            portfolio_id=PORTFOLIO_ID,
            accepted_requested_limit=1,
            source_project_count=2,
            selected_project_count=2,
            total_duration_seconds=3600,
            selected_duration_seconds=1800,
            remaining_duration_seconds=1800,
            selected_project_ids=["not-a-uuid", 123],  # type: ignore[list-item]
        )
        with pytest.raises(PortfolioProjectFocusTaskWorkUnitProjectionError):
            project_current_task_work_units_from_focus_binding(
                hostile, _main_portfolio()
            )

    def test_original_not_semantically_trusted_after_revalidation(self) -> None:
        # A caller object whose raw fields FAIL the V1.36 invariants
        # (count/tuple mismatch) is rejected: the boundary cannot read the
        # caller-owned original to build a "valid enough" projection —
        # fresh revalidation is authoritative.
        inconsistent = PortfolioProjectEffortFocusBinding.model_construct(
            decision_id=DECISION_ID,
            decided_at=DECIDED_AT,
            portfolio_id=PORTFOLIO_ID,
            accepted_requested_limit=1,
            source_project_count=5,
            selected_project_count=5,  # raw field inconsistent with ids
            total_duration_seconds=3600,
            selected_duration_seconds=1800,
            remaining_duration_seconds=1800,
            selected_project_ids=(P_ALPHA,),
        )
        with pytest.raises(PortfolioProjectFocusTaskWorkUnitProjectionError):
            project_current_task_work_units_from_focus_binding(
                inconsistent, _main_portfolio()
            )

    def test_fresh_validated_copy_is_authoritative(self) -> None:
        # A model_construct() object carrying fully coherent values still
        # only passes via fresh strict re-validation; its provenance fields
        # flow into the projection EXACTLY.
        constructed = PortfolioProjectEffortFocusBinding.model_construct(
            decision_id=DECISION_ID,
            decided_at=DECIDED_AT,
            portfolio_id=PORTFOLIO_ID,
            accepted_requested_limit=1,
            source_project_count=1,
            selected_project_count=1,
            total_duration_seconds=3600,
            selected_duration_seconds=1800,
            remaining_duration_seconds=1800,
            selected_project_ids=(P_ALPHA,),
        )
        projection = project_current_task_work_units_from_focus_binding(
            constructed, _main_portfolio()
        )
        assert projection.decision_id == DECISION_ID
        assert projection.decided_at == DECIDED_AT
        assert projection.portfolio_id == PORTFOLIO_ID

    def test_genuine_portfolio_required(self) -> None:
        wrong = [
            None,
            "a portfolio",
            {"id": str(PORTFOLIO_ID)},
            _ForeignModel(),
            types.SimpleNamespace(
                id=PORTFOLIO_ID, entities=(), relations=()
            ),
        ]
        binding = _binding(selected=(P_ALPHA,))
        for item in wrong:
            with pytest.raises(PortfolioProjectFocusTaskWorkUnitProjectionError):
                project_current_task_work_units_from_focus_binding(
                    binding, item  # type: ignore[arg-type]
                )


# ---------------------------------------------------------------------------
# Portfolio compatibility.
# ---------------------------------------------------------------------------


class TestPortfolioCompatibility:
    def test_portfolio_id_mismatch_rejected(self) -> None:
        binding = _binding(selected=(P_ALPHA,))
        other = _main_portfolio()  # same entities, but a different identity
        other_id_conflicting = Portfolio(
            id=OTHER_PORTFOLIO_ID,
            name="Other",
            entities=list(other.entities),
            relations=list(other.relations),
        )
        with pytest.raises(
            PortfolioProjectFocusTaskWorkUnitProjectionError,
            match="does not exactly match",
        ):
            project_current_task_work_units_from_focus_binding(
                binding, other_id_conflicting
            )

    def test_binding_of_other_portfolio_rejected(self) -> None:
        binding = _binding(portfolio_id=OTHER_PORTFOLIO_ID, selected=(P_ALPHA,))
        with pytest.raises(
            PortfolioProjectFocusTaskWorkUnitProjectionError,
            match="does not exactly match",
        ):
            project_current_task_work_units_from_focus_binding(
                binding, _main_portfolio()
            )

    def test_selected_project_missing_from_current_portfolio_rejected(self) -> None:
        missing = uuid.UUID("f0000000-0000-4000-8000-000000000009")
        binding = _binding(selected=(missing,), source_count=1)
        with pytest.raises(
            PortfolioProjectFocusTaskWorkUnitProjectionError,
            match="not present in the CURRENT portfolio",
        ):
            project_current_task_work_units_from_focus_binding(
                binding, _main_portfolio()
            )

    @pytest.mark.parametrize(
        "non_project_id",
        [D_GATE, WP_BACKEND, T1A, T1B, T2_TASK3, T2_TASK4],
    )
    def test_selected_id_resolving_to_non_project_rejected(self, non_project_id) -> None:
        binding = _binding(selected=(non_project_id,), source_count=1)
        with pytest.raises(
            PortfolioProjectFocusTaskWorkUnitProjectionError,
            match="must resolve to a PROJECT entity",
        ):
            project_current_task_work_units_from_focus_binding(
                binding, _main_portfolio()
            )


# ---------------------------------------------------------------------------
# WBS authority.
# ---------------------------------------------------------------------------


class TestWbsAuthority:
    def test_build_work_breakdown_used_per_selected_project(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[Portfolio, uuid.UUID]] = []
        original = v137_mod.build_work_breakdown

        def spy(portfolio: Portfolio, root_id: uuid.UUID):
            calls.append((portfolio, root_id))
            return original(portfolio, root_id)

        monkeypatch.setattr(v137_mod, "build_work_breakdown", spy)
        binding = _binding(selected=(P_BETA, P_ALPHA))

        project_current_task_work_units_from_focus_binding(
            binding, _main_portfolio()
        )

        assert [call_root for _, call_root in calls] == [P_BETA, P_ALPHA]

    def test_work_breakdown_error_translated_with_cause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original_error = WorkBreakdownError("wbs boom (V1.1)")

        def broken(portfolio: Portfolio, root_id: uuid.UUID):
            raise original_error

        monkeypatch.setattr(v137_mod, "build_work_breakdown", broken)
        with pytest.raises(
            PortfolioProjectFocusTaskWorkUnitProjectionError,
            match="wbs boom",
        ) as excinfo:
            project_current_task_work_units_from_focus_binding(
                _binding(selected=(P_ALPHA,)), _main_portfolio()
            )

        assert excinfo.value.__cause__ is original_error

    def test_real_v11_error_translated(self) -> None:
        # A genuine V1.1 invariant failure (cycle) must surface through the
        # boundary as the V1.37 error, not the raw WorkBreakdownError.
        project_a = _entity(P_ALPHA, EntityType.PROJECT, "Cycle A")
        task = _entity(T1A, EntityType.TASK, "Cycle task")
        cycle_portfolio = _portfolio(
            entities=[project_a, task],
            relations=[
                _belongs_to(task, project_a),
                _belongs_to(project_a, task),
            ],
        )
        # the cycle sits under P_ALPHA which is the only selected project
        binding = _binding(selected=(P_ALPHA,))
        with pytest.raises(
            PortfolioProjectFocusTaskWorkUnitProjectionError,
        ) as excinfo:
            project_current_task_work_units_from_focus_binding(
                binding, cycle_portfolio
            )
        assert isinstance(excinfo.value.__cause__, WorkBreakdownError)

    def test_no_independent_containment_grammar(self) -> None:
        module_vars = vars(v137_mod)
        assert "is_work_breakdown_containment_allowed" not in module_vars
        assert "RelationType" not in module_vars
        assert "BELONGS_TO" not in module_vars

        source = Path(v137_mod.__file__).read_text()
        assert "BELONGS_TO" not in source
        assert "portfolio.relations" not in source


# ---------------------------------------------------------------------------
# Order / structure.
# ---------------------------------------------------------------------------


class TestOrderStructure:
    def test_selected_project_order_exactly_preserved(self) -> None:
        binding = _binding(selected=(P_BETA, P_ALPHA))
        projection = project_current_task_work_units_from_focus_binding(
            binding, _main_portfolio()
        )
        assert [row.project_id for row in projection.projects] == [
            P_BETA,
            P_ALPHA,
        ]
        assert projection.selected_project_count == 2

    def test_exact_v11_task_pre_order_and_no_sorting(self) -> None:
        binding = _binding(selected=(P_BETA, P_ALPHA))
        projection = project_current_task_work_units_from_focus_binding(
            binding, _main_portfolio()
        )
        by_project = {row.project_id: row.task_ids for row in projection.projects}

        # entity order lists T1B before T1A (and T1A < T1B): a UUID-sorted
        # output would be (T1A, T1B), so this asserts no sorting.
        assert by_project[P_ALPHA] == (T1B, T1A)
        assert by_project[P_BETA] == (T2_TASK3, T2_TASK4)

    def test_tasks_nested_under_deliverable_and_work_package_captured(
        self,
    ) -> None:
        projection = project_current_task_work_units_from_focus_binding(
            _binding(selected=(P_ALPHA,)), _main_portfolio()
        )
        assert set(projection.projects[0].task_ids) == {T1A, T1B}

        projection_beta = project_current_task_work_units_from_focus_binding(
            _binding(selected=(P_BETA,)), _main_portfolio()
        )

        assert set(projection_beta.projects[0].task_ids) == {T2_TASK3, T2_TASK4}

    def test_deeper_valid_wbs_nesting_captured(self) -> None:
        deep_project = _entity(P_ALPHA, EntityType.PROJECT, "Deep")
        deliverable = _entity(D_GATE, EntityType.DELIVERABLE, "Release")
        mid_package = _entity(WP_BACKEND, EntityType.WORK_PACKAGE, "Core")
        leaf_package = _entity(T1A, EntityType.WORK_PACKAGE, "Leaf set")
        deep_task = _entity(T1B, EntityType.TASK, "Final step")
        deep_portfolio = _portfolio(
            entities=[deep_project, deliverable, mid_package, leaf_package, deep_task],
            relations=[
                _belongs_to(deliverable, deep_project),
                _belongs_to(mid_package, deliverable),
                _belongs_to(leaf_package, mid_package),
                _belongs_to(deep_task, leaf_package),
            ],
        )
        projection = project_current_task_work_units_from_focus_binding(
            _binding(selected=(P_ALPHA,)), deep_portfolio
        )
        assert projection.projects[0].task_ids == (T1B,)

    def test_same_title_task_siblings_both_preserved(self) -> None:
        projection = project_current_task_work_units_from_focus_binding(
            _binding(selected=(P_BETA, P_ALPHA)), _main_portfolio()
        )
        by_project = {row.project_id: row.task_ids for row in projection.projects}
        # T1B and T1A share the title "Ship"; T4 also shares it with T1A/T1B
        # but lives under P_BETA. Every identity is preserved.
        assert by_project[P_ALPHA] == (T1B, T1A)
        assert T2_TASK4 in by_project[P_BETA]

    def test_project_with_no_task_retained_with_empty_tuple(self) -> None:
        empty_project = _entity(P_ALPHA, EntityType.PROJECT, "Bare project")
        bare_portfolio = _portfolio(entities=[empty_project], relations=[])
        projection = project_current_task_work_units_from_focus_binding(
            _binding(selected=(P_ALPHA,)), bare_portfolio
        )
        assert len(projection.projects) == 1
        assert projection.projects[0].project_id == P_ALPHA
        assert projection.projects[0].task_ids == ()

    def test_non_task_wbs_nodes_excluded_from_task_ids(self) -> None:
        projection = project_current_task_work_units_from_focus_binding(
            _binding(selected=(P_BETA, P_ALPHA)), _main_portfolio()
        )
        all_tasks = [
            task_id for row in projection.projects for task_id in row.task_ids
        ]
        for wbs_not_task in (P_ALPHA, P_BETA, D_GATE, WP_BACKEND):
            assert wbs_not_task not in all_tasks


# ---------------------------------------------------------------------------
# STATUS NON-POLICY.
# ---------------------------------------------------------------------------


class TestStatusNonPolicy:
    @pytest.mark.parametrize(
        "status",
        [
            EntityStatus.ACTIVE,
            EntityStatus.WAITING,
            EntityStatus.PAUSED,
            EntityStatus.COMPLETED,
            EntityStatus.CANCELLED,
            EntityStatus.ARCHIVED,
        ],
    )
    def test_task_retained_regardless_of_status(self, status: EntityStatus) -> None:
        project = _entity(P_ALPHA, EntityType.PROJECT, "Status project")
        task = _entity(T1A, EntityType.TASK, "Any status task", status=status)
        status_portfolio = _portfolio(
            entities=[project, task],
            relations=[_belongs_to(task, project)],
        )
        projection = project_current_task_work_units_from_focus_binding(
            _binding(selected=(P_ALPHA,)), status_portfolio
        )
        assert projection.projects[0].task_ids == (T1A,)

    def test_all_statuses_together_projected(self) -> None:
        project = _entity(P_ALPHA, EntityType.PROJECT, "All statuses")
        statuses = [
            EntityStatus.ACTIVE,
            EntityStatus.WAITING,
            EntityStatus.PAUSED,
            EntityStatus.COMPLETED,
            EntityStatus.CANCELLED,
            EntityStatus.ARCHIVED,
        ]
        task_ids = [
            uuid.uuid5(uuid.NAMESPACE_DNS, f"v137-task-{index}")
            for index in range(len(statuses))
        ]
        tasks = [
            _entity(task_id, EntityType.TASK, f"status task {index}", status)
            for index, (task_id, status) in enumerate(zip(task_ids, statuses, strict=True))
        ]
        all_portfolio = _portfolio(
            entities=[project, *tasks],
            relations=[_belongs_to(task, project) for task in tasks],
        )
        projection = project_current_task_work_units_from_focus_binding(
            _binding(selected=(P_ALPHA,)), all_portfolio
        )
        assert projection.projects[0].task_ids == tuple(task_ids)


# ---------------------------------------------------------------------------
# Empty state.
# ---------------------------------------------------------------------------


class TestEmptyState:
    def test_zero_selection_returns_empty_without_wbs_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def forbidden(portfolio: Portfolio, root_id: uuid.UUID):
            pytest.fail("build_work_breakdown must not be called for an empty focus")

        monkeypatch.setattr(v137_mod, "build_work_breakdown", forbidden)
        binding = _binding(selected=(), source_count=2, count=0)
        assert binding.selected_project_ids == ()

        projection = project_current_task_work_units_from_focus_binding(
            binding, _main_portfolio()
        )

        assert projection.projects == ()
        assert projection.selected_project_count == 0

    def test_zero_selection_fabricates_nothing(self) -> None:
        own_decision = uuid.UUID("63636363-6363-4363-8363-636363636363")
        own_time = datetime(2025, 8, 1, 12, 0, tzinfo=UTC)
        binding = _binding(
            selected=(),
            source_count=0,
            count=0,
            decision_id=own_decision,
            decided_at=own_time,
        )
        projection = project_current_task_work_units_from_focus_binding(
            binding, _main_portfolio()
        )
        assert projection.decision_id == own_decision
        assert projection.decided_at == own_time
        assert projection.portfolio_id == PORTFOLIO_ID


# ---------------------------------------------------------------------------
# Discipline.
# ---------------------------------------------------------------------------


class TestDiscipline:
    def test_repeated_identical_calls_value_identical(self) -> None:
        binding = _binding(selected=(P_BETA, P_ALPHA))
        portfolio = _main_portfolio()
        first = project_current_task_work_units_from_focus_binding(
            binding, portfolio
        )
        second = project_current_task_work_units_from_focus_binding(
            binding, portfolio
        )
        assert first == second

    def test_binding_not_mutated(self) -> None:
        binding = _binding(selected=(P_BETA, P_ALPHA))
        before = binding.model_dump(mode="json")
        project_current_task_work_units_from_focus_binding(
            binding, _main_portfolio()
        )
        assert binding.model_dump(mode="json") == before

    def test_portfolio_not_mutated(self) -> None:
        portfolio = _main_portfolio()
        before = portfolio.model_dump(mode="json")
        project_current_task_work_units_from_focus_binding(
            _binding(selected=(P_BETA, P_ALPHA)), portfolio
        )
        assert portfolio.model_dump(mode="json") == before

    def test_no_repository_persistence_or_provider_surface(self) -> None:
        module_vars = vars(v137_mod)
        assert not any(
            "repository" in name.lower()
            or "persist" in name.lower()
            or "provider" in name.lower()
            for name in module_vars
        )

        source = Path(v137_mod.__file__).read_text()
        assert "datetime.now" not in source
        assert "uuid4" not in source
        assert "uuid5" not in source
        assert "random" not in source
        assert "import os" not in source

    def test_public_exports_correct(self) -> None:
        required = [
            "FocusedProjectTaskWorkUnits",
            "PortfolioProjectFocusTaskWorkUnitProjection",
            "PortfolioProjectFocusTaskWorkUnitProjectionError",
            "project_current_task_work_units_from_focus_binding",
        ]
        for name in required:
            assert name in app.__all__
            assert getattr(app, name) is not None

    def test_error_is_a_value_error(self) -> None:
        assert issubclass(
            PortfolioProjectFocusTaskWorkUnitProjectionError, ValueError
        )
