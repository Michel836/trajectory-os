"""V1.38 — CURRENT status and exact relation projection for focused TASK work units.

Projects ONE genuine, freshly strict-revalidated V1.37
``PortfolioProjectFocusTaskWorkUnitProjection`` plus one matching CURRENT
canonical ``Portfolio`` onto the exact CURRENT status and the exact
CURRENT incoming/outgoing relations of every focused TASK work unit.

V1.38 is explicitly **FACTUAL CURRENT PROJECTION ONLY**. It copies no
policy and infers nothing: it does not infer readiness, blockage,
dependency satisfaction, waiting satisfaction, next action, next task,
priority, score, rank, recommendation, or execution order. It does not
rebuild the work breakdown, does not persist, does not mutate, generates
no UUID, reads no clock, and calls no repository / provider / AI /
runtime boundary.

Authority:

- the V1.37 projection is the sole authority for the focused TASK
  identities, their order, and the accepted focus-decision provenance;
- the CURRENT canonical ``Portfolio`` is the sole authority for CURRENT
  entity status and CURRENT relation rows.

Relation semantics are deliberately minimal: a relation is incoming to a
focused task exactly when its ``target_id`` equals the task identity, and
outgoing exactly when its ``source_id`` equals it. No relation type is
interpreted, none is filtered — membership (``belongs_to``) rows are
projected exactly like every other relation type — and direction is never
read semantically beyond ``source_id`` / ``target_id`` equality.

CURRENT / HISTORICAL SEMANTICS:

V1.38 claims CURRENT facts only. It does NOT prove the projected status
existed when the focus decision was made, nor that any projected relation
existed at that time; historical state was never persisted and is never
invented.

TASK STATUS AND RELATIONS DO NOT MEAN READY:

Task rows are projected with their exact CURRENT status regardless of
lifecycle state, and relation rows are copied with their exact type and
direction: they do NOT express readiness, blockage, dependency, or
waiting satisfaction of any kind.
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

from trajectory_os.application.execution_effort_project_focus_task_work_units import (  # noqa: E501
    PortfolioProjectFocusTaskWorkUnitProjection,
)
from trajectory_os.domain.entities import (
    EntityStatus,
    EntityType,
    SourceKind,
    TrajectoryEntity,
)
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.domain.relations import RelationType, TrajectoryRelation

__all__ = [
    "FocusedProjectTaskCurrentRelations",
    "FocusedTaskCurrentRelation",
    "FocusedTaskCurrentRelations",
    "PortfolioProjectFocusTaskCurrentRelationProjection",
    "PortfolioProjectFocusTaskCurrentRelationProjectionError",
    "project_current_relations_for_focused_task_work_units",
]


class PortfolioProjectFocusTaskCurrentRelationProjectionError(ValueError):
    """Raised when a genuine V1.37 focus task projection cannot be
    projected onto CURRENT statuses and relations.

    Raised for: a ``focused_tasks`` payload that is not a genuine V1.37
    ``PortfolioProjectFocusTaskWorkUnitProjection``, a payload that fails
    fresh strict re-validation (hostile ``model_construct`` payloads, bad
    scalars, malformed nested project/task payloads), a ``portfolio``
    that is not a genuine ``Portfolio``, an exact ``portfolio.id``
    mismatch, a V1.37 project identity that is missing from the CURRENT
    portfolio or resolves to a non-PROJECT entity, or a V1.37 task
    identity that is missing from the CURRENT portfolio or resolves to a
    non-TASK entity.
    """


# ---------------------------------------------------------------------------
# Models (immutable, self-validating).
# ---------------------------------------------------------------------------


class FocusedTaskCurrentRelation(BaseModel):
    """The exact copy of ONE canonical CURRENT ``TrajectoryRelation``
    touching a focused TASK work unit.

    No normalization, no reinterpretation, and no generated values: the
    relation id, type, ``source_id`` / ``target_id`` endpoints, source
    kind, and confidence are copied exactly from the CURRENT portfolio
    relation. Direction is not read semantically: a row is incoming or
    outgoing purely via ``target_id`` / ``source_id`` equality with the
    focused task (enforced by its owning
    ``FocusedTaskCurrentRelations`` row).
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    relation_id: UUID
    relation_type: RelationType
    source_id: UUID
    target_id: UUID
    source: SourceKind
    confidence: float


class FocusedTaskCurrentRelations(BaseModel):
    """One focused TASK work unit's exact CURRENT status plus its exact
    CURRENT incoming/outgoing relations.

    ``status`` is the task's exact CURRENT ``EntityStatus``: it is a
    copied fact, not an interpretation of readiness or blockage. Every
    incoming relation has ``target_id == task_id``; every outgoing
    relation has ``source_id == task_id``; relation IDs are unique
    within each direction; and both tuples retain the exact
    ``Portfolio.relations`` order restricted to the task. Empty tuples
    are coherent (a task with zero incoming or zero outgoing relations).
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    task_id: UUID
    status: EntityStatus
    incoming_relations: tuple[FocusedTaskCurrentRelation, ...]
    outgoing_relations: tuple[FocusedTaskCurrentRelation, ...]

    @model_validator(mode="after")
    def _validate_row_invariants(self) -> FocusedTaskCurrentRelations:
        for relation in self.incoming_relations:
            if relation.target_id != self.task_id:
                raise ValueError(
                    "every incoming relation must have target_id "
                    "equal to the focused task_id"
                )

        for relation in self.outgoing_relations:
            if relation.source_id != self.task_id:
                raise ValueError(
                    "every outgoing relation must have source_id "
                    "equal to the focused task_id"
                )

        incoming_ids = [r.relation_id for r in self.incoming_relations]
        if len(incoming_ids) != len(set(incoming_ids)):
            raise ValueError(
                "relation IDs must be unique within incoming_relations"
            )

        outgoing_ids = [r.relation_id for r in self.outgoing_relations]
        if len(outgoing_ids) != len(set(outgoing_ids)):
            raise ValueError(
                "relation IDs must be unique within outgoing_relations"
            )

        return self


class FocusedProjectTaskCurrentRelations(BaseModel):
    """One selected project's focused TASK work units, each carrying its
    exact CURRENT status and exact CURRENT incoming/outgoing relations.

    Task IDs are unique within the row and the ``tasks`` tuple order is
    the exact V1.37 task tuple order: no sorting, no deduplication, no
    status or relation filtering. An empty ``tasks`` tuple is coherent
    (a selected project with zero focused TASK work units).
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    project_id: UUID
    tasks: tuple[FocusedTaskCurrentRelations, ...]

    @model_validator(mode="after")
    def _validate_project_row_invariants(
        self,
    ) -> FocusedProjectTaskCurrentRelations:
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("task IDs must be unique within a project row")
        return self


class PortfolioProjectFocusTaskCurrentRelationProjection(BaseModel):
    """The complete immutable V1.38 CURRENT status/relation projection.

    ``decision_id`` and ``decided_at`` carry the freshly strict-
    revalidated V1.37 (accepted focus decision) provenance EXACTLY;
    ``portfolio_id``, ``selected_project_count``, and the ``projects``
    tuple align with the revalidated V1.37 projection. The ``projects``
    tuple order is the exact V1.37 project order and is preserved
    exactly. No generated identity or timestamp appears here.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    decision_id: UUID
    decided_at: datetime
    portfolio_id: UUID
    selected_project_count: Annotated[StrictInt, Field(ge=0)]
    projects: tuple[FocusedProjectTaskCurrentRelations, ...]

    @model_validator(mode="after")
    def _validate_projection_invariants(
        self,
    ) -> PortfolioProjectFocusTaskCurrentRelationProjection:
        if len(self.projects) != self.selected_project_count:
            raise ValueError(
                "selected_project_count must equal the length of "
                f"projects (got {len(self.projects)})"
            )

        project_ids = [row.project_id for row in self.projects]
        if len(project_ids) != len(set(project_ids)):
            raise ValueError("project IDs must be unique across project rows")

        task_ids = [
            task.task_id for row in self.projects for task in row.tasks
        ]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError(
                "task IDs must be globally unique across project rows"
            )

        return self


# ---------------------------------------------------------------------------
# Pure projection boundary.
# ---------------------------------------------------------------------------


def project_current_relations_for_focused_task_work_units(
    focused_tasks: PortfolioProjectFocusTaskWorkUnitProjection,
    portfolio: Portfolio,
) -> PortfolioProjectFocusTaskCurrentRelationProjection:
    """Project ONE genuine V1.37 focus task work-unit projection plus the
    matching CURRENT canonical Portfolio onto the exact CURRENT status
    and exact CURRENT incoming/outgoing relations of every focused TASK
    work unit.

    Pure and deterministic: no repository argument, no persistence, no
    provider / AI / runtime boundary, no generated identity or timestamp,
    no work-breakdown construction, and no portfolio mutation.

    Steps (deliberately ordered; every failure is raised BEFORE the
    output is built):

      1. require a genuine V1.37
         ``PortfolioProjectFocusTaskWorkUnitProjection`` instance
         (``None``, dicts, strings, foreign models, and duck types are
         rejected);
      2. freshly strict-revalidate the COMPLETE V1.37 payload and retain
         ONLY the validated copy — never trust an already-created object
         (hostile ``model_construct`` values or attribute tampering are
         rejected);
      3. from this point on every semantic V1.37 read uses ONLY the
         retained validated projection, never the caller-owned original;
      4. require ``portfolio`` to be a genuine ``Portfolio`` instance;
      5. require ``portfolio.id == validated.portfolio_id`` EXACTLY;
      6. if ``selected_project_count == 0`` (which implies
         ``projects == ()`` by V1.37 invariants): return an empty
         projection WITHOUT projecting any task, entity, or relation and
         WITHOUT fabricating anything;
      7. otherwise: build one local entity lookup index from
         ``portfolio.entities`` (first-occurrence semantics preserved)
         and build incoming/outgoing relation indexes in a single pass
         over ``portfolio.relations``, preserving the exact original
         relation order;
      8. iterate the validated V1.37 ``projects`` rows in exact tuple
         order;
      9. for every V1.37 project: require it currently exists in the
         portfolio and its ``entity_type`` is exactly ``PROJECT``;
     10. iterate the V1.37 ``task_ids`` in exact tuple order;
     11. for every V1.37 task: require it currently exists in the
         portfolio and its ``entity_type`` is exactly ``TASK``;
     12. copy the task's exact CURRENT ``EntityStatus``;
     13. project every CURRENT relation with ``target_id == task_id``
         (incoming) and every CURRENT relation with
         ``source_id == task_id`` (outgoing), each in exact
         ``Portfolio.relations`` order; no relation type is filtered,
         sorted, or deduplicated — every distinct canonical relation row
         is preserved;
     14. emit exactly one task row for every V1.37 task, even with zero
         incoming and zero outgoing relations;
     15. emit exactly one project row for every V1.37 project, even with
         zero tasks;
     16. construct the immutable projection using the validated V1.37
         provenance and project/task order EXACTLY and return.

    Repeated identical calls are value-identical.
    """

    # -- 1. genuine V1.37 focus task projection --------------------------
    if not isinstance(
        focused_tasks, PortfolioProjectFocusTaskWorkUnitProjection
    ):
        raise PortfolioProjectFocusTaskCurrentRelationProjectionError(
            "a genuine V1.37 "
            "PortfolioProjectFocusTaskWorkUnitProjection is required, "
            f"got {type(focused_tasks).__name__}"
        )

    # -- 2. freshly strict-revalidate the COMPLETE V1.37 payload ---------
    try:
        focused_payload: object = focused_tasks.model_dump(mode="python")
    except (AttributeError, TypeError, ValueError) as exc:
        raise (
            PortfolioProjectFocusTaskCurrentRelationProjectionError(
                "the supplied focus task projection is not the V1.37 "
                "shape"
            )
        ) from exc

    try:
        validated = PortfolioProjectFocusTaskWorkUnitProjection.model_validate(
            focused_payload, strict=True
        )
    except ValidationError as exc:
        raise (
            PortfolioProjectFocusTaskCurrentRelationProjectionError(
                "the supplied focus task projection failed strict "
                "re-validation"
            )
        ) from exc

    # -- 3. from here on: ONLY the retained validated V1.37 projection ---

    # -- 4. genuine Portfolio ---------------------------------------------
    if not isinstance(portfolio, Portfolio):
        raise PortfolioProjectFocusTaskCurrentRelationProjectionError(
            "a genuine Portfolio is required, "
            f"got {type(portfolio).__name__}"
        )

    # -- 5. exact portfolio identity ---------------------------------------
    if portfolio.id != validated.portfolio_id:
        raise PortfolioProjectFocusTaskCurrentRelationProjectionError(
            "the supplied portfolio does not exactly match the focus "
            "task projection portfolio_id"
        )

    # -- 6. empty focus: no projection, nothing fabricated ------------------
    if validated.selected_project_count == 0:
        return PortfolioProjectFocusTaskCurrentRelationProjection(
            decision_id=validated.decision_id,
            decided_at=validated.decided_at,
            portfolio_id=validated.portfolio_id,
            selected_project_count=0,
            projects=(),
        )

    # -- 7. one-pass CURRENT entity and relation indexes -------------------
    # Entity lookup with first-occurrence semantics mirroring
    # ``Portfolio.get_entity``. Portfolio validation guarantees entity
    # IDs are unique, so this never drops a distinct entity.
    entity_lookup: dict[UUID, TrajectoryEntity] = {}
    for portfolio_entity in portfolio.entities:
        entity_lookup.setdefault(portfolio_entity.id, portfolio_entity)

    # Incoming/outgoing indexes in a single pass over
    # ``portfolio.relations``; append order preserves the exact original
    # relation order within every task.
    incoming_index: dict[UUID, list[TrajectoryRelation]] = {}
    outgoing_index: dict[UUID, list[TrajectoryRelation]] = {}
    for relation in portfolio.relations:
        incoming_index.setdefault(relation.target_id, []).append(relation)
        outgoing_index.setdefault(relation.source_id, []).append(relation)

    # -- 8-15. exact V1.37 project/task order, CURRENT facts ---------------
    rows: list[FocusedProjectTaskCurrentRelations] = []

    for project_row in validated.projects:
        project_entity: TrajectoryEntity | None = entity_lookup.get(
            project_row.project_id
        )

        if project_entity is None:
            raise (
                PortfolioProjectFocusTaskCurrentRelationProjectionError(
                    "a V1.37 focused project is not present in the "
                    f"CURRENT portfolio: {project_row.project_id}"
                )
            )

        if project_entity.entity_type is not EntityType.PROJECT:
            raise (
                PortfolioProjectFocusTaskCurrentRelationProjectionError(
                    "a V1.37 focused project identity must resolve to a "
                    "PROJECT entity, "
                    f"got {project_entity.entity_type.value}"
                )
            )

        task_rows: list[FocusedTaskCurrentRelations] = []

        for task_id in project_row.task_ids:
            task_entity: TrajectoryEntity | None = entity_lookup.get(task_id)

            if task_entity is None:
                raise (
                    PortfolioProjectFocusTaskCurrentRelationProjectionError(
                        "a V1.37 focused task is not present in the "
                        f"CURRENT portfolio: {task_id}"
                    )
                )

            if task_entity.entity_type is not EntityType.TASK:
                raise (
                    PortfolioProjectFocusTaskCurrentRelationProjectionError(
                        "a V1.37 focused task identity must resolve to a "
                        "TASK entity, "
                        f"got {task_entity.entity_type.value}"
                    )
                )

            task_rows.append(
                FocusedTaskCurrentRelations(
                    task_id=task_id,
                    status=task_entity.status,
                    incoming_relations=tuple(
                        FocusedTaskCurrentRelation(
                            relation_id=relation.id,
                            relation_type=relation.relation_type,
                            source_id=relation.source_id,
                            target_id=relation.target_id,
                            source=relation.source,
                            confidence=relation.confidence,
                        )
                        for relation in incoming_index.get(task_id, ())
                    ),
                    outgoing_relations=tuple(
                        FocusedTaskCurrentRelation(
                            relation_id=relation.id,
                            relation_type=relation.relation_type,
                            source_id=relation.source_id,
                            target_id=relation.target_id,
                            source=relation.source,
                            confidence=relation.confidence,
                        )
                        for relation in outgoing_index.get(task_id, ())
                    ),
                )
            )

        rows.append(
            FocusedProjectTaskCurrentRelations(
                project_id=project_row.project_id,
                tasks=tuple(task_rows),
            )
        )

    # -- 16. immutable projection from the validated V1.37 projection -----
    return PortfolioProjectFocusTaskCurrentRelationProjection(
        decision_id=validated.decision_id,
        decided_at=validated.decided_at,
        portfolio_id=validated.portfolio_id,
        selected_project_count=validated.selected_project_count,
        projects=tuple(rows),
    )
