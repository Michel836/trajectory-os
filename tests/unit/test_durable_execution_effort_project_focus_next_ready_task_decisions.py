"""Unit tests for the V1.43 durable next READY TASK decision persistence boundary.

Covers: the strict immutable record model (frozen, extra-forbid, aware
timestamp, genuine-and-revalidatable nested V1.42 decision, exact field
set), the exact command semantics (strict pre-I/O validation in order,
``add`` called exactly once with the exact record, explicit
``decision_id`` / ``decided_at`` preserved), every failure path
appending exactly zero records, repository failure propagation unchanged,
value-equivalent V1.42 decisions with distinct durable decision IDs as
separately representable records, and the public surface / signature /
no-hidden-defaults invariants.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ValidationError

from trajectory_os.application.execution_effort_project_focus_next_ready_task_decision import (  # noqa: E501
    PortfolioProjectFocusNextReadyTaskDecision,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_decision_persistence import (  # noqa: E501
    DurablePortfolioProjectFocusNextReadyTaskDecisionError,
    PortfolioProjectFocusNextReadyTaskDecisionRecord,
    PortfolioProjectFocusNextReadyTaskDecisionRepository,
    record_next_ready_task_decision_durably,
)

PORTFOLIO = UUID("61616161-6161-4161-8161-616161616161")
DECISION_ID = UUID("62626262-6262-4262-8262-626262626262")
DECISION_ID_B = UUID("64646464-6464-4464-8464-646464646464")
DECIDED_AT = datetime(2025, 7, 1, 8, 30, tzinfo=UTC)
DECIDED_AT_OFFSET = datetime(
    2025, 7, 1, 10, 30, tzinfo=timezone(timedelta(hours=2))
)

P_A = UUID("b0000000-0000-4000-8000-00000000000b")
T_A = UUID("d1d1d1d1-0000-4000-8000-000000000011")


class _ForeignModel(BaseModel):
    """A different Pydantic model; must never be accepted as the decision."""

    model_config = {"frozen": True}

    field: str = "foreign"


class _FakeRepository:
    """Structural ``add``/``list_history`` fake recording every append."""

    def __init__(self) -> None:
        self.added: list[object] = []
        self.fail_with: Exception | None = None

    def add(
        self, record: PortfolioProjectFocusNextReadyTaskDecisionRecord
    ) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.added.append(record)

    def list_history(
        self, portfolio_id: UUID
    ) -> tuple[PortfolioProjectFocusNextReadyTaskDecisionRecord, ...]:
        return ()


def _decision_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "decision_id": UUID("83636363-6363-4363-8363-836363636363"),
        "decided_at": datetime(2025, 7, 3, 10, 0, 30, tzinfo=UTC),
        "portfolio_id": PORTFOLIO,
        "selected_project_count": 3,
        "ready_task_count": 5,
        "accepted_project_id": P_A,
        "accepted_task_id": T_A,
    }
    base.update(overrides)
    return base


def _decision(**overrides: object) -> PortfolioProjectFocusNextReadyTaskDecision:
    """One GENUINE (fully validated) V1.42 decision."""
    return PortfolioProjectFocusNextReadyTaskDecision(
        **_decision_kwargs(**overrides)  # type: ignore[arg-type]
    )


def _hostile_constructed_decision() -> PortfolioProjectFocusNextReadyTaskDecision:
    """A model_construct() state genuine construction could never produce.

    The nested ``portfolio_id`` is an ``int`` (strict UUID violation in
    payload mode) — real construction fails, ``model_construct`` skips
    validation. Fresh strict re-validation MUST reject it.
    """
    return PortfolioProjectFocusNextReadyTaskDecision.model_construct(
        **_decision_kwargs(portfolio_id=123)  # type: ignore[arg-type]
    )


def _hostile_constructed_invariant() -> (
    PortfolioProjectFocusNextReadyTaskDecision
):
    """A model_construct() state violating the ``ready_task_count >= 1`` invariant."""
    return PortfolioProjectFocusNextReadyTaskDecision.model_construct(
        **_decision_kwargs(ready_task_count=0)  # type: ignore[arg-type]
    )


class _NoneOffsetTz(tzinfo):
    """tzinfo with an explicit None UTC offset ("naive" aware tzinfo)."""

    def utcoffset(self, *args) -> None:  # type: ignore[override]
        return None

    def dst(self, *args) -> None:  # type: ignore[override]
        return None

    def tzname(self, *args) -> str:  # type: ignore[override]
        return "none"


def _naive_tzinfo_datetime() -> datetime:
    """datetime with a tzinfo whose utcoffset() is None (explicit naive)."""
    return datetime(2025, 7, 1, 8, 30, tzinfo=_NoneOffsetTz())


# ---------------------------------------------------------------------------
# Record model.
# ---------------------------------------------------------------------------


class TestRecordModel:
    def test_exact_three_fields(self) -> None:
        assert (
            set(PortfolioProjectFocusNextReadyTaskDecisionRecord.model_fields)
            == {
                "decision_id",
                "decided_at",
                "decision",
            }
        )
        # no status / actor / current / effective / execution metadata
        for forbidden in (
            "status",
            "actor",
            "current",
            "effective",
            "latest",
            "execution",
        ):
            assert forbidden not in PortfolioProjectFocusNextReadyTaskDecisionRecord.model_fields  # noqa: E501

    def test_strict_frozen_extra_forbid(self) -> None:
        config = PortfolioProjectFocusNextReadyTaskDecisionRecord.model_config
        assert config.get("strict") is True
        assert config.get("frozen") is True
        assert config.get("extra") == "forbid"

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioProjectFocusNextReadyTaskDecisionRecord(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                decision=_decision(),
                status="final",
            )

    def test_rejects_string_decision_id(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioProjectFocusNextReadyTaskDecisionRecord(
                decision_id=str(DECISION_ID),  # type: ignore[arg-type]
                decided_at=DECIDED_AT,
                decision=_decision(),
            )

    def test_rejects_naive_decided_at(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioProjectFocusNextReadyTaskDecisionRecord(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT.replace(tzinfo=None),  # type: ignore[arg-type]
                decision=_decision(),
            )

    def test_rejects_none_offset_tzinfo(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioProjectFocusNextReadyTaskDecisionRecord(
                decision_id=DECISION_ID,
                decided_at=_naive_tzinfo_datetime(),  # type: ignore[arg-type]
                decision=_decision(),
            )

    def test_rejects_foreign_nested_decision(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioProjectFocusNextReadyTaskDecisionRecord(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                decision=_ForeignModel(),  # type: ignore[arg-type]
            )

    def test_rejects_hostile_constructed_nested_decision(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioProjectFocusNextReadyTaskDecisionRecord(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                decision=_hostile_constructed_decision(),
            )

    def test_rejects_hostile_constructed_nested_invariant(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioProjectFocusNextReadyTaskDecisionRecord(
                decision_id=DECISION_ID,
                decided_at=DECIDED_AT,
                decision=_hostile_constructed_invariant(),
            )

    def test_genuine_nested_roundtrip_survives(self) -> None:
        decision = _decision()
        record = PortfolioProjectFocusNextReadyTaskDecisionRecord(
            decision_id=DECISION_ID,
            decided_at=DECIDED_AT,
            decision=decision,
        )
        assert record.decision == decision
        assert (
            record.decision.model_dump(mode="python")
            == decision.model_dump(mode="python")
        )
        again = PortfolioProjectFocusNextReadyTaskDecisionRecord.model_validate(
            record.model_dump(mode="python"), strict=True
        )
        assert again == record

    def test_valid_record_frozen(self) -> None:
        record = PortfolioProjectFocusNextReadyTaskDecisionRecord(
            decision_id=DECISION_ID,
            decided_at=DECIDED_AT,
            decision=_decision(),
        )
        with pytest.raises(ValidationError):
            record.decision_id = uuid4()  # type: ignore[misc]

    def test_offset_preserved_through_construction(self) -> None:
        record = PortfolioProjectFocusNextReadyTaskDecisionRecord(
            decision_id=DECISION_ID,
            decided_at=DECIDED_AT_OFFSET,
            decision=_decision(),
        )
        assert record.decided_at == DECIDED_AT_OFFSET
        assert record.decided_at.utcoffset() == timedelta(hours=2)


# ---------------------------------------------------------------------------
# Command.
# ---------------------------------------------------------------------------


class TestCommand:
    def test_appends_exactly_once_and_returns_exact_record(
        self,
    ) -> None:
        repository = _FakeRepository()
        decision = _decision()
        returned = record_next_ready_task_decision_durably(
            DECISION_ID, DECIDED_AT, decision, repository=repository
        )
        assert len(repository.added) == 1
        assert returned is repository.added[0]
        assert isinstance(
            repository.added[0],
            PortfolioProjectFocusNextReadyTaskDecisionRecord,
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
        record = record_next_ready_task_decision_durably(
            DECISION_ID, DECIDED_AT, _decision(), repository=repository
        )
        assert record.decision_id == DECISION_ID
        assert record.decided_at == DECIDED_AT

    def test_rejects_non_uuid_decision_id_before_repository(self) -> None:
        repository = _FakeRepository()
        for bad in (
            str(DECISION_ID),
            bytes(16),
            DECISION_ID.int,
            None,
            (DECISION_ID,),
        ):
            with pytest.raises(
                DurablePortfolioProjectFocusNextReadyTaskDecisionError
            ):
                record_next_ready_task_decision_durably(
                    bad, DECIDED_AT, _decision(), repository=repository
                )
        assert repository.added == []

    def test_rejects_string_uuid_explicitly(self) -> None:
        # No string coercion at the command authority boundary.
        repository = _FakeRepository()
        with pytest.raises(
            DurablePortfolioProjectFocusNextReadyTaskDecisionError,
            match="UUID instance",
        ):
            record_next_ready_task_decision_durably(
                "62626262-6262-4262-8262-626262626262",
                DECIDED_AT,
                _decision(),
                repository=repository,
            )
        assert repository.added == []

    def test_rejects_non_datetime_and_naive_decided_at(self) -> None:
        repository = _FakeRepository()
        for bad in (
            "2025-07-01T08:30:00+00:00",
            DECIDED_AT.timestamp(),
            None,
            (DECIDED_AT,),
            DECIDED_AT.replace(tzinfo=None),
            _naive_tzinfo_datetime(),
        ):
            with pytest.raises(
                DurablePortfolioProjectFocusNextReadyTaskDecisionError
            ):
                record_next_ready_task_decision_durably(
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
            PortfolioProjectFocusNextReadyTaskDecision.model_dump(
                _decision(), mode="json"
            ),
        ):
            with pytest.raises(
                DurablePortfolioProjectFocusNextReadyTaskDecisionError
            ):
                record_next_ready_task_decision_durably(
                    DECISION_ID, DECIDED_AT, bad, repository=repository
                )
        assert repository.added == []

    def test_rejects_hostile_constructed_decision(self) -> None:
        repository = _FakeRepository()
        with pytest.raises(
            DurablePortfolioProjectFocusNextReadyTaskDecisionError
        ):
            record_next_ready_task_decision_durably(
                DECISION_ID,
                DECIDED_AT,
                _hostile_constructed_decision(),
                repository=repository,
            )
        with pytest.raises(
            DurablePortfolioProjectFocusNextReadyTaskDecisionError
        ):
            record_next_ready_task_decision_durably(
                DECISION_ID,
                DECIDED_AT,
                _hostile_constructed_invariant(),
                repository=repository,
            )
        assert repository.added == []

    def test_fresh_revalidation_retains_validated_copy_only(self) -> None:
        """The record carries the re-validated copy, and a hostile
        caller-owned instance is never used for the stored payload: a
        hostile instance that passes isinstance but fails strict
        re-validation is rejected, while a genuine one is retained
        byte-identically."""
        repository = _FakeRepository()

        # genuine: retained copy is value-identical
        genuine = _decision()
        returned = record_next_ready_task_decision_durably(
            DECISION_ID, DECIDED_AT, genuine, repository=repository
        )
        assert returned.decision == genuine
        assert returned.decision is not genuine  # retained copy, not caller object

        # hostile: rejected before any add
        repository.added.clear()
        with pytest.raises(
            DurablePortfolioProjectFocusNextReadyTaskDecisionError
        ):
            record_next_ready_task_decision_durably(
                DECISION_ID,
                DECIDED_AT,
                _hostile_constructed_decision(),
                repository=repository,
            )
        assert repository.added == []

    def test_repository_failure_propagates_unchanged(self) -> None:
        repository = _FakeRepository()
        repository.fail_with = RuntimeError("storage down")
        with pytest.raises(RuntimeError, match="storage down"):
            record_next_ready_task_decision_durably(
                DECISION_ID, DECIDED_AT, _decision(), repository=repository
            )
        assert repository.added == []

    def test_value_equivalent_decisions_distinct_ids_repr(
        self,
    ) -> None:
        """Value-equivalent V1.42 decisions with distinct durable decision
        IDs are separately representable records."""
        decision_a = _decision()
        decision_b = _decision()  # value-equivalent
        assert decision_a == decision_b

        record_a = PortfolioProjectFocusNextReadyTaskDecisionRecord(
            decision_id=DECISION_ID, decided_at=DECIDED_AT, decision=decision_a
        )
        record_b = PortfolioProjectFocusNextReadyTaskDecisionRecord(
            decision_id=DECISION_ID_B, decided_at=DECIDED_AT, decision=decision_b
        )
        assert record_a.decision == record_b.decision
        assert record_a.decision_id != record_b.decision_id
        assert record_a != record_b

        repository = _FakeRepository()
        first = record_next_ready_task_decision_durably(
            DECISION_ID, DECIDED_AT, decision_a, repository=repository
        )
        second = record_next_ready_task_decision_durably(
            DECISION_ID_B, DECIDED_AT, decision_b, repository=repository
        )
        assert len(repository.added) == 2
        assert first.decision_id == DECISION_ID
        assert second.decision_id == DECISION_ID_B

    def test_boundary_error_is_narrow_value_error(self) -> None:
        assert issubclass(
            DurablePortfolioProjectFocusNextReadyTaskDecisionError, ValueError
        )
        assert not issubclass(
            DurablePortfolioProjectFocusNextReadyTaskDecisionError, TypeError
        )


# ---------------------------------------------------------------------------
# Public surface.
# ---------------------------------------------------------------------------


class TestPublicSurface:
    def test_package_exports(self) -> None:
        import trajectory_os.application as application

        for name in (
            "DurablePortfolioProjectFocusNextReadyTaskDecisionError",
            "PortfolioProjectFocusNextReadyTaskDecisionRecord",
            "PortfolioProjectFocusNextReadyTaskDecisionRepository",
            "record_next_ready_task_decision_durably",
        ):
            assert name in application.__all__
            assert getattr(application, name) is not None

    def test_adapter_exports(self) -> None:
        import trajectory_os.adapters.persistence as adapters

        for name in (
            "SqlitePortfolioProjectFocusNextReadyTaskDecisionRepository",
            "DuplicatePortfolioProjectFocusNextReadyTaskDecisionError",
        ):
            assert name in adapters.__all__
            assert getattr(adapters, name) is not None

    def test_command_signature_and_no_hidden_defaults(self) -> None:
        signature = inspect.signature(record_next_ready_task_decision_durably)
        parameters = list(signature.parameters)
        assert (
            parameters
            == ["decision_id", "decided_at", "decision", "repository"]
        )
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
            for name in dir(
                PortfolioProjectFocusNextReadyTaskDecisionRepository
            )
            if not name.startswith("_")
        }
        assert methods == {"add", "list_history"}

    def test_modules_import_only_allowed_dependencies(self) -> None:

        import trajectory_os.adapters.persistence.sqlite_portfolio_project_focus_next_ready_task_decisions as a  # noqa: E501
        import trajectory_os.application.execution_effort_project_focus_next_ready_task_decision_persistence as p  # noqa: E501

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

        assert imported_trajectory_modules(inspect.getsource(p)) == {  # type: ignore[arg-type]  # noqa: E501
            "trajectory_os.application.execution_effort_project_focus_next_ready_task_decision"
        }
        assert imported_trajectory_modules(inspect.getsource(a)) == {  # type: ignore[arg-type]  # noqa: E501
            "trajectory_os.adapters.persistence.models",
            "trajectory_os.application.execution_effort_project_focus_next_ready_task_decision",
            "trajectory_os.application.execution_effort_project_focus_next_ready_task_decision_persistence",
        }
