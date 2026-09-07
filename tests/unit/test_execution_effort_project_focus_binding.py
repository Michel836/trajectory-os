"""V1.36 — Exact binding of a durable accepted focus decision to an
identity-bearing V1.29 selection.

Covers:
* the ``PortfolioProjectEffortFocusBinding`` model (strict / frozen /
  extra-forbid, exact field set, duplicate-ID and count/tuple mismatch
  rejection, duration None-availability coherence);
* ``bind_durable_portfolio_effort_focus_decision`` boundary:
  - genuine V1.35 record and genuine V1.29 selection required;
  - hostile ``model_construct`` records / selections / nested rows / exact
    shares rejected by fresh strict re-validation;
  - every exact compatibility mismatch (portfolio, counts, limit, durations,
    and exact ``None`` availability) rejected;
  - positive / incomplete / zero-total / empty state semantics;
  - exact ``selected_project_ids`` projection and V1.29 tuple-order
    preservation;
  - determinism, input immutability, no repository / UUID / clock
    dependency, V1.30 reuse, and the public API surface.
"""

from __future__ import annotations

import inspect
import types
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError

import trajectory_os.application as app
import trajectory_os.application.execution_effort_project_focus_binding as v136_mod
from trajectory_os.application import (
    ExactProjectEffortShare,
    PortfolioProjectEffortFocusBinding,
    PortfolioProjectEffortFocusBindingError,
    PortfolioProjectEffortFocusDecision,
    PortfolioProjectEffortFocusDecisionRecord,
    PortfolioProjectEffortRank,
    PortfolioProjectEffortShare,
    PortfolioProjectEffortShareSummary,
    PortfolioProjectEffortTopSelection,
    bind_durable_portfolio_effort_focus_decision,
    rank_portfolio_project_effort,
    select_top_ranked_portfolio_project_effort,
    summarize_selected_portfolio_project_effort,
)

PORTFOLIO = uuid.UUID("61616161-6161-4161-8161-616161616161")
OTHER_PORTFOLIO = uuid.UUID("61616161-6161-4161-8161-616161616162")
DECISION_ID = uuid.UUID("62626262-6262-4262-8262-626262626262")
DECIDED_AT = datetime(2025, 7, 1, 8, 30, tzinfo=UTC)

TWO_HOURS = 2 * 3600
ONE_HOUR = 3600
THIRTY_MINUTES = 30 * 60


# ---------------------------------------------------------------------------
# Fixtures — genuine V1.27 -> V1.28 -> V1.29 data and matching V1.35 record.
# ---------------------------------------------------------------------------


def _share_row(
    project_id: uuid.UUID, total: int, portfolio_total: int
) -> PortfolioProjectEffortShare:
    return PortfolioProjectEffortShare(
        project_id=project_id,
        total_duration_seconds=total,
        share=ExactProjectEffortShare(
            numerator_duration_seconds=total,
            denominator_duration_seconds=portfolio_total,
        ),
    )


def _share_summary(project_totals: list[int]) -> PortfolioProjectEffortShareSummary:
    total = sum(project_totals)
    return PortfolioProjectEffortShareSummary(
        portfolio_id=PORTFOLIO,
        project_count=len(project_totals),
        total_duration_seconds=total,
        projects=tuple(_share_row(uuid.uuid4(), t, total) for t in project_totals),
    )


def _incomplete_share_summary() -> PortfolioProjectEffortShareSummary:
    return PortfolioProjectEffortShareSummary(
        portfolio_id=PORTFOLIO,
        project_count=2,
        total_duration_seconds=None,
        projects=(
            PortfolioProjectEffortShare(
                project_id=uuid.uuid4(), total_duration_seconds=ONE_HOUR
            ),
            PortfolioProjectEffortShare(
                project_id=uuid.uuid4(), total_duration_seconds=None
            ),
        ),
    )


def _zero_total_share_summary() -> PortfolioProjectEffortShareSummary:
    return PortfolioProjectEffortShareSummary(
        portfolio_id=PORTFOLIO,
        project_count=2,
        total_duration_seconds=0,
        projects=(
            PortfolioProjectEffortShare(project_id=uuid.uuid4(), total_duration_seconds=0),
            PortfolioProjectEffortShare(project_id=uuid.uuid4(), total_duration_seconds=0),
        ),
    )


def _empty_share_summary() -> PortfolioProjectEffortShareSummary:
    return PortfolioProjectEffortShareSummary(
        portfolio_id=PORTFOLIO,
        project_count=0,
        total_duration_seconds=0,
        projects=(),
    )


def _v128_ranking(project_totals: list[int]):  # type: ignore[no-untyped-def]
    return rank_portfolio_project_effort(_share_summary(project_totals))


def _v129_selection(project_totals: list[int], limit: int) -> PortfolioProjectEffortTopSelection:
    return select_top_ranked_portfolio_project_effort(
        _v128_ranking(project_totals), limit
    )


def _selection(
    incomplete: bool = False, zero: bool = False, empty: bool = False
) -> PortfolioProjectEffortTopSelection:
    if incomplete:
        base = _incomplete_share_summary()
    elif zero:
        base = _zero_total_share_summary()
    elif empty:
        base = _empty_share_summary()
    else:
        raise ValueError("a state flag is required")
    return select_top_ranked_portfolio_project_effort(
        rank_portfolio_project_effort(base), 1
    )


def _decision_matching(
    summary, **overrides: object  # type: ignore[no-untyped-def]
) -> PortfolioProjectEffortFocusDecision:
    """A genuine V1.34 decision whose ACCEPTED side exactly matches the
    supplied V1.30 summary (reference == accepted, so deltas are 0 where a
    delta is representable).

    V1.34 encodes the incomplete (total ``None``) and zero-total (total ``0``)
    domains as ``None`` for every duration / duration-delta field, so this
    helper mirrors those exact ``None``s (a zero-total decision never
    carries scalar ``0`` duration fields).
    """
    total = summary.total_duration_seconds
    if total is None or total == 0:
        ref_sel_d: int | None = None
        ref_rem_d: int | None = None
        acc_sel_d: int | None = None
        acc_rem_d: int | None = None
        sel_delta: int | None = None
        rem_delta: int | None = None
    else:
        ref_sel_d = summary.selected_duration_seconds
        ref_rem_d = summary.remaining_duration_seconds
        acc_sel_d = ref_sel_d
        acc_rem_d = ref_rem_d
        sel_delta = 0
        rem_delta = 0
    base: dict[str, object] = {
        "portfolio_id": summary.portfolio_id,
        "source_project_count": summary.source_project_count,
        "total_duration_seconds": total,
        "reference_requested_limit": summary.requested_limit,
        "reference_selected_project_count": summary.selected_project_count,
        "reference_selected_duration_seconds": ref_sel_d,
        "reference_remaining_duration_seconds": ref_rem_d,
        "accepted_requested_limit": summary.requested_limit,
        "accepted_selected_project_count": summary.selected_project_count,
        "accepted_selected_project_count_delta": 0,
        "accepted_selected_duration_seconds": acc_sel_d,
        "accepted_selected_duration_delta_seconds": sel_delta,
        "accepted_remaining_duration_seconds": acc_rem_d,
        "accepted_remaining_duration_delta_seconds": rem_delta,
    }
    base.update(overrides)
    return PortfolioProjectEffortFocusDecision(**base)


def _record(decision, **overrides: object) -> PortfolioProjectEffortFocusDecisionRecord:  # type: ignore[no-untyped-def]
    base: dict[str, object] = {
        "decision_id": DECISION_ID,
        "decided_at": DECIDED_AT,
        "decision": decision,
    }
    base.update(overrides)
    return PortfolioProjectEffortFocusDecisionRecord(**base)


def _summary_and_selection(project_totals: list[int], limit: int) -> tuple:  # type: ignore[type-arg]
    selection = _v129_selection(project_totals, limit)
    summary = summarize_selected_portfolio_project_effort(selection)
    return summary, selection


def _bound_positive() -> PortfolioProjectEffortFocusBinding:
    summary, selection = _summary_and_selection([TWO_HOURS, ONE_HOUR, THIRTY_MINUTES], 1)
    record = _record(_decision_matching(summary))
    return bind_durable_portfolio_effort_focus_decision(record, selection)


class _ForeignRecord(BaseModel):
    """A different Pydantic model; must never be accepted."""

    model_config = {"frozen": True}

    field: str = "foreign"


# ---------------------------------------------------------------------------
# Binding model invariants.
# ---------------------------------------------------------------------------


class TestBindingModel:
    def test_strict_frozen_and_extra_forbid(self) -> None:
        binding = _bound_positive()
        assert binding.model_config["frozen"] is True
        assert binding.model_config["extra"] == "forbid"
        with pytest.raises(ValidationError):
            PortfolioProjectEffortFocusBinding(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO,
                accepted_requested_limit=1,
                source_project_count=1,
                selected_project_count=1,
                total_duration_seconds=ONE_HOUR,
                selected_duration_seconds=ONE_HOUR,
                remaining_duration_seconds=0,
                selected_project_ids=(PORTFOLIO,),
                unexpected_extra="not-allowed",
            )
        with pytest.raises(ValidationError):
            binding.accepted_requested_limit = 2  # type: ignore[misc]

    def test_rejects_string_and_float_scalars(self) -> None:
        base: dict[str, object] = {
            "decision_id": DECISION_ID,
            "decided_at": DECIDED_AT,
            "portfolio_id": PORTFOLIO,
            "accepted_requested_limit": 1,
            "source_project_count": 1,
            "selected_project_count": 1,
            "total_duration_seconds": ONE_HOUR,
            "selected_duration_seconds": ONE_HOUR,
            "remaining_duration_seconds": 0,
            "selected_project_ids": (PORTFOLIO,),
        }
        for fields in (
            {"accepted_requested_limit": "1"},
            {"total_duration_seconds": 100.0},
            {"selected_project_count": "1"},
        ):
            with pytest.raises(ValidationError):
                PortfolioProjectEffortFocusBinding(**{**base, **fields})

    def test_rejects_bool_scalars(self) -> None:
        base: dict[str, object] = {
            "decision_id": DECISION_ID,
            "decided_at": DECIDED_AT,
            "portfolio_id": PORTFOLIO,
            "accepted_requested_limit": 1,
            "source_project_count": 1,
            "selected_project_count": 1,
            "total_duration_seconds": ONE_HOUR,
            "selected_duration_seconds": ONE_HOUR,
            "remaining_duration_seconds": 0,
            "selected_project_ids": (PORTFOLIO,),
        }
        for fields in ({"total_duration_seconds": True}, {"selected_project_count": False}):
            with pytest.raises(
                ValidationError,
                match="must not be a boolean|Input should be a valid integer",
            ):
                PortfolioProjectEffortFocusBinding(**{**base, **fields})

    def test_rejects_duplicate_selected_project_ids(self) -> None:
        pid = uuid.uuid4()
        with pytest.raises(ValidationError, match="selected_project_ids must be unique"):
            PortfolioProjectEffortFocusBinding(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO,
                accepted_requested_limit=1,
                source_project_count=2,
                selected_project_count=2,
                total_duration_seconds=ONE_HOUR,
                selected_duration_seconds=ONE_HOUR,
                remaining_duration_seconds=0,
                selected_project_ids=(pid, pid),
            )

    def test_rejects_count_tuple_mismatch(self) -> None:
        with pytest.raises(
            ValidationError, match="selected_project_count must equal the length"
        ):
            PortfolioProjectEffortFocusBinding(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO,
                accepted_requested_limit=1,
                source_project_count=3,
                selected_project_count=2,
                total_duration_seconds=ONE_HOUR,
                selected_duration_seconds=ONE_HOUR,
                remaining_duration_seconds=0,
                selected_project_ids=(uuid.uuid4(),),
            )

    def test_rejects_selected_count_exceeding_source_count(self) -> None:
        with pytest.raises(
            ValidationError, match="may not exceed source_project_count"
        ):
            PortfolioProjectEffortFocusBinding(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO,
                accepted_requested_limit=1,
                source_project_count=1,
                selected_project_count=2,
                total_duration_seconds=ONE_HOUR,
                selected_duration_seconds=ONE_HOUR,
                remaining_duration_seconds=0,
                selected_project_ids=(uuid.uuid4(), uuid.uuid4()),
            )

    def test_rejects_partial_duration_none_mix(self) -> None:
        # All three totals must be None (incomplete) or all present
        # (complete): a partial mix is not coherent.
        with pytest.raises(
            ValidationError, match="must be either all None"
        ):
            PortfolioProjectEffortFocusBinding(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                portfolio_id=PORTFOLIO,
                accepted_requested_limit=1,
                source_project_count=2,
                selected_project_count=1,
                total_duration_seconds=ONE_HOUR,
                selected_duration_seconds=None,
                remaining_duration_seconds=0,
                selected_project_ids=(uuid.uuid4(),),
            )


# ---------------------------------------------------------------------------
# Boundary: inputs.
# ---------------------------------------------------------------------------


class TestBoundaryInput:
    def test_requires_genuine_v135_record(self) -> None:
        selection = _v129_selection([ONE_HOUR, THIRTY_MINUTES], 1)
        with pytest.raises(
            PortfolioProjectEffortFocusBindingError, match="genuine V1.35"
        ):
            bind_durable_portfolio_effort_focus_decision(None, selection)  # type: ignore[arg-type]
        with pytest.raises(
            PortfolioProjectEffortFocusBindingError, match="genuine V1.35"
        ):
            bind_durable_portfolio_effort_focus_decision("nope", selection)  # type: ignore[arg-type]
        with pytest.raises(
            PortfolioProjectEffortFocusBindingError, match="genuine V1.35"
        ):
            bind_durable_portfolio_effort_focus_decision(
                _ForeignRecord(), selection
            )
        # A duck object exposing the same attribute names is foreign.
        summary, sel = _summary_and_selection([ONE_HOUR], 1)
        record = _record(_decision_matching(summary))
        duck = types.SimpleNamespace(**record.model_dump(mode="python"))
        with pytest.raises(
            PortfolioProjectEffortFocusBindingError, match="genuine V1.35"
        ):
            bind_durable_portfolio_effort_focus_decision(duck, sel)  # type: ignore[arg-type]

    def test_requires_genuine_v129_selection(self) -> None:
        summary, sel = _summary_and_selection([ONE_HOUR, THIRTY_MINUTES], 1)
        record = _record(_decision_matching(summary))
        with pytest.raises(
            PortfolioProjectEffortFocusBindingError, match="genuine V1.29"
        ):
            bind_durable_portfolio_effort_focus_decision(record, None)  # type: ignore[arg-type]
        with pytest.raises(
            PortfolioProjectEffortFocusBindingError, match="genuine V1.29"
        ):
            bind_durable_portfolio_effort_focus_decision(record, "nope")  # type: ignore[arg-type]
        with pytest.raises(
            PortfolioProjectEffortFocusBindingError, match="genuine V1.29"
        ):
            bind_durable_portfolio_effort_focus_decision(
                record, sel.to_payload()
            )
        duck = types.SimpleNamespace(**sel.model_dump(mode="python"))
        with pytest.raises(
            PortfolioProjectEffortFocusBindingError, match="genuine V1.29"
        ):
            bind_durable_portfolio_effort_focus_decision(record, duck)  # type: ignore[arg-type]

    def test_rejects_hostile_v135_record(self) -> None:
        # A model_construct record carrying a tampered nested decision
        # (accepted_selected + accepted_remaining != total) must be
        # rejected by fresh strict re-validation.
        _summary, selection = _summary_and_selection([ONE_HOUR, THIRTY_MINUTES], 1)
        hostile_decision = PortfolioProjectEffortFocusDecision.model_construct(
            portfolio_id=PORTFOLIO,
            source_project_count=2,
            total_duration_seconds=ONE_HOUR,
            reference_requested_limit=1,
            reference_selected_project_count=1,
            reference_selected_duration_seconds=ONE_HOUR,
            reference_remaining_duration_seconds=0,
            accepted_requested_limit=1,
            accepted_selected_project_count=1,
            accepted_selected_project_count_delta=0,
            accepted_selected_duration_seconds=ONE_HOUR,
            accepted_selected_duration_delta_seconds=0,
            accepted_remaining_duration_seconds=ONE_HOUR,  # 3600+3600 != 3600
            accepted_remaining_duration_delta_seconds=0,
        )
        hostile_record = PortfolioProjectEffortFocusDecisionRecord.model_construct(
            decision_id=DECISION_ID,
            decided_at=DECIDED_AT,
            decision=hostile_decision,
        )
        with pytest.raises(
            PortfolioProjectEffortFocusBindingError, match="failed strict re-validation"
        ):
            bind_durable_portfolio_effort_focus_decision(hostile_record, selection)

    def test_rejects_hostile_v129_selection(self) -> None:
        # Hostile model_construct selection with a count/source mismatch
        # must be rejected by fresh strict re-validation.
        summary, _sel = _summary_and_selection([ONE_HOUR], 1)
        record = _record(_decision_matching(summary))
        hostile = PortfolioProjectEffortTopSelection.model_construct(
            portfolio_id=PORTFOLIO,
            requested_limit=1,
            source_project_count=1,
            selected_project_count=2,
            total_duration_seconds=ONE_HOUR,
            projects=(),
        )
        with pytest.raises(
            PortfolioProjectEffortFocusBindingError, match="strict re-validation"
        ):
            bind_durable_portfolio_effort_focus_decision(record, hostile)

    def test_rejects_hostile_nested_v129_project(self) -> None:
        # A constructed row with a rank but no complete total/share is a
        # V1.29 invariant violation only fresh revalidation can catch.
        summary, _sel = _summary_and_selection([ONE_HOUR], 1)
        record = _record(_decision_matching(summary))
        hostile_row = PortfolioProjectEffortRank.model_construct(
            project_id=uuid.uuid4(),
            total_duration_seconds=None,
            rank=1,
            share=None,
        )
        hostile_selection = PortfolioProjectEffortTopSelection.model_construct(
            portfolio_id=PORTFOLIO,
            requested_limit=1,
            source_project_count=1,
            selected_project_count=1,
            total_duration_seconds=ONE_HOUR,
            projects=(hostile_row,),
        )
        with pytest.raises(
            PortfolioProjectEffortFocusBindingError, match="strict re-validation"
        ):
            bind_durable_portfolio_effort_focus_decision(record, hostile_selection)

    def test_rejects_hostile_nested_exact_share(self) -> None:
        # A constructed exact share whose numerator exceeds its denominator
        # must be rejected.
        summary, _sel = _summary_and_selection([ONE_HOUR], 1)
        record = _record(_decision_matching(summary))
        hostile_share = ExactProjectEffortShare.model_construct(
            numerator_duration_seconds=TWO_HOURS,
            denominator_duration_seconds=ONE_HOUR,
        )
        hostile_row = PortfolioProjectEffortRank.model_construct(
            project_id=uuid.uuid4(),
            total_duration_seconds=ONE_HOUR,
            rank=1,
            share=hostile_share,
        )
        hostile_selection = PortfolioProjectEffortTopSelection.model_construct(
            portfolio_id=PORTFOLIO,
            requested_limit=1,
            source_project_count=1,
            selected_project_count=1,
            total_duration_seconds=ONE_HOUR,
            projects=(hostile_row,),
        )
        with pytest.raises(
            PortfolioProjectEffortFocusBindingError, match="strict re-validation"
        ):
            bind_durable_portfolio_effort_focus_decision(record, hostile_selection)


# ---------------------------------------------------------------------------
# Boundary: exact compatibility mismatches.
# ---------------------------------------------------------------------------


class TestCompatibilityMismatch:
    def test_portfolio_mismatch_rejected(self) -> None:
        summary, selection = _summary_and_selection([ONE_HOUR], 1)
        record = _record(
            _decision_matching(summary, portfolio_id=OTHER_PORTFOLIO)
        )
        with pytest.raises(
            PortfolioProjectEffortFocusBindingError, match="portfolio_id"
        ):
            # The record's portfolio now points to OTHER_PORTFOLIO, but the
            # selection still binds PORTFOLIO -> mismatch.
            bind_durable_portfolio_effort_focus_decision(record, selection)

    def test_source_count_mismatch_rejected(self) -> None:
        summary, selection = _summary_and_selection([ONE_HOUR, THIRTY_MINUTES], 1)
        record = _record(
            _decision_matching(summary, source_project_count=summary.source_project_count + 1)
        )
        with pytest.raises(
            PortfolioProjectEffortFocusBindingError, match="source_project_count"
        ):
            bind_durable_portfolio_effort_focus_decision(record, selection)

    def test_requested_limit_mismatch_rejected(self) -> None:
        summary, selection = _summary_and_selection([ONE_HOUR, THIRTY_MINUTES], 1)
        record = _record(
            _decision_matching(summary, accepted_requested_limit=2)
        )
        with pytest.raises(
            PortfolioProjectEffortFocusBindingError, match="requested_limit"
        ):
            bind_durable_portfolio_effort_focus_decision(record, selection)

    def test_selected_count_mismatch_rejected(self) -> None:
        summary, selection = _summary_and_selection(
            [TWO_HOURS, ONE_HOUR, THIRTY_MINUTES], 1
        )
        # selected_project_count_delta stays 0 which is only consistent with
        # reference==accepted, but accepted != V1.30 selected triggers the
        # compatibility rejection before the decision model is re-validated.
        record = _record(
            _decision_matching(
                summary,
                accepted_selected_project_count=summary.selected_project_count + 1,
                reference_selected_project_count=(
                    summary.selected_project_count + 1
                ),
            )
        )
        with pytest.raises(
            PortfolioProjectEffortFocusBindingError, match="selected_project_count"
        ):
            bind_durable_portfolio_effort_focus_decision(record, selection)

    def test_total_none_value_mismatch_rejected(self) -> None:
        # A genuinely valid incomplete (total None) decision whose scalar
        # counts match a positive-total selection but whose total is None
        # must be rejected on the exact total check (including None
        # availability).
        summary, selection = _summary_and_selection([ONE_HOUR], 1)
        record = _record(
            _decision_matching(
                summary,
                total_duration_seconds=None,
                reference_selected_duration_seconds=None,
                reference_remaining_duration_seconds=None,
                accepted_selected_duration_seconds=None,
                accepted_selected_duration_delta_seconds=None,
                accepted_remaining_duration_seconds=None,
                accepted_remaining_duration_delta_seconds=None,
            )
        )
        with pytest.raises(
            PortfolioProjectEffortFocusBindingError, match="total_duration_seconds"
        ):
            bind_durable_portfolio_effort_focus_decision(record, selection)

    def test_total_value_value_mismatch_rejected(self) -> None:
        summary, selection = _summary_and_selection([TWO_HOURS, ONE_HOUR], 1)
        record = _record(
            _decision_matching(
                summary,
                total_duration_seconds=summary.total_duration_seconds + 1,
                reference_remaining_duration_seconds=(
                    summary.remaining_duration_seconds + 1
                ),
                reference_selected_duration_seconds=summary.selected_duration_seconds,
                accepted_remaining_duration_seconds=(
                    summary.remaining_duration_seconds + 1
                ),
            )
        )
        with pytest.raises(
            PortfolioProjectEffortFocusBindingError, match="total_duration_seconds"
        ):
            bind_durable_portfolio_effort_focus_decision(record, selection)

    def test_selected_duration_mismatch_rejected(self) -> None:
        # The V1.34 decision enforces accepted_selected + accepted_remaining
        # == total, so bumping the selected side by one requires dropping the
        # remaining side by one to keep the decision genuinely valid and thus
        # exercise the (earlier) selected-duration compatibility check.
        summary, selection = _summary_and_selection([TWO_HOURS, ONE_HOUR], 1)
        record = _record(
            _decision_matching(
                summary,
                accepted_selected_duration_seconds=(
                    summary.selected_duration_seconds + 1
                ),
                accepted_remaining_duration_seconds=(
                    summary.remaining_duration_seconds - 1
                ),
                reference_selected_duration_seconds=(
                    summary.selected_duration_seconds + 1
                ),
                reference_remaining_duration_seconds=(
                    summary.remaining_duration_seconds - 1
                ),
            )
        )
        with pytest.raises(
            PortfolioProjectEffortFocusBindingError,
            match="selected_duration_seconds",
        ):
            bind_durable_portfolio_effort_focus_decision(record, selection)

    def test_remaining_duration_mismatch_rejected(self) -> None:
        # A decision whose remaining side differs from the V1.30 summary is
        # rejected. Because the V1.34 invariants force accepted_remaining to
        # equal total - accepted_selected, changing it necessarily changes the
        # total or selected side as well; the boundary rejects such a decision
        # (the remaining branch is the final defensive guard).
        summary, selection = _summary_and_selection([TWO_HOURS, ONE_HOUR], 1)
        record = _record(
            _decision_matching(
                summary,
                accepted_remaining_duration_seconds=(
                    summary.remaining_duration_seconds + 1
                ),
                reference_remaining_duration_seconds=(
                    summary.remaining_duration_seconds + 1
                ),
                total_duration_seconds=summary.total_duration_seconds + 1,
            )
        )
        with pytest.raises(PortfolioProjectEffortFocusBindingError):
            bind_durable_portfolio_effort_focus_decision(record, selection)


# ---------------------------------------------------------------------------
# Boundary: exact semantics and states.
# ---------------------------------------------------------------------------


class TestBoundarySemantics:
    def test_positive_state_exact_projection(self) -> None:
        summary, selection = _summary_and_selection(
            [TWO_HOURS, ONE_HOUR, THIRTY_MINUTES], 1
        )
        record = _record(_decision_matching(summary))
        binding = bind_durable_portfolio_effort_focus_decision(record, selection)

        assert binding.decision_id == DECISION_ID
        assert binding.decided_at == DECIDED_AT
        assert binding.portfolio_id == summary.portfolio_id
        assert binding.accepted_requested_limit == summary.requested_limit
        assert binding.source_project_count == summary.source_project_count
        assert binding.selected_project_count == summary.selected_project_count
        assert binding.total_duration_seconds == summary.total_duration_seconds
        assert binding.selected_duration_seconds == summary.selected_duration_seconds
        assert (
            binding.remaining_duration_seconds
            == summary.remaining_duration_seconds
        )
        assert len(binding.selected_project_ids) == binding.selected_project_count
        assert (
            binding.selected_project_ids
            == tuple(p.project_id for p in selection.projects)
        )

    def test_exact_selected_project_ids_projection(self) -> None:
        summary, selection = _summary_and_selection(
            [TWO_HOURS, ONE_HOUR, THIRTY_MINUTES, ONE_HOUR], 2
        )
        record = _record(_decision_matching(summary))
        binding = bind_durable_portfolio_effort_focus_decision(record, selection)
        expected = tuple(project.project_id for project in selection.projects)
        assert binding.selected_project_ids == expected
        assert len(expected) == binding.selected_project_count

    def test_v129_tuple_order_preserved(self) -> None:
        # Build two selections with the same multiset of totals but a
        # distinct authoritative order; the binding must mirror each one's
        # exact tuple order (no sorting / UUID ordering).
        a_summary, a_selection = _summary_and_selection(
            [ONE_HOUR, TWO_HOURS, THIRTY_MINUTES], 3
        )
        b_summary, b_selection = _summary_and_selection(
            [THIRTY_MINUTES, TWO_HOURS, ONE_HOUR], 3
        )
        a_binding = bind_durable_portfolio_effort_focus_decision(
            _record(_decision_matching(a_summary)), a_selection
        )
        b_binding = bind_durable_portfolio_effort_focus_decision(
            _record(_decision_matching(b_summary)), b_selection
        )
        assert a_binding.selected_project_ids == tuple(
            p.project_id for p in a_selection.projects
        )
        assert b_binding.selected_project_ids == tuple(
            p.project_id for p in b_selection.projects
        )
        # The two tuples are order-distinct (fresh uuid4 rows), proving the
        # binding preserves each V1.29 order rather than reordering.
        assert a_binding.selected_project_ids != b_binding.selected_project_ids

    def test_incomplete_state_exact_none_and_empty_tuple(self) -> None:
        # A genuine incomplete V1.29 selection AND an incomplete V1.35 record
        # are both encoded with exact ``None`` durations, so they bind and
        # project an empty identity tuple with all-``None`` durations.
        selection = _selection(incomplete=True)
        summary = summarize_selected_portfolio_project_effort(selection)
        record = _record(_decision_matching(summary))
        binding = bind_durable_portfolio_effort_focus_decision(record, selection)
        assert binding.selected_project_ids == ()
        assert binding.selected_project_count == 0
        assert binding.total_duration_seconds is None
        assert binding.selected_duration_seconds is None
        assert binding.remaining_duration_seconds is None

    def test_zero_total_genuine_record_rejected(self) -> None:
        # V1.34 encodes a zero-total decision with ``None`` duration fields,
        # while V1.30 encodes a zero-total selection with scalar ``0``. Under
        # the exact (including-None) compatibility rule a genuine zero-total
        # V1.35 record therefore cannot bind here — V1.36 rejects it rather
        # than reconciling the two encodings (those semantics are frozen).
        selection = _selection(zero=True)
        summary = summarize_selected_portfolio_project_effort(selection)
        assert summary.total_duration_seconds == 0
        record = _record(_decision_matching(summary))
        with pytest.raises(PortfolioProjectEffortFocusBindingError):
            bind_durable_portfolio_effort_focus_decision(record, selection)

    def _model_state(self, total: int | None) -> PortfolioProjectEffortFocusBinding:
        if total is None:
            fields: dict[str, object] = {
                "total_duration_seconds": None,
                "selected_duration_seconds": None,
                "remaining_duration_seconds": None,
            }
        else:
            fields = {
                "total_duration_seconds": total,
                "selected_duration_seconds": 0,
                "remaining_duration_seconds": total,
            }
        return PortfolioProjectEffortFocusBinding(
            decision_id=DECISION_ID,
            decided_at=DECIDED_AT,
            portfolio_id=PORTFOLIO,
            accepted_requested_limit=1,
            source_project_count=2,
            selected_project_count=0,
            selected_project_ids=(),
            **fields,
        )

    def test_binding_model_zero_total_state_semantics(self) -> None:
        # The binding MODEL fully expresses the zero-total / empty state:
        # exact zero durations + empty identity tuple + no fabricated ids.
        binding = self._model_state(0)
        assert binding.selected_project_ids == ()
        assert binding.selected_project_count == 0
        assert binding.total_duration_seconds == 0
        assert binding.selected_duration_seconds == 0
        assert binding.remaining_duration_seconds == 0
        assert len(binding.selected_project_ids) == 0

    def test_binding_model_incomplete_state_semantics(self) -> None:
        # The binding MODEL fully expresses the incomplete state: exact
        # ``None`` durations + empty identity tuple + no fabricated ids.
        binding = self._model_state(None)
        assert binding.selected_project_ids == ()
        assert binding.selected_project_count == 0
        assert binding.total_duration_seconds is None
        assert binding.selected_duration_seconds is None
        assert binding.remaining_duration_seconds is None


# ---------------------------------------------------------------------------
# Boundary: behavior / discipline.
# ---------------------------------------------------------------------------


class TestBoundaryBehavior:
    def test_repeated_identical_calls_value_identical(self) -> None:
        summary, selection = _summary_and_selection(
            [TWO_HOURS, ONE_HOUR, ONE_HOUR, THIRTY_MINUTES], 2
        )
        record = _record(_decision_matching(summary))
        first = bind_durable_portfolio_effort_focus_decision(record, selection)
        second = bind_durable_portfolio_effort_focus_decision(record, selection)
        third = bind_durable_portfolio_effort_focus_decision(record, selection)
        assert first == second == third
        assert (
            first.model_dump(mode="python")
            == second.model_dump(mode="python")
            == third.model_dump(mode="python")
        )

    def test_input_objects_not_mutated(self) -> None:
        summary, selection = _summary_and_selection([TWO_HOURS, ONE_HOUR], 1)
        record = _record(_decision_matching(summary))
        selection_before = selection.model_dump(mode="python")
        record_before = record.model_dump(mode="python")
        bind_durable_portfolio_effort_focus_decision(record, selection)
        assert selection.model_dump(mode="python") == selection_before
        assert record.model_dump(mode="python") == record_before
        assert selection.selected_project_count == summary.selected_project_count

    def test_no_repository_persistence_dependency(self) -> None:
        # The boundary takes exactly two arguments — no repository, no
        # keyword-only persistence surface.
        signature = inspect.signature(bind_durable_portfolio_effort_focus_decision)
        assert list(signature.parameters) == [
            "durable_decision",
            "selection",
        ]
        # And the module exposes no repository / provider / persistence symbol.
        module_symbols = set(vars(v136_mod))
        assert not any(
            "Repository" in name or "Provider" in name
            for name in module_symbols
        )
        # It only touches the frozen V1.35 record model plus the V1.29 / V1.30
        # boundaries — nothing from a SQL / runtime / AI surface.
        for forbidden in ("sqlite", "ai_engine", "runtime_boundary", "provider"):
            assert not any(
                forbidden in name.lower() for name in module_symbols
            )

    def test_no_hidden_uuid_or_time_generation(self) -> None:
        # decision_id / decided_at are copied from the supplied durable
        # record, not generated from a clock or uuid4().
        summary, selection = _summary_and_selection([ONE_HOUR], 1)
        record = _record(_decision_matching(summary))
        binding = bind_durable_portfolio_effort_focus_decision(record, selection)
        assert binding.decision_id == record.decision_id
        assert binding.decided_at == record.decided_at
        source = inspect.getsource(v136_mod)
        assert "uuid4" not in source
        assert "datetime.now" not in source
        assert "utcnow" not in source

    def test_v130_summary_boundary_is_reused(self) -> None:
        # The binding's scalar fields must EXACTLY equal the independent
        # V1.30 summary of the same selection — proof that V1.30 arithmetic
        # is reused, not duplicated.
        summary, selection = _summary_and_selection(
            [TWO_HOURS, ONE_HOUR, ONE_HOUR, THIRTY_MINUTES], 2
        )
        record = _record(_decision_matching(summary))
        binding = bind_durable_portfolio_effort_focus_decision(record, selection)
        independent = summarize_selected_portfolio_project_effort(selection)
        assert binding.portfolio_id == independent.portfolio_id
        assert binding.accepted_requested_limit == independent.requested_limit
        assert binding.source_project_count == independent.source_project_count
        assert binding.selected_project_count == independent.selected_project_count
        assert binding.total_duration_seconds == independent.total_duration_seconds
        assert (
            binding.selected_duration_seconds
            == independent.selected_duration_seconds
        )
        assert (
            binding.remaining_duration_seconds
            == independent.remaining_duration_seconds
        )


# ---------------------------------------------------------------------------
# Public API surface.
# ---------------------------------------------------------------------------


class TestPublicApi:
    def test_application_exports_v136_surface(self) -> None:
        assert "PortfolioProjectEffortFocusBinding" in app.__all__
        assert "PortfolioProjectEffortFocusBindingError" in app.__all__
        assert "bind_durable_portfolio_effort_focus_decision" in app.__all__
        assert app.PortfolioProjectEffortFocusBinding is PortfolioProjectEffortFocusBinding
        assert (
            app.PortfolioProjectEffortFocusBindingError
            is PortfolioProjectEffortFocusBindingError
        )
        assert (
            app.bind_durable_portfolio_effort_focus_decision
            is bind_durable_portfolio_effort_focus_decision
        )

    def test_dependency_discipline_no_forbidden_imports(self) -> None:
        source = inspect.getsource(v136_mod)
        for forbidden in (
            "focus_scenario_set",
            "selection_comparison",
            "sqlite",
            "PortfolioRepository",
        ):
            assert forbidden.lower() not in source.lower()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
