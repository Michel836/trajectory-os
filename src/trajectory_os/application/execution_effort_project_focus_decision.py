"""V1.34 — Explicit human-accepted portfolio focus decision.

V1.34 is the boundary where an EXPLICIT HUMAN choice is represented.
AI proposes; deterministic code validates; a human decides; persistence
records accepted change. V1.34 itself never recommends, ranks, optimizes,
infers, auto-selects, scores, prefers, sorts, deduplicates, or persists
anything. V1.33 remains descriptive only: it presents
alternatives. V1.34 records which one the human explicitly chose and
nothing else.

Its SOLE input authority is one genuine V1.33
``PortfolioProjectEffortFocusScenarioSet`` and one genuine V1.33
``PortfolioProjectEffortFocusScenario`` **explicitly selected by the
human**. It does not recompute V1.32 comparisons, V1.31 coverage, V1.30
summary, V1.29 selection, V1.28 ranking, V1.27 shares, project rows,
repositories, durable state, or AI/provider output. It introduces no rank,
no score, no priority, no importance / urgency / risk / impact / ROI, no
percentage, no ratio, no float, no ``Decimal``, no division, no rounding,
no normalized value, no generated decision / scenario identifier, no UUID
generation, no timestamp, and no persistence metadata of any kind.

V1.34 performs no I/O, no wall-clock or uuid generation, no randomness,
no provider / AI calls, no repository / durable reads, and writes nothing
of any kind. Inputs are never mutated, and repeated calls on the same
authoritative inputs are value-identical.

Single pure boundary:

``accept_portfolio_effort_focus_decision(scenario_set, accepted_scenario)``
requires (1) a genuine V1.33 scenario set and (2) a genuine V1.33 focus
scenario explicitly selected by the human. It accepts NO weaker selector —
no requested limit, no selected count, no index, no rank, no score — in
place of the scenario value itself. For EVERY supplied object — even on a
genuine pre-built one — it:

1. requires a genuine V1.33
   ``PortfolioProjectEffortFocusScenarioSet`` instance (``None``, dicts,
   strings, foreign models, duck types, lists, and wrong model types are
   rejected);
2. requires a genuine V1.33
   ``PortfolioProjectEffortFocusScenario`` instance for the
   human-accepted scenario (same rejections);
3. obtains a COMPLETE Python-mode payload from the supplied set and
   freshly strict-revalidates EVERY nested scenario as a
   ``PortfolioProjectEffortFocusScenario``;
4. reconstructs and freshly strict-revalidates the COMPLETE
   ``PortfolioProjectEffortFocusScenarioSet`` using those retained
   validated nested scenario objects (hostile ``model_construct`` set
   states and hostile nested scenario states are rejected);
5. separately obtains a complete Python-mode payload from the accepted
   scenario and freshly strict-revalidates it as a
   ``PortfolioProjectEffortFocusScenario`` (hostile ``model_construct``
   states with invalid field values are rejected; cross-field
   incoherence is handled by exact membership in step 6).

After that boundary, EVERY semantic read and EVERY output projection uses
ONLY the retained freshly validated objects — never the original
caller-supplied instances. The V1.33 models have NO ``to_payload()``
method, so the revalidation boundary is deliberately a careful Pydantic
strict re-validation of complete payloads. This is the fix for the class
of bug where re-validation is used merely as a check and the original
object is read afterward. The per-scenario invariants of V1.33 are
enforced by its set model; the union of steps 3 and 4 therefore rejects
any hostile nested scenario state, and V1.34 does not weaken
genuine-scenario-tuple invariants in the process.

Membership — EXACT MODEL VALUE EQUALITY only:

The freshly validated accepted scenario is compared against the scenarios
in the freshly validated scenario set by exact model value equality
(every field, including every exact ``None``). Exact matches are counted:

* **zero matches** -> ``PortfolioProjectEffortFocusDecisionError``;
* **exactly one match** -> the decision accepts that scenario;
* **more than one exact match** -> ``PortfolioProjectEffortFocusDecisionError``
  (the human choice is ambiguous with respect to this set, because V1.33
  deliberately preserves scalar-equal alternatives without deduplication;
  V1.34 invents no tie-breaking or lookup policy).

No first/last tie-breaking, no tuple-position identity, no sorting, and no
deduplication. The accepted output copies the unique matching validated
scenario EXACTLY, and the SET copy inside the freshly validated set is
authoritative for the final projection — the independently revalidated
accepted scenario is used only to PROVE membership.

Decision model — ``PortfolioProjectEffortFocusDecision`` is an immutable,
self-validating, scalar-only record:

* strict, frozen, ``extra="forbid"``; no nested V1.33 model, no generated
  UUID, no timestamp, and no persistence metadata;
* ``reference_*`` fields copy the freshly validated set scalars EXACTLY;
  ``accepted_*`` fields copy the unique matching validated scenario
  scalars EXACTLY;
* in the positive-total domain the reference and the accepted scenario
  each decompose the shared authoritative total, every accepted duration
  delta equals accepted minus reference, and the two accepted duration
  deltas conserve to exactly zero;
* in the incomplete and zero-total domains NO numeric effort duration or
  duration delta is exposed (no attempted fabrication of effort);
* every construction — including direct construction — is semantically
  coherent.
"""

from __future__ import annotations

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

from trajectory_os.application.execution_effort_project_focus_scenario_set import (
    PortfolioProjectEffortFocusScenario,
    PortfolioProjectEffortFocusScenarioSet,
)

__all__ = [
    "PortfolioProjectEffortFocusDecision",
    "PortfolioProjectEffortFocusDecisionError",
    "accept_portfolio_effort_focus_decision",
]


class PortfolioProjectEffortFocusDecisionError(ValueError):
    """Raised when the explicitly human-accepted focus scenario cannot be
    accepted against its V1.33 focus scenario set."""


# ---------------------------------------------------------------------------
# Decision model (immutable, self-validating, scalar-only).
# ---------------------------------------------------------------------------


class PortfolioProjectEffortFocusDecision(BaseModel):
    """The record of one EXPLICIT human-accepted focus scenario.

    ``portfolio_id``, ``source_project_count``, and
    ``total_duration_seconds`` mirror the shared authoritative V1.33
    scenario-set domain EXACTLY; the ``reference_*`` fields copy the
    freshly validated set reference scalars EXACTLY; the ``accepted_*``
    fields copy the unique matching freshly validated scenario scalars
    EXACTLY (``accepted_selected_project_count_delta`` and the accepted
    duration deltas are the scenario's own deltas relative to that
    reference).

    V1.34 records the human choice only. It carries no ordering, no
    recommendation, no rank, no score, no preferred / best flag, no
    importance / urgency / risk / impact / ROI, no percentage, no ratio, no
    normalized value, no generated identifier, no timestamp, and no
    persistence metadata. In the incomplete and zero-total domains every
    duration / duration-delta field is exactly ``None`` (no fabricated
    effort); in the positive-total domain they are all present, non-
    negative on both sides, each side decomposing the same authoritative
    total, and the two accepted duration deltas conserve to exactly zero.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    portfolio_id: UUID
    source_project_count: Annotated[StrictInt, Field(ge=0)]
    total_duration_seconds: Annotated[StrictInt, Field(ge=0)] | None = None

    reference_requested_limit: Annotated[StrictInt, Field(ge=1)]
    reference_selected_project_count: Annotated[StrictInt, Field(ge=0)]
    reference_selected_duration_seconds: Annotated[StrictInt, Field(ge=0)] | None = (
        None
    )
    reference_remaining_duration_seconds: (
        Annotated[StrictInt, Field(ge=0)] | None
    ) = None

    accepted_requested_limit: Annotated[StrictInt, Field(ge=1)]
    accepted_selected_project_count: Annotated[StrictInt, Field(ge=0)]
    accepted_selected_project_count_delta: StrictInt

    accepted_selected_duration_seconds: Annotated[StrictInt, Field(ge=0)] | None = (
        None
    )
    accepted_selected_duration_delta_seconds: StrictInt | None = None

    accepted_remaining_duration_seconds: Annotated[StrictInt, Field(ge=0)] | None = (
        None
    )
    accepted_remaining_duration_delta_seconds: StrictInt | None = None

    @model_validator(mode="before")
    @classmethod
    def _validate_non_bool_ints(cls, value: object) -> object:
        if isinstance(value, dict):
            int_fields = (
                "source_project_count",
                "total_duration_seconds",
                "reference_requested_limit",
                "reference_selected_project_count",
                "reference_selected_duration_seconds",
                "reference_remaining_duration_seconds",
                "accepted_requested_limit",
                "accepted_selected_project_count",
                "accepted_selected_project_count_delta",
                "accepted_selected_duration_seconds",
                "accepted_selected_duration_delta_seconds",
                "accepted_remaining_duration_seconds",
                "accepted_remaining_duration_delta_seconds",
            )
            for field in int_fields:
                if isinstance(value.get(field), bool):
                    raise ValueError(f"{field} must not be a boolean")
        return value

    @model_validator(mode="after")
    def _validate_focus_decision_invariants(
        self,
    ) -> PortfolioProjectEffortFocusDecision:
        if self.reference_selected_project_count > self.source_project_count:
            raise ValueError(
                "reference_selected_project_count may not exceed "
                "source_project_count"
            )

        if self.accepted_selected_project_count > self.source_project_count:
            raise ValueError(
                "accepted_selected_project_count may not exceed "
                "source_project_count"
            )

        if (
            self.accepted_selected_project_count_delta
            != self.accepted_selected_project_count
            - self.reference_selected_project_count
        ):
            raise ValueError(
                "accepted_selected_project_count_delta must equal "
                "accepted_selected_project_count - "
                "reference_selected_project_count"
            )

        total = self.total_duration_seconds

        # Incomplete (total is None) or zero-total (total == 0) domain:
        # NO reference or accepted side may expose a numeric effort
        # duration or duration delta — no fabricated effort.
        if total is None or total == 0:
            duration_fields = (
                self.reference_selected_duration_seconds,
                self.reference_remaining_duration_seconds,
                self.accepted_selected_duration_seconds,
                self.accepted_selected_duration_delta_seconds,
                self.accepted_remaining_duration_seconds,
                self.accepted_remaining_duration_delta_seconds,
            )
            if any(value is not None for value in duration_fields):
                state = (
                    "an incomplete decision (total_duration_seconds is None)"
                    if total is None
                    else "a zero-total decision (total_duration_seconds == 0)"
                )
                raise ValueError(
                    f"{state} may not expose any numeric effort duration "
                    "or duration delta"
                )
            return self

        # Positive-total domain: ALL duration / duration-delta fields are
        # required.
        reference_selected = self.reference_selected_duration_seconds
        reference_remaining = self.reference_remaining_duration_seconds
        accepted_selected = self.accepted_selected_duration_seconds
        accepted_selected_delta = self.accepted_selected_duration_delta_seconds
        accepted_remaining = self.accepted_remaining_duration_seconds
        accepted_remaining_delta = self.accepted_remaining_duration_delta_seconds

        if reference_selected is None:
            raise ValueError(
                "reference_selected_duration_seconds must not be None "
                "for a positive-total decision"
            )
        if reference_remaining is None:
            raise ValueError(
                "reference_remaining_duration_seconds must not be None "
                "for a positive-total decision"
            )
        if accepted_selected is None:
            raise ValueError(
                "accepted_selected_duration_seconds must not be None "
                "for a positive-total decision"
            )
        if accepted_selected_delta is None:
            raise ValueError(
                "accepted_selected_duration_delta_seconds must not be "
                "None for a positive-total decision"
            )
        if accepted_remaining is None:
            raise ValueError(
                "accepted_remaining_duration_seconds must not be None "
                "for a positive-total decision"
            )
        if accepted_remaining_delta is None:
            raise ValueError(
                "accepted_remaining_duration_delta_seconds must not be "
                "None for a positive-total decision"
            )

        if reference_selected > total:
            raise ValueError(
                "reference_selected_duration_seconds may not exceed "
                "total_duration_seconds"
            )
        if accepted_selected > total:
            raise ValueError(
                "accepted_selected_duration_seconds may not exceed "
                "total_duration_seconds"
            )
        if reference_selected + reference_remaining != total:
            raise ValueError(
                "reference_selected_duration_seconds + "
                "reference_remaining_duration_seconds must equal "
                "total_duration_seconds"
            )
        if accepted_selected + accepted_remaining != total:
            raise ValueError(
                "accepted_selected_duration_seconds + "
                "accepted_remaining_duration_seconds must equal "
                "total_duration_seconds"
            )
        if accepted_selected_delta != accepted_selected - reference_selected:
            raise ValueError(
                "accepted_selected_duration_delta_seconds must equal "
                "accepted_selected_duration_seconds - "
                "reference_selected_duration_seconds"
            )
        if accepted_remaining_delta != accepted_remaining - reference_remaining:
            raise ValueError(
                "accepted_remaining_duration_delta_seconds must equal "
                "accepted_remaining_duration_seconds - "
                "reference_remaining_duration_seconds"
            )

        # Conservation: the accepted scenario and the reference describe
        # the SAME authoritative total, so the two accepted duration
        # deltas always cancel exactly.
        if accepted_selected_delta + accepted_remaining_delta != 0:
            raise ValueError(
                "accepted_selected_duration_delta_seconds + "
                "accepted_remaining_duration_delta_seconds must equal 0 "
                "(the accepted scenario and the reference describe the "
                "same authoritative total)"
            )

        return self


# ---------------------------------------------------------------------------
# Pure acceptance boundary.
# ---------------------------------------------------------------------------


def accept_portfolio_effort_focus_decision(
    scenario_set: PortfolioProjectEffortFocusScenarioSet,
    accepted_scenario: PortfolioProjectEffortFocusScenario,
) -> PortfolioProjectEffortFocusDecision:
    """Record the EXPLICIT human choice of one scenario from a genuine
    V1.33 focus scenario set.

    ``accepted_scenario`` is the scenario EXPLICITLY selected by the
    human. No requested limit, selected count, index, rank, score, or any
    other weaker selector is accepted in its place.

    Steps:
      1. require a genuine V1.33
         ``PortfolioProjectEffortFocusScenarioSet`` (``None``, dicts,
         strings, foreign models, duck types, lists, and wrong model types
         are rejected);
      2. require a genuine V1.33
         ``PortfolioProjectEffortFocusScenario`` for the human-accepted
         scenario (same rejections);
      3. obtain a COMPLETE Python-mode payload from the supplied set and
         freshly strict-revalidate EVERY nested scenario as a
         ``PortfolioProjectEffortFocusScenario``, retaining ONLY those
         validated objects;
      4. reconstruct and freshly strict-revalidate the COMPLETE set using
         those retained validated nested scenario objects (hostile
         ``model_construct`` set states, broken invariants, and hostile
         nested scenario states are rejected);
      5. separately obtain a complete Python-mode payload from the
         accepted scenario and freshly strict-revalidate it as a
         ``PortfolioProjectEffortFocusScenario`` (hostile
         ``model_construct`` states with invalid field values are
         rejected; cross-field incoherence is handled by exact
         membership in step 6);
      6. by EXACT MODEL VALUE EQUALITY against the freshly validated set
         scenarios, count exact matches of the freshly validated accepted
         scenario: zero matches are rejected, more than one exact match is
         rejected as ambiguous, and exactly one match is accepted;
      7. project the decision using ONLY the retained freshly validated
         objects: the set provenance / reference scalars EXACTLY from the
         freshly validated set, and the accepted scenario scalars EXACTLY
         from the unique matching validated scenario in the freshly
         validated set — the independently revalidated accepted scenario
         is used only to prove membership.

    V1.34 is the human-choice boundary only: it never recommends, ranks,
    optimizes, infers, auto-selects, sorts, deduplicates, or persists. No
    I/O, no writes, no repository access, no V1.32 or earlier
    recomputation, no project rows, no classification or recommendation.
    The inputs are never mutated and repeated calls on the same
    authoritative inputs are value-identical.
    """
    if not isinstance(scenario_set, PortfolioProjectEffortFocusScenarioSet):
        raise PortfolioProjectEffortFocusDecisionError(
            "a genuine V1.33 PortfolioProjectEffortFocusScenarioSet is "
            f"required, got {type(scenario_set).__name__}"
        )

    if not isinstance(accepted_scenario, PortfolioProjectEffortFocusScenario):
        raise PortfolioProjectEffortFocusDecisionError(
            "a genuine V1.33 PortfolioProjectEffortFocusScenario is "
            f"required for the human-accepted scenario, got "
            f"{type(accepted_scenario).__name__}"
        )

    # Complete Python-mode payload of the supplied set.
    try:
        set_payload: dict[str, object] = scenario_set.model_dump(mode="python")
    except (AttributeError, TypeError, ValueError) as exc:
        raise PortfolioProjectEffortFocusDecisionError(
            "the supplied V1.33 scenario set is not the V1.33 shape"
        ) from exc
    if not isinstance(set_payload, dict):
        raise PortfolioProjectEffortFocusDecisionError(
            "the supplied V1.33 scenario set is not the V1.33 shape"
        )

    # Freshly strict-revalidate EVERY nested scenario and retain only the
    # validated objects.
    scenarios_payload: object = set_payload.get("scenarios")
    if not isinstance(scenarios_payload, tuple):
        raise PortfolioProjectEffortFocusDecisionError(
            "the supplied V1.33 scenario set does not expose an ordered "
            "genuine-scenario tuple payload"
        )
    try:
        retained_scenarios: tuple[PortfolioProjectEffortFocusScenario, ...] = tuple(
            PortfolioProjectEffortFocusScenario.model_validate(
                scenario_payload, strict=True
            )
            for scenario_payload in scenarios_payload
        )
    except ValidationError as exc:
        raise PortfolioProjectEffortFocusDecisionError(
            "a nested V1.33 focus scenario in the supplied set failed "
            "strict re-validation"
        ) from exc

    # Reconstruct and freshly strict-revalidate the COMPLETE set using
    # the retained validated nested scenario objects.
    try:
        validated_set = PortfolioProjectEffortFocusScenarioSet.model_validate(
            {**set_payload, "scenarios": retained_scenarios}, strict=True
        )
    except ValidationError as exc:
        raise PortfolioProjectEffortFocusDecisionError(
            "the supplied V1.33 scenario set failed strict re-validation"
        ) from exc

    # Separately, freshly strict-revalidate the human-accepted scenario.
    try:
        accepted_payload: dict[str, object] = accepted_scenario.model_dump(
            mode="python"
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise PortfolioProjectEffortFocusDecisionError(
            "the supplied human-accepted scenario is not the V1.33 shape"
        ) from exc

    try:
        validated_accepted = PortfolioProjectEffortFocusScenario.model_validate(
            accepted_payload, strict=True
        )
    except ValidationError as exc:
        raise PortfolioProjectEffortFocusDecisionError(
            "the supplied human-accepted scenario failed strict "
            "re-validation"
        ) from exc

    # Membership by EXACT MODEL VALUE EQUALITY. Count exact matches.
    matches = tuple(
        scenario
        for scenario in validated_set.scenarios
        if scenario == validated_accepted
    )
    if not matches:
        raise PortfolioProjectEffortFocusDecisionError(
            "the human-accepted scenario is not present in the scenario "
            "set"
        )
    if len(matches) > 1:
        raise PortfolioProjectEffortFocusDecisionError(
            "the human-accepted scenario occurs more than once in the "
            "scenario set; the human choice is ambiguous with respect to "
            "this set and must not be silently resolved"
        )

    # Projection: ONLY the retained freshly validated objects. The SET
    # copy of the unique matching scenario is authoritative.
    accepted = matches[0]
    return PortfolioProjectEffortFocusDecision(
        portfolio_id=validated_set.portfolio_id,
        source_project_count=validated_set.source_project_count,
        total_duration_seconds=validated_set.total_duration_seconds,
        reference_requested_limit=validated_set.reference_requested_limit,
        reference_selected_project_count=(
            validated_set.reference_selected_project_count
        ),
        reference_selected_duration_seconds=(
            validated_set.reference_selected_duration_seconds
        ),
        reference_remaining_duration_seconds=(
            validated_set.reference_remaining_duration_seconds
        ),
        accepted_requested_limit=accepted.requested_limit,
        accepted_selected_project_count=accepted.selected_project_count,
        accepted_selected_project_count_delta=(
            accepted.selected_project_count_delta
        ),
        accepted_selected_duration_seconds=(
            accepted.selected_duration_seconds
        ),
        accepted_selected_duration_delta_seconds=(
            accepted.selected_duration_delta_seconds
        ),
        accepted_remaining_duration_seconds=accepted.remaining_duration_seconds,
        accepted_remaining_duration_delta_seconds=(
            accepted.remaining_duration_delta_seconds
        ),
    )
