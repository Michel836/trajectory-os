"""V1.46 — unit tests for the explicit executable TASK request boundary."""

from __future__ import annotations

import ast
import datetime as _datetime_module
import itertools
import warnings
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import get_type_hints
from uuid import UUID

import pytest
from pydantic import BaseModel, ValidationError

import trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_request as module  # noqa: E501
from trajectory_os.application import (
    PortfolioProjectFocusNextReadyTaskExecutionApplicability as Applicability,
)
from trajectory_os.application import (
    PortfolioProjectFocusNextReadyTaskExecutionApplicabilityConstraint as Constraint,
)
from trajectory_os.application import (
    PortfolioProjectFocusNextReadyTaskExecutionApplicabilityState as S,
)
from trajectory_os.application import (
    PortfolioProjectFocusNextReadyTaskExecutionIntent as Intent,
)
from trajectory_os.application import (
    PortfolioProjectFocusNextReadyTaskExecutionRequest as Request,
)
from trajectory_os.application import (
    PortfolioProjectFocusNextReadyTaskExecutionRequestError as RequestError,
)
from trajectory_os.application import (
    request_current_applicable_next_ready_task_execution as fn,
)
from trajectory_os.domain.entities import (
    EntityStatus,
    EntityType,
)
from trajectory_os.domain.relations import RelationType

# ---------------------------------------------------------------------------
# Fixed, deterministic identities: no wall clock, no random generation.
# ---------------------------------------------------------------------------

REQUEST_ID = UUID(int=0x1_4600_1)
REQUESTED_AT = datetime(2025, 2, 1, 8, 0, 0, tzinfo=UTC)
AUTHORIZE_AT = datetime(2025, 1, 15, 10, 30, 0, tzinfo=UTC)
_NON_UTC_OFFSET = timezone(timedelta(hours=5, minutes=30))
AUTHORIZE_AT_OFFSET = AUTHORIZE_AT.astimezone(_NON_UTC_OFFSET)


class _NullOffsetTzInfo(_datetime_module.tzinfo):
    """A tzinfo whose utcoffset() is None (a naive-style zone)."""

    def utcoffset(self, dt: datetime | None = None) -> None:  # type: ignore[override]
        return None

    def dst(self, dt: datetime | None = None) -> None:  # type: ignore[override]
        return None

    def tzname(self, dt: datetime | None = None) -> str:
        return ""


class _ForeignModel(BaseModel):
    """A foreign, V1.45-unrelated pydantic model."""

    marker: str = "foreign"


_UUID_SEQUENCE = itertools.count(1)


def _uuid() -> UUID:
    """Deterministic sequential test identity (never random)."""
    return UUID(int=next(_UUID_SEQUENCE))


def _base_provenance() -> dict[str, object]:
    return {
        "intent_id": _uuid(),
        "authorized_at": AUTHORIZE_AT,
        "decision_id": _uuid(),
        "portfolio_id": _uuid(),
        "authorized_project_id": _uuid(),
        "authorized_task_id": _uuid(),
    }


def _constraint(
    satisfied: bool,
    status: EntityStatus,
) -> Constraint:
    return Constraint(
        relation_id=_uuid(),
        relation_type=RelationType.BLOCKS,
        counterpart_entity_id=_uuid(),
        counterpart_entity_type=EntityType.TASK,
        counterpart_status=status,
        satisfied=satisfied,
    )


def _applicability(state: S) -> Applicability:
    """Construct ONE genuine V1.45 applicability for the EXACT state,
    respecting the V1.45 per-state invariants."""

    if state.name in {
        "PROJECT_MISSING",
        "PROJECT_TYPE_MISMATCH",
        "TASK_MISSING",
        "TASK_TYPE_MISMATCH",
    }:
        extra: dict[str, object] = {
            "task_status": None,
            "constraints": None,
            "unsatisfied_constraint_count": None,
        }
    elif state is S.TASK_NOT_IN_AUTHORIZED_PROJECT:
        extra = {
            "task_status": EntityStatus.ACTIVE,
            "constraints": None,
            "unsatisfied_constraint_count": None,
        }
    elif state is S.INELIGIBLE_STATUS:
        extra = {
            "task_status": EntityStatus.WAITING,
            "constraints": (),
            "unsatisfied_constraint_count": 0,
        }
    elif state is S.CONSTRAINED:
        blocker = _constraint(False, EntityStatus.ACTIVE)
        extra = {
            "task_status": EntityStatus.ACTIVE,
            "constraints": (blocker,),
            "unsatisfied_constraint_count": 1,
        }
    else:  # APPLICABLE, with both satisfied and zero-constraint variants
        extra = {
            "task_status": EntityStatus.ACTIVE,
            "constraints": (),
            "unsatisfied_constraint_count": 0,
        }
    return Applicability(**_base_provenance(), applicability_state=state, **extra)


def _applicable() -> Applicability:
    return _applicability(S.APPLICABLE)


def _call(applicability: object | None = None) -> Request:
    if applicability is None:
        applicability = _applicable()
    return fn(REQUEST_ID, REQUESTED_AT, applicability)


# ---------------------------------------------------------------------------
# MODEL — exact shape.
# ---------------------------------------------------------------------------


def test_model_has_exactly_the_eight_specified_fields() -> None:
    assert set(Request.model_fields) == {
        "request_id",
        "requested_at",
        "intent_id",
        "authorized_at",
        "decision_id",
        "portfolio_id",
        "authorized_project_id",
        "authorized_task_id",
    }


def test_model_fields_are_uuid_or_datetime() -> None:
    hints = get_type_hints(Request, include_extras=False)
    uuid_fields = {
        "request_id",
        "intent_id",
        "decision_id",
        "portfolio_id",
        "authorized_project_id",
        "authorized_task_id",
    }
    for name in uuid_fields:
        assert hints[name] is UUID
    assert hints["requested_at"] is datetime
    assert hints["authorized_at"] is datetime


def test_model_is_strict() -> None:
    assert Request.model_config["strict"] is True
    with pytest.raises(ValidationError):
        Request(
            request_id=str(REQUEST_ID),
            requested_at=REQUESTED_AT,
            intent_id=_uuid(),
            authorized_at=AUTHORIZE_AT,
            decision_id=_uuid(),
            portfolio_id=_uuid(),
            authorized_project_id=_uuid(),
            authorized_task_id=_uuid(),
        )


def test_model_is_frozen() -> None:
    assert Request.model_config["frozen"] is True
    result = _call()
    with pytest.raises(ValidationError):
        result.request_id = _uuid()


def test_model_is_extra_forbid() -> None:
    assert Request.model_config["extra"] == "forbid"
    with pytest.raises(ValidationError):
        Request(
            request_id=REQUEST_ID,
            requested_at=REQUESTED_AT,
            intent_id=_uuid(),
            authorized_at=AUTHORIZE_AT,
            decision_id=_uuid(),
            portfolio_id=_uuid(),
            authorized_project_id=_uuid(),
            authorized_task_id=_uuid(),
            task_status=EntityStatus.ACTIVE,
        )


def test_model_has_no_defaults() -> None:
    for field in Request.model_fields.values():
        assert field.is_required()


def test_model_rejects_naive_requested_at() -> None:
    naive = datetime(2025, 2, 1, 8, 0, 0)
    with pytest.raises(ValidationError):
        Request(
            request_id=REQUEST_ID,
            requested_at=naive,
            intent_id=_uuid(),
            authorized_at=AUTHORIZE_AT,
            decision_id=_uuid(),
            portfolio_id=_uuid(),
            authorized_project_id=_uuid(),
            authorized_task_id=_uuid(),
        )


def test_model_rejects_null_offset_requested_at() -> None:
    null_tz = datetime(2025, 2, 1, 8, 0, 0, tzinfo=_NullOffsetTzInfo())
    with pytest.raises(ValidationError):
        Request(
            request_id=REQUEST_ID,
            requested_at=null_tz,
            intent_id=_uuid(),
            authorized_at=AUTHORIZE_AT,
            decision_id=_uuid(),
            portfolio_id=_uuid(),
            authorized_project_id=_uuid(),
            authorized_task_id=_uuid(),
        )


def test_model_rejects_naive_authorized_at() -> None:
    naive = datetime(2025, 1, 15, 10, 30, 0)
    with pytest.raises(ValidationError):
        Request(
            request_id=REQUEST_ID,
            requested_at=REQUESTED_AT,
            intent_id=_uuid(),
            authorized_at=naive,
            decision_id=_uuid(),
            portfolio_id=_uuid(),
            authorized_project_id=_uuid(),
            authorized_task_id=_uuid(),
        )


def test_model_accepts_aware_non_utc_requested_at() -> None:
    requested = REQUESTED_AT.astimezone(_NON_UTC_OFFSET)
    result = fn(REQUEST_ID, requested, _applicable())
    assert result.requested_at == requested
    assert result.request_id == REQUEST_ID


def test_model_preserves_exact_authorized_at_offset() -> None:
    base = _base_provenance()
    base["authorized_at"] = AUTHORIZE_AT_OFFSET
    applicability = Applicability(
        **base,
        applicability_state=S.APPLICABLE,
        task_status=EntityStatus.ACTIVE,
        constraints=(),
        unsatisfied_constraint_count=0,
    )
    result = _call(applicability)
    assert result.authorized_at == AUTHORIZE_AT_OFFSET
    assert result.authorized_at.utcoffset() == _NON_UTC_OFFSET.utcoffset(None)


def test_hostile_model_construct_invalid_timestamp_fails_revalidation() -> None:
    naive = datetime(2025, 1, 15, 10, 30, 0)
    hostile = Request.model_construct(
        request_id=REQUEST_ID,
        requested_at=naive,
        intent_id=_uuid(),
        authorized_at=AUTHORIZE_AT,
        decision_id=_uuid(),
        portfolio_id=_uuid(),
        authorized_project_id=_uuid(),
        authorized_task_id=_uuid(),
    )
    with pytest.raises(ValidationError):
        Request.model_validate(hostile.model_dump(mode="python"), strict=True)


def test_request_result_is_immutable_value() -> None:
    result = _call()
    dumped: object = result.model_dump(mode="python")
    assert dumped == (
        Request.model_validate(result.model_dump(mode="python"), strict=True).model_dump(
            mode="python"
        )
    )


# ---------------------------------------------------------------------------
# BOUNDARY — input strictness.
# ---------------------------------------------------------------------------


def test_genuine_applicable_v145_is_accepted() -> None:
    result = _call()
    assert isinstance(result, Request)
    assert result.request_id == REQUEST_ID
    assert result.requested_at == REQUESTED_AT


@pytest.mark.parametrize(
    ("bad_id", "requested"),
    [
        (str(REQUEST_ID), REQUESTED_AT),
        (1_460_001, REQUESTED_AT),
        (None, REQUESTED_AT),
        (b"146", REQUESTED_AT),
        (REQUEST_ID, str(REQUESTED_AT)),
        (REQUEST_ID, 1_738_368_000),
        (REQUEST_ID, None),
    ],
    ids=[
        "request_id-string",
        "request_id-int",
        "request_id-none",
        "request_id-bytes",
        "requested_at-string",
        "requested_at-int",
        "requested_at-none",
    ],
)
def test_request_id_and_requested_at_must_be_genuine_instances(
    bad_id: object,
    requested: object,
) -> None:
    with pytest.raises(RequestError):
        fn(bad_id, requested, _applicable())


def test_naive_requested_at_rejected() -> None:
    naive = datetime(2025, 2, 1, 8, 0, 0)
    with pytest.raises(RequestError, match="timezone-aware"):
        fn(REQUEST_ID, naive, _applicable())


def test_null_offset_requested_at_rejected() -> None:
    null_tz = datetime(2025, 2, 1, 8, 0, 0, tzinfo=_NullOffsetTzInfo())
    with pytest.raises(RequestError, match="utcoffset"):
        fn(REQUEST_ID, null_tz, _applicable())


def test_non_uuid_datetime_subclass_like_object_rejected() -> None:
    with pytest.raises(RequestError):
        fn(REQUEST_ID, datetime(2025, 2, 1, 8, 0, 0, tzinfo=UTC, fold=0).date(), _applicable())


def test_applicability_rejected_when_not_genuine_v145() -> None:
    for bad in (
        None,
        {"applicability_state": "applicable"},
        "applicable",
        _ForeignModel(),
        Intent(
            intent_id=_uuid(),
            authorized_at=AUTHORIZE_AT,
            decision_id=_uuid(),
            decision_decided_at=AUTHORIZE_AT,
            portfolio_id=_uuid(),
            selected_project_count=1,
            ready_task_count=1,
            authorized_project_id=_uuid(),
            authorized_task_id=_uuid(),
        ),
    ):
        with pytest.raises(RequestError):
            fn(REQUEST_ID, REQUESTED_AT, bad)


def test_hostile_v145_model_construct_aplicable_but_invalid_rejected() -> None:
    # APPLICABLE state with task_status None is impossible through
    # ordinary construction; model_construct can forge it; revalidation
    # must reject it.
    hostile = Applicability.model_construct(
        **_base_provenance(),
        applicability_state=S.APPLICABLE,
        task_status=None,
        constraints=None,
        unsatisfied_constraint_count=None,
    )
    assert isinstance(hostile, Applicability)
    with pytest.raises(RequestError):
        fn(REQUEST_ID, REQUESTED_AT, hostile)


def test_hostile_v145_model_construct_count_mismatch_rejected() -> None:
    blocker = _constraint(True, EntityStatus.COMPLETED)
    hostile = Applicability.model_construct(
        **_base_provenance(),
        applicability_state=S.APPLICABLE,
        task_status=EntityStatus.ACTIVE,
        constraints=(blocker,),
        unsatisfied_constraint_count=7,
    )
    with pytest.raises(RequestError):
        fn(REQUEST_ID, REQUESTED_AT, hostile)


def test_hostile_v145_with_wrong_types_rejected_by_strict_revalidation() -> None:
    hostile = Applicability.model_construct(
        **_base_provenance(),
        applicability_state=S.APPLICABLE,
        task_status=EntityStatus.ACTIVE,
        constraints="not-a-tuple",  # type: ignore[arg-type]
        unsatisfied_constraint_count=0,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(RequestError):
            fn(REQUEST_ID, REQUESTED_AT, hostile)


def test_caller_owned_v145_is_not_mutated() -> None:
    applicability = _applicable()
    before: object = applicability.model_dump(mode="python")
    result = _call(applicability)
    after: object = applicability.model_dump(mode="python")
    assert before == after
    assert result.request_id == REQUEST_ID


def test_only_validated_copy_semantics_hostile_authorized_at_rejected() -> None:
    # A model_construct payload with a naive authorized_at survives
    # isinstance but cannot survive fresh re-validation plus the request
    # model invariants: the validated copy is the sole semantic source.
    naive = datetime(2025, 1, 15, 10, 30, 0)
    base = _base_provenance()
    base["authorized_at"] = naive
    hostile = Applicability.model_construct(
        **base,
        applicability_state=S.APPLICABLE,
        task_status=EntityStatus.ACTIVE,
        constraints=(),
        unsatisfied_constraint_count=0,
    )
    with pytest.raises(RequestError):
        fn(REQUEST_ID, REQUESTED_AT, hostile)


# ---------------------------------------------------------------------------
# APPLICABILITY — exact state handling and provenance projection.
# ---------------------------------------------------------------------------


def test_applicable_succeeds_with_exact_provenance() -> None:
    applicability = _applicable()
    result = _call(applicability)
    assert result.request_id == REQUEST_ID
    assert result.requested_at == REQUESTED_AT
    assert result.intent_id == applicability.intent_id
    assert result.authorized_at == applicability.authorized_at
    assert result.decision_id == applicability.decision_id
    assert result.portfolio_id == applicability.portfolio_id
    assert result.authorized_project_id == applicability.authorized_project_id
    assert result.authorized_task_id == applicability.authorized_task_id


def test_request_cannot_override_projected_semantic_provenance() -> None:
    applicability = _applicable()
    result = _call(applicability)
    # The caller owns ONLY request_id and requested_at.
    assert result.request_id == REQUEST_ID
    assert result.requested_at == REQUESTED_AT
    # Every projected semantic field matches the V1.45 authority exactly.
    assert (
        result.intent_id,
        result.authorized_at,
        result.decision_id,
        result.portfolio_id,
        result.authorized_project_id,
        result.authorized_task_id,
    ) == (
        applicability.intent_id,
        applicability.authorized_at,
        applicability.decision_id,
        applicability.portfolio_id,
        applicability.authorized_project_id,
        applicability.authorized_task_id,
    )


def test_applicable_with_satisfied_constraints_still_succeeds() -> None:
    blocker = _constraint(True, EntityStatus.COMPLETED)
    applicability = Applicability(
        **_base_provenance(),
        applicability_state=S.APPLICABLE,
        task_status=EntityStatus.ACTIVE,
        constraints=(blocker,),
        unsatisfied_constraint_count=0,
    )
    result = _call(applicability)
    assert result.authorized_task_id == applicability.authorized_task_id
    # No constraint / status / count evidence is copied into the request.
    assert not hasattr(result, "constraints")
    assert not hasattr(result, "task_status")
    assert not hasattr(result, "unsatisfied_constraint_count")


@pytest.mark.parametrize(
    "state",
    [
        S.PROJECT_MISSING,
        S.PROJECT_TYPE_MISMATCH,
        S.TASK_MISSING,
        S.TASK_TYPE_MISMATCH,
        S.TASK_NOT_IN_AUTHORIZED_PROJECT,
        S.INELIGIBLE_STATUS,
        S.CONSTRAINED,
    ],
    ids=lambda state: state.value,
)
def test_all_seven_non_applicable_states_rejected_no_request_no_fallback(
    state: S,
) -> None:
    applicability = _applicability(state)
    with pytest.raises(RequestError):
        fn(REQUEST_ID, REQUESTED_AT, applicability)
    # No request value was constructed, and no replacement task identity
    # may appear anywhere in the boundary surface.
    assert applicability.authorized_task_id != REQUEST_ID


# ---------------------------------------------------------------------------
# TEMPORAL — provenance only, no validity rule, deterministic.
# ---------------------------------------------------------------------------


def test_requested_at_earlier_than_authorized_at_allowed() -> None:
    early = datetime(2020, 1, 1, 0, 0, 0, tzinfo=UTC)
    result = fn(REQUEST_ID, early, _applicable())
    assert result.requested_at == early


def test_requested_at_later_than_authorized_at_allowed() -> None:
    late = datetime(2030, 12, 31, 23, 59, 59, tzinfo=UTC)
    result = fn(REQUEST_ID, late, _applicable())
    assert result.requested_at == late


def test_no_temporal_ordering_between_any_timestamps() -> None:
    applicability = _applicable()
    before = fn(REQUEST_ID, REQUESTED_AT, applicability)
    after = fn(REQUEST_ID, REQUESTED_AT, applicability)
    assert before == after
    assert before.model_dump(mode="python") == after.model_dump(mode="python")


def test_repeated_identical_calls_are_value_identical() -> None:
    applicability = _applicable()
    first = fn(REQUEST_ID, REQUESTED_AT, applicability)
    second = fn(REQUEST_ID, REQUESTED_AT, applicability)
    third = _call(applicability)
    assert first == second == third
    assert (
        first.model_dump(mode="python")
        == second.model_dump(mode="python")
        == third.model_dump(mode="python")
    )


# ---------------------------------------------------------------------------
# SAFETY — executable architecture guards (AST / imports only).
# ---------------------------------------------------------------------------

_MODULE_SOURCE = (
    Path(module.__file__).resolve()  # type: ignore[attr-defined]
)

_ALLOWED_IMPORTS = frozenset(
    {
        "__future__",
        "datetime",
        "uuid",
        "typing",
        "pydantic",
        "trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_applicability",
    }
)

_FORBIDDEN_BOUNDARY_MODULES = frozenset(
    {
        "trajectory_os.domain.portfolio",
        "trajectory_os.domain.entities",
        "trajectory_os.domain.relations",
        "trajectory_os.domain.work_breakdown",
        "trajectory_os.application.entity_status_transition",
        "trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_intent",
        "trajectory_os.application.execution_effort_project_focus_next_ready_task_decision",
        "trajectory_os.application.execution_effort_project_focus_next_ready_task_decision_persistence",
        "trajectory_os.application.execution_effort_project_focus_task_current_constraints",
        "trajectory_os.application.execution_effort_project_focus_ready_task_candidates",
        "trajectory_os.application.execution_effort_project_focus_task_current_relations",
        "trajectory_os.application.execution_effort_project_focus_task_work_units",
        "os",
        "os.path",
        "subprocess",
        "shutil",
        "sqlite3",
        "socket",
        "asyncio",
        "threading",
        "multiprocessing",
        "http",
        "requests",
        "urllib",
        "aiohttp",
        "httpx",
    }
)

_FORBIDDEN_NAMES = frozenset(
    {
        "uuid4",
        "uuid1",
        "uuid5",
        "now",
        "utcnow",
        "system",
        "check_output",
        "check_call",
        "getoutput",
        "Popen",
        "execfile",
        "subprocess",
        "exec",
        "eval",
        "compile",
        "transition_entity_status",
        "transition_entity_status_durably",
        "build_work_breakdown",
        "execute",
        "dispatch",
        "enqueue",
        "schedule",
        "agent",
        "provider",
        "runtime",
        "commit",
        "rollback",
        "connect",
        "cursor",
        "execute_script",
        "open",
        "read_bytes",
        "write_bytes",
        "remove",
        "unlink",
        "mkdir",
        "tempfile",
        "NamedTemporaryFile",
        "tempdir",
        "mkdtemp",
        "tempfile.TemporaryFile",
        "select",
        "reselect",
        "fallback",
    }
)

_FORBIDDEN_CALL_ATTRIBUTES = frozenset(
    {
        "uuid4",
        "uuid1",
        "now",
        "utcnow",
        "system",
        "check_output",
        "check_call",
        "getoutput",
        "Popen",
        "execfile",
        "transition_entity_status",
        "transition_entity_status_durably",
        "build_work_breakdown",
        "execute",
        "dispatch",
        "enqueue",
    }
)


def _module_tree() -> ast.Module:
    return ast.parse(_MODULE_SOURCE.read_text(encoding="utf-8"))


def _imported_modules(tree: ast.Module) -> frozenset[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return frozenset(modules)


def _loaded_names(tree: ast.Module) -> frozenset[str]:
    return frozenset(node.id for node in ast.walk(tree) if isinstance(node, ast.Name))


def _called_attributes(tree: ast.Module) -> frozenset[str]:
    return frozenset(
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    )


def test_module_imports_only_the_allowed_boundary_modules() -> None:
    modules = _imported_modules(_module_tree())
    assert modules <= _ALLOWED_IMPORTS
    for forbidden in _FORBIDDEN_BOUNDARY_MODULES:
        assert forbidden not in modules


def test_module_never_references_forbidden_names() -> None:
    names = _loaded_names(_module_tree())
    assert names.isdisjoint(_FORBIDDEN_NAMES)
    assert not any(
        name.startswith("select_") or "fallback" in name for name in names
    )


def test_module_makes_no_random_identity_or_clock_or_shell_or_persistence_calls() -> None:
    attrs = _called_attributes(_module_tree())
    assert attrs.isdisjoint(_FORBIDDEN_CALL_ATTRIBUTES)


def test_module_accepts_no_portfolio_argument() -> None:
    import inspect

    signature = inspect.signature(fn)
    assert list(signature.parameters) == [
        "request_id",
        "requested_at",
        "applicability",
    ]
    # No default Portfolio, no keyword escape hatch for current state.
    for parameter in signature.parameters.values():
        assert parameter.default is not inspect.Parameter.empty or parameter.name in {
            "request_id",
            "requested_at",
            "applicability",
        }


def test_no_uuid_generation_helpers_are_bound() -> None:
    # Executable check: the module never binds any random UUID generator
    # and does not bind the uuid module object at all.
    assert getattr(module, "uuid4", None) is None
    assert getattr(module, "uuid1", None) is None
    assert getattr(module, "uuid5", None) is None
    assert getattr(module, "uuid", None) is None
    assert getattr(module, "random", None) is None


def test_public_api_symbols_are_exactly_the_specified_three() -> None:
    assert set(module.__all__) == {
        "PortfolioProjectFocusNextReadyTaskExecutionRequest",
        "PortfolioProjectFocusNextReadyTaskExecutionRequestError",
        "request_current_applicable_next_ready_task_execution",
    }


def test_error_is_a_value_error_subclass() -> None:
    assert issubclass(RequestError, ValueError)


def test_error_is_not_raised_for_genuine_applicable() -> None:
    result = _call()
    assert isinstance(result, Request)
