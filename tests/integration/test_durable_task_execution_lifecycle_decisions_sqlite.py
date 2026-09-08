"""V1.56 — SQLite append-only persistence for V1.55 human lifecycle decision
history.

Focused integration tests for
``SqliteTaskExecutionLifecycleDecisionRepository``: exact V1.55 record
round-trip, ADD revalidation before any write, duplicate PK translation,
deterministic aware-instant history ordering, strict portfolio scope gating,
scalar/snapshot corruption coherence, and executable (AST / surface)
architecture guards.
"""

from __future__ import annotations

import ast
import inspect as _inspect
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trajectory_os.adapters.persistence import (
    SqliteTaskExecutionLifecycleDecisionRepository,
)
from trajectory_os.adapters.persistence import (
    sqlite_task_execution_lifecycle_decisions as _adapter,
)
from trajectory_os.adapters.persistence.models import (
    PortfolioRow,
    TaskExecutionLifecycleDecisionRecordRow,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle import (  # noqa: E501
    TaskExecutionLifecycleDecision,
    TaskExecutionLifecycleDisposition,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_lifecycle_decision_persistence import (  # noqa: E501
    TaskExecutionLifecycleDecisionRecord,
)


def _uuid(value: int) -> UUID:
    return UUID(int=value)


_PORTFOLIO_ID = _uuid(100)
_TASK_ID = _uuid(105)
_LIFECYCLE_DECISION_ID = _uuid(110)
_EXEC_RECORD_ID = _uuid(120)
_REQUEST_ID = _uuid(130)
_INTENT_ID = _uuid(140)
_EXEC_DECISION_ID = _uuid(150)
_PROJECT_ID = _uuid(160)
_RECORDED_AT_DEFAULT = _uuid(200)

DECIDED_AT = datetime(2026, 9, 5, 12, 30, tzinfo=UTC)
EXECUTION_RECORDED_AT = datetime(
    2026, 9, 4, 16, 45, tzinfo=timezone(timedelta(hours=-5))
)
RECORDED_AT = datetime(
    2026, 9, 8, 23, 45, 12, tzinfo=timezone(timedelta(hours=5, minutes=30))
)


def _decision(
    *,
    portfolio_id: UUID = _PORTFOLIO_ID,
    disposition: TaskExecutionLifecycleDisposition = (
        TaskExecutionLifecycleDisposition.NO_LIFECYCLE_CHANGE
    ),
    execution_succeeded: bool = True,
) -> TaskExecutionLifecycleDecision:
    """One genuine V1.50 decision with valid V1.50 invariants."""

    return TaskExecutionLifecycleDecision(
        lifecycle_decision_id=_LIFECYCLE_DECISION_ID,
        decided_at=DECIDED_AT,
        execution_record_id=_EXEC_RECORD_ID,
        execution_recorded_at=EXECUTION_RECORDED_AT,
        request_id=_REQUEST_ID,
        intent_id=_INTENT_ID,
        execution_decision_id=_EXEC_DECISION_ID,
        portfolio_id=portfolio_id,
        authorized_project_id=_PROJECT_ID,
        authorized_task_id=_TASK_ID,
        execution_succeeded=execution_succeeded,
        disposition=disposition,
    )


def _record(
    *,
    decision_record_id: UUID = _RECORDED_AT_DEFAULT,
    recorded_at: datetime = RECORDED_AT,
    decision: TaskExecutionLifecycleDecision | None = None,
) -> TaskExecutionLifecycleDecisionRecord:
    return TaskExecutionLifecycleDecisionRecord(
        decision_record_id=decision_record_id,
        recorded_at=recorded_at,
        decision=decision if decision is not None else _decision(),
    )


def _seed_portfolio(
    repository: SqliteTaskExecutionLifecycleDecisionRepository,
    portfolio_id: UUID,
) -> None:
    with Session(repository.engine) as session:
        session.execute(
            insert(PortfolioRow).values(
                id=str(portfolio_id),
                name=f"portfolio-{portfolio_id}",
            )
        )
        session.commit()


# ---------------------------------------------------------------------------
# ADD
# ---------------------------------------------------------------------------


def test_round_trip_preserves_exact_record_and_original_offsets(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "lifecycle.sqlite"
    repository = SqliteTaskExecutionLifecycleDecisionRepository(database_path)

    _seed_portfolio(repository, _PORTFOLIO_ID)
    record = _record()

    repository.add(record)

    history = repository.list_history(_PORTFOLIO_ID)

    assert history == (record,)
    assert history[0].decision_record_id == record.decision_record_id
    assert history[0].recorded_at == record.recorded_at
    assert history[0].recorded_at.isoformat() == record.recorded_at.isoformat()
    assert history[0].recorded_at.utcoffset() == timedelta(
        hours=5, minutes=30
    )
    assert history[0].decision == record.decision
    assert history[0].decision.decided_at.isoformat() == (
        record.decision.decided_at.isoformat()
    )
    assert history[0].decision.execution_recorded_at == (
        record.decision.execution_recorded_at
    )
    assert history[0].decision.execution_recorded_at.utcoffset() == timedelta(
        hours=-5
    )

    repository.close()


def test_add_requires_a_genuine_v155_record(tmp_path: Path) -> None:
    repository = SqliteTaskExecutionLifecycleDecisionRepository(
        tmp_path / "lifecycle.sqlite"
    )

    _seed_portfolio(repository, _PORTFOLIO_ID)

    with pytest.raises(TypeError):
        repository.add(_decision())

    with pytest.raises(TypeError):
        repository.add(None)

    # Nothing was written: empty history.
    assert repository.list_history(_PORTFOLIO_ID) == ()

    repository.close()


def test_add_revalidates_the_full_v155_outer_record_freshly(
    tmp_path: Path,
) -> None:
    """A hostile outer record that bypasses the record validator (naive
    recorded_at) must be rejected by ADD's fresh strict revalidation before
    any row is written."""

    repository = SqliteTaskExecutionLifecycleDecisionRepository(
        tmp_path / "lifecycle.sqlite"
    )

    _seed_portfolio(repository, _PORTFOLIO_ID)

    hostile = TaskExecutionLifecycleDecisionRecord.model_construct(
        decision_record_id=_uuid(300),
        recorded_at=RECORDED_AT.replace(tzinfo=None),  # naive: bypasses validator
        decision=_decision(),
    )

    with pytest.raises(ValueError):
        repository.add(hostile)

    # No row was written.
    with Session(repository.engine) as session:
        rows = session.scalars(select(TaskExecutionLifecycleDecisionRecordRow)).all()
    assert rows == []

    repository.close()


def test_nested_hostile_v150_decision_rejected_before_db_write(
    tmp_path: Path,
) -> None:
    """A V1.50 decision claiming COMPLETE_TASK over a failed execution is
    semantically invalid and must be rejected before any write."""

    repository = SqliteTaskExecutionLifecycleDecisionRepository(
        tmp_path / "lifecycle.sqlite"
    )

    _seed_portfolio(repository, _PORTFOLIO_ID)

    hostile_decision = TaskExecutionLifecycleDecision.model_construct(
        lifecycle_decision_id=_LIFECYCLE_DECISION_ID,
        decided_at=DECIDED_AT,
        execution_record_id=_EXEC_RECORD_ID,
        execution_recorded_at=EXECUTION_RECORDED_AT,
        request_id=_REQUEST_ID,
        intent_id=_INTENT_ID,
        execution_decision_id=_EXEC_DECISION_ID,
        portfolio_id=_PORTFOLIO_ID,
        authorized_project_id=_PROJECT_ID,
        authorized_task_id=_TASK_ID,
        execution_succeeded=False,
        disposition=TaskExecutionLifecycleDisposition.COMPLETE_TASK,
    )
    hostile_record = TaskExecutionLifecycleDecisionRecord.model_construct(
        decision_record_id=_uuid(301),
        recorded_at=RECORDED_AT,
        decision=hostile_decision,
    )

    with pytest.raises(ValueError):
        repository.add(hostile_record)

    with Session(repository.engine) as session:
        rows = session.scalars(select(TaskExecutionLifecycleDecisionRecordRow)).all()
    assert rows == []

    repository.close()


def test_decision_record_id_is_persisted_exactly(tmp_path: Path) -> None:
    repository = SqliteTaskExecutionLifecycleDecisionRepository(
        tmp_path / "lifecycle.sqlite"
    )

    distinct = _uuid(424242)
    _seed_portfolio(repository, _PORTFOLIO_ID)
    record = _record(decision_record_id=distinct)

    repository.add(record)

    with Session(repository.engine) as session:
        row = session.scalars(
            select(TaskExecutionLifecycleDecisionRecordRow)
        ).first()
    assert row is not None
    assert row.decision_record_id == str(distinct)
    assert repository.list_history(_PORTFOLIO_ID)[0].decision_record_id == (
        distinct
    )

    repository.close()


def test_duplicate_decision_record_id_is_rejected_by_db_constraint(
    tmp_path: Path,
) -> None:
    repository = SqliteTaskExecutionLifecycleDecisionRepository(
        tmp_path / "lifecycle.sqlite"
    )

    _seed_portfolio(repository, _PORTFOLIO_ID)

    original = _record(
        decision_record_id=_uuid(400),
        recorded_at=datetime(2026, 9, 8, 8, 0, tzinfo=UTC),
    )
    conflicting = _record(
        decision_record_id=_uuid(400),
        recorded_at=datetime(2026, 9, 9, 9, 0, tzinfo=UTC),
    )

    repository.add(original)

    with pytest.raises(
        _adapter.DuplicateTaskExecutionLifecycleDecisionRecordError,
        match="already exists",
    ):
        repository.add(conflicting)

    # Duplicate failure preserves the original row unchanged.
    assert repository.list_history(_PORTFOLIO_ID) == (original,)

    repository.close()


def test_equivalent_decisions_are_preserved_under_distinct_record_ids(
    tmp_path: Path,
) -> None:
    repository = SqliteTaskExecutionLifecycleDecisionRepository(
        tmp_path / "lifecycle.sqlite"
    )

    _seed_portfolio(repository, _PORTFOLIO_ID)

    exact_decision = _decision()
    first = _record(
        decision_record_id=_uuid(401),
        recorded_at=datetime(2026, 9, 8, 8, 0, tzinfo=UTC),
        decision=exact_decision,
    )
    second = _record(
        decision_record_id=_uuid(402),
        recorded_at=datetime(2026, 9, 8, 9, 0, tzinfo=UTC),
        decision=exact_decision,
    )

    repository.add(first)
    repository.add(second)

    assert repository.list_history(_PORTFOLIO_ID) == (first, second)

    repository.close()


def test_foreign_key_integrity_error_is_not_misclassified_as_duplicate(
    tmp_path: Path,
) -> None:
    repository = SqliteTaskExecutionLifecycleDecisionRepository(
        tmp_path / "foreign-key.sqlite"
    )

    # Deliberately do NOT seed the referenced portfolio.
    record = _record(
        decision_record_id=_uuid(403),
        decision=_decision(portfolio_id=_uuid(800)),
    )

    with pytest.raises(IntegrityError) as exc_info:
        repository.add(record)

    assert "FOREIGN KEY constraint failed" in str(exc_info.value.orig)
    assert not isinstance(
        exc_info.value,
        _adapter.DuplicateTaskExecutionLifecycleDecisionRecordError,
    )

    repository.close()


# ---------------------------------------------------------------------------
# HISTORY
# ---------------------------------------------------------------------------


def test_empty_history_returns_empty_tuple(tmp_path: Path) -> None:
    repository = SqliteTaskExecutionLifecycleDecisionRepository(
        tmp_path / "lifecycle.sqlite"
    )

    assert repository.list_history(_uuid(999)) == ()

    repository.close()


def test_list_history_accepts_genuine_uuid_portfolio_scope(
    tmp_path: Path,
) -> None:
    """A genuine UUID scope passes the identity gate normally."""

    database_path = tmp_path / "scope-genuine.sqlite"
    repository = SqliteTaskExecutionLifecycleDecisionRepository(database_path)

    _seed_portfolio(repository, _PORTFOLIO_ID)
    record = _record(decision_record_id=_uuid(210))
    repository.add(record)

    assert repository.list_history(_PORTFOLIO_ID) == (record,)

    repository.close()


class _EngineTouchSpy:
    """Raises on ANY engine attribute access, proving no DB interaction."""

    def __init__(self) -> None:
        self.touches = 0

    def __getattr__(self, name: str) -> object:
        self.touches += 1
        raise AssertionError(
            f"list_history must not touch the engine ({name!r}) for a "
            "non-UUID portfolio scope"
        )


@pytest.mark.parametrize(
    "invalid_portfolio_id",
    [
        str(_PORTFOLIO_ID),  # string representation of a genuine UUID
        "0b091172-6166-464a-8b72-f1b803b4a6a9",  # arbitrary string
        "portfolio",  # plain non-UUID string
        42,  # int
        None,  # None
    ],
    ids=[
        "stringified-uuid",
        "arbitrary-uuid-string",
        "arbitrary-string",
        "int",
        "none",
    ],
)
def test_list_history_rejects_non_uuid_scope_before_any_db_interaction(
    tmp_path: Path,
    invalid_portfolio_id: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strict portfolio scope identity before repository access.

    The failure must happen BEFORE any Session / engine / SQL interaction,
    proven by executable spies, not merely by an empty result.
    """

    repository = SqliteTaskExecutionLifecycleDecisionRepository(
        tmp_path / "scope-invalid.sqlite"
    )

    session_touches = 0

    def _no_session(*args: object, **kwargs: object) -> object:
        nonlocal session_touches
        session_touches += 1
        raise AssertionError(
            "list_history must not open a session for a non-UUID scope"
        )

    engine_spy = _EngineTouchSpy()
    original_engine = repository._engine

    monkeypatch.setattr(_adapter, "Session", _no_session, raising=False)
    monkeypatch.setattr(
        repository, "_engine", engine_spy, raising=False
    )
    try:
        with pytest.raises(
            TypeError, match="portfolio_id must already be a genuine UUID"
        ):
            repository.list_history(invalid_portfolio_id)
    finally:
        repository._engine = original_engine
        repository.close()

    assert session_touches == 0
    assert engine_spy.touches == 0


def test_history_is_scoped_to_exact_portfolio(tmp_path: Path) -> None:
    repository = SqliteTaskExecutionLifecycleDecisionRepository(
        tmp_path / "lifecycle.sqlite"
    )

    first_portfolio = _uuid(100)
    second_portfolio = _uuid(300)
    _seed_portfolio(repository, first_portfolio)
    _seed_portfolio(repository, second_portfolio)

    first = _record(
        decision_record_id=_uuid(201),
        decision=_decision(portfolio_id=first_portfolio),
    )
    second = _record(
        decision_record_id=_uuid(202),
        decision=_decision(portfolio_id=second_portfolio),
    )

    repository.add(first)
    repository.add(second)

    assert repository.list_history(first_portfolio) == (first,)
    assert repository.list_history(second_portfolio) == (second,)

    repository.close()


def test_history_orders_by_true_aware_instant_not_iso_text(
    tmp_path: Path,
) -> None:
    repository = SqliteTaskExecutionLifecycleDecisionRepository(
        tmp_path / "lifecycle.sqlite"
    )

    _seed_portfolio(repository, _PORTFOLIO_ID)

    earlier_true_instant = _record(
        decision_record_id=_uuid(203),
        recorded_at=datetime(2026, 9, 8, 10, 0, tzinfo=timezone(timedelta(hours=2))),
    )
    later_true_instant = _record(
        decision_record_id=_uuid(201),
        recorded_at=datetime(2026, 9, 8, 8, 30, tzinfo=UTC),
    )

    # Lexically, "08:30+00:00" sorts before "10:00+02:00", but
    # chronologically 10:00+02:00 == 08:00Z and is earlier.
    repository.add(later_true_instant)
    repository.add(earlier_true_instant)

    assert repository.list_history(_PORTFOLIO_ID) == (
        earlier_true_instant,
        later_true_instant,
    )

    repository.close()


def test_equal_instants_use_decision_record_uuid_integer_tie_breaker(
    tmp_path: Path,
) -> None:
    repository = SqliteTaskExecutionLifecycleDecisionRepository(
        tmp_path / "lifecycle.sqlite"
    )

    _seed_portfolio(repository, _PORTFOLIO_ID)

    same_instant_a = datetime(2026, 9, 8, 8, 0, tzinfo=UTC)
    same_instant_b = datetime(2026, 9, 8, 10, 0, tzinfo=timezone(timedelta(hours=2)))

    lower_uuid = _record(decision_record_id=_uuid(201), recorded_at=same_instant_a)
    higher_uuid = _record(
        decision_record_id=_uuid(209),
        recorded_at=same_instant_b,
    )

    repository.add(higher_uuid)
    repository.add(lower_uuid)

    assert repository.list_history(_PORTFOLIO_ID) == (lower_uuid, higher_uuid)

    repository.close()


def test_persistence_survives_independent_repository_reopen(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "lifecycle.sqlite"
    record = _record()

    first_repository = SqliteTaskExecutionLifecycleDecisionRepository(
        database_path
    )
    _seed_portfolio(first_repository, _PORTFOLIO_ID)
    first_repository.add(record)
    first_repository.close()

    second_repository = (
        SqliteTaskExecutionLifecycleDecisionRepository(database_path)
    )

    assert second_repository.list_history(_PORTFOLIO_ID) == (record,)

    second_repository.close()


def test_read_back_reconstructs_exact_public_v155_record(
    tmp_path: Path,
) -> None:
    repository = SqliteTaskExecutionLifecycleDecisionRepository(
        tmp_path / "lifecycle.sqlite"
    )

    _seed_portfolio(repository, _PORTFOLIO_ID)
    record = _record()

    repository.add(record)

    rebuilt = repository.list_history(_PORTFOLIO_ID)[0]

    assert type(rebuilt) is TaskExecutionLifecycleDecisionRecord
    assert rebuilt == record
    assert rebuilt.model_dump(mode="python") == record.model_dump(mode="python")
    assert rebuilt.decision == record.decision
    assert rebuilt.decision.portfolio_id == record.decision.portfolio_id
    assert rebuilt.decision.disposition is record.decision.disposition

    repository.close()


# ---------------------------------------------------------------------------
# CORRUPTION
# ---------------------------------------------------------------------------


def _update_row(
    repository: SqliteTaskExecutionLifecycleDecisionRepository,
    decision_record_id: UUID,
    **values: object,
) -> None:
    with Session(repository.engine) as session:
        session.execute(
            update(TaskExecutionLifecycleDecisionRecordRow)
            .where(
                TaskExecutionLifecycleDecisionRecordRow.decision_record_id
                == str(decision_record_id)
            )
            .values(**values)
        )
        session.commit()


def test_malformed_decision_snapshot_is_rejected_on_read(
    tmp_path: Path,
) -> None:
    repository = SqliteTaskExecutionLifecycleDecisionRepository(
        tmp_path / "lifecycle.sqlite"
    )

    _seed_portfolio(repository, _PORTFOLIO_ID)
    record = _record(decision_record_id=_uuid(501))
    repository.add(record)

    _update_row(
        repository,
        record.decision_record_id,
        decision_snapshot="{not-valid-json",
    )

    with pytest.raises(ValueError):
        repository.list_history(_PORTFOLIO_ID)

    repository.close()


@pytest.mark.parametrize(
    ("column_name", "corrupt_value", "match"),
    [
        ("lifecycle_decision_id", str(_uuid(602)), "lifecycle_decision_id"),
        ("execution_record_id", str(_uuid(603)), "execution_record_id"),
        ("authorized_task_id", str(_uuid(604)), "authorized_task_id"),
        ("disposition", "NOT_A_REAL_DISPOSITION", "disposition"),
        ("decided_at", "2020-01-01T00:00:00+00:00", "decided_at"),
    ],
)
def test_explicit_scalar_column_snapshot_mismatch_is_rejected(
    tmp_path: Path,
    column_name: str,
    corrupt_value: str,
    match: str,
) -> None:
    repository = SqliteTaskExecutionLifecycleDecisionRepository(
        tmp_path / f"{column_name}.sqlite"
    )

    _seed_portfolio(repository, _PORTFOLIO_ID)
    record = _record(decision_record_id=_uuid(510))
    repository.add(record)

    _update_row(
        repository,
        record.decision_record_id,
        **{column_name: corrupt_value},
    )

    with pytest.raises(ValueError, match=match):
        repository.list_history(_PORTFOLIO_ID)

    repository.close()


def test_portfolio_column_snapshot_mismatch_is_rejected(
    tmp_path: Path,
) -> None:
    repository = SqliteTaskExecutionLifecycleDecisionRepository(
        tmp_path / "portfolio-mismatch.sqlite"
    )

    original_portfolio_id = _uuid(100)
    corrupt_portfolio_id = _uuid(700)

    _seed_portfolio(repository, original_portfolio_id)
    _seed_portfolio(repository, corrupt_portfolio_id)

    record = _record(
        decision_record_id=_uuid(511),
        decision=_decision(portfolio_id=original_portfolio_id),
    )
    repository.add(record)

    _update_row(
        repository,
        record.decision_record_id,
        portfolio_id=str(corrupt_portfolio_id),
    )

    with pytest.raises(ValueError, match="portfolio_id"):
        repository.list_history(corrupt_portfolio_id)

    repository.close()


@pytest.mark.parametrize(
    "invalid_timestamp",
    [
        "not-a-datetime",
        "2026-9-8T10:00:00",  # naive, no offset
    ],
)
def test_invalid_stored_recorded_at_is_rejected_not_normalized(
    tmp_path: Path,
    invalid_timestamp: str,
) -> None:
    repository = SqliteTaskExecutionLifecycleDecisionRepository(
        tmp_path / "recorded-at.sqlite"
    )

    _seed_portfolio(repository, _PORTFOLIO_ID)
    record = _record(decision_record_id=_uuid(513))
    repository.add(record)

    _update_row(
        repository,
        record.decision_record_id,
        recorded_at=invalid_timestamp,
    )

    # A naive / invalid stored timestamp is rejected, never silently
    # normalized to an aware value.
    with pytest.raises(ValueError):
        repository.list_history(_PORTFOLIO_ID)

    repository.close()


# ---------------------------------------------------------------------------
# ARCHITECTURE (executable surface / AST guards)
# ---------------------------------------------------------------------------


def test_repository_exposes_only_append_and_read_api() -> None:
    public = [
        name
        for name, member in vars(SqliteTaskExecutionLifecycleDecisionRepository).items()
        if not name.startswith("__") and callable(member)
    ]

    forbidden = {
        "update",
        "delete",
        "replace",
        "upsert",
        "save",
        "patch",
        "latest",
        "current",
        "effective",
        "supersede",
    }

    assert not (forbidden & set(public)), (
        "repository must not expose mutable / current / effective APIs: "
        f"{sorted(forbidden & set(public))}"
    )
    assert "add" in public
    assert "list_history" in public


def test_module_never_invokes_decision_or_application_or_io() -> None:
    source = inspect_source(_adapter)
    tree = ast.parse(source)

    called_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            called_names.add(_callee_name(node.func))

    forbidden = {
        "decide_task_execution_lifecycle",
        "record_task_execution_lifecycle_decision_durably",
        "transition_entity_status",
        "load",
        "save",
        "uuid4",
        "now",
        "utcnow",
        "subprocess",
        "open",
        "Popen",
        "run",
        "shell",
    }

    # `open` is a builtin we never use; the adapter does not call it.
    assert not (forbidden & called_names), (
        "adapter must not invoke V1.50 decision creation, lifecycle "
        "transitions, Portfolio load/save, clock/UUID generation, or IO: "
        f"{sorted(forbidden & called_names)}"
    )


def test_module_does_not_reference_forbidden_symbols() -> None:
    source = inspect_source(_adapter)

    forbidden = {
        "model_construct",
        "model_dump_update",
        "uuid4",
        "utcnow",
        "subprocess",
        "datetime.now",
        "datetime.utcnow",
        "pickle",
        "decide_task_execution_lifecycle",
        "transition_entity_status",
    }

    hits = sorted(symbol for symbol in forbidden if symbol in source)
    assert hits == [], (
        "adapter must not reference V1.50 decision creation, transitions, "
        f"clock/UUID generation, subprocess, pickle, or model_construct: {hits}"
    )


def inspect_source(module: object) -> str:
    return _inspect.getsource(module) if hasattr(module, "__file__") else ""


def _callee_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""
