"""V1.33 — Deterministic focus-scenario set from authoritative V1.32
comparisons.

V1.33 is *descriptive decision support only*, not recommendation. AI
proposes; deterministic code validates; a human decides; persistence
records accepted change. V1.33 itself never chooses, recommends, ranks,
optimizes, prefers, scores, or persists a focus decision. Its SOLE input
authority is a NON-EMPTY ORDERED collection of genuine V1.32
``PortfolioProjectEffortSelectionComparison`` values. It groups those
comparisons — the ones sharing the same portfolio/domain and the same
scalar LEFT/reference focus — into one immutable ordered focus scenario
set.

It does not read, import, or recompute V1.31 coverage, V1.30 summary,
V1.29 selection, V1.28 ranking, V1.27 shares, project rows, repositories,
or persistence. It introduces no "best" / "worst" / "recommended" label,
no rank, no score, no priority, no importance / urgency / risk / impact /
ROI, no Pareto / 80-20 policy, no threshold, no selection recommendation,
no percentages, no ratios, no floats, no ``Decimal``, no division, no
floor division, no rounding, no fraction reduction, and no normalized
score.

V1.33 performs no I/O, no wall-clock or uuid generation, no randomness,
no provider / AI calls, no repository / durable reads, and writes nothing
of any kind. Inputs are never mutated, and repeated calls on the same
inputs are value-identical.

Caller order is preserved EXACTLY: no sorting, no ranking, no
normalization, no top-N, no deduplication, no effort-based or
delta-magnitude reordering. Scalar-equal alternatives remain present as
separate scenarios.

Single pure boundary:

``build_portfolio_effort_focus_scenario_set(comparisons)`` requires a
NON-EMPTY *tuple* of genuine V1.32
``PortfolioProjectEffortSelectionComparison`` values (``None``, dicts,
strings, foreign models, duck types, lists, and malformed elements are
rejected). For EVERY element — even on a genuine pre-built object — it:

1. requires a genuine ``PortfolioProjectEffortSelectionComparison``
   instance;
2. calls ``comparison.to_payload()``;
3. freshly strict-revalidates with
   ``PortfolioProjectEffortSelectionComparison.model_validate(payload,
   strict=True)``;
4. retains the returned validated object;
5. uses ONLY the validated object for every later semantic read and
   output projection (never the original input object).

This is the deliberate fix for the V1.32 class of bug where re-validation
was used merely as a check and the original object was read afterward.

Common reference — the revalidated comparisons must have exact equality
of: ``portfolio_id``, ``source_project_count``,
``total_duration_seconds`` (including exact availability),
``left_requested_limit``, ``left_selected_project_count``,
``left_selected_duration_seconds`` (including ``None``), and
``left_remaining_duration_seconds`` (including ``None``). V1.33 proves
scalar reference equivalence only; it never infers a common reference
from matching deltas and never uses project rows to prove stronger
identity.

Result semantics (exact integer copying from the V1.32 RIGHT side and
delta fields only — no project rows, no percentages, no scores, no rank,
no generated scenario UUID):

* **Positive-total domain** — reference and scenario effort fields are
  exact integers copied from V1.32; the reference decomposes the total
  (``reference_selected + reference_remaining == total``), every scenario
  decomposes the total, every delta exactly equals scenario minus
  reference, and the two duration deltas conserve to exactly zero.
* **Incomplete domain** (``total_duration_seconds is None``) and
  **zero-total domain** (``total_duration_seconds == 0``) — no effort
  amount or delta is fabricated; all reference and scenario duration /
  duration-delta fields are exactly ``None``.

The output models are self-validating (strict, frozen, ``extra="forbid"``,
before/after validator layers) so a
``PortfolioProjectEffortFocusScenario`` or a
``PortfolioProjectEffortFocusScenarioSet`` carries a semantically
coherent state on every construction — including direct construction.
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

from trajectory_os.application.execution_effort_project_selection_comparison import (
    PortfolioProjectEffortSelectionComparison,
)

__all__ = [
    "PortfolioProjectEffortFocusScenario",
    "PortfolioProjectEffortFocusScenarioSet",
    "PortfolioProjectEffortFocusScenarioSetError",
    "build_portfolio_effort_focus_scenario_set",
]


class PortfolioProjectEffortFocusScenarioSetError(ValueError):
    """Raised when a collection of V1.32 comparisons does not form one
    coherent, same-reference focus-scenario set."""


# ---------------------------------------------------------------------------
# Scenario model (immutable, self-validating).
# ---------------------------------------------------------------------------


class PortfolioProjectEffortFocusScenario(BaseModel):
    """One alternative focus selection, projected from ONE V1.32
    comparison.

    ``requested_limit`` and ``selected_project_count`` copy the V1.32
    RIGHT side unchanged; the ``*_delta`` fields copy the V1.32 ``right -
    left`` deltas unchanged (i.e. relative to the common LEFT/reference
    focus). In the incomplete and zero-total domains every duration and
    duration-delta field is exactly ``None`` (no fabricated effort); in
    the positive-total domain they are all exact non-negative integers and
    jointly decompose the shared authoritative total with conserving
    deltas.

    V1.33 carries no ordering between scenarios, no recommendation, no
    score, no rank, no generated identifier, and no project rows.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    requested_limit: Annotated[StrictInt, Field(ge=1)]
    selected_project_count: Annotated[StrictInt, Field(ge=0)]
    selected_project_count_delta: StrictInt

    selected_duration_seconds: Annotated[StrictInt, Field(ge=0)] | None = None
    selected_duration_delta_seconds: StrictInt | None = None

    remaining_duration_seconds: Annotated[StrictInt, Field(ge=0)] | None = None
    remaining_duration_delta_seconds: StrictInt | None = None

    @model_validator(mode="before")
    @classmethod
    def _validate_non_bool_ints(cls, value: object) -> object:
        if isinstance(value, dict):
            int_fields = (
                "requested_limit",
                "selected_project_count",
                "selected_project_count_delta",
                "selected_duration_seconds",
                "selected_duration_delta_seconds",
                "remaining_duration_seconds",
                "remaining_duration_delta_seconds",
            )
            for field in int_fields:
                if isinstance(value.get(field), bool):
                    raise ValueError(f"{field} must not be a boolean")
        return value


# ---------------------------------------------------------------------------
# Scenario-set model (immutable, self-validating).
# ---------------------------------------------------------------------------


class PortfolioProjectEffortFocusScenarioSet(BaseModel):
    """An immutable ordered set of focus scenarios sharing ONE common
    reference focus.

    ``portfolio_id``, ``source_project_count``, and
    ``total_duration_seconds`` mirror the shared authoritative V1.32
    comparison domain EXACTLY, and the ``reference_*`` fields copy the
    common LEFT/reference focus exactly. ``scenarios`` preserves caller
    input order EXACTLY (never sorted, never deduplicated).

    Set-level invariants (enforced on every construction, including
    direct construction):

    * ``scenarios`` is a non-empty tuple of genuine
      ``PortfolioProjectEffortFocusScenario`` instances;
    * the reference selected count does not exceed the source count, and
      no scenario selected count exceeds the source count;
    * in the positive-total domain the reference decomposes the total and
      every scenario decomposes the total;
    * every ``selected_project_count_delta`` equals the scenario selected
      count minus the reference selected count;
    * in the positive-total domain every duration delta equals the
      scenario duration minus the reference duration, and
      ``selected_duration_delta + remaining_duration_delta == 0``
      (conservation on every scenario);
    * in the incomplete and zero-total domains NO reference or scenario
      exposes a numeric effort duration or duration delta.

    V1.33 groups and reports only. It never chooses, ranks, recommends,
    or scores a scenario.
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

    scenarios: tuple[PortfolioProjectEffortFocusScenario, ...]

    @model_validator(mode="before")
    @classmethod
    def _validate_strict_collection(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value

        int_fields = (
            "source_project_count",
            "total_duration_seconds",
            "reference_requested_limit",
            "reference_selected_project_count",
            "reference_selected_duration_seconds",
            "reference_remaining_duration_seconds",
        )
        for field in int_fields:
            if isinstance(value.get(field), bool):
                raise ValueError(f"{field} must not be a boolean")

        scenarios = value.get("scenarios")
        if scenarios is not None and (
            not isinstance(scenarios, tuple)
            or not all(
                isinstance(scenario, PortfolioProjectEffortFocusScenario)
                for scenario in scenarios
            )
        ):
            raise ValueError(
                "scenarios must be an immutable ordered tuple of genuine "
                "PortfolioProjectEffortFocusScenario instances"
            ) from None
        return value

    @model_validator(mode="after")
    def _validate_focus_scenario_set_invariants(
        self,
    ) -> PortfolioProjectEffortFocusScenarioSet:
        if not self.scenarios:
            raise ValueError("a focus scenario set must contain at least one scenario")

        if self.reference_selected_project_count > self.source_project_count:
            raise ValueError(
                "reference_selected_project_count may not exceed "
                "source_project_count"
            )

        total = self.total_duration_seconds

        # Incomplete (total is None) or zero-total (total == 0) domain:
        # NO reference or scenario may expose a numeric effort duration
        # or duration delta — no partially fabricated effort.
        unavailability_allowed = total is None or total == 0
        if unavailability_allowed and (
            self.reference_selected_duration_seconds is not None
            or self.reference_remaining_duration_seconds is not None
        ):
            state = (
                "an incomplete set (total_duration_seconds is None)"
                if total is None
                else "a zero-total set (total_duration_seconds == 0)"
            )
            raise ValueError(
                f"{state} may not expose any numeric reference effort duration"
            )

        if total is not None and total > 0:
            reference_selected = self.reference_selected_duration_seconds
            reference_remaining = self.reference_remaining_duration_seconds
            if reference_selected is None or reference_remaining is None:
                raise ValueError(
                    "reference_selected_duration_seconds and "
                    "reference_remaining_duration_seconds must not be "
                    "None for a positive-total set"
                )
            if reference_selected > total:
                raise ValueError(
                    "reference_selected_duration_seconds may not exceed "
                    "total_duration_seconds"
                )
            if reference_selected + reference_remaining != total:
                raise ValueError(
                    "reference_selected_duration_seconds + "
                    "reference_remaining_duration_seconds must equal "
                    "total_duration_seconds"
                )

        for index, scenario in enumerate(self.scenarios, start=1):
            if scenario.selected_project_count > self.source_project_count:
                raise ValueError(
                    f"scenario {index}: selected_project_count may not "
                    "exceed source_project_count"
                )
            if (
                scenario.selected_project_count_delta
                != scenario.selected_project_count
                - self.reference_selected_project_count
            ):
                raise ValueError(
                    f"scenario {index}: selected_project_count_delta must "
                    "equal selected_project_count - "
                    "reference_selected_project_count"
                )

            if unavailability_allowed:
                if (
                    scenario.selected_duration_seconds is not None
                    or scenario.selected_duration_delta_seconds is not None
                    or scenario.remaining_duration_seconds is not None
                    or scenario.remaining_duration_delta_seconds is not None
                ):
                    raise ValueError(
                        f"scenario {index}: an unavailable-total set may "
                        "not expose any numeric effort duration or "
                        "duration delta"
                    )
                continue

            if total is None or total == 0:
                raise ValueError(
                    f"scenario {index}: total_duration_seconds must be "
                    "None, zero, or a positive integer"
                )

            selected = scenario.selected_duration_seconds
            remaining = scenario.remaining_duration_seconds
            if selected is None or remaining is None:
                raise ValueError(
                    f"scenario {index}: selected_duration_seconds and "
                    "remaining_duration_seconds must not be None for a "
                    "positive-total set"
                )
            if selected > total:
                raise ValueError(
                    f"scenario {index}: selected_duration_seconds may not "
                    "exceed total_duration_seconds"
                )
            if selected + remaining != total:
                raise ValueError(
                    f"scenario {index}: selected_duration_seconds + "
                    "remaining_duration_seconds must equal "
                    "total_duration_seconds"
                )

            reference_selected = self.reference_selected_duration_seconds
            reference_remaining = self.reference_remaining_duration_seconds
            if reference_selected is None or reference_remaining is None:
                raise ValueError(
                    f"scenario {index}: the positive-total reference must "
                    "expose numeric effort durations"
                )

            if scenario.selected_duration_delta_seconds is None:
                raise ValueError(
                    f"scenario {index}: selected_duration_delta_seconds "
                    "must not be None for a positive-total set"
                )
            if scenario.remaining_duration_delta_seconds is None:
                raise ValueError(
                    f"scenario {index}: remaining_duration_delta_seconds "
                    "must not be None for a positive-total set"
                )
            if (
                scenario.selected_duration_delta_seconds
                != selected - reference_selected
            ):
                raise ValueError(
                    f"scenario {index}: selected_duration_delta_seconds "
                    "must equal selected_duration_seconds - "
                    "reference_selected_duration_seconds"
                )
            if (
                scenario.remaining_duration_delta_seconds
                != remaining - reference_remaining
            ):
                raise ValueError(
                    f"scenario {index}: remaining_duration_delta_seconds "
                    "must equal remaining_duration_seconds - "
                    "reference_remaining_duration_seconds"
                )
            if (
                scenario.selected_duration_delta_seconds
                + scenario.remaining_duration_delta_seconds
                != 0
            ):
                raise ValueError(
                    f"scenario {index}: selected_duration_delta_seconds + "
                    "remaining_duration_delta_seconds must equal 0 (the "
                    "scenario and the reference describe the same "
                    "authoritative total)"
                )

        return self


# ---------------------------------------------------------------------------
# Pure grouping boundary.
# ---------------------------------------------------------------------------


def build_portfolio_effort_focus_scenario_set(
    comparisons: tuple[PortfolioProjectEffortSelectionComparison, ...],
) -> PortfolioProjectEffortFocusScenarioSet:
    """Group a NON-EMPTY ORDERED tuple of authoritative V1.32 comparisons
    into one immutable ordered focus scenario set.

    Steps:
      1. require a NON-EMPTY ordered ``tuple`` of comparisons (lists,
         ``None``, dicts, strings, and foreign containers are rejected);
      2. for EVERY element: require a genuine V1.32
         ``PortfolioProjectEffortSelectionComparison`` value (``None``,
         dicts, strings, foreign models, and duck types are rejected),
         freshly and strictly re-validate its COMPLETE payload, and
         retain ONLY the returned validated object — hostile
         ``model_construct`` values and hostile scalar types in any
         element are rejected;
      3. using ONLY the revalidated objects, require the same
         authoritative comparison domain and the same scalar LEFT/
         reference focus: equal ``portfolio_id``, equal
         ``source_project_count``, equal ``total_duration_seconds``
         (including equal availability), equal
         ``left_requested_limit``, equal ``left_selected_project_count``,
         equal ``left_selected_duration_seconds``, and equal
         ``left_remaining_duration_seconds``;
      4. project ONE scenario per comparison, in EXACT caller input
         order, copying only the V1.32 RIGHT-side scalars and exact
         ``* - reference`` delta scalars; in the incomplete and
         zero-total domains every duration / duration-delta field is
         exactly ``None``.

    V1.33 is descriptive decision support only: it never chooses,
    recommends, ranks, optimizes, or scores a scenario, never deduplicates
    or reorders, and never persists anything. No I/O, no writes, no
    repository access, no V1.31 or earlier recomputation, no project
    rows, no classification or recommendation. The inputs are never
    mutated and repeated calls are value-identical.
    """
    if not isinstance(comparisons, tuple):
        raise PortfolioProjectEffortFocusScenarioSetError(
            "a non-empty ordered tuple of genuine V1.32 "
            "PortfolioProjectEffortSelectionComparison values is required, "
            f"got {type(comparisons).__name__}"
        )
    if not comparisons:
        raise PortfolioProjectEffortFocusScenarioSetError(
            "at least one V1.32 comparison is required"
        )

    validated: list[PortfolioProjectEffortSelectionComparison] = []
    for index, comparison in enumerate(comparisons, start=1):
        if not isinstance(comparison, PortfolioProjectEffortSelectionComparison):
            raise PortfolioProjectEffortFocusScenarioSetError(
                "a genuine V1.32 "
                "PortfolioProjectEffortSelectionComparison is required for "
                f"element {index}, got {type(comparison).__name__}"
            )

        try:
            payload: dict[str, object] = comparison.to_payload()
        except (AttributeError, TypeError) as exc:
            raise PortfolioProjectEffortFocusScenarioSetError(
                f"the supplied V1.32 comparison at element {index} is not "
                "the V1.32 shape"
            ) from exc

        try:
            validated.append(
                PortfolioProjectEffortSelectionComparison.model_validate(
                    payload, strict=True
                )
            )
        except ValidationError as exc:
            raise PortfolioProjectEffortFocusScenarioSetError(
                f"the supplied V1.32 comparison at element {index} failed "
                "strict re-validation"
            ) from exc

    reference = validated[0]
    for index, candidate in enumerate(validated[1:], start=2):
        if candidate.portfolio_id != reference.portfolio_id:
            raise PortfolioProjectEffortFocusScenarioSetError(
                f"element {index}: comparisons must describe the same "
                "portfolio"
            )
        if candidate.source_project_count != reference.source_project_count:
            raise PortfolioProjectEffortFocusScenarioSetError(
                f"element {index}: comparisons must describe the same "
                "source_project_count"
            )
        if (
            candidate.total_duration_seconds is None
        ) != (reference.total_duration_seconds is None):
            raise PortfolioProjectEffortFocusScenarioSetError(
                f"element {index}: comparisons must have the same total "
                "availability (incomplete and complete domains cannot be "
                "mixed)"
            )
        if (
            candidate.total_duration_seconds
            != reference.total_duration_seconds
        ):
            raise PortfolioProjectEffortFocusScenarioSetError(
                f"element {index}: comparisons must describe the same "
                "authoritative total_duration_seconds"
            )
        if candidate.left_requested_limit != reference.left_requested_limit:
            raise PortfolioProjectEffortFocusScenarioSetError(
                f"element {index}: comparisons must share the same "
                "reference (left) requested_limit"
            )
        if candidate.left_selected_project_count != reference.left_selected_project_count:
            raise PortfolioProjectEffortFocusScenarioSetError(
                f"element {index}: comparisons must share the same "
                "reference (left) selected_project_count"
            )
        if (
            candidate.left_selected_duration_seconds
            != reference.left_selected_duration_seconds
        ):
            raise PortfolioProjectEffortFocusScenarioSetError(
                f"element {index}: comparisons must share the same "
                "reference (left) selected_duration_seconds"
            )
        if (
            candidate.left_remaining_duration_seconds
            != reference.left_remaining_duration_seconds
        ):
            raise PortfolioProjectEffortFocusScenarioSetError(
                f"element {index}: comparisons must share the same "
                "reference (left) remaining_duration_seconds"
            )

    scenarios = tuple(
        PortfolioProjectEffortFocusScenario(
            requested_limit=comparison.right_requested_limit,
            selected_project_count=comparison.right_selected_project_count,
            selected_project_count_delta=comparison.selected_project_count_delta,
            selected_duration_seconds=comparison.right_selected_duration_seconds,
            selected_duration_delta_seconds=(
                comparison.selected_duration_delta_seconds
            ),
            remaining_duration_seconds=comparison.right_remaining_duration_seconds,
            remaining_duration_delta_seconds=(
                comparison.remaining_duration_delta_seconds
            ),
        )
        for comparison in validated
    )

    return PortfolioProjectEffortFocusScenarioSet(
        portfolio_id=reference.portfolio_id,
        source_project_count=reference.source_project_count,
        total_duration_seconds=reference.total_duration_seconds,
        reference_requested_limit=reference.left_requested_limit,
        reference_selected_project_count=reference.left_selected_project_count,
        reference_selected_duration_seconds=(
            reference.left_selected_duration_seconds
        ),
        reference_remaining_duration_seconds=(
            reference.left_remaining_duration_seconds
        ),
        scenarios=scenarios,
    )
