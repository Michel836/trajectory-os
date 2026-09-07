"""V1.40 — deterministic READY TASK candidate projection.

Projects ONE genuine, freshly strict-revalidated V1.39
``PortfolioProjectFocusTaskCurrentConstraintEvaluation`` into the exact
set of focused TASK work units that are READY, preserving the exact
V1.39 project order and, within each project, the exact V1.39 task
order, with every project row retained (including rows whose READY
task set is empty).

V1.40 is a PURE PROJECTION ONLY. It is a deterministic, explainable,
policy-bounded, immutable application boundary:

- deterministic and pure: identical validated input yields identical
  output; repeated calls are value-identical;
- explainable: every retained candidate is exactly one V1.39 READY task
  row — no inference anywhere;
- immutable: all output models are strict / frozen / extra-forbid;
- policy-bounded: the ONLY policy is the exact filter below — nothing
  else is interpreted;
- no side effects: no persistence, no provider / AI / runtime boundary,
  no work-breakdown construction, no wall clock, no generated identity
  or timestamp.

V1.40 does NOT:

- choose one next task, nor recommend / rank / score / sort /
  prioritize any task or project;
- infer urgency, importance, value, impact, or risk;
- use effort duration as any selection policy;
- inspect titles, descriptions, timestamps, or relation details;
- consult a Portfolio or rebuild the work breakdown (no call to
  ``build_work_breakdown``);
- re-evaluate lifecycle status, constraints, relation types,
  counterpart statuses, constraint satisfaction, or readiness:
  V1.39 already owns those semantics EXACTLY;
- persist, or read the wall clock, or generate UUIDs / timestamps;
- mutate the caller-owned V1.39 evaluation.

Any future choice between multiple READY candidates belongs to a later
milestone (V1.41+) and is explicitly OUT OF SCOPE here.

Authority:

The sole semantic input authority is ONE genuine V1.39
``PortfolioProjectFocusTaskCurrentConstraintEvaluation``. The boundary
requires that genuine instance, freshly strict-revalidates the COMPLETE
payload, retains only the validated copy, and from that point performs
every semantic read ONLY from the retained validated copy. NO
Portfolio argument is accepted or required and no other semantic
authority is consulted.

Exact filter policy

A focused TASK is a V1.40 candidate IF AND ONLY IF

    task.readiness_state is FocusedTaskConstraintReadinessState.READY

READY             -> retained
CONSTRAINED       -> excluded
INELIGIBLE_STATUS -> excluded

Ordering policy

1. V1.39 project tuple order is preserved EXACTLY;
2. within each project, the V1.39 task tuple is traversed in EXACT
   order and READY rows are retained in place;
3. no sorting, no ranking, no reordering, no deduplication, no
   secondary key of any kind;
4. project rows whose filtered READY set is empty (zero source tasks,
   all CONSTRAINED, all INELIGIBLE_STATUS, or mixed leaving zero)
   REMAIN in the output with ``tasks == ()``.
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

from trajectory_os.application.execution_effort_project_focus_task_current_constraints import (  # noqa: E501
    FocusedTaskConstraintReadinessState,
    PortfolioProjectFocusTaskCurrentConstraintEvaluation,
)

__all__ = [
    "PortfolioProjectFocusReadyTaskCandidates",
    "PortfolioProjectFocusReadyTaskCandidatesError",
    "ReadyProjectTaskCandidates",
    "ReadyTaskCandidate",
    "project_ready_task_candidates_from_current_constraint_evaluation",
]


class PortfolioProjectFocusReadyTaskCandidatesError(ValueError):
    """Raised when a genuine V1.39 READY-relevant payload cannot be
    projected.

    Raised for: a payload that is not a genuine
    ``PortfolioProjectFocusTaskCurrentConstraintEvaluation``, or a
    payload that fails fresh strict re-validation (hostile
    ``model_construct`` payloads, bad scalars, malformed nested
    project/task/constraint payloads, inconsistent invariant-carrying
    fields).
    """


# ---------------------------------------------------------------------------
# Output models (immutable, self-validating).
# ---------------------------------------------------------------------------


class ReadyTaskCandidate(BaseModel):
    """One V1.40 READY TASK candidate.

    ``task_id`` is the exact identity of one focused TASK that was
    READY in the freshly validated V1.39 source. That is its ONLY
    meaning: no status, no readiness state, no constraint, no title,
    no description, no score, no rank, no priority, no urgency, no
    timestamp, and no generated identity appears here.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    task_id: UUID


class ReadyProjectTaskCandidates(BaseModel):
    """One selected project's V1.40 READY TASK candidates.

    ``project_id`` is the exact V1.39 project identity; ``tasks`` is
    the exact in-place filtered READY task tuple in exact validated
    V1.39 task order (an empty tuple is coherent: a selected project
    with zero READY TASK work units).

    Invariants (enforced):

    - task IDs are unique within the row (the retained READY rows
      inherit the V1.39 unique-task invariant);
    - an empty ``tasks`` tuple is valid;
    - no generated data appears here.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    project_id: UUID
    tasks: tuple[ReadyTaskCandidate, ...]

    @model_validator(mode="after")
    def _validate_project_row_invariants(self) -> ReadyProjectTaskCandidates:
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("task IDs must be unique within a project row")
        return self


class PortfolioProjectFocusReadyTaskCandidates(BaseModel):
    """The complete immutable V1.40 READY TASK candidate projection.

    ``decision_id``, ``decided_at``, and ``portfolio_id`` carry the
    freshly strict-revalidated V1.39 provenance EXACTLY;
    ``selected_project_count`` exactly equals ``len(projects)`` and is
    preserved from V1.39; ``ready_task_count`` exactly equals the total
    number of READY task candidates across all project rows; the
    ``projects`` tuple preserves the exact validated V1.39 project
    order, including project rows whose READY set is empty.

    Invariants (enforced):

    - ``selected_project_count == len(projects)``;
    - project IDs are unique across project rows;
    - task IDs are globally unique across all project rows;
    - ``ready_task_count`` exactly equals the total number of READY
      task candidate rows (which implies zero READY when
      ``selected_project_count == 0``);
    - no generated identity or timestamp appears here.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    decision_id: UUID
    decided_at: datetime
    portfolio_id: UUID
    selected_project_count: Annotated[StrictInt, Field(ge=0)]
    ready_task_count: Annotated[StrictInt, Field(ge=0)]
    projects: tuple[ReadyProjectTaskCandidates, ...]

    @model_validator(mode="after")
    def _validate_final_invariants(
        self,
    ) -> PortfolioProjectFocusReadyTaskCandidates:
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

        if len(task_ids) != self.ready_task_count:
            raise ValueError(
                "ready_task_count must exactly equal the total number "
                f"of READY task candidate rows (got {len(task_ids)})"
            )

        return self


# ---------------------------------------------------------------------------
# Pure ready-task candidate projection boundary.
# ---------------------------------------------------------------------------


def project_ready_task_candidates_from_current_constraint_evaluation(
    evaluation: PortfolioProjectFocusTaskCurrentConstraintEvaluation,
) -> PortfolioProjectFocusReadyTaskCandidates:
    """Project ONE genuine V1.39 CURRENT constraint/readiness
    evaluation into the deterministic READY TASK candidate projection.

    Pure and deterministic: no Portfolio argument, no repository
    argument, no persistence, no provider / AI / runtime boundary, no
    work-breakdown construction, no ranking / scoring / selection
    policy, no generated identity or timestamp, no clock read, the
    caller-owned evaluation is never mutated.

    Steps (deliberately ordered; every failure is raised BEFORE the
    output is built):

      1. require a genuine V1.39
         ``PortfolioProjectFocusTaskCurrentConstraintEvaluation``
         instance (``None``, dicts, strings, foreign models, and duck
         types are rejected);
      2. freshly strict-revalidate the COMPLETE V1.39 payload (hostile
         ``model_construct`` values or attribute tampering at top-level
         AND nested levels are rejected) and retain ONLY the validated
         copy;
      3. from this point on every semantic V1.39 read uses ONLY the
         retained validated evaluation, never the caller-owned
         original;
      4. traverse the validated V1.39 ``projects`` rows in exact tuple
         order;
      5. within each row, traverse the validated V1.39 task tuple in
         exact order and retain EXACTLY the rows whose
         ``readiness_state is FocusedTaskConstraintReadinessState.READY``
         in place — no re-evaluation of lifecycle status, constraints,
         relations, counterpart statuses, or readiness (V1.39 owns
         those semantics); no sorting, no ranking, no deduplication,
         no secondary key;
      6. retain EVERY project row, including rows whose filtered READY
         set is empty (``tasks == ()``);
      7. compute ``selected_project_count`` from the validated count
         (exactly ``len(projects)``) and ``ready_task_count`` as the
         exact total number of retained READY rows;
      8. copy the V1.39 provenance (``decision_id``, ``decided_at``,
         ``portfolio_id``) exactly;
      9. construct and return the exact immutable projection.

    Repeated identical calls are value-identical.
    """

    # -- 1. genuine V1.39 constraint/readiness evaluation ----------------
    if not isinstance(
        evaluation,
        PortfolioProjectFocusTaskCurrentConstraintEvaluation,
    ):
        raise (
            PortfolioProjectFocusReadyTaskCandidatesError(
                "a genuine V1.39 "
                "PortfolioProjectFocusTaskCurrentConstraintEvaluation "
                f"is required, got {type(evaluation).__name__}"
            )
        )

    # -- 2. freshly strict-revalidate the COMPLETE V1.39 payload ----------
    try:
        evaluation_payload: object = evaluation.model_dump(mode="python")
    except (AttributeError, TypeError, ValueError) as exc:
        raise (
            PortfolioProjectFocusReadyTaskCandidatesError(
                "the supplied V1.39 constraint/readiness evaluation is "
                "not the V1.39 shape"
            )
        ) from exc

    try:
        validated = (
            PortfolioProjectFocusTaskCurrentConstraintEvaluation.model_validate(  # noqa: E501
                evaluation_payload, strict=True
            )
        )
    except ValidationError as exc:
        raise (
            PortfolioProjectFocusReadyTaskCandidatesError(
                "the supplied V1.39 constraint/readiness evaluation "
                "failed strict re-validation"
            )
        ) from exc

    # -- 3. from here on: ONLY the retained validated V1.39 evaluation ---

    # -- 4-6. exact V1.39 project order; in-place READY filter ------------
    rows: list[ReadyProjectTaskCandidates] = []
    ready_task_total = 0

    for project_row in validated.projects:
        task_rows = tuple(
            ReadyTaskCandidate(task_id=task_row.task_id)
            for task_row in project_row.tasks
            if task_row.readiness_state
            is FocusedTaskConstraintReadinessState.READY
        )
        ready_task_total += len(task_rows)
        rows.append(
            ReadyProjectTaskCandidates(
                project_id=project_row.project_id,
                tasks=task_rows,
            )
        )

    # -- 7-9. exact counts, provenance, immutable output ------------------
    return PortfolioProjectFocusReadyTaskCandidates(
        decision_id=validated.decision_id,
        decided_at=validated.decided_at,
        portfolio_id=validated.portfolio_id,
        selected_project_count=validated.selected_project_count,
        ready_task_count=ready_task_total,
        projects=tuple(rows),
    )
