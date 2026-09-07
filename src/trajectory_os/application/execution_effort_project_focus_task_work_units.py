"""V1.37 — CURRENT task work-unit projection from an accepted project focus.

Projects ONE genuine, freshly revalidated V1.36
``PortfolioProjectEffortFocusBinding`` onto the CURRENT canonical TASK
identities contained in each selected project's V1.1 work breakdown.

V1.37 is explicitly **STRUCTURAL IDENTITY PROJECTION ONLY**. It chooses no
task, recommends no task, filters by no status, infers no readiness, ranks
nothing, does not persist, does not mutate, generates no UUID, reads no
clock, and calls no repository / provider / AI / runtime boundary.

Authority:

- the V1.36 binding supplies the exact selected project identities and the
  authoritative selected-project order (and the accepted decision
  provenance);
- V1.1 ``build_work_breakdown(portfolio, project_id)`` is the SOLE
  authoritative CURRENT-WBS boundary: membership, containment grammar,
  ambiguity / cycle validation, sibling ordering, and pre-order
  structural semantics.

CURRENT-WBS / PROVENANCE:

V1.37 claims CURRENT structural membership only. It does NOT claim the
returned TASKs existed when the V1.34/V1.35 focus decision was accepted:
historical WBS identity was never persisted and is never invented.

TASK DOES NOT MEAN READY:

TASK rows are projected regardless of status (ACTIVE, WAITING, PAUSED,
COMPLETED, CANCELLED, ARCHIVED, or any other lifecycle state) and NO
timestamp, estimate, confidence, description, or title is interpreted as
execution readiness.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    model_validator,
)

from trajectory_os.application.execution_effort_project_focus_binding import (
    PortfolioProjectEffortFocusBinding,
)
from trajectory_os.domain.entities import EntityType, TrajectoryEntity
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.work_breakdown import (
    WorkBreakdownError,
    WorkBreakdownNode,
    build_work_breakdown,
)

__all__ = [
    "FocusedProjectTaskWorkUnits",
    "PortfolioProjectFocusTaskWorkUnitProjection",
    "PortfolioProjectFocusTaskWorkUnitProjectionError",
    "project_current_task_work_units_from_focus_binding",
]


class PortfolioProjectFocusTaskWorkUnitProjectionError(ValueError):
    """Raised when a genuine V1.36 focus binding cannot be projected onto
    the CURRENT task work units of its selected projects.

    Raised for: a ``binding`` that is not a genuine V1.36
    ``PortfolioProjectEffortFocusBinding``, a binding that fails fresh
    strict re-validation (hostile ``model_construct`` payloads, bad
    scalars, malformed ``selected_project_ids``), a ``portfolio`` that is
    not a genuine ``Portfolio``, an exact ``portfolio.id`` mismatch, a
    selected project identity that is missing from the CURRENT portfolio or
    resolves to a non-PROJECT entity, or a V1.1 ``WorkBreakdownError``
    (preserved as ``__cause__``).
    """


# ---------------------------------------------------------------------------
# Models (immutable, self-validating).
# ---------------------------------------------------------------------------


class FocusedProjectTaskWorkUnits(BaseModel):
    """One selected project's CURRENT canonical TASK work-unit identities.

    Carries the exact selected ``project_id`` and the exact task
    identities projected from its CURRENT V1.1 work breakdown in exact
    V1.1 pre-order: no sorting, no deduplication, no ranking, no
    readiness filtering. An empty ``task_ids`` tuple is coherent (a
    selected project with no projected TASK nodes).
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    project_id: UUID
    task_ids: tuple[UUID, ...]

    @model_validator(mode="after")
    def _validate_row_invariants(self) -> FocusedProjectTaskWorkUnits:
        if len(self.task_ids) != len(set(self.task_ids)):
            raise ValueError("task_ids must be unique within a project row")
        return self


class PortfolioProjectFocusTaskWorkUnitProjection(BaseModel):
    """The complete immutable V1.37 CURRENT task work-unit projection.

    ``decision_id`` and ``decided_at`` carry the V1.36 (accepted focus
    decision) provenance EXACTLY; ``portfolio_id``,
    ``selected_project_count``, and the ``projects`` tuple mirror the
    freshly revalidated V1.36 binding. The ``projects`` tuple order is the
    semantically authoritative V1.36 selected-project order and is
    preserved exactly. No generated identity or timestamp appears here.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    decision_id: UUID
    decided_at: datetime
    portfolio_id: UUID
    selected_project_count: Annotated[StrictInt, Field(ge=0)]
    projects: tuple[FocusedProjectTaskWorkUnits, ...]

    @model_validator(mode="after")
    def _validate_projection_invariants(
        self,
    ) -> PortfolioProjectFocusTaskWorkUnitProjection:
        if len(self.projects) != self.selected_project_count:
            raise ValueError(
                "selected_project_count must equal the length of "
                f"projects (got {len(self.projects)})"
            )

        project_ids = [row.project_id for row in self.projects]
        if len(project_ids) != len(set(project_ids)):
            raise ValueError("project_ids must be unique across project rows")

        task_ids = [
            task_id for row in self.projects for task_id in row.task_ids
        ]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("task_ids must be globally unique across rows")

        return self


# ---------------------------------------------------------------------------
# Pure projection boundary.
# ---------------------------------------------------------------------------


def project_current_task_work_units_from_focus_binding(
    binding: PortfolioProjectEffortFocusBinding,
    portfolio: Portfolio,
) -> PortfolioProjectFocusTaskWorkUnitProjection:
    """Project ONE genuine V1.36 focus binding onto the CURRENT canonical
    TASK identities of each selected project's V1.1 work breakdown.

    Pure and deterministic: no repository argument, no persistence, no
    provider / AI / runtime boundary, no generated identity or timestamp,
    no portfolio mutation, and no independent WBS containment grammar —
    V1.1 ``build_work_breakdown`` is the sole WBS structural authority and
    its returned ``WorkBreakdownStructure`` is flattened iteratively in
    exact pre-order.

    Steps (deliberately ordered; every failure is raised BEFORE the output
    is built):

      1. require a genuine V1.36 ``PortfolioProjectEffortFocusBinding``
         instance (``None``, dicts, strings, foreign models, and duck
         types are rejected);
      2. freshly strict-revalidate the COMPLETE V1.36 payload and retain
         ONLY the validated copy — never trust an already-created object
         (hostile ``model_construct`` values or attribute tampering are
         rejected);
      3. from this point on every semantic binding read uses ONLY the
         retained validated binding, never the caller-owned original;
      4. require ``portfolio`` to be a genuine ``Portfolio`` instance;
      5. require ``portfolio.id == validated.portfolio_id`` EXACTLY;
      6. if ``selected_project_count == 0`` (which implies
         ``selected_project_ids == ()`` by V1.36 invariants): return an
         empty projection WITHOUT projecting any work breakdown and
         WITHOUT fabricating anything;
      7. otherwise iterate the validated ``selected_project_ids`` in exact
         tuple order;
      8. for every selected project: require it currently exists in the
         portfolio and its ``entity_type`` is exactly ``PROJECT``;
      9. call ``build_work_breakdown(portfolio, project_id)``; a V1.1
         ``WorkBreakdownError`` is translated into
         ``PortfolioProjectFocusTaskWorkUnitProjectionError`` with the
         original preserved as ``__cause__``;
     10. flatten ONLY the returned ``WorkBreakdownStructure`` iteratively
         in exact pre-order (no independent containment traversal);
     11. a node is a TASK exactly when its canonical CURRENT entity from
         the portfolio has ``entity_type is EntityType.TASK``;
     12. collect every TASK id in exact V1.1 pre-order (status is never
         consulted: TASKs are projected in every lifecycle state);
     13. emit exactly one ``FocusedProjectTaskWorkUnits`` row for every
         selected project, even with an empty ``task_ids`` tuple;
     14. construct the immutable projection using the validated V1.36
         provenance and selected-project order EXACTLY and return.

    Repeated identical calls are value-identical.
    """
    # -- 1. genuine V1.36 focus binding -----------------------------------
    if not isinstance(binding, PortfolioProjectEffortFocusBinding):
        raise PortfolioProjectFocusTaskWorkUnitProjectionError(
            "a genuine V1.36 PortfolioProjectEffortFocusBinding is "
            f"required, got {type(binding).__name__}"
        )

    # -- 2. freshly strict-revalidate the COMPLETE V1.36 payload ----------
    try:
        binding_payload: object = binding.model_dump(mode="python")
    except (AttributeError, TypeError, ValueError) as exc:
        raise PortfolioProjectFocusTaskWorkUnitProjectionError(
            "the supplied focus binding is not the V1.36 shape"
        ) from exc

    try:
        validated = PortfolioProjectEffortFocusBinding.model_validate(
            binding_payload, strict=True
        )
    except ValidationError as exc:
        raise PortfolioProjectFocusTaskWorkUnitProjectionError(
            "the supplied focus binding failed strict re-validation"
        ) from exc

    # -- 3. from here on: ONLY the retained validated binding -------------

    # -- 4. genuine Portfolio ----------------------------------------------
    if not isinstance(portfolio, Portfolio):
        raise PortfolioProjectFocusTaskWorkUnitProjectionError(
            "a genuine Portfolio is required, "
            f"got {type(portfolio).__name__}"
        )

    # -- 5. exact portfolio identity ---------------------------------------
    if portfolio.id != validated.portfolio_id:
        raise PortfolioProjectFocusTaskWorkUnitProjectionError(
            "the supplied portfolio does not exactly match the focus "
            "binding portfolio_id"
        )

    # -- 6. empty focus: no WBS projection, nothing fabricated -------------
    rows: list[FocusedProjectTaskWorkUnits]

    if validated.selected_project_count == 0:
        rows = []
    else:
        # -- 7. exact validated selected-project order ---------------------
        rows = []

        # Local CURRENT entity index: O(1) lookups instead of repeated
        # linear scans. First-occurrence semantics mirror
        # ``Portfolio.get_entity`` exactly.
        entity_lookup: dict[UUID, TrajectoryEntity] = {}
        for portfolio_entity in portfolio.entities:
            entity_lookup.setdefault(portfolio_entity.id, portfolio_entity)

        for project_id in validated.selected_project_ids:
            # -- 8. CURRENT existence and exact PROJECT type --------------
            project_entity: TrajectoryEntity | None = entity_lookup.get(
                project_id
            )

            if project_entity is None:
                raise PortfolioProjectFocusTaskWorkUnitProjectionError(
                    "selected project not present in the CURRENT "
                    f"portfolio: {project_id}"
                )

            if project_entity.entity_type is not EntityType.PROJECT:
                raise PortfolioProjectFocusTaskWorkUnitProjectionError(
                    "a selected project identity must resolve to a "
                    f"PROJECT entity, got {project_entity.entity_type.value}"
                )

            # -- 9. V1.1 is the sole WBS structural authority --------------
            try:
                structure = build_work_breakdown(portfolio, project_id)
            except WorkBreakdownError as exc:
                raise (
                    PortfolioProjectFocusTaskWorkUnitProjectionError(
                        "the CURRENT work breakdown of the selected "
                        f"project could not be projected: {exc}"
                    )
                ) from exc

            # -- 10-12. iterative exact pre-order TASK collection ----------
            task_ids: list[UUID] = []
            stack: list[WorkBreakdownNode] = [structure.root]

            while stack:
                node = stack.pop()

                entity = entity_lookup.get(node.entity_id)
                if entity is not None and (
                    entity.entity_type is EntityType.TASK
                ):
                    task_ids.append(node.entity_id)

                stack.extend(reversed(node.children))

            # -- 13. one row per selected project, even when empty --------
            rows.append(
                FocusedProjectTaskWorkUnits(
                    project_id=project_id,
                    task_ids=tuple(task_ids),
                )
            )

    # -- 14. immutable projection from the validated binding EXACTLY -------
    return PortfolioProjectFocusTaskWorkUnitProjection(
        decision_id=validated.decision_id,
        decided_at=validated.decided_at,
        portfolio_id=validated.portfolio_id,
        selected_project_count=validated.selected_project_count,
        projects=tuple(rows),
    )
