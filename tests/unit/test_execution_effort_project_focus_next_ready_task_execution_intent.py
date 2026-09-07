"""Unit tests for the V1.44 explicit human-authorized execution intent.

Covers: the strict immutable intent model (frozen, extra-forbid, exactly
nine fields, aware timestamps with non-None UTC offset, count invariants,
original-offset preservation, no hidden defaults, no generated identity or
time), the exact authorization boundary semantics (strict validation in
order, genuine V1.43 record requirement, fresh COMPLETE strict
re-validation including hostile ``model_construct`` payloads at the V1.43
and nested V1.42 level, exact projection of every semantic value from the
retained validated copy, no caller override of projected values, the
deliberate absence of any temporal precedence enforcement), the
no-side-effect / no-mutation guarantees, and material architecture guards
(the module's sole dependency is the V1.43 durable record module and no
status-transition, WBS, provider, subprocess, persistence, identity, or
wall-clock surface is referenced in code).
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import BaseModel, ValidationError

from trajectory_os.application.execution_effort_project_focus_next_ready_task_decision import (  # noqa: E501
    PortfolioProjectFocusNextReadyTaskDecision,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_decision_persistence import (  # noqa: E501
    PortfolioProjectFocusNextReadyTaskDecisionRecord,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_intent import (  # noqa: E501
    PortfolioProjectFocusNextReadyTaskExecutionIntent,
    PortfolioProjectFocusNextReadyTaskExecutionIntentError,
    authorize_next_ready_task_execution,
)

PORTFOLIO = UUID("61616161-6161-4161-8161-616161616161")
RECORD_ID = UUID("63636363-6363-4363-8363-636363636363")
RECORD_DECIDED_AT = datetime(2025, 7, 1, 8, 30, tzinfo=UTC)
RECORD_DECIDED_AT_OFFSET = datetime(
    2025, 7, 1, 10, 30, tzinfo=timezone(timedelta(hours=2))
)
NESTED_DECIDED_AT = datetime(2025, 7, 3, 10, 0, 30, tzinfo=UTC)
INTENT_ID = UUID("65656565-6565-4565-8565-656565656565")
AUTHORIZED_AT = datetime(2025, 7, 5, 12, 0, tzinfo=UTC)
AUTHORIZED_AT_OFFSET = datetime(
    2025, 7, 5, 14, 0, tzinfo=timezone(timedelta(hours=2))
)
AUTHORIZED_AT_EARLY = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)

P_A = UUID("b0000000-0000-4000-8000-00000000000b")
T_A = UUID("d1d1d1d1-0000-4000-8000-000000000011")


class _ForeignModel(BaseModel):
    """A different Pydantic model; must never be accepted as the record."""

    model_config = {"frozen": True}

    field: str = "foreign"


def _decision_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "decision_id": UUID("83636363-6363-4363-8363-836363636363"),
        "decided_at": NESTED_DECIDED_AT,
        "portfolio_id": PORTFOLIO,
        "selected_project_count": 3,
        "ready_task_count": 5,
        "accepted_project_id": P_A,
        "accepted_task_id": T_A,
    }
    base.update(overrides)
    return base


def _decision(**overrides: object) -> (
    PortfolioProjectFocusNextReadyTaskDecision
):
    """One GENUINE (fully validated) V1.42 decision."""
    return PortfolioProjectFocusNextReadyTaskDecision(
        **_decision_kwargs(**overrides)  # type: ignore[arg-type]
    )


def _record(
    record_id: object = RECORD_ID,
    record_decided_at: object = RECORD_DECIDED_AT,
    **decision_overrides: object,
) -> PortfolioProjectFocusNextReadyTaskDecisionRecord:
    """One GENUINE (fully validated) durable V1.43 record."""
    return PortfolioProjectFocusNextReadyTaskDecisionRecord(
        decision_id=record_id,  # type: ignore[arg-type]
        decided_at=record_decided_at,  # type: ignore[arg-type]
        decision=_decision(**decision_overrides),
    )


def _hostile_nested_decision() -> (
    PortfolioProjectFocusNextReadyTaskDecision
):
    """Nested V1.42 state genuine construction could never produce.

    The nested ``portfolio_id`` is an ``int`` (strict UUID violation in
    payload mode). Fresh COMPLETE strict re-validation of the enclosing
    record MUST reject it.
    """
    return PortfolioProjectFocusNextReadyTaskDecision.model_construct(  # type: ignore[arg-type]
        **_decision_kwargs(portfolio_id=123)  # type: ignore[arg-type]
    )


def _hostile_nested_invariant() -> (
    PortfolioProjectFocusNextReadyTaskDecision
):
    """Nested V1.42 state violating the ``ready_task_count >= 1`` invariant."""
    return PortfolioProjectFocusNextReadyTaskDecision.model_construct(  # type: ignore[arg-type]
        **_decision_kwargs(ready_task_count=0)  # type: ignore[arg-type]
    )


def _hostile_record(decision: object) -> (
    PortfolioProjectFocusNextReadyTaskDecisionRecord
):
    """A V1.43 record state whose skipped validators it must not survive."""
    return (
        PortfolioProjectFocusNextReadyTaskDecisionRecord.model_construct(  # type: ignore[arg-type]
            decision_id=RECORD_ID,
            decided_at=RECORD_DECIDED_AT,
            decision=decision,  # type: ignore[arg-type]
        )
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
    return datetime(2025, 7, 5, 12, 0, tzinfo=_NoneOffsetTz())


# ---------------------------------------------------------------------------
# Intent model.
# ---------------------------------------------------------------------------


def _intent_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "intent_id": INTENT_ID,
        "authorized_at": AUTHORIZED_AT,
        "decision_id": RECORD_ID,
        "decision_decided_at": RECORD_DECIDED_AT,
        "portfolio_id": PORTFOLIO,
        "selected_project_count": 3,
        "ready_task_count": 5,
        "authorized_project_id": P_A,
        "authorized_task_id": T_A,
    }
    base.update(overrides)
    return base


class TestIntentModel:
    def test_exact_nine_fields(self) -> None:
        names = list(
            PortfolioProjectFocusNextReadyTaskExecutionIntent.model_fields
        )
        assert names == [
            "intent_id",
            "authorized_at",
            "decision_id",
            "decision_decided_at",
            "portfolio_id",
            "selected_project_count",
            "ready_task_count",
            "authorized_project_id",
            "authorized_task_id",
        ]

    def test_strict(self) -> None:
        assert (
            PortfolioProjectFocusNextReadyTaskExecutionIntent.model_config[
                "strict"
            ]
            is True
        )

    def test_frozen(self) -> None:
        assert (
            PortfolioProjectFocusNextReadyTaskExecutionIntent.model_config[
                "frozen"
            ]
            is True
        )

    def test_extra_forbid(self) -> None:
        assert (
            PortfolioProjectFocusNextReadyTaskExecutionIntent.model_config[
                "extra"
            ]
            == "forbid"
        )

    def test_no_field_has_defaults(self) -> None:
        for name, field in (
            PortfolioProjectFocusNextReadyTaskExecutionIntent.model_fields.items()  # noqa: E501
        ):
            assert field.is_required(), (
                f"{name} must be required (no hidden identity/time "
                "defaults, no generated defaults)"
            )

    def test_uuid_field_annotations(self) -> None:
        fields = (
            PortfolioProjectFocusNextReadyTaskExecutionIntent.model_fields
        )
        for name in (
            "intent_id",
            "decision_id",
            "portfolio_id",
            "authorized_project_id",
            "authorized_task_id",
        ):
            assert fields[name].annotation is UUID, name
        for name in ("authorized_at", "decision_decided_at"):
            assert fields[name].annotation is datetime, name

    def _valid(self, **overrides: object) -> (
        PortfolioProjectFocusNextReadyTaskExecutionIntent
    ):
        return PortfolioProjectFocusNextReadyTaskExecutionIntent(
            **_intent_kwargs(**overrides)  # type: ignore[arg-type]
        )

    def test_intent_id_uuid_only(self) -> None:
        with pytest.raises(ValidationError):
            self._valid(
                intent_id="65656565-6565-4565-8565-656565656565"
            )
        with pytest.raises(ValidationError):
            self._valid(intent_id=123)
        with pytest.raises(ValidationError):
            self._valid(intent_id=None)

    def test_aware_authorized_at_required(self) -> None:
        with pytest.raises(ValidationError):
            self._valid(
                authorized_at=datetime(2025, 7, 5, 12, 0)
            )
        with pytest.raises(ValidationError):
            self._valid(authorized_at=_naive_tzinfo_datetime())

    def test_aware_decision_decided_at_required(self) -> None:
        with pytest.raises(ValidationError):
            self._valid(decision_decided_at=datetime(2025, 7, 1, 8, 30))
        with pytest.raises(ValidationError):
            self._valid(decision_decided_at=_naive_tzinfo_datetime())

    def test_decision_decided_at_original_offset_preserved(self) -> None:
        intent = self._valid(decision_decided_at=RECORD_DECIDED_AT_OFFSET)
        assert (
            intent.decision_decided_at.utcoffset()
            == timedelta(hours=2)
        )
        assert intent.decision_decided_at == RECORD_DECIDED_AT_OFFSET

    def test_selected_project_count_nonnegative(self) -> None:
        self._valid(selected_project_count=0)
        with pytest.raises(ValidationError):
            self._valid(selected_project_count=-1)

    def test_ready_task_count_at_least_one(self) -> None:
        self._valid(ready_task_count=1)
        with pytest.raises(ValidationError):
            self._valid(ready_task_count=0)

    def test_direct_invalid_construction_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._valid(portfolio_id="not-a-uuid")
        with pytest.raises(ValidationError):
            self._valid(selected_project_count="three")
        with pytest.raises(ValidationError):
            self._valid(authorized_project_id=None)

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            self._valid(execution_started=True)

    def test_frozen_rejects_mutation(self) -> None:
        intent = self._valid()
        with pytest.raises(ValidationError):
            intent.portfolio_id = PORTFOLIO  # type: ignore[misc]

    def test_generated_identity_and_time_rejected_on_boundary(self) -> None:
        """The boundary function's executable code never references any
        identity or timestamp generation surface (names in code only; the
        prose docstring is intentionally excluded)."""
        source = inspect.getsource(authorize_next_ready_task_execution)
        referenced: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Name):
                referenced.add(node.id)
            elif isinstance(node, ast.Attribute):
                referenced.add(node.attr)
        forbidden = {"uuid4", "utcnow", "now"}
        assert referenced & forbidden == set(), (
            sorted(referenced & forbidden)
        )

    def test_generated_identity_and_time_rejected_on_model(self) -> None:
        """The model itself offers no identity or timestamp generation:
        every field is required with no default or default_factory."""
        for name, field in (
            PortfolioProjectFocusNextReadyTaskExecutionIntent.model_fields.items()  # noqa: E501
        ):  # noqa: E501
            assert field.default_factory is None, name
            assert field.is_required(), name


# ---------------------------------------------------------------------------
# Authorization boundary.
# ---------------------------------------------------------------------------


class TestBoundary:
    def test_valid_authorization_returns_exact_immutable_intent(self) -> (
        None
    ):
        record = _record(record_decided_at=RECORD_DECIDED_AT_OFFSET)
        intent = authorize_next_ready_task_execution(
            INTENT_ID, AUTHORIZED_AT_OFFSET, record
        )
        assert isinstance(
            intent, PortfolioProjectFocusNextReadyTaskExecutionIntent
        )
        assert intent.intent_id == INTENT_ID
        assert intent.authorized_at == AUTHORIZED_AT_OFFSET
        assert intent.decision_id == record.decision_id
        assert intent.decision_decided_at == RECORD_DECIDED_AT_OFFSET
        assert intent.portfolio_id == PORTFOLIO
        assert intent.selected_project_count == 3
        assert intent.ready_task_count == 5
        assert intent.authorized_project_id == P_A
        assert intent.authorized_task_id == T_A
        with pytest.raises(ValidationError):
            intent.intent_id = INTENT_ID  # type: ignore[misc]

    def test_non_uuid_intent_id_rejected(self) -> None:
        record = _record()
        for bad in (1, b"x", True, ["x"]):
            with pytest.raises(
                PortfolioProjectFocusNextReadyTaskExecutionIntentError
            ):
                authorize_next_ready_task_execution(bad, AUTHORIZED_AT, record)  # type: ignore[arg-type]

    def test_string_uuid_intent_id_rejected(self) -> None:
        record = _record()
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskExecutionIntentError
        ):
            authorize_next_ready_task_execution(
                "65656565-6565-4565-8565-656565656565", AUTHORIZED_AT, record
            )

    def test_bytes_int_none_intent_id_rejected(self) -> None:
        record = _record()
        for bad in (b"x", 123, None):
            with pytest.raises(
                PortfolioProjectFocusNextReadyTaskExecutionIntentError
            ):
                authorize_next_ready_task_execution(
                    bad, AUTHORIZED_AT, record  # type: ignore[arg-type]
                )

    def test_non_datetime_authorized_at_rejected(self) -> None:
        record = _record()
        for bad in (1, None, timedelta(hours=1)):
            with pytest.raises(
                PortfolioProjectFocusNextReadyTaskExecutionIntentError
            ):
                authorize_next_ready_task_execution(
                    INTENT_ID, bad, record  # type: ignore[arg-type]
                )

    def test_string_datetime_authorized_at_rejected(self) -> None:
        record = _record()
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskExecutionIntentError
        ):
            authorize_next_ready_task_execution(
                INTENT_ID, "2025-07-05T12:00:00+00:00", record
            )

    def test_naive_authorized_at_rejected(self) -> None:
        record = _record()
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskExecutionIntentError
        ):
            authorize_next_ready_task_execution(
                INTENT_ID, datetime(2025, 7, 5, 12, 0), record
            )

    def test_none_offset_authorized_at_rejected(self) -> None:
        record = _record()
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskExecutionIntentError
        ):
            authorize_next_ready_task_execution(
                INTENT_ID, _naive_tzinfo_datetime(), record
            )

    def test_non_genuine_record_rejected(self) -> None:
        record = _record()
        for bad in (
            None,
            "record",
            {"decision_id": str(RECORD_ID)},
            _ForeignModel(),
            SimpleNamespace(decision=record),
        ):
            with pytest.raises(
                PortfolioProjectFocusNextReadyTaskExecutionIntentError
            ):
                authorize_next_ready_task_execution(
                    INTENT_ID, AUTHORIZED_AT, bad  # type: ignore[arg-type]
                )

    def test_v142_decision_is_not_a_valid_record(self) -> None:
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskExecutionIntentError
        ):
            authorize_next_ready_task_execution(
                INTENT_ID, AUTHORIZED_AT, _decision()  # type: ignore[arg-type]
            )

    def test_unrelated_model_is_not_a_valid_record(self) -> None:
        class _NotARecord(BaseModel):
            decision_id: UUID = RECORD_ID

        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskExecutionIntentError
        ):
            authorize_next_ready_task_execution(
                INTENT_ID,
                AUTHORIZED_AT,
                _NotARecord(),  # type: ignore[arg-type]
            )

    def test_hostile_record_level_model_construct_rejected(self) -> None:
        """Record-level model_construct skipping the record validators."""
        bad = (
            PortfolioProjectFocusNextReadyTaskDecisionRecord.model_construct(  # type: ignore[arg-type]
                decision_id=1,
                decided_at=RECORD_DECIDED_AT,
                decision=_decision(),
            )
        )
        assert isinstance(bad, PortfolioProjectFocusNextReadyTaskDecisionRecord)  # noqa: E501
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskExecutionIntentError
        ):
            authorize_next_ready_task_execution(
                INTENT_ID, AUTHORIZED_AT, bad
            )

    def test_hostile_record_naive_timestamp_rejected(self) -> None:
        bad = (
            PortfolioProjectFocusNextReadyTaskDecisionRecord.model_construct(  # type: ignore[arg-type]
                decision_id=RECORD_ID,
                decided_at=datetime(2025, 7, 1, 8, 30),
                decision=_decision(),
            )
        )
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskExecutionIntentError
        ):
            authorize_next_ready_task_execution(
                INTENT_ID, AUTHORIZED_AT, bad
            )

    def test_hostile_nested_decision_rejected(self) -> None:
        """Hostile nested V1.42 state inside a genuine-shaped V1.43 record
        is rejected by the fresh COMPLETE strict re-validation."""
        record = _hostile_record(_hostile_nested_decision())
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskExecutionIntentError
        ):
            authorize_next_ready_task_execution(
                INTENT_ID, AUTHORIZED_AT, record
            )

    def test_hostile_nested_invariant_rejected(self) -> None:
        """Nested V1.42 ``ready_task_count == 0`` cannot survive."""
        record = _hostile_record(_hostile_nested_invariant())
        with pytest.raises(
            PortfolioProjectFocusNextReadyTaskExecutionIntentError
        ):
            authorize_next_ready_task_execution(
                INTENT_ID, AUTHORIZED_AT, record
            )

    def test_fresh_complete_revalidation_occurs(self) -> None:
        """The fresh strict re-validation round-trip defines the record
        values the intent carries; its outer timestamp offset is
        projected exactly, including through strict re-validation."""
        record = _record(record_decided_at=RECORD_DECIDED_AT_OFFSET)
        intent = authorize_next_ready_task_execution(
            INTENT_ID, AUTHORIZED_AT, record
        )
        assert intent.decision_id == record.decision_id
        assert intent.decision_decided_at == record.decided_at
        assert (
            intent.decision_decided_at.utcoffset()
            == record.decided_at.utcoffset()
        )

    def test_retained_fresh_copy_semantic_reads(self) -> None:
        """The caller-owned record is never read for semantic values: the
        boundary's output is entirely determined by the (value-validated)
        record content, and value-identical records yield value-identical
        intents while hostile-shaped records are rejected outright."""
        good = _record()
        fresh = _record()
        assert good == fresh
        assert (
            authorize_next_ready_task_execution(
                INTENT_ID, AUTHORIZED_AT, good
            )
            == authorize_next_ready_task_execution(
                INTENT_ID, AUTHORIZED_AT, fresh
            )
        )

    def test_exact_projection_of_record_fields(self) -> None:
        record = _record(
            selected_project_count=7,
            ready_task_count=2,
            accepted_project_id=UUID(
                "c0000000-0000-4000-8000-00000000000c"
            ),
            accepted_task_id=UUID("d2d2d2d2-0000-4000-8000-000000000022"),
        )
        intent = authorize_next_ready_task_execution(
            INTENT_ID, AUTHORIZED_AT, record
        )
        assert intent.decision_id == record.decision_id
        assert intent.decision_decided_at == record.decided_at
        assert intent.decision_decided_at == RECORD_DECIDED_AT
        assert intent.decision_decided_at != NESTED_DECIDED_AT
        assert intent.portfolio_id == record.decision.portfolio_id
        assert (
            intent.selected_project_count
            == record.decision.selected_project_count
            == 7
        )
        assert (
            intent.ready_task_count
            == record.decision.ready_task_count
            == 2
        )
        assert intent.authorized_project_id == (
            record.decision.accepted_project_id
        )
        assert intent.authorized_task_id == (
            record.decision.accepted_task_id
        )

    def test_decision_decided_at_preserves_record_offset(self) -> None:
        record = _record(record_decided_at=RECORD_DECIDED_AT_OFFSET)
        intent = authorize_next_ready_task_execution(
            INTENT_ID, AUTHORIZED_AT, record
        )
        assert intent.decision_decided_at.utcoffset() == timedelta(hours=2)

    def test_caller_cannot_override_projected_values(self) -> None:
        """The signature takes exactly three positional parameters:
        nothing besides ``intent_id`` / ``authorized_at`` /
        ``durable_decision`` exists, so no projected value can be
        overridden by the caller."""
        signature = inspect.signature(authorize_next_ready_task_execution)
        parameters = list(signature.parameters)
        assert parameters == [
            "intent_id",
            "authorized_at",
            "durable_decision",
        ]
        for name in parameters:
            assert (
                signature.parameters[name].default
                is inspect.Parameter.empty
            )
        record = _record()
        intent = authorize_next_ready_task_execution(
            INTENT_ID, AUTHORIZED_AT, record
        )
        assert intent.portfolio_id == PORTFOLIO
        assert intent.authorized_project_id == P_A
        assert intent.authorized_task_id == T_A

    # ------------------------- temporal rule ------------------------------

    def test_authorized_at_earlier_than_record_decided_at_allowed(self) -> (  # noqa: E501
        None
    ):
        record = _record()
        intent = authorize_next_ready_task_execution(
            INTENT_ID, AUTHORIZED_AT_EARLY, record
        )
        assert intent.authorized_at == AUTHORIZED_AT_EARLY
        assert intent.decision_decided_at == RECORD_DECIDED_AT

    def test_authorized_at_earlier_than_nested_decision_allowed(self) -> (
        None
    ):
        record = _record(record_decided_at=datetime(2025, 7, 10, tzinfo=UTC))
        intent = authorize_next_ready_task_execution(
            INTENT_ID, AUTHORIZED_AT_EARLY, record
        )
        assert intent.authorized_at == AUTHORIZED_AT_EARLY
        assert intent.authorized_at < record.decision.decided_at
        assert intent.decision_decided_at == record.decided_at


class TestNoSideEffects:
    def test_supplied_record_remains_unchanged(self) -> None:
        record = _record()
        before = record.model_dump(mode="python")
        authorize_next_ready_task_execution(INTENT_ID, AUTHORIZED_AT, record)
        assert record.model_dump(mode="python") == before

    def test_repeated_calls_are_value_identical(self) -> None:
        record = _record()
        first = authorize_next_ready_task_execution(
            INTENT_ID, AUTHORIZED_AT, record
        )
        second = authorize_next_ready_task_execution(
            INTENT_ID, AUTHORIZED_AT, record
        )
        assert first == second

    def test_no_execution_status_or_persistence_side_effects_in_code(self) -> (  # noqa: E501
        None
    ):
        """Material architecture guard: the module's executable code never
        references status-transition, work-breakdown, provider, process,
        persistence, identity-generation, or wall-clock surfaces (docstring
        prose is deliberately excluded)."""
        import trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_intent as m  # noqa: E501

        source = inspect.getsource(m)
        referenced: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Name):
                referenced.add(node.id)
            elif isinstance(node, ast.Attribute):
                referenced.add(node.attr)

        forbidden = {
            "entity_status_transition",
            "EntityStatus",
            "build_work_breakdown",
            "Portfolio",
            "transition_entity_status",
            "transition_entity_status_durably",
            "subprocess",
            "os_system",
            "popen",
            "uuid4",
            "utcnow",
            "now",
            "repository",
            "Sqlite",
            "sqlite",
        }
        assert referenced & forbidden == set(), (
            f"module must not reference {sorted(referenced & forbidden)}"
        )


# ---------------------------------------------------------------------------
# Public surface and module dependency guard.
# ---------------------------------------------------------------------------


class TestPublicSurfaceAndArchitecture:
    def test_package_exports(self) -> None:
        import trajectory_os.application as application

        for name in (
            "PortfolioProjectFocusNextReadyTaskExecutionIntent",
            "PortfolioProjectFocusNextReadyTaskExecutionIntentError",
            "authorize_next_ready_task_execution",
        ):
            assert name in application.__all__
            assert getattr(application, name) is not None

    def test_error_type(self) -> None:
        assert issubclass(
            PortfolioProjectFocusNextReadyTaskExecutionIntentError, ValueError
        )

    def test_modules_import_only_allowed_dependencies(self) -> None:
        import trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_intent as m  # noqa: E501

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

        assert imported_trajectory_modules(inspect.getsource(m)) == {  # type: ignore[arg-type]  # noqa: E501
            "trajectory_os.application.execution_effort_project_focus_next_ready_task_decision_persistence"
        }
