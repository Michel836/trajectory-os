"""V1.29 — Deterministic top-ranked project effort selection projection.

V1.29 derives a deterministic bounded selection of the highest-effort ranked
projects from a genuine V1.28 ``PortfolioProjectEffortRanking``.  Being
selected is execution-effort semantics ONLY — it is NOT a business priority,
strategic importance, value, urgency, impact, risk, or recommendation.  No
percentages, no floats, no ``Decimal``, no division, no rounding, no effort
or percentage thresholds, no concentration analysis, and no Pareto/80-20
policy are introduced.

V1.29 is a projection only.  It performs no I/O, no wall-clock or uuid
reads, no provider/AI calls, no repository or durable composition, and no
recomputation from V1.27 or earlier layers.  Its sole input authority is
the caller-supplied V1.28 ranking: V1.29 reads only the portfolio ID, the
V1.28 ``project_count``, the authoritative portfolio total, and each
project's ``project_id``, exact ``total_duration_seconds``, dense ``rank``,
and exact share state.  The input is never mutated.

Selection semantics:

* ``limit`` must be a strict positive integer (``>= 1``); ``bool``, float,
  string, ``None``, zero, and negative values are rejected.
* The requested ``limit`` is an ordinal boundary BEFORE dense-rank tie
  expansion: it is a maximum number of ranking positions, never permission
  to split a dense-rank tie.  If the boundary position falls inside a
  dense-rank tie group, the complete tie group is included, so the selected
  project count MAY exceed ``limit`` — and does so ONLY because of that
  tie expansion.
* Cutoff is derived solely from the authoritative V1.28 dense ranks:
  no re-sorting by UUID, title, timestamp, or any secondary key, and no
  re-derivation of ordering from secondary data.
* V1.28 ranks are preserved EXACTLY and never renumbered.
* The selection tuple preserves the authoritative V1.28 row order (it is a
  filter over the authoritative ordering, not a re-order).
* Exact project IDs, exact project totals, and exact (numerator,
  denominator) share semantics are preserved in value (a freshly
  constructed equivalent value is permitted — no Python object-identity
  guarantee exists).
* If ``limit >= project_count``, the complete semantic ranking is returned.

Unavailable states:

* **Incomplete ranking** — NO selection is fabricated:
  ``projects == ()``, ``selected_project_count == 0``, and
  ``total_duration_seconds`` mirrors the authoritative unavailable total
  (``None``).
* **Complete zero-total ranking** — NO selection ordering is invented:
  ``projects == ()``, ``selected_project_count == 0``, and
  ``total_duration_seconds == 0``.
* **Empty ranking** — remains empty (``source_project_count == 0``,
  ``projects == ()``, ``selected_project_count == 0``, total exactly
  ``0``); nothing is synthesized.

Validation semantics mirror the repository convention: hostile
``model_construct`` values at the top level, inside the nested projects
tuple, and inside a nested exact share must be rejected by FRESH STRICT
re-validation, never trusted.  The output model is self-validating (strict,
frozen, ``extra="forbid"``, before/after validator layers) so a
``PortfolioProjectEffortTopSelection`` carries a semantically coherent
state on every construction — including direct construction.  Concretely:

* ``selected_project_count`` must equal the number of ``projects`` entries
  and may never exceed ``source_project_count``;
* a no-selection (``projects == ()``) state may only mirror an empty or
  unavailable V1.28 state (total ``0`` or ``None``) — a positive total
  without any selected row is rejected;
* a selection WITH rows requires every row to be ranked, share-carrying,
  with a complete project total, and a strictly positive
  ``total_duration_seconds`` (a zero-total or incomplete ranking cannot
  carry selection rows);
* every selected project total must not exceed the portfolio total;
* when the requested limit covers the whole source ranking (complete
  positive-total state), the selection must include every source project;
* when the limit does not cover the whole source ranking (complete
  positive-total state), the selected count must be at least the requested
  limit — the boundary row is always included, and any excess is only
  legitimate as dense-rank tie expansion;
* a carried row (when present) is FRESH STRICT REVALIDATED — a hostile
  ``model_construct`` nested row or its nested exact share is rejected,
  never trusted;
* project IDs are unique.
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

from trajectory_os.application.execution_effort_project_ranking import (
    PortfolioProjectEffortRank,
    PortfolioProjectEffortRanking,
)

__all__ = [
    "PortfolioProjectEffortTopSelection",
    "PortfolioProjectEffortTopSelectionError",
    "select_top_ranked_portfolio_project_effort",
]


class PortfolioProjectEffortTopSelectionError(ValueError):
    """Raised when a supplied V1.28 ranking or selection limit is not usable."""


# ---------------------------------------------------------------------------
# Projected selection model (immutable, self-validating).
# ---------------------------------------------------------------------------


class PortfolioProjectEffortTopSelection(BaseModel):
    """Immutable deterministic bounded top-ranked selection of one V1.28 ranking.

    ``requested_limit`` is a strict integer ``>= 1``; ``source_project_count``
    is the V1.28 ``project_count``; ``selected_project_count`` MUST equal the
    number of ``projects`` entries and MUST NOT exceed
    ``source_project_count``; ``total_duration_seconds`` mirrors the
    authoritative V1.28 portfolio total (``0`` for an empty or complete
    zero-total state, ``None`` for the incomplete state, and strictly
    positive whenever a non-empty selection exists).  Project IDs are unique.

    Row invariants — a selected row is meaningful only against its own
    complete project total, so:

    * every selected row MUST carry a complete ``total_duration_seconds``,
      a dense ``rank`` (``>= 1``), and an exact ``share`` (selection rows
      never exist for unavailable or zero-total states);
    * every selected project total MUST NOT exceed the selection's
      ``total_duration_seconds``;
    * a carried row (when present) is FRESH STRICT REVALIDATED — a hostile
      ``model_construct`` nested row, or a hostile nested exact share inside
      it, is rejected, never trusted.

    Ranks are the EXACT dense ranks supplied by V1.28; V1.29 never renumbers
    them.  The ``projects`` tuple preserves the authoritative V1.28 row
    order for the selected rows.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    portfolio_id: UUID
    requested_limit: Annotated[StrictInt, Field(ge=1)]
    source_project_count: Annotated[StrictInt, Field(ge=0)]
    selected_project_count: Annotated[StrictInt, Field(ge=0)]
    total_duration_seconds: Annotated[StrictInt, Field(ge=0)] | None = None
    projects: tuple[PortfolioProjectEffortRank, ...]

    def to_payload(self) -> dict[str, object]:
        """Serialize this selection into a plain structure (pure, no I/O)."""
        return {
            "portfolio_id": self.portfolio_id,
            "requested_limit": self.requested_limit,
            "source_project_count": self.source_project_count,
            "selected_project_count": self.selected_project_count,
            "total_duration_seconds": self.total_duration_seconds,
            "projects": tuple(project.to_payload() for project in self.projects),
        }

    @model_validator(mode="before")
    @classmethod
    def _validate_non_bool_ints(cls, value: object) -> object:
        if isinstance(value, dict):
            for field in (
                "requested_limit",
                "source_project_count",
                "selected_project_count",
                "total_duration_seconds",
            ):
                if isinstance(value.get(field), bool):
                    raise ValueError(f"{field} must not be a boolean")
        return value

    @model_validator(mode="after")
    def _validate_selection_invariants(
        self,
    ) -> PortfolioProjectEffortTopSelection:
        if self.selected_project_count != len(self.projects):
            raise ValueError(
                f"selected_project_count={self.selected_project_count} does not "
                f"equal the number of selected project entries "
                f"({len(self.projects)})"
            )

        if self.selected_project_count > self.source_project_count:
            raise ValueError(
                "selected_project_count may not exceed source_project_count"
            )

        # Hostile nested rows (including their nested exact shares) are NEVER
        # trusted: every row is freshly strictly revalidated (this re-runs
        # the row-level rank/share invariants AND revalidates the nested
        # share against ExactProjectEffortShare).  Any
        # validation/attribute/type failure on a hostile row is converted
        # into the selection's normal validation path (ValueError ⇒
        # ValidationError), never leaked.
        revalidated: list[PortfolioProjectEffortRank] = []
        for project in self.projects:
            try:
                revalidated.append(
                    PortfolioProjectEffortRank.model_validate(
                        project.to_payload(), strict=True
                    )
                )
            except (AttributeError, TypeError, ValidationError) as exc:
                raise ValueError(
                    "a selected project entry failed fresh strict "
                    "revalidation and is rejected"
                ) from exc

        # Duplicate IDs are checked ONLY AFTER successful revalidation, so
        # unhashable hostile project_ids are already rejected above and can
        # never leak a raw TypeError from set() construction.
        project_ids = [project.project_id for project in revalidated]
        if len(project_ids) != len(set(project_ids)):
            raise ValueError("duplicate project IDs are not allowed")

        if not revalidated:
            # No-selection state: it may only MIRROR an empty or
            # unavailable V1.28 state (total exactly 0, or None for the
            # incomplete state).  A positive total with no selected rows
            # would be a fabricated selection and is rejected.
            if self.total_duration_seconds not in (0, None):
                raise ValueError(
                    "a no-selection state must carry total_duration_seconds "
                    f"of 0 or None, got {self.total_duration_seconds!r}"
                )
            return self

        if self.total_duration_seconds is None:
            raise ValueError(
                "selected rows must not be exposed while the portfolio total "
                "(total_duration_seconds) is None"
            )

        if self.total_duration_seconds == 0:
            raise ValueError(
                "a complete zero-total ranking cannot expose a top selection"
            )

        for project in revalidated:
            if project.rank is None:
                raise ValueError(
                    "a top selection must expose only ranked projects"
                )
            if project.share is None:
                raise ValueError(
                    "a ranked project of a top selection must expose an "
                    "exact share"
                )
            if project.total_duration_seconds is None:
                raise ValueError(
                    "a ranked project of a top selection must carry a "
                    "complete project total"
                )
            if project.total_duration_seconds > self.total_duration_seconds:
                raise ValueError(
                    "a selected project total must not exceed the portfolio "
                    "total"
                )

        # Cross-row V1.29 selection invariants.  A direct construction
        # must still describe a top-ranked prefix by dense rank: rank 1 is
        # necessarily present, no dense-rank gaps may exist, and every
        # exact share denominator must mirror this portfolio total.
        selected_ranks = {
            project.rank
            for project in revalidated
            if project.rank is not None
        }
        max_selected_rank = max(selected_ranks)
        if selected_ranks != set(range(1, max_selected_rank + 1)):
            raise ValueError(
                "selected ranks must form a dense top-ranked prefix "
                "starting at rank 1"
            )

        for project in revalidated:
            if (
                project.share is not None
                and project.share.denominator_duration_seconds
                != self.total_duration_seconds
            ):
                raise ValueError(
                    "selected project share denominator must equal the "
                    "portfolio total"
                )

        # Limit/source/selected consistency for a complete positive-total
        # state (the only state where a selection may exist):
        #  * when the limit covers the whole source ranking, the selection
        #    must include EVERY source project;
        #  * otherwise the boundary row is always included (tie expansion
        #    may only ADD projects, never drop the boundary row).
        if self.requested_limit >= self.source_project_count:
            if self.selected_project_count != self.source_project_count:
                raise ValueError(
                    "when the requested limit covers the whole source "
                    "ranking, the selection must include every source "
                    "project"
                )
        elif self.selected_project_count < self.requested_limit:
            raise ValueError(
                "the selected count may not be below the requested limit: "
                "the boundary row is always included and tie expansion may "
                "only add projects"
            )

        return self


# ---------------------------------------------------------------------------
# Pure selection boundary.
# ---------------------------------------------------------------------------


def select_top_ranked_portfolio_project_effort(
    ranking: PortfolioProjectEffortRanking,
    limit: int,
) -> PortfolioProjectEffortTopSelection:
    """Select the top-ranked projects of one genuine V1.28 ranking.

    Steps:
      1. require a genuine ``PortfolioProjectEffortRanking`` (V1.28);
      2. require ``limit`` to be a strict positive integer (``>= 1``; bool,
         float, string, ``None``, zero, and negatives are rejected);
      3. freshly/strictly re-validate the WHOLE supplied V1.28 ranking
         (rejects hostile ``model_construct`` values at the top level and in
         the nested projects tuple, including nested exact shares);
      4. for a complete positive-total V1.28 ranking, select the rows
         belonging to the top dense ranks before tie expansion: the cutoff
         rank is the rank of the ``limit``-th position in the authoritative
         V1.28 effort order, and the ENTIRE dense-rank tie group at the
         cutoff is included (ties are never split; the selected count may
         therefore exceed ``limit``);
      5. for empty, incomplete, and complete zero-total V1.28 rankings,
         preserve the unavailable/empty selection state exactly (NO
         selection fabricated: ``projects == ()``,
         ``selected_project_count == 0``);
      6. return an immutable ``PortfolioProjectEffortTopSelection`` preserving
         the authoritative V1.28 project order and EXACT V1.28 ranks.

    No I/O, no writes, no WBS reconstruction, no estimate/provenance access,
    no repository composition, no division or rounding, no re-sorting by
    secondary keys, and no ranking recomputation: everything is derived from
    the caller-supplied, now re-validated V1.28 ranking and its exact dense
    ranks.  The input is never mutated.
    """
    if not isinstance(ranking, PortfolioProjectEffortRanking):
        raise PortfolioProjectEffortTopSelectionError(
            "a genuine V1.28 PortfolioProjectEffortRanking instance "
            f"is required, got {type(ranking).__name__}"
        )

    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit < 1
    ):
        raise PortfolioProjectEffortTopSelectionError(
            "limit must be a strict positive integer >= 1, "
            f"got {limit!r}"
        )

    try:
        projects = tuple(ranking.projects)
        payload: object = {
            "portfolio_id": ranking.portfolio_id,
            "project_count": ranking.project_count,
            "total_duration_seconds": ranking.total_duration_seconds,
            "projects": projects,
        }
    except (AttributeError, TypeError) as exc:
        raise PortfolioProjectEffortTopSelectionError(
            "supplied V1.28 ranking is not the V1.28 shape"
        ) from exc

    try:
        validated = PortfolioProjectEffortRanking.model_validate(
            payload, strict=True
        )
    except ValidationError as exc:
        raise PortfolioProjectEffortTopSelectionError(
            "supplied V1.28 ranking failed strict re-validation"
        ) from exc

    if not validated.projects:
        # Empty ranking: nothing is synthesized; the no-selection state
        # mirrors the authoritative zero total.
        return PortfolioProjectEffortTopSelection(
            portfolio_id=validated.portfolio_id,
            requested_limit=limit,
            source_project_count=0,
            selected_project_count=0,
            total_duration_seconds=0,
            projects=(),
        )

    portfolio_total = validated.total_duration_seconds
    if portfolio_total is None or portfolio_total == 0:
        # Incomplete (None) or complete zero-total (0) ranking: NO
        # selection is fabricated and NO ordering is invented; the
        # no-selection state mirrors the authoritative total exactly.
        return PortfolioProjectEffortTopSelection(
            portfolio_id=validated.portfolio_id,
            requested_limit=limit,
            source_project_count=validated.project_count,
            selected_project_count=0,
            total_duration_seconds=portfolio_total,
            projects=(),
        )

    # Complete positive-total V1.28 ranking: every row carries an exact
    # dense rank (the V1.28 invariants guarantee this).  The project tuple
    # itself remains in authoritative V1.28 row order and is never re-sorted.
    # The cutoff is derived only from the dense-rank domain 1, 2, 3, ...
    # until the cumulative number of projects reaches ``limit``.  All rows
    # with rank <= that cutoff are then selected by filtering the original
    # V1.28 tuple, so the complete cutoff tie is included without changing
    # authoritative row order.
    rank_counts: dict[int, int] = {}
    for project in validated.projects:
        if project.rank is None:
            raise PortfolioProjectEffortTopSelectionError(
                "unreachable: a complete positive-total ranking row is "
                "unranked"
            )
        rank_counts[project.rank] = rank_counts.get(project.rank, 0) + 1

    if validated.project_count <= limit:
        # limit >= project_count: the complete semantic ranking is returned.
        selected = validated.projects
    else:
        running = 0
        boundary_rank: int | None = None
        # V1.28 guarantees dense ranks with no gaps, therefore the exact
        # rank domain is 1..number_of_distinct_ranks. This iterates rank
        # values only; it never sorts or reorders project rows.
        for rank in range(1, len(rank_counts) + 1):
            running += rank_counts[rank]
            if running >= limit:
                boundary_rank = rank
                break
        if boundary_rank is None:
            raise PortfolioProjectEffortTopSelectionError(
                "unreachable: no cutoff rank within the authoritative "
                "V1.28 ranking"
            )
        selected = tuple(
            project
            for project in validated.projects
            if project.rank is not None and project.rank <= boundary_rank
        )

    # Rows are preserved EXACTLY (ids, totals, ranks, and exact share
    # values); V1.28 ranks are never renumbered and the authoritative V1.28
    # row order is preserved (this is a filter, never a re-sort).
    selected_projects = tuple(
        PortfolioProjectEffortRank(
            project_id=project.project_id,
            total_duration_seconds=project.total_duration_seconds,
            rank=project.rank,
            share=project.share,
        )
        for project in selected
    )
    return PortfolioProjectEffortTopSelection(
        portfolio_id=validated.portfolio_id,
        requested_limit=limit,
        source_project_count=validated.project_count,
        selected_project_count=len(selected_projects),
        total_duration_seconds=portfolio_total,
        projects=selected_projects,
    )
