"""V1.36 — Exact binding of a durable accepted focus decision to one
identity-bearing V1.29 project selection.

V1.34/V1.35 are deliberately **scalar-only**: the durable accepted focus
decision names a scalar focus (requested limit, selected count, selected /
remaining / total durations) but carries NO selected project identities.
V1.29 still carries the exact selected project IDs and the authoritative
tuple order. V1.36 bridges the two: it deterministically binds ONE genuine
durable V1.35
``PortfolioProjectEffortFocusDecisionRecord`` and ONE genuine
identity-bearing V1.29 ``PortfolioProjectEffortTopSelection`` — but ONLY
when they are exactly compatible.

Authority:

- the V1.29 identity-bearing selection supplies the exact selected project
  IDs and their authoritative order;
- the V1.30 summary
  (``summarize_selected_portfolio_project_effort``) is the authoritative
  scalar bridge from the V1.29 selection; V1.36 REUSES that boundary and
  does NOT duplicate any V1.30 selected/remaining arithmetic;
- the durable V1.35 record (and its nested V1.34 decision) supplies the
  ACCEPTED scalar focus.

Single pure boundary:

``bind_durable_portfolio_effort_focus_decision(durable_decision, selection)``
requires (1) a genuine V1.35
``PortfolioProjectEffortFocusDecisionRecord`` and (2) a genuine V1.29
``PortfolioProjectEffortTopSelection``. It takes NO repository, performs
NO persistence, does NO I/O, generates NO UUID, reads NO clock, calls NO
repository / provider / AI / runtime boundary, and mutates nothing.

Steps (every failure raised BEFORE any output is built):

1. require a genuine V1.35
   ``PortfolioProjectEffortFocusDecisionRecord`` instance (``None``,
   dicts, strings, foreign models, duck types, lists, and wrong model
   types are rejected);
2. freshly strict-revalidate the COMPLETE V1.35 record (including its
   nested V1.34 decision) and retain ONLY the validated copy;
3. require a genuine V1.29 ``PortfolioProjectEffortTopSelection``
   instance (same rejections);
4. freshly strict-revalidate the COMPLETE V1.29 payload — including every
   nested ``PortfolioProjectEffortRank`` row and its nested exact share —
   and retain ONLY the validated copy;
5. call the existing V1.30 boundary
   ``summarize_selected_portfolio_project_effort(validated_selection)``
   and retain the returned authoritative V1.30 summary;
6. compare the V1.30 summary EXACTLY (including exact ``None``
   availability) against the ACCEPTED side of the retained V1.35 decision;
   ANY mismatch is rejected;
7. project the immutable V1.36 binding using ONLY the retained validated
   objects and the authoritative V1.30 summary — never the original
   caller-supplied instances.

V1.36 proves EXACT SEMANTIC COMPATIBILITY between the explicitly supplied
V1.29 selection and the durable V1.35 accepted decision. It MUST NOT claim
that this exact object, or the historical V1.29 instance, was the original
upstream object used when the V1.34/V1.35 decision was created: that
historical pointer was never persisted and is never invented.
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

from trajectory_os.application.execution_effort_project_focus_decision_persistence import (
    PortfolioProjectEffortFocusDecisionRecord,
)
from trajectory_os.application.execution_effort_project_selection_summary import (
    summarize_selected_portfolio_project_effort,
)
from trajectory_os.application.execution_effort_project_top_selection import (
    PortfolioProjectEffortTopSelection,
)

__all__ = [
    "PortfolioProjectEffortFocusBinding",
    "PortfolioProjectEffortFocusBindingError",
    "bind_durable_portfolio_effort_focus_decision",
]


class PortfolioProjectEffortFocusBindingError(ValueError):
    """Raised when a durable focus decision and a V1.29 selection cannot be
    bound to an exact V1.36 identity-bearing focus binding.

    Raised for: a ``durable_decision`` that is not a genuine V1.35
    ``PortfolioProjectEffortFocusDecisionRecord``, a ``selection`` that is
    not a genuine V1.29 ``PortfolioProjectEffortTopSelection``, a record or
    selection that fails fresh strict re-validation (hostile
    ``model_construct()`` payloads, broken nested rows / exact shares), or
    ANY exact compatibility mismatch between the authoritative V1.30 summary
    and the ACCEPTED side of the durable decision.
    """


# ---------------------------------------------------------------------------
# Binding model (immutable, self-validating).
# ---------------------------------------------------------------------------


class PortfolioProjectEffortFocusBinding(BaseModel):
    """An exact, non-prescriptive binding between a durable accepted focus
    decision (V1.35) and one identity-bearing V1.29 project selection.

    Fields mirror the durable decision provenance (``decision_id``,
    ``decided_at``), the authoritative V1.30 scalar summary
    (``portfolio_id``, the counts, and the three duration totals including
    exact ``None`` availability), and the V1.29 ACCEPTED selected identity
    projection ``selected_project_ids`` (exact V1.29 tuple order).

    The model is strict, frozen, ``extra="forbid"``, and carries no nested
    Portfolio, no nested V1.29 selection, no generated binding UUID, and no
    generated timestamp. ``selected_project_ids`` is projected EXACTLY from
    the freshly validated V1.29 selection tuple — no sorting, no
    deduplication, no rank-based reordering, no UUID ordering.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    decision_id: UUID
    decided_at: datetime
    portfolio_id: UUID
    accepted_requested_limit: Annotated[StrictInt, Field(ge=1)]
    source_project_count: Annotated[StrictInt, Field(ge=0)]
    selected_project_count: Annotated[StrictInt, Field(ge=0)]
    total_duration_seconds: Annotated[StrictInt, Field(ge=0)] | None = None
    selected_duration_seconds: Annotated[StrictInt, Field(ge=0)] | None = None
    remaining_duration_seconds: (
        Annotated[StrictInt, Field(ge=0)] | None
    ) = None
    selected_project_ids: tuple[UUID, ...]

    @model_validator(mode="before")
    @classmethod
    def _validate_non_bool_ints(cls, value: object) -> object:
        if isinstance(value, dict):
            for field in (
                "accepted_requested_limit",
                "source_project_count",
                "selected_project_count",
                "total_duration_seconds",
                "selected_duration_seconds",
                "remaining_duration_seconds",
            ):
                if isinstance(value.get(field), bool):
                    raise ValueError(f"{field} must not be a boolean")
        return value

    @model_validator(mode="after")
    def _validate_binding_invariants(
        self,
    ) -> PortfolioProjectEffortFocusBinding:
        if self.selected_project_count != len(self.selected_project_ids):
            raise ValueError(
                "selected_project_count must equal the length of "
                f"selected_project_ids (got {len(self.selected_project_ids)})"
            )

        if len(self.selected_project_ids) != len(set(self.selected_project_ids)):
            raise ValueError("selected_project_ids must be unique")

        if self.selected_project_count > self.source_project_count:
            raise ValueError(
                "selected_project_count may not exceed source_project_count"
            )

        # The three duration totals must have EXACT None availability in
        # lockstep: either all None (incomplete state) or all present
        # (complete state). No fabricated mixing of None and values.
        durations = (
            self.total_duration_seconds,
            self.selected_duration_seconds,
            self.remaining_duration_seconds,
        )
        if any(value is None for value in durations) and not all(
            value is None for value in durations
        ):
            raise ValueError(
                "total/selected/remaining duration seconds must be either "
                "all None (incomplete) or all present (complete); a "
                "partial mix is not a coherent focus binding"
            )

        return self


# ---------------------------------------------------------------------------
# Pure binding boundary.
# ---------------------------------------------------------------------------


def bind_durable_portfolio_effort_focus_decision(
    durable_decision: PortfolioProjectEffortFocusDecisionRecord,
    selection: PortfolioProjectEffortTopSelection,
) -> PortfolioProjectEffortFocusBinding:
    """Bind a genuine durable V1.35 accepted focus decision to ONE genuine
    identity-bearing V1.29 project selection.

    ``durable_decision`` is a genuine V1.35
    ``PortfolioProjectEffortFocusDecisionRecord`` and ``selection`` is a
    genuine V1.29 ``PortfolioProjectEffortTopSelection``. No project,
    task, or work unit is chosen, ranked, scored, preferred, inferred, or
    reconstructed: V1.36 only proves EXACT semantic compatibility between
    the two explicitly supplied objects and projects the exact selected
    identity/order that V1.29 already carries.

    Steps (every failure is raised BEFORE any output is built):

      1. require a genuine V1.35
         ``PortfolioProjectEffortFocusDecisionRecord`` (``None``, dicts,
         strings, foreign models, duck types, lists, and wrong model types
         are rejected);
      2. freshly strict-revalidate the COMPLETE V1.35 record (including its
         nested V1.34 decision) and retain ONLY the validated copy;
      3. require a genuine V1.29
         ``PortfolioProjectEffortTopSelection`` (same rejections);
      4. freshly strict-revalidate the COMPLETE V1.29 payload — including
         every nested ``PortfolioProjectEffortRank`` row and its nested
         exact share — and retain ONLY the validated copy;
      5. call the existing V1.30 boundary
         ``summarize_selected_portfolio_project_effort(validated_selection)``
         and retain the returned authoritative V1.30 summary;
      6. compare the V1.30 summary EXACTLY (including exact ``None``
         availability) against the ACCEPTED side of the retained V1.35
         decision — ANY mismatch is rejected; no tolerance, no nearest
         match, no fallback, no alternative lookup;
      7. project the immutable V1.36 binding using ONLY the retained
         validated objects and the authoritative V1.30 summary.

    No repository is touched, nothing is persisted, no UUID is generated,
    no clock is read, no provider / AI / runtime boundary is called, and
    the inputs are never mutated. Repeated identical calls are
    value-identical.
    """
    # -- 1. genuine V1.35 durable decision record -------------------------
    if not isinstance(durable_decision, PortfolioProjectEffortFocusDecisionRecord):
        raise PortfolioProjectEffortFocusBindingError(
            "a genuine V1.35 PortfolioProjectEffortFocusDecisionRecord is "
            f"required, got {type(durable_decision).__name__}"
        )

    # -- 2. freshly strict-revalidate the COMPLETE V1.35 record -----------
    try:
        record_payload: object = durable_decision.model_dump(mode="python")
    except (AttributeError, TypeError, ValueError) as exc:
        raise PortfolioProjectEffortFocusBindingError(
            "the supplied durable focus decision is not the V1.35 shape"
        ) from exc

    try:
        validated_record = PortfolioProjectEffortFocusDecisionRecord.model_validate(
            record_payload, strict=True
        )
    except ValidationError as exc:
        raise PortfolioProjectEffortFocusBindingError(
            "the supplied durable focus decision failed strict "
            "re-validation"
        ) from exc

    # -- 3. genuine V1.29 identity-bearing selection ----------------------
    if not isinstance(selection, PortfolioProjectEffortTopSelection):
        raise PortfolioProjectEffortFocusBindingError(
            "a genuine V1.29 PortfolioProjectEffortTopSelection is "
            f"required, got {type(selection).__name__}"
        )

    # -- 4. freshly strict-revalidate the COMPLETE V1.29 selection --------
    try:
        selection_payload: object = selection.model_dump(mode="python")
    except (AttributeError, TypeError, ValueError) as exc:
        raise PortfolioProjectEffortFocusBindingError(
            "the supplied V1.29 selection is not the V1.29 shape"
        ) from exc

    try:
        validated_selection = PortfolioProjectEffortTopSelection.model_validate(
            selection_payload, strict=True
        )
    except ValidationError as exc:
        raise PortfolioProjectEffortFocusBindingError(
            "the supplied V1.29 selection failed strict re-validation "
            "(including nested project rows and exact shares)"
        ) from exc

    # -- 5. authoritative V1.30 scalar bridge (REUSE, never duplicate) ----
    summary = summarize_selected_portfolio_project_effort(validated_selection)

    # -- 6. EXACT compatibility against the ACCEPTED side of the decision -
    decision = validated_record.decision

    if summary.portfolio_id != decision.portfolio_id:
        raise PortfolioProjectEffortFocusBindingError(
            "the V1.30 portfolio_id does not exactly match the durable "
            "decision portfolio_id"
        )

    if summary.source_project_count != decision.source_project_count:
        raise PortfolioProjectEffortFocusBindingError(
            "the V1.30 source_project_count does not exactly match the "
            "durable decision source_project_count"
        )

    if summary.requested_limit != decision.accepted_requested_limit:
        raise PortfolioProjectEffortFocusBindingError(
            "the V1.30 requested_limit does not exactly match the durable "
            "decision accepted_requested_limit"
        )

    if summary.selected_project_count != decision.accepted_selected_project_count:
        raise PortfolioProjectEffortFocusBindingError(
            "the V1.30 selected_project_count does not exactly match the "
            "durable decision accepted_selected_project_count"
        )

    if summary.total_duration_seconds != decision.total_duration_seconds:
        raise PortfolioProjectEffortFocusBindingError(
            "the V1.30 total_duration_seconds does not exactly match the "
            "durable decision total_duration_seconds (including exact None "
            "availability)"
        )

    if summary.selected_duration_seconds != (
        decision.accepted_selected_duration_seconds
    ):
        raise PortfolioProjectEffortFocusBindingError(
            "the V1.30 selected_duration_seconds does not exactly match "
            "the durable decision accepted_selected_duration_seconds "
            "(including exact None)"
        )

    if summary.remaining_duration_seconds != (
        decision.accepted_remaining_duration_seconds
    ):
        raise PortfolioProjectEffortFocusBindingError(
            "the V1.30 remaining_duration_seconds does not exactly match "
            "the durable decision accepted_remaining_duration_seconds "
            "(including exact None)"
        )

    # -- 7. project the binding from ONLY validated objects + V1.30 -------
    selected_project_ids = tuple(
        project.project_id for project in validated_selection.projects
    )

    return PortfolioProjectEffortFocusBinding(
        decision_id=validated_record.decision_id,
        decided_at=validated_record.decided_at,
        portfolio_id=summary.portfolio_id,
        accepted_requested_limit=summary.requested_limit,
        source_project_count=summary.source_project_count,
        selected_project_count=summary.selected_project_count,
        total_duration_seconds=summary.total_duration_seconds,
        selected_duration_seconds=summary.selected_duration_seconds,
        remaining_duration_seconds=summary.remaining_duration_seconds,
        selected_project_ids=selected_project_ids,
    )
