"""V1.41 — deterministic first READY TASK selection.

Projects ONE genuine, freshly strict-revalidated V1.40
``PortfolioProjectFocusReadyTaskCandidates`` into EXACTLY ONE deterministic
selection: the FIRST READY task candidate encountered when traversing the
V1.40 project rows in exact tuple order and, within each project, the task
candidates in exact tuple order.

V1.41 is a PURE PROJECTION ONLY. It is a deterministic, explainable,
policy-bounded, immutable application boundary:

- deterministic and pure: identical validated input yields identical
  output; repeated calls are value-identical;
- explainable: the selection is exactly the first candidate in canonical
  V1.40 order — no inference anywhere;
- immutable: all output models are strict / frozen / extra-forbid;
- policy-bounded: the ONLY selection policy is "first candidate in
  canonical V1.40 order" — nothing else is interpreted;
- no side effects: no persistence, no provider / AI / runtime boundary,
  no work-breakdown construction, no wall clock, no generated identity
  or timestamp.

The selected task is NOT claimed to be best, highest priority, urgent,
important, valuable, recommended, shortest, newest, or strategically
preferred. It is simply the FIRST candidate in canonical V1.40 order.

V1.41 does NOT:

- recommend, rank, score, sort, or prioritize any task or project;
- use effort duration as any selection policy;
- inspect titles, descriptions, timestamps, statuses, constraints, or
  relation details (V1.40 already owns those semantics EXACTLY);
- consult a Portfolio or rebuild the work breakdown (no call to
  ``build_work_breakdown``);
- persist, or read the wall clock, or generate UUIDs / timestamps;
- mutate the caller-owned V1.40 projection.

Any richer selection policy belongs to a later milestone and is
explicitly OUT OF SCOPE here.

Authority:

The sole semantic input authority is ONE genuine V1.40
``PortfolioProjectFocusReadyTaskCandidates``. The boundary requires that
genuine instance, freshly strict-revalidates the COMPLETE payload,
retains only the validated copy, and from that point performs every
semantic read ONLY from the retained validated copy. NO Portfolio
argument is accepted or required and no other semantic authority is
consulted.

Exact selection policy

The selection is EXACTLY:

1. V1.40 project row tuple order, traversed in exact order;
2. within each project row, the V1.40 task candidate tuple order,
   traversed in exact order;
3. the FIRST task candidate encountered is selected;
4. traversal stops immediately after that first candidate.

No sorting, no ranking, no scoring, no reordering, no deduplication, no
inspection of UUID value ordering, no secondary key of any kind.

If no candidate exists globally (zero selected projects, or every
project row has an empty task tuple), the selection is the EXACT no
selection state: ``selected_project_id is None`` and
``selected_task_id is None``.
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

from trajectory_os.application.execution_effort_project_focus_ready_task_candidates import (  # noqa: E501
    PortfolioProjectFocusReadyTaskCandidates,
)

__all__ = [
    "PortfolioProjectFocusNextReadyTaskSelection",
    "PortfolioProjectFocusNextReadyTaskSelectionError",
    "select_first_ready_task_candidate",
]


class PortfolioProjectFocusNextReadyTaskSelectionError(ValueError):
    """Raised when a genuine V1.40 projection cannot be selected from.

    Raised for: a payload that is not a genuine
    ``PortfolioProjectFocusReadyTaskCandidates``, or a payload that
    fails fresh strict re-validation (hostile ``model_construct``
    payloads, bad scalars, malformed nested project/task payloads,
    inconsistent invariant-carrying fields).
    """


# ---------------------------------------------------------------------------
# Output model (immutable, self-validating).
# ---------------------------------------------------------------------------


class PortfolioProjectFocusNextReadyTaskSelection(BaseModel):
    """The complete immutable V1.41 first READY TASK selection.

    ``decision_id``, ``decided_at``, and ``portfolio_id`` carry the
    freshly strict-revalidated V1.40 provenance EXACTLY;
    ``selected_project_count`` and ``ready_task_count`` are preserved
    from the validated V1.40 payload EXACTLY (no count normalization,
    no recomputation on the output side).

    ``selected_project_id`` and ``selected_task_id`` identify EXACTLY
    the first candidate in canonical V1.40 order, or are BOTH ``None``
    when no candidate exists globally. The selected pair carries ONLY
    identity: no status, no readiness state, no constraint, no title,
    no description, no score, no rank, no priority, no urgency, no
    recommendation state, no timestamp, and no generated identity
    appears here.

    Invariants (enforced):

    - ``selected_project_id`` and ``selected_task_id`` are BOTH ``None``
      or BOTH non-``None``;
    - ``ready_task_count == 0`` requires BOTH selected IDs to be
      ``None``;
    - ``ready_task_count > 0`` requires BOTH selected IDs to be
      non-``None``;
    - no generated identity or timestamp appears here.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    decision_id: UUID
    decided_at: datetime
    portfolio_id: UUID
    selected_project_count: Annotated[StrictInt, Field(ge=0)]
    ready_task_count: Annotated[StrictInt, Field(ge=0)]
    selected_project_id: UUID | None
    selected_task_id: UUID | None

    @model_validator(mode="after")
    def _validate_selection_invariants(
        self,
    ) -> PortfolioProjectFocusNextReadyTaskSelection:
        if (self.selected_project_id is None) != (
            self.selected_task_id is None
        ):
            raise ValueError(
                "selected_project_id and selected_task_id must both be "
                "None or both be non-None"
            )

        if self.ready_task_count == 0 and self.selected_project_id is not None:
            raise ValueError(
                "ready_task_count == 0 requires no selection "
                "(both selected IDs must be None)"
            )

        if self.ready_task_count > 0 and self.selected_project_id is None:
            raise ValueError(
                "ready_task_count > 0 requires a selection "
                "(both selected IDs must be non-None)"
            )

        return self


# ---------------------------------------------------------------------------
# Pure first READY TASK selection boundary.
# ---------------------------------------------------------------------------


def select_first_ready_task_candidate(
    candidates: PortfolioProjectFocusReadyTaskCandidates,
) -> PortfolioProjectFocusNextReadyTaskSelection:
    """Select the FIRST READY task candidate from ONE genuine V1.40
    READY TASK candidate projection.

    Pure and deterministic: no Portfolio argument, no repository
    argument, no persistence, no provider / AI / runtime boundary, no
    work-breakdown construction, no ranking / scoring / selection policy
    beyond the exact first-candidate rule, no generated identity or
    timestamp, no clock read, the caller-owned projection is never
    mutated.

    Steps (deliberately ordered; every failure is raised BEFORE the
    output is built):

      1. require a genuine V1.40
         ``PortfolioProjectFocusReadyTaskCandidates`` instance
         (``None``, dicts, strings, foreign models, and duck types are
         rejected);
      2. freshly strict-revalidate the COMPLETE V1.40 payload (hostile
         ``model_construct`` values or attribute tampering at top-level
         AND nested levels are rejected) and retain ONLY the validated
         copy;
      3. from this point on every semantic V1.40 read uses ONLY the
         retained validated projection, never the caller-owned
         original;
      4. traverse the validated V1.40 ``projects`` rows in exact tuple
         order;
      5. within each row, traverse the validated task candidate tuple
         in exact order and select the FIRST candidate encountered;
      6. stop immediately after that first candidate — no sorting, no
         ranking, no deduplication, no secondary key, no UUID value
         inspection;
      7. if no candidate exists globally, select the exact no selection
         state (both selected IDs ``None``);
      8. copy the V1.40 provenance (``decision_id``, ``decided_at``,
         ``portfolio_id``) and counts (``selected_project_count``,
         ``ready_task_count``) EXACTLY;
      9. construct and return the exact immutable selection.

    Repeated identical calls are value-identical.
    """

    # -- 1. genuine V1.40 ready-task candidate projection ------------------
    if not isinstance(
        candidates, PortfolioProjectFocusReadyTaskCandidates
    ):
        raise (
            PortfolioProjectFocusNextReadyTaskSelectionError(
                "a genuine V1.40 "
                "PortfolioProjectFocusReadyTaskCandidates is required, "
                f"got {type(candidates).__name__}"
            )
        )

    # -- 2. freshly strict-revalidate the COMPLETE V1.40 payload -----------
    try:
        candidates_payload: object = candidates.model_dump(mode="python")
    except (AttributeError, TypeError, ValueError) as exc:
        raise (
            PortfolioProjectFocusNextReadyTaskSelectionError(
                "the supplied V1.40 ready-task candidate projection is "
                "not the V1.40 shape"
            )
        ) from exc

    try:
        validated = (
            PortfolioProjectFocusReadyTaskCandidates.model_validate(
                candidates_payload, strict=True
            )
        )
    except ValidationError as exc:
        raise (
            PortfolioProjectFocusNextReadyTaskSelectionError(
                "the supplied V1.40 ready-task candidate projection "
                "failed strict re-validation"
            )
        ) from exc

    # -- 3. from here on: ONLY the retained validated V1.40 projection ----

    # -- 4-6. exact V1.40 project-major / task-major first candidate ------
    selected_project_id: UUID | None = None
    selected_task_id: UUID | None = None

    for project_row in validated.projects:
        if len(project_row.tasks) == 0:
            continue
        first_task = project_row.tasks[0]
        selected_project_id = project_row.project_id
        selected_task_id = first_task.task_id
        break  # stop immediately after the first candidate

    # -- 7 no candidate: both selected IDs remain None ---------------------

    # -- 8-9. exact provenance, exact counts, immutable output -------------
    return PortfolioProjectFocusNextReadyTaskSelection(
        decision_id=validated.decision_id,
        decided_at=validated.decided_at,
        portfolio_id=validated.portfolio_id,
        selected_project_count=validated.selected_project_count,
        ready_task_count=validated.ready_task_count,
        selected_project_id=selected_project_id,
        selected_task_id=selected_task_id,
    )
