"""V1.42 — explicit human-accepted next READY TASK decision.

V1.42 is the HUMAN DECISION boundary in the V1.41 line. The architectural
principle it implements is:

    deterministic code derives
    ->
    human explicitly accepts
    ->
    later persistence records accepted change

V1.42 itself derives nothing. It merely records that a human EXPLICITLY
confirmed the specific selected (project, task) pair that V1.41 already
deterministically derived. It never recommends, ranks, scores, prefers,
sorts, substitutes, tie-breaks, executes, persists, touches the wall
clock, or generates any identity or timestamp.

Its SOLE semantic input authority is one genuine V1.41
``PortfolioProjectFocusNextReadyTaskSelection`` plus one EXPLICIT pair of
UUIDs (``accepted_project_id``, ``accepted_task_id``) confirmed by the
human. The boundary:

1. requires a genuine V1.41
   ``PortfolioProjectFocusNextReadyTaskSelection`` instance (``None``,
   dicts, strings, foreign models, and duck types are rejected);
2. freshly strict-revalidates the COMPLETE V1.41 payload and retains
   ONLY the validated copy (hostile ``model_construct`` values at
   top level are rejected);
3. from that point on, performs EVERY semantic read ONLY from the
   retained validated copy — never from the caller-owned instance;
4. rejects the V1.41 no-selection state (zero READY candidates, or a
   missing selected project / task ID): no-selection is a V1.41
   OBSERVATION, deliberately not an acceptable human execution
   decision, and no decision object may represent an accepted empty
   selection;
5. strictly validates ``accepted_project_id`` and
   ``accepted_task_id`` as real ``UUID`` values (strings, ints, bools,
   ``None``, and any other non-``UUID`` value are rejected);
6. requires the accepted pair to EQUAL the freshly validated V1.41
   selected pair EXACTLY — exact project ID AND exact task ID, both,
   together; any mismatch (wrong project, wrong task, both wrong) is
   rejected; no weaker selector (tuple position, index, rank, title,
   requested limit, task UUID without project UUID, project UUID
   without task UUID) is accepted anywhere;
7. returns one immutable, self-validating
   ``PortfolioProjectFocusNextReadyTaskDecision`` that preserves the
   V1.41 provenance (``decision_id``, ``decided_at``,
   ``portfolio_id``), the exact counts (``selected_project_count``,
   ``ready_task_count``), and the exact accepted pair.

V1.42 does NOT recompute readiness, candidate membership, or ordering;
does not choose or replace a task; does not inspect V1.40 ordering,
constraints, relations, lifecycle state, titles, descriptions,
timestamps, effort, rank, score, deadline, urgency, priority, business
value, impact, or risk; does not consult a Portfolio or rebuild the work
breakdown (no call to ``build_work_breakdown``); does not persist, write
to any repository or database, update any task status (no
ACTIVE / COMPLETED / in-progress transition), execute any action, call
any provider / AI / runtime boundary, read the wall clock, generate
UUIDs or timestamps, or mutate its inputs.

Authority:

The sole semantic source authority is ONE genuine V1.41
``PortfolioProjectFocusNextReadyTaskSelection``. No Portfolio argument
is accepted, and no other semantic authority is consulted.
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
)

from trajectory_os.application.execution_effort_project_focus_next_ready_task_selection import (  # noqa: E501
    PortfolioProjectFocusNextReadyTaskSelection,
)

__all__ = [
    "PortfolioProjectFocusNextReadyTaskDecision",
    "PortfolioProjectFocusNextReadyTaskDecisionError",
    "accept_next_ready_task_selection",
]


class PortfolioProjectFocusNextReadyTaskDecisionError(ValueError):
    """Raised when an explicit human acceptance cannot be recorded
    against a genuine V1.41 selection.

    Raised for: a payload that is not a genuine
    ``PortfolioProjectFocusNextReadyTaskSelection``, a payload that
    fails fresh strict re-validation (hostile ``model_construct``
    payloads, bad scalars), the V1.41 no-selection state (zero READY
    candidates or a missing selected identity), an argument that is not
    a real ``UUID``, or an accepted (project, task) pair that does not
    equal the freshly validated V1.41 selected pair EXACTLY.
    """


# ---------------------------------------------------------------------------
# Decision model (immutable, self-validating, scalar-only).
# ---------------------------------------------------------------------------


class PortfolioProjectFocusNextReadyTaskDecision(BaseModel):
    """The complete immutable V1.42 explicit human-accepted decision.

    ``decision_id``, ``decided_at``, ``portfolio_id``,
    ``selected_project_count``, and ``ready_task_count`` carry the
    freshly strict-revalidated V1.41 provenance and counts EXACTLY —
    no count normalization, no recomputation, no generated identity,
    no generated timestamp.

    ``accepted_project_id`` and ``accepted_task_id`` are the EXPLICIT
    human-confirmed pair; they are non-nullable and equal the V1.41
    selected pair EXACTLY. The accepted pair carries ONLY identity: no
    status, no readiness state, no constraint, no title, no
    description, no score, no rank, no priority, no urgency, no
    recommendation state, no execution state, and no generated identity
    appears here.

    Invariants (enforced): ``ready_task_count >= 1`` (a decision is
    always an accepted task, never an accepted empty selection) and
    both accepted IDs are non-nullable UUIDs.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    decision_id: UUID
    decided_at: datetime
    portfolio_id: UUID
    selected_project_count: Annotated[StrictInt, Field(ge=0)]
    ready_task_count: Annotated[StrictInt, Field(ge=1)]
    accepted_project_id: UUID
    accepted_task_id: UUID


# ---------------------------------------------------------------------------
# Pure explicit human-acceptance boundary.
# ---------------------------------------------------------------------------


def accept_next_ready_task_selection(
    selection: PortfolioProjectFocusNextReadyTaskSelection,
    accepted_project_id: UUID,
    accepted_task_id: UUID,
) -> PortfolioProjectFocusNextReadyTaskDecision:
    """Record the EXPLICIT human acceptance of ONE genuine V1.41 next
    READY TASK selection.

    ``accepted_project_id`` and ``accepted_task_id`` are the pair the
    human EXPLICITLY confirmed. No index, rank, title, tuple position,
    requested limit, task UUID without project UUID, project UUID
    without task UUID, or any other weaker selector is accepted.

    Pure and deterministic: no Portfolio argument, no repository
    argument, no persistence, no provider / AI / runtime boundary, no
    work-breakdown construction, no readiness / candidate / ordering
    recomputation, no ranking / scoring, no generated identity or
    timestamp, no clock read, the caller-owned selection is never
    mutated.

    Steps (deliberately ordered; every failure is raised BEFORE the
    decision is built):

      1. require a genuine V1.41
         ``PortfolioProjectFocusNextReadyTaskSelection`` instance
         (``None``, dicts, strings, foreign models, and duck types are
         rejected);
      2. freshly strict-revalidate the COMPLETE V1.41 payload (hostile
         ``model_construct`` values are rejected) and retain ONLY the
         validated copy;
      3. from this point on every semantic read uses ONLY the retained
         validated selection, never the caller-owned original;
      4. reject the V1.41 no-selection state (``ready_task_count == 0``
         or a missing selected project / task ID) — no decision
         object may represent an accepted empty selection;
      5. strictly validate ``accepted_project_id`` and
         ``accepted_task_id`` as genuine ``UUID`` values;
      6. require the accepted pair to equal the validated
         ``selected_project_id`` / ``selected_task_id`` EXACTLY (both
         at once); any mismatch is rejected;
      7. construct and return the exact immutable accepted decision
         with exact upstream provenance, exact counts, and the exact
         accepted pair.

    Repeated identical calls are value-identical.
    """

    # -- 1. genuine V1.41 next READY TASK selection -----------------------
    if not isinstance(
        selection, PortfolioProjectFocusNextReadyTaskSelection
    ):
        raise (
            PortfolioProjectFocusNextReadyTaskDecisionError(
                "a genuine V1.41 "
                "PortfolioProjectFocusNextReadyTaskSelection is "
                f"required, got {type(selection).__name__}"
            )
        )

    # -- 2. freshly strict-revalidate the COMPLETE V1.41 payload ----------
    try:
        selection_payload: object = selection.model_dump(mode="python")
    except (AttributeError, TypeError, ValueError) as exc:
        raise (
            PortfolioProjectFocusNextReadyTaskDecisionError(
                "the supplied V1.41 next READY TASK selection is not "
                "the V1.41 shape"
            )
        ) from exc

    try:
        validated = (
            PortfolioProjectFocusNextReadyTaskSelection.model_validate(
                selection_payload, strict=True
            )
        )
    except ValidationError as exc:
        raise (
            PortfolioProjectFocusNextReadyTaskDecisionError(
                "the supplied V1.41 next READY TASK selection failed "
                "strict re-validation"
            )
        ) from exc

    # -- 3. from here on: ONLY the retained validated V1.41 selection -----

    # -- 4. the V1.41 no-selection state is deliberately non-acceptable ----
    if (
        validated.ready_task_count == 0
        or validated.selected_project_id is None
        or validated.selected_task_id is None
    ):
        raise (
            PortfolioProjectFocusNextReadyTaskDecisionError(
                "the V1.41 selection carries no READY task "
                "(no-selection is an observation, not an acceptable "
                "human decision)"
            )
        )

    # -- 5. strictly validate both explicit accepted UUID arguments -------
    if not isinstance(accepted_project_id, UUID):
        raise (
            PortfolioProjectFocusNextReadyTaskDecisionError(
                "accepted_project_id must be a genuine UUID explicitly "
                "confirmed by the human, "
                f"got {type(accepted_project_id).__name__}"
            )
        )

    if not isinstance(accepted_task_id, UUID):
        raise (
            PortfolioProjectFocusNextReadyTaskDecisionError(
                "accepted_task_id must be a genuine UUID explicitly "
                "confirmed by the human, "
                f"got {type(accepted_task_id).__name__}"
            )
        )

    # -- 6. exact equality with the validated V1.41 selected pair ---------
    if accepted_project_id != validated.selected_project_id:
        raise (
            PortfolioProjectFocusNextReadyTaskDecisionError(
                "accepted_project_id does not match the V1.41 selected "
                "project"
            )
        )

    if accepted_task_id != validated.selected_task_id:
        raise (
            PortfolioProjectFocusNextReadyTaskDecisionError(
                "accepted_task_id does not match the V1.41 selected task"
            )
        )

    # -- 7. exact provenance, exact counts, immutable decision ------------
    return PortfolioProjectFocusNextReadyTaskDecision(
        decision_id=validated.decision_id,
        decided_at=validated.decided_at,
        portfolio_id=validated.portfolio_id,
        selected_project_count=validated.selected_project_count,
        ready_task_count=validated.ready_task_count,
        accepted_project_id=accepted_project_id,
        accepted_task_id=accepted_task_id,
    )
