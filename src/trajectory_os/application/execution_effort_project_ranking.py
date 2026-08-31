"""V1.28 — Exact project execution-effort ranking projection.

V1.28 derives an exact project execution-effort ranking from a genuine V1.27
``PortfolioProjectEffortShareSummary``. A ranking assigns each project a DENSE
integer rank in descending order of its exact ``total_duration_seconds``:
equal totals share the same rank, and the next distinct (strictly lower)
total receives the next integer rank (``400 -> 1``, ``300 -> 2``,
``300 -> 2``, ``100 -> 3``). No percentages, no floats, no division, no
rounding, no top-N, and no business priority/value/urgency/ROI semantics are
introduced — ranks compare exact integer durations only.

V1.28 is a projection only. It performs no I/O, no wall-clock or uuid
reads, no provider/AI calls, no repository or durable composition, and no
recomputation from earlier layers (V1.26 or below). Its sole input authority
is the caller-supplied V1.27 summary: V1.28 reads only each project's
``project_id``, exact ``total_duration_seconds``, and exact share state.
The input is never mutated.

Ranking availability is derived ONLY from the V1.27 state:

* **Complete positive-total portfolio** — every project exposes a complete
  total and the portfolio total is positive: EVERY project receives an exact
  dense integer rank (``rank >= 1``, ``1`` reserved for the highest total),
  ties preserve the authoritative V1.27 project order, zero-duration
  projects rank after positive ones, and every project's exact
  numerator/denominator share semantics are preserved in value: the ranking
  share is exactly equal to the authoritative V1.27 share (a freshly
  constructed equivalent share is permitted — no Python object-identity
  guarantee exists) and nothing is recomputed from V1.26 or earlier layers.
* **Incomplete portfolio** — NO ranking is fabricated: every project carries
  ``rank == None`` (and no share), mirroring the V1.27 unavailable state.
* **Complete zero-total portfolio** — NO ranks are invented and NO ``0/0``
  share is constructed: every project carries ``rank == None`` and no share.
* **Empty portfolio** — remains empty (``projects == ()``, total exactly
  ``0``); nothing is synthesized.

The unavailable-ranking state is expressed minimally and structurally:
``rank is None`` on every row, consistent with the V1.27 ``share is None``
pattern. A separate ``ranking_available`` boolean is deliberately NOT
introduced — the state is already unambiguous and self-validating models
reject any mixed or contradictory combination.

Validation semantics mirror the repository convention: hostile
``model_construct`` values at the top level, inside the nested projects
tuple, and inside a nested share must be rejected by FRESH STRICT
re-validation, never trusted. Both output models are self-validating
(frozen, strict, ``extra="forbid"``, before/after validator layers) so a
``PortfolioProjectEffortRank`` or ``PortfolioProjectEffortRanking`` carries
a semantically coherent state on every construction — including direct
construction. Concretely:

* a rank may never be exposed while its project total is incomplete;
* a rank and a share always co-occur (both exist for complete
  positive-total projects, neither exists otherwise);
* an empty ranking may never carry a non-``0`` (or ``None``) total;
* an incomplete (non-empty) ranking may never expose a complete total or
  ANY rank or share;
* a complete zero-total ranking may never expose a rank or any share
  (no ``0/0``);
* a complete positive-total ranking must expose a rank, an exact share,
  and a complete non-negative total for every project, with the summary
  total equal to the exact strict sum of project totals and ranks exactly
  dense in descending order of exact totals.
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

from trajectory_os.application.execution_effort_project_shares import (
    ExactProjectEffortShare,
    PortfolioProjectEffortShareSummary,
)

__all__ = [
    "PortfolioProjectEffortRank",
    "PortfolioProjectEffortRanking",
    "PortfolioProjectEffortRankingError",
    "rank_portfolio_project_effort",
]


class PortfolioProjectEffortRankingError(ValueError):
    """Raised when a supplied V1.27 share summary is not usable."""


# ---------------------------------------------------------------------------
# Projected ranking models (immutable, self-validating).
# ---------------------------------------------------------------------------


def _validate_non_bool_ints(
    value: dict[str, object],
) -> dict[str, object]:
    for field in ("total_duration_seconds", "rank"):
        if isinstance(value.get(field), bool):
            raise ValueError(f"{field} must not be a boolean")
    return value


class PortfolioProjectEffortRank(BaseModel):
    """Projected dense-rank row for one V1.27 project record.

    Invariants — a rank is meaningful only against its own complete project
    total, so:

    * ``rank`` being non-``None`` REQUIRES
      ``total_duration_seconds`` to be a non-``None`` strict integer total
      (a rank may never be exposed while the project total is incomplete);
    * ``rank`` being non-``None`` REQUIRES ``share`` to be non-``None``
      (a rank and a share always co-occur);
    * ``share`` being non-``None`` REQUIRES ``rank`` to be non-``None`` and
      ``share.numerator_duration_seconds == total_duration_seconds``;
    * a carried ``share`` (when present) is FRESH STRICT REVALIDATED — a
      hostile ``model_construct`` nested share is rejected, never trusted.

    ``rank`` is a strict integer ``>= 1`` (dense) or ``None`` for
    unavailable-ranking states (incomplete, zero-total, and empty
    portfolios). ``share`` carries the exact V1.27 numerator/denominator
    pair in value (never rounded, never altered; a freshly constructed
    equivalent share is permitted — no object-identity guarantee) or is
    ``None`` exactly when
    the V1.27 summary exposed no share for that project.
    ``total_duration_seconds`` is the exact complete total carried by the
    V1.27 record (``0`` allowed), or ``None`` when that total was
    incomplete.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    project_id: UUID
    total_duration_seconds: Annotated[StrictInt, Field(ge=0)] | None = None
    rank: Annotated[StrictInt, Field(ge=1)] | None = None
    share: ExactProjectEffortShare | None = None

    def to_payload(self) -> dict[str, object]:
        """Serialize this rank entry into a plain dict (pure, no I/O)."""
        return {
            "project_id": self.project_id,
            "total_duration_seconds": self.total_duration_seconds,
            "rank": self.rank,
            "share": (
                self.share.to_payload() if self.share is not None else None
            ),
        }

    @model_validator(mode="before")
    @classmethod
    def _validate_non_bool_ints(
        cls, value: object
    ) -> object:
        if isinstance(value, dict):
            return _validate_non_bool_ints(value)
        return value

    @model_validator(mode="after")
    def _validate_rank_consistency(self) -> PortfolioProjectEffortRank:
        if self.share is not None:
            # A nested model instance may be a hostile model_construct
            # result, so it is NEVER trusted as-is: read its exact field
            # values back (via to_payload, which reads attributes and
            # triggers no serializer), revalidate them strictly, and
            # enforce the row invariants against the revalidated values.
            revalidated = ExactProjectEffortShare.model_validate(
                self.share.to_payload(), strict=True
            )
            if self.total_duration_seconds is None:
                raise ValueError(
                    "a share must not be exposed while the project total "
                    "(total_duration_seconds) is None"
                )
            if (
                revalidated.numerator_duration_seconds
                != self.total_duration_seconds
            ):
                raise ValueError(
                    "share.numerator_duration_seconds must equal "
                    "total_duration_seconds"
                )
            if self.rank is None:
                raise ValueError(
                    "a share may only be exposed for a ranked project of "
                    "a complete positive-total portfolio"
                )

        if self.rank is not None:
            if self.total_duration_seconds is None:
                raise ValueError(
                    "a rank must not be exposed while the project total "
                    "(total_duration_seconds) is None"
                )
            if self.share is None:
                raise ValueError(
                    "a ranked project of a complete positive-total "
                    "portfolio must expose an exact share"
                )
        return self


class PortfolioProjectEffortRanking(BaseModel):
    """Complete projected exact dense ranking for one portfolio.

    ``project_count`` must equal the number of ``projects`` entries; project
    IDs are unique. ``total_duration_seconds`` is the complete portfolio
    total (``0`` for an empty or complete zero-total portfolio; ``None`` ONLY
    when the state is genuinely incomplete — at least one project total is
    ``None`` — and never an ambiguous stand-in for a known total).

    Semantic invariants (all states pinned down; each contradictory
    combination is rejected on every construction, including via a
    ``projects`` tuple):

    * **Empty** (``project_count == 0`` ⇒ ``projects == ()``):
      ``total_duration_seconds`` MUST be exactly ``0`` (``None`` or a
      non-``0`` total on an empty ranking is rejected; nothing synthesized).
    * **Incomplete** (any
      ``PortfolioProjectEffortRank.total_duration_seconds is None``):
      ``total_duration_seconds`` MUST be ``None`` and EVERY project's
      ``rank`` and ``share`` MUST be ``None`` (no partial ranking).
    * The converse: if ``total_duration_seconds`` is ``None`` on a non-empty
      ranking, the state MUST genuinely be incomplete, exposing NO rank and
      NO share.
    * **Complete zero-total** (every project total is ``0``): the
      ``total_duration_seconds`` MUST be ``0`` and every project's ``rank``
      and ``share`` MUST be ``None`` (no ranks invented, no ``0/0``).
    * **Complete positive total** (all project totals complete and
      ``sum > 0``): EVERY project's ``rank`` MUST be a strict integer
      ``>= 1`` and ``share`` MUST be non-``None`` with
      ``share.numerator_duration_seconds == that project total`` and
      ``share.denominator_duration_seconds == the exact summary total``
      (``total_duration_seconds`` MUST equal that exact strict sum).
      Ranks MUST be EXACTLY DENSE in descending order of exact project
      totals: ties share one rank, the next strictly distinct lower total
      gets the next integer rank, rank ``1`` belongs to the highest total,
      and no rank value exists beyond the number of distinct totals.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    portfolio_id: UUID
    project_count: Annotated[StrictInt, Field(ge=0)]
    total_duration_seconds: Annotated[StrictInt, Field(ge=0)] | None = None
    projects: tuple[PortfolioProjectEffortRank, ...]

    @model_validator(mode="before")
    @classmethod
    def _validate_non_bool_ints(
        cls, value: object
    ) -> object:
        if isinstance(value, dict):
            for field in ("project_count", "total_duration_seconds"):
                if isinstance(value.get(field), bool):
                    raise ValueError(f"{field} must not be a boolean")
        return value

    @model_validator(mode="after")
    def _validate_ranking_invariants(
        self,
    ) -> PortfolioProjectEffortRanking:
        if self.project_count != len(self.projects):
            raise ValueError(
                f"project_count={self.project_count} does not equal the number "
                f"of projected project entries ({len(self.projects)})"
            )

        # Hostile nested rows (including their nested shares) are NEVER
        # trusted: every row is freshly strictly revalidated (this
        # re-runs the row-level rank/share invariants AND revalidates the
        # nested share against ExactProjectEffortShare), which also rejects
        # hostile model_construct shares. Any validation/attribute/type
        # failure on a hostile row is converted into the ranking's normal
        # validation path (ValueError ⇒ ValidationError), never leaked.
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
                    "a projected project entry failed fresh strict "
                    "revalidation and is rejected"
                ) from exc

        # Duplicate IDs are checked ONLY AFTER successful revalidation, so
        # unhashable hostile project_ids are already rejected above and
        # can never leak a raw TypeError from set() construction.
        project_ids = [project.project_id for project in revalidated]
        if len(project_ids) != len(set(project_ids)):
            raise ValueError("duplicate project IDs are not allowed")

        if not revalidated:
            # Empty state: total is exactly 0 — never None (a fabricated
            # incomplete state) and never a non-0 sum (nothing synthesized).
            if self.total_duration_seconds != 0:
                raise ValueError(
                    "an empty ranking must have total_duration_seconds == "
                    f"0, got {self.total_duration_seconds!r}"
                )
            return self

        if self.total_duration_seconds is None:
            # Incomplete state: no complete total may exist and NO rank and
            # NO share may be exposed (no partial ranking fabricated).
            if any(project.rank is not None for project in revalidated):
                raise ValueError(
                    "an incomplete portfolio cannot expose ranks"
                )
            if any(project.share is not None for project in revalidated):
                raise ValueError(
                    "an incomplete portfolio cannot expose shares"
                )
            return self

        if self.total_duration_seconds == 0:
            # Zero-total state: no rank exists at all (no invented ranks,
            # no 0/0 share, no "complete with zero denominator" pseudo-state).
            if any(
                project.total_duration_seconds != 0 for project in revalidated
            ):
                raise ValueError(
                    "a complete zero-total portfolio requires every project "
                    "total to be 0"
                )
            if any(project.rank is not None for project in revalidated):
                raise ValueError(
                    "a complete zero-total portfolio cannot expose ranks"
                )
            if any(project.share is not None for project in revalidated):
                raise ValueError(
                    "a complete zero-total portfolio cannot expose shares"
                )
            return self

        # ALL project totals are complete ⇒ the total is DETERMINED:
        # any other value must equal the exact strict sum.
        project_totals: list[int] = []
        for project in revalidated:
            if project.total_duration_seconds is None:
                raise ValueError(
                    "a ranked portfolio requires every project total to be "
                    "complete"
                )
            project_totals.append(project.total_duration_seconds)
        if self.total_duration_seconds != sum(project_totals):
            raise ValueError(
                f"total_duration_seconds={self.total_duration_seconds} does "
                f"not equal the exact sum of project totals "
                f"({sum(project_totals)})"
            )

        # Positive complete total: ranks must be EXACTLY dense in
        # descending order of exact totals (ties share a rank; the next
        # distinct lower total gets the next integer; rank 1 at the max).
        distinct_totals = sorted(set(project_totals), reverse=True)
        expected_rank = {
            total: position + 1
            for position, total in enumerate(distinct_totals)
        }

        for project, total in zip(revalidated, project_totals, strict=True):
            if project.rank is None:
                raise ValueError(
                    "a complete positive-total portfolio must expose a "
                    "rank for every project"
                )
            if project.rank != expected_rank[total]:
                raise ValueError(
                    "ranks must be exactly dense in descending order of "
                    "exact project totals: ties share a rank and the next "
                    "distinct total gets the next integer rank"
                )
            if project.share is None:
                raise ValueError(
                    "a complete positive-total portfolio must expose "
                    "a share for every project"
                )
            if project.share.numerator_duration_seconds != total:
                raise ValueError(
                    "share numerator must equal the project's total"
                )
            if (
                project.share.denominator_duration_seconds
                != self.total_duration_seconds
            ):
                raise ValueError(
                    "share denominator must equal the exact summary total"
                )

        return self


# ---------------------------------------------------------------------------
# Pure projection boundary.
# ---------------------------------------------------------------------------


def rank_portfolio_project_effort(
    shares: PortfolioProjectEffortShareSummary,
) -> PortfolioProjectEffortRanking:
    """Rank projects by exact execution effort from one genuine V1.27 summary.

    Steps:
      1. require a genuine ``PortfolioProjectEffortShareSummary``
         (V1.27);
      2. freshly/strictly re-validate the whole supplied V1.27 summary
         (rejects hostile ``model_construct`` values at the top level and in
         the nested projects tuple, including nested exact shares);
      3. for a complete positive-total portfolio, assign each project a
         DENSE integer rank in descending order of its exact
         ``total_duration_seconds`` (ties share a rank, ties preserve the
         authoritative V1.27 project order, zero-duration projects rank
         after positive ones), carrying an exact share whose numerator and
         denominator are exactly equal to the authoritative V1.27 values
         (a freshly constructed equivalent share is permitted; no Python
         object-identity guarantee exists);
      4. for empty, incomplete, and complete zero-total portfolios,
         preserve the unavailable/empty state exactly (no ranks invented,
         no shares fabricated, nothing synthesized);
      5. return an immutable ``PortfolioProjectEffortRanking``.

    No I/O, no writes, no WBS reconstruction, no estimate/provenance access,
    no repository composition, no division or rounding: everything is derived
    from the caller-supplied, now re-validated V1.27 summary. The input is
    never mutated.
    """
    if not isinstance(shares, PortfolioProjectEffortShareSummary):
        raise PortfolioProjectEffortRankingError(
            "a genuine V1.27 PortfolioProjectEffortShareSummary instance "
            f"is required, got {type(shares).__name__}"
        )

    try:
        projects = tuple(shares.projects)
        payload: object = {
            "portfolio_id": shares.portfolio_id,
            "project_count": shares.project_count,
            "total_duration_seconds": shares.total_duration_seconds,
            "projects": projects,
        }
    except (AttributeError, TypeError) as exc:
        raise PortfolioProjectEffortRankingError(
            "supplied V1.27 share summary is not the V1.27 shape"
        ) from exc

    try:
        validated = PortfolioProjectEffortShareSummary.model_validate(
            payload, strict=True
        )
    except ValidationError as exc:
        raise PortfolioProjectEffortRankingError(
            "supplied V1.27 share summary failed strict re-validation"
        ) from exc

    if not validated.projects:
        # Empty portfolio: zero projects, zero total, nothing synthesized.
        return PortfolioProjectEffortRanking(
            portfolio_id=validated.portfolio_id,
            project_count=0,
            total_duration_seconds=0,
            projects=(),
        )

    if validated.total_duration_seconds is None:
        # Incomplete state: preserve the unambiguous unavailable-ranking
        # state — no partial ranking is fabricated, and each row still
        # mirrors its V1.27 total (known or None) with no rank and no
        # share.
        return PortfolioProjectEffortRanking(
            portfolio_id=validated.portfolio_id,
            project_count=validated.project_count,
            total_duration_seconds=None,
            projects=tuple(
                PortfolioProjectEffortRank(
                    project_id=project.project_id,
                    total_duration_seconds=project.total_duration_seconds,
                    rank=None,
                    share=None,
                )
                for project in validated.projects
            ),
        )

    if validated.total_duration_seconds == 0:
        # Complete zero-total state: no rank is invented and no 0/0 share
        # is constructed; rows mirror their V1.27 exact zero totals.
        return PortfolioProjectEffortRanking(
            portfolio_id=validated.portfolio_id,
            project_count=validated.project_count,
            total_duration_seconds=0,
            projects=tuple(
                PortfolioProjectEffortRank(
                    project_id=project.project_id,
                    total_duration_seconds=project.total_duration_seconds,
                    rank=None,
                    share=None,
                )
                for project in validated.projects
            ),
        )

    # Complete positive-total portfolio: the V1.27 invariants guarantee
    # every project total is complete and every share is the exact
    # (project total, portfolio total) pair.
    portfolio_total: int = validated.total_duration_seconds
    complete_totals: list[int] = []
    for project in validated.projects:
        if project.total_duration_seconds is None:
            raise PortfolioProjectEffortRankingError(
                "unreachable: an incomplete project total bypassed the "
                "incomplete state handled above"
            )
        complete_totals.append(project.total_duration_seconds)

    distinct_totals = sorted(set(complete_totals), reverse=True)
    rank_of = {
        total: position + 1
        for position, total in enumerate(distinct_totals)
    }

    ranked_projects = tuple(
        PortfolioProjectEffortRank(
            project_id=project.project_id,
            total_duration_seconds=complete_totals[index],
            rank=rank_of[complete_totals[index]],
            share=ExactProjectEffortShare(
                numerator_duration_seconds=complete_totals[index],
                denominator_duration_seconds=portfolio_total,
            ),
        )
        for index, project in enumerate(validated.projects)
    )
    return PortfolioProjectEffortRanking(
        portfolio_id=validated.portfolio_id,
        project_count=validated.project_count,
        total_duration_seconds=portfolio_total,
        projects=ranked_projects,
    )
