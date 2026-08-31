"""Unit tests for the V1.35 durable focus-decision persistence boundary.

Covers: the strict immutable record model (frozen, extra-forbid, aware
timestamp, genuine-and-revalidatable nested V1.34 decision, exact field
set), the exact command semantics (strict pre-I/O validation in order,
``add`` called exactly once with the exact record, explicit
``decision_id`` / ``decided_at`` preserved), every failure path
appending exactly zero records, repository failure propagation unchanged,
and the public surface / signature / no-hidden-defaults invariants.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ValidationError

from trajectory_os.application.execution_effort_project_focus_decision import (
    PortfolioProjectEffortFocusDecision,
)
from trajectory_os.application.execution_effort_project_focus_decision_persistence import (
    DurablePortfolioProjectEffortFocusDecisionError,
    PortfolioProjectEffortFocusDecisionRecord,
    PortfolioProjectEffortFocusDecisionRepository,
    record_portfolio_effort_focus_decision_durably,
)

PORTFOLIO = UUID("61616161-6161-4161-8161-616161616161")
OTHER_PORTFOLIO = UUID("61616161-6161-4161-8161-616161616162")
DECISION_ID = UUID("62626262-6262-4262-8262-626262626262")
DECIDED_AT = datetime(2025, 7, 1, 8, 30, tzinfo=UTC)
DECIDED_AT_OFFSET = datetime(
    2025, 7, 1, 10, 30, tzinfo=timezone(timedelta(hours=2))
)


class _ForeignModel(BaseModel):
    """A different Pydantic model; must never be accepted as the decision."""

    model_config = {"frozen": True}

    field: str = "foreign"


class _FakeRepository:
    """Structural ``add``/``list_history`` fake recording every append."""

    def __init__(self) -> None:
        self.added: list[object] = []
        self.fail_with: Exception | None = None

    def add(self, record: PortfolioProjectEffortFocusDecisionRecord) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.added.append(record)

    def list_history(
        self, portfolio_id: UUID
    ) -> tuple[PortfolioProjectEffortFocusDecisionRecord, ...]:
        return ()


def _decision_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "portfolio_id": PORTFOLIO,
        "source_project_count": 3,
        "total_duration_seconds": 10000,
        "reference_requested_limit": 1,
        "reference_selected_project_count": 1,
        "reference_selected_duration_seconds": 6000,
        "reference_remaining_duration_seconds": 4000,
        "accepted_requested_limit": 1,
        "accepted_selected_project_count": 1,
        "accepted_selected_project_count_delta": 0,
        "accepted_selected_duration_seconds": 6000,
        "accepted_selected_duration_delta_seconds": 0,
        "accepted_remaining_duration_seconds": 4000,
        "accepted_remaining_duration_delta_seconds": 0,
    }
    base.update(overrides)
    return base


def _decision(**overrides: object) -> PortfolioProjectEffortFocusDecision:
    return PortfolioProjectEffortFocusDecision(**_decision_kwargs(**overrides))


def _hostile_constructed_decision() -> PortfolioProjectEffortFocusDecision:
    """A model_construct() state genuine construction could never produce.

    The nested ``portfolio_id`` is an ``int`` (strict UUID violation in
    payload mode) — real construction fails, ``model_construct`` skips
    validation. Fresh strict re-validation MUST reject it.
    """
    return PortfolioProjectEffortFocusDecision.model_construct(
        portfolio_id=123,  # type: ignore[arg-type]
        source_project_count=3,
        reference_requested_limit=1,
        reference_selected_project_count=1,
        reference_selected_duration_seconds=6000,
        reference_remaining_duration_seconds=4000,
        accepted_requested_limit=1,
        accepted_selected_project_count=1,
        accepted_selected_project_count_delta=0,
        accepted_selected_duration_seconds=6000,
        accepted_selected_duration_delta_seconds=0,
        accepted_remaining_duration_seconds=4000,
        accepted_remaining_duration_delta_seconds=0,
    )


def _hostile_constructed_invariant() -> PortfolioProjectEffortFocusDecision:
    """A well-typed model_construct() state that breaks the invariants."""
    return PortfolioProjectEffortFocusDecision.model_construct(
        portfolio_id=PORTFOLIO,
        source_project_count=3,
        reference_requested_limit=1,
        reference_selected_project_count=9,  # > source_project_count
        reference_selected_duration_seconds=6000,
        reference_remaining_duration_seconds=4000,
        accepted_requested_limit=1,
        accepted_selected_project_count=1,
        accepted_selected_project_count_delta=0,
        accepted_selected_duration_seconds=6000,
        accepted_selected_duration_delta_seconds=0,
        accepted_remaining_duration_seconds=4000,
        accepted_remaining_duration_delta_seconds=0,
    )


# ---------------------------------------------------------------------------
# Record model.
# ---------------------------------------------------------------------------


class TestRecordModel:
    def test_exact_three_fields(self) -> None:
        assert set(PortfolioProjectEffortFocusDecisionRecord.model_fields) == {
            "decision_id",
            "decided_at",
            "decision",
        }

    def test_strict_frozen_extra_forbid(self) -> None:
        config = PortfolioProjectEffortFocusDecisionRecord.model_config
        assert config.get("strict") is True
        assert config.get("frozen") is True
        assert config.get("extra") == "forbid"

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioProjectEffortFocusDecisionRecord(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                decision=_decision(),
                status="final",
            )

    def test_rejects_string_decision_id(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioProjectEffortFocusDecisionRecord(
                decision_id=str(DECISION_ID),
                decided_at=DECIDED_AT,
                decision=_decision(),
            )

    def test_rejects_naive_decided_at(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioProjectEffortFocusDecisionRecord(
                decision_id=DECISION_ID,
                decided_at=datetime(2025, 7, 1, 8, 30),
                decision=_decision(),
            )

    def test_dict_decision_coerces_to_fully_validated_genuine_decision(self) -> None:
        """A dict payload becomes a fully validated genuine decision.

        At the record level, strict-mode conversion of the nested dict
        enforces every V1.34 invariant and strict scalar, so the result is
        a genuine :class:`PortfolioProjectEffortFocusDecision`. The
        authority-level rejection of dict payloads (no coercion of the
        argument itself) is enforced at the command boundary — see
        ``TestCommand.test_rejects_non_genuine_decision_payloads``.
        """
        record = PortfolioProjectEffortFocusDecisionRecord(
            decision_id=DECISION_ID,
            decided_at=DECIDED_AT,
            decision=_decision_kwargs(),  # type: ignore[arg-type]
        )
        assert isinstance(record.decision, PortfolioProjectEffortFocusDecision)
        assert record.decision == _decision()

    def test_json_string_decision_rejected(self) -> None:
        import json

        with pytest.raises((ValidationError, TypeError)):
            PortfolioProjectEffortFocusDecisionRecord(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                decision=json.dumps(_decision_kwargs()),  # type: ignore[arg-type]
            )

    def test_rejects_none_decision(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioProjectEffortFocusDecisionRecord(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                decision=None,  # type: ignore[arg-type]
            )

    def test_rejects_foreign_decision_model(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioProjectEffortFocusDecisionRecord(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                decision=_ForeignModel(),  # type: ignore[arg-type]
            )

    def test_rejects_hostile_constructed_nested_decision(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioProjectEffortFocusDecisionRecord(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                decision=_hostile_constructed_decision(),
            )

    def test_rejects_hostile_constructed_nested_invariant(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioProjectEffortFocusDecisionRecord(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                decision=_hostile_constructed_invariant(),
            )

    def test_valid_record_frozen(self) -> None:
        record = PortfolioProjectEffortFocusDecisionRecord(
            decision_id=DECISION_ID,
            decided_at=DECIDED_AT,
            decision=_decision(),
        )
        with pytest.raises(ValidationError):
            record.decision_id = uuid4()  # type: ignore[misc]

    def test_offset_preserved_through_construction(self) -> None:
        record = PortfolioProjectEffortFocusDecisionRecord(
            decision_id=DECISION_ID,
            decided_at=DECIDED_AT_OFFSET,
            decision=_decision(),
        )
        assert record.decided_at == DECIDED_AT_OFFSET
        assert record.decided_at.utcoffset() == timedelta(hours=2)
        # Round-tripping through validation keeps the exact offset.
        again = PortfolioProjectEffortFocusDecisionRecord.model_validate(
            record.model_dump(mode="python"), strict=True
        )
        assert again.decided_at.isoformat() == DECIDED_AT_OFFSET.isoformat()

    def test_genuine_nested_roundtrip_survives(self) -> None:
        decision = _decision()
        record = PortfolioProjectEffortFocusDecisionRecord(
            decision_id=DECISION_ID,
            decided_at=DECIDED_AT,
            decision=decision,
        )
        assert record.decision == decision
        assert (
            record.decision.model_dump(mode="python")
            == decision.model_dump(mode="python")
        )


# ---------------------------------------------------------------------------
# Command.
# ---------------------------------------------------------------------------


class TestCommand:
    def test_appends_exactly_once_and_returns_exact_record(
        self,
    ) -> None:
        repository = _FakeRepository()
        decision = _decision()
        returned = record_portfolio_effort_focus_decision_durably(
            DECISION_ID, DECIDED_AT, decision, repository=repository
        )
        assert len(repository.added) == 1
        assert returned is repository.added[0]
        assert isinstance(
            repository.added[0], PortfolioProjectEffortFocusDecisionRecord
        )
        assert repository.added[0].decision_id == DECISION_ID
        assert repository.added[0].decided_at == DECIDED_AT
        assert (
            repository.added[0].decision.model_dump(mode="python")
            == decision.model_dump(mode="python")
        )

    def test_caller_supplied_id_and_timestamp_preserved(
        self,
    ) -> None:
        repository = _FakeRepository()
        returned = record_portfolio_effort_focus_decision_durably(
            DECISION_ID, DECIDED_AT_OFFSET, _decision(), repository=repository
        )
        assert returned.decision_id == DECISION_ID
        assert returned.decided_at == DECIDED_AT_OFFSET
        assert returned.decided_at.utcoffset() == timedelta(hours=2)

    def test_rejects_non_uuid_decision_id(self) -> None:
        repository = _FakeRepository()
        for bad in (
            str(DECISION_ID),
            bytes(DECISION_ID.bytes),
            DECISION_ID.int,
            None,
        ):
            with pytest.raises(DurablePortfolioProjectEffortFocusDecisionError):
                record_portfolio_effort_focus_decision_durably(
                    bad, DECIDED_AT, _decision(), repository=repository
                )
        assert repository.added == []

    def test_rejects_non_datetime_or_naive_decided_at(self) -> None:
        repository = _FakeRepository()
        for bad in (
            "2025-07-01T08:30:00Z",
            1720000000,
            None,
            datetime(2025, 7, 1, 8, 30),
        ):
            with pytest.raises(DurablePortfolioProjectEffortFocusDecisionError):
                record_portfolio_effort_focus_decision_durably(
                    DECISION_ID, bad, _decision(), repository=repository
                )
        assert repository.added == []

    def test_rejects_non_genuine_decision_payloads(self) -> None:
        repository = _FakeRepository()
        for bad in (
            _decision_kwargs(),
            {"portfolio_id": PORTFOLIO},
            "decision",
            None,
            _ForeignModel(),
            PortfolioProjectEffortFocusDecision.model_dump(
                _decision(), mode="json"
            ),
        ):
            with pytest.raises(DurablePortfolioProjectEffortFocusDecisionError):
                record_portfolio_effort_focus_decision_durably(
                    DECISION_ID, DECIDED_AT, bad, repository=repository
                )
        assert repository.added == []

    def test_rejects_hostile_constructed_decision(self) -> None:
        repository = _FakeRepository()
        with pytest.raises(DurablePortfolioProjectEffortFocusDecisionError):
            record_portfolio_effort_focus_decision_durably(
                DECISION_ID,
                DECIDED_AT,
                _hostile_constructed_decision(),
                repository=repository,
            )
        with pytest.raises(DurablePortfolioProjectEffortFocusDecisionError):
            record_portfolio_effort_focus_decision_durably(
                DECISION_ID,
                DECIDED_AT,
                _hostile_constructed_invariant(),
                repository=repository,
            )
        assert repository.added == []

    def test_repository_failure_propagates_unchanged(self) -> None:
        repository = _FakeRepository()
        repository.fail_with = RuntimeError("storage down")
        with pytest.raises(RuntimeError, match="storage down"):
            record_portfolio_effort_focus_decision_durably(
                DECISION_ID, DECIDED_AT, _decision(), repository=repository
            )
        assert repository.added == []

    def test_boundary_error_is_narrow_value_error(self) -> None:
        assert issubclass(DurablePortfolioProjectEffortFocusDecisionError, ValueError)
        assert not issubclass(DurablePortfolioProjectEffortFocusDecisionError, TypeError)


# ---------------------------------------------------------------------------
# Public surface.
# ---------------------------------------------------------------------------


class TestPublicSurface:
    def test_package_exports(self) -> None:
        import trajectory_os.application as application

        for name in (
            "DurablePortfolioProjectEffortFocusDecisionError",
            "PortfolioProjectEffortFocusDecisionRecord",
            "PortfolioProjectEffortFocusDecisionRepository",
            "record_portfolio_effort_focus_decision_durably",
        ):
            assert name in application.__all__
            assert getattr(application, name) is not None

    def test_adapter_exports(self) -> None:
        import trajectory_os.adapters.persistence as adapters

        for name in (
            "SqlitePortfolioProjectEffortFocusDecisionRepository",
            "DuplicatePortfolioProjectEffortFocusDecisionError",
        ):
            assert name in adapters.__all__
            assert getattr(adapters, name) is not None

    def test_command_signature_and_no_hidden_defaults(self) -> None:
        signature = inspect.signature(record_portfolio_effort_focus_decision_durably)
        parameters = list(signature.parameters)
        assert parameters == ["decision_id", "decided_at", "decision", "repository"]
        for name in ("decision_id", "decided_at", "decision"):
            assert (
                signature.parameters[name].default is inspect.Parameter.empty
            )
        assert (
            signature.parameters["repository"].kind
            is inspect.Parameter.KEYWORD_ONLY
        )

    def test_protocol_surface(self) -> None:
        methods = {
            name
            for name in dir(PortfolioProjectEffortFocusDecisionRepository)
            if not name.startswith("_")
        }
        assert methods == {"add", "list_history"}

    def test_modules_import_only_allowed_dependencies(self) -> None:
        import ast

        import trajectory_os.adapters.persistence.sqlite_portfolio_project_focus_decisions as a  # noqa: E501
        import trajectory_os.application.execution_effort_project_focus_decision_persistence as p  # noqa: E501

        def imported_trajectory_modules(source: str) -> set[str]:
            tree = ast.parse(source)
            found: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if (
                        node.module == "trajectory_os"
                        or node.module.startswith("trajectory_os.")
                    ):
                        found.add(node.module)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if (
                            alias.name == "trajectory_os"
                            or alias.name.startswith("trajectory_os.")
                        ):
                            found.add(alias.name)
            return found

        assert imported_trajectory_modules(inspect.getsource(p)) == {
            "trajectory_os.application.execution_effort_project_focus_decision"
        }
        assert imported_trajectory_modules(inspect.getsource(a)) == {
            "trajectory_os.adapters.persistence.models",
            "trajectory_os.application.execution_effort_project_focus_decision",
            "trajectory_os.application.execution_effort_project_focus_decision_persistence",
        }
