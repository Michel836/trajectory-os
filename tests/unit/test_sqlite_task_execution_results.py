from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import insert
from sqlalchemy.orm import Session

from trajectory_os.adapters.persistence.models import PortfolioRow
from trajectory_os.adapters.persistence.sqlite_task_execution_results import (
    DuplicateTaskExecutionResultRecordError,
    SqliteTaskExecutionResultRepository,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution import (
    TaskExecutionResult,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_execution_persistence import (  # noqa: E501
    TaskExecutionResultRecord,
)


def _uuid(value: int) -> UUID:
    return UUID(int=value)


_DEFAULT_PORTFOLIO_ID = _uuid(100)
_DEFAULT_REQUEST_ID = _uuid(101)
_DEFAULT_INTENT_ID = _uuid(102)
_DEFAULT_DECISION_ID = _uuid(103)
_DEFAULT_PROJECT_ID = _uuid(104)
_DEFAULT_TASK_ID = _uuid(105)
_DEFAULT_EXECUTION_RECORD_ID = _uuid(200)


def _result(
    *,
    portfolio_id: UUID = _DEFAULT_PORTFOLIO_ID,
    request_id: UUID = _DEFAULT_REQUEST_ID,
    intent_id: UUID = _DEFAULT_INTENT_ID,
    decision_id: UUID = _DEFAULT_DECISION_ID,
    project_id: UUID = _DEFAULT_PROJECT_ID,
    task_id: UUID = _DEFAULT_TASK_ID,
    succeeded: bool = True,
) -> TaskExecutionResult:
    return TaskExecutionResult(
        request_id=request_id,
        intent_id=intent_id,
        decision_id=decision_id,
        portfolio_id=portfolio_id,
        authorized_project_id=project_id,
        authorized_task_id=task_id,
        succeeded=succeeded,
    )


def _record(
    *,
    execution_record_id: UUID = _DEFAULT_EXECUTION_RECORD_ID,
    recorded_at: datetime = datetime(
        2026,
        9,
        8,
        10,
        15,
        tzinfo=timezone(timedelta(hours=2)),
    ),
    result: TaskExecutionResult | None = None,
) -> TaskExecutionResultRecord:
    return TaskExecutionResultRecord(
        execution_record_id=execution_record_id,
        recorded_at=recorded_at,
        result=result if result is not None else _result(),
    )


def _seed_portfolio(
    repository: SqliteTaskExecutionResultRepository,
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


def test_round_trip_preserves_exact_record_and_original_offset(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "execution-results.sqlite"
    repository = SqliteTaskExecutionResultRepository(database_path)

    portfolio_id = _uuid(100)
    _seed_portfolio(repository, portfolio_id)

    recorded_at = datetime(
        2026,
        9,
        8,
        23,
        45,
        12,
        345678,
        tzinfo=timezone(timedelta(hours=5, minutes=30)),
    )
    record = _record(recorded_at=recorded_at)

    repository.add(record)

    history = repository.list_history(portfolio_id)

    assert history == (record,)
    assert history[0].recorded_at == recorded_at
    assert history[0].recorded_at.isoformat() == recorded_at.isoformat()
    assert history[0].result == record.result

    repository.close()


def test_empty_history_returns_empty_tuple(tmp_path: Path) -> None:
    repository = SqliteTaskExecutionResultRepository(
        tmp_path / "execution-results.sqlite"
    )

    assert repository.list_history(_uuid(999)) == ()

    repository.close()


def test_history_is_scoped_to_exact_portfolio(tmp_path: Path) -> None:
    repository = SqliteTaskExecutionResultRepository(
        tmp_path / "execution-results.sqlite"
    )

    first_portfolio = _uuid(100)
    second_portfolio = _uuid(300)
    _seed_portfolio(repository, first_portfolio)
    _seed_portfolio(repository, second_portfolio)

    first = _record(
        execution_record_id=_uuid(201),
        result=_result(portfolio_id=first_portfolio),
    )
    second = _record(
        execution_record_id=_uuid(202),
        result=_result(
            portfolio_id=second_portfolio,
            request_id=_uuid(301),
            intent_id=_uuid(302),
            decision_id=_uuid(303),
            project_id=_uuid(304),
            task_id=_uuid(305),
        ),
    )

    repository.add(first)
    repository.add(second)

    assert repository.list_history(first_portfolio) == (first,)
    assert repository.list_history(second_portfolio) == (second,)

    repository.close()


def test_history_orders_by_true_aware_instant_not_iso_text(
    tmp_path: Path,
) -> None:
    repository = SqliteTaskExecutionResultRepository(
        tmp_path / "execution-results.sqlite"
    )

    portfolio_id = _uuid(100)
    _seed_portfolio(repository, portfolio_id)

    earlier_true_instant = _record(
        execution_record_id=_uuid(203),
        recorded_at=datetime(
            2026,
            9,
            8,
            10,
            0,
            tzinfo=timezone(timedelta(hours=2)),
        ),
    )
    later_true_instant = _record(
        execution_record_id=_uuid(201),
        recorded_at=datetime(
            2026,
            9,
            8,
            8,
            30,
            tzinfo=UTC,
        ),
    )

    # Lexically, "08:30+00:00" sorts before "10:00+02:00",
    # but chronologically 10:00+02:00 == 08:00Z and is earlier.
    repository.add(later_true_instant)
    repository.add(earlier_true_instant)

    assert repository.list_history(portfolio_id) == (
        earlier_true_instant,
        later_true_instant,
    )

    repository.close()


def test_equal_instants_use_execution_record_uuid_integer_tie_breaker(
    tmp_path: Path,
) -> None:
    repository = SqliteTaskExecutionResultRepository(
        tmp_path / "execution-results.sqlite"
    )

    portfolio_id = _uuid(100)
    _seed_portfolio(repository, portfolio_id)

    same_instant_a = datetime(
        2026,
        9,
        8,
        8,
        0,
        tzinfo=UTC,
    )
    same_instant_b = datetime(
        2026,
        9,
        8,
        10,
        0,
        tzinfo=timezone(timedelta(hours=2)),
    )

    lower_uuid = _record(
        execution_record_id=_uuid(201),
        recorded_at=same_instant_a,
    )
    higher_uuid = _record(
        execution_record_id=_uuid(209),
        recorded_at=same_instant_b,
    )

    repository.add(higher_uuid)
    repository.add(lower_uuid)

    assert repository.list_history(portfolio_id) == (
        lower_uuid,
        higher_uuid,
    )

    repository.close()


def test_equivalent_results_are_preserved_under_distinct_record_ids(
    tmp_path: Path,
) -> None:
    repository = SqliteTaskExecutionResultRepository(
        tmp_path / "execution-results.sqlite"
    )

    portfolio_id = _uuid(100)
    _seed_portfolio(repository, portfolio_id)

    exact_result = _result()
    first = _record(
        execution_record_id=_uuid(201),
        recorded_at=datetime(
            2026,
            9,
            8,
            8,
            0,
            tzinfo=UTC,
        ),
        result=exact_result,
    )
    second = _record(
        execution_record_id=_uuid(202),
        recorded_at=datetime(
            2026,
            9,
            8,
            9,
            0,
            tzinfo=UTC,
        ),
        result=exact_result,
    )

    repository.add(first)
    repository.add(second)

    assert repository.list_history(portfolio_id) == (first, second)

    repository.close()


def test_duplicate_record_id_is_rejected_without_modifying_existing_row(
    tmp_path: Path,
) -> None:
    repository = SqliteTaskExecutionResultRepository(
        tmp_path / "execution-results.sqlite"
    )

    portfolio_id = _uuid(100)
    _seed_portfolio(repository, portfolio_id)

    original = _record(
        execution_record_id=_uuid(201),
        result=_result(succeeded=True),
    )
    conflicting = _record(
        execution_record_id=original.execution_record_id,
        recorded_at=datetime(
            2026,
            9,
            9,
            12,
            0,
            tzinfo=UTC,
        ),
        result=_result(
            request_id=_uuid(401),
            intent_id=_uuid(402),
            decision_id=_uuid(403),
            project_id=_uuid(404),
            task_id=_uuid(405),
            succeeded=False,
        ),
    )

    repository.add(original)

    with pytest.raises(
        DuplicateTaskExecutionResultRecordError,
        match="already exists",
    ):
        repository.add(conflicting)

    assert repository.list_history(portfolio_id) == (original,)

    repository.close()


def test_persistence_survives_independent_repository_reopen(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "execution-results.sqlite"
    portfolio_id = _uuid(100)
    record = _record()

    first_repository = SqliteTaskExecutionResultRepository(database_path)
    _seed_portfolio(first_repository, portfolio_id)
    first_repository.add(record)
    first_repository.close()

    second_repository = SqliteTaskExecutionResultRepository(database_path)

    assert second_repository.list_history(portfolio_id) == (record,)

    second_repository.close()


def _update_execution_result_row(
    repository: SqliteTaskExecutionResultRepository,
    execution_record_id: UUID,
    **values: object,
) -> None:
    from sqlalchemy import update

    from trajectory_os.adapters.persistence.models import (
        TaskExecutionResultRecordRow,
    )

    with Session(repository.engine) as session:
        session.execute(
            update(TaskExecutionResultRecordRow)
            .where(
                TaskExecutionResultRecordRow.execution_record_id
                == str(execution_record_id)
            )
            .values(**values)
        )
        session.commit()


def test_malformed_result_snapshot_is_rejected_on_read(
    tmp_path: Path,
) -> None:
    repository = SqliteTaskExecutionResultRepository(
        tmp_path / "execution-results.sqlite"
    )

    portfolio_id = _uuid(100)
    _seed_portfolio(repository, portfolio_id)

    record = _record(execution_record_id=_uuid(501))
    repository.add(record)

    _update_execution_result_row(
        repository,
        record.execution_record_id,
        result_snapshot="{not-valid-json",
    )

    with pytest.raises(ValueError):
        repository.list_history(portfolio_id)

    repository.close()


@pytest.mark.parametrize(
    ("column_name", "corrupt_value"),
    [
        ("request_id", str(_uuid(601))),
        ("intent_id", str(_uuid(602))),
        ("decision_id", str(_uuid(603))),
        ("authorized_project_id", str(_uuid(604))),
        ("authorized_task_id", str(_uuid(605))),
    ],
)
def test_explicit_identity_column_snapshot_mismatch_is_rejected(
    tmp_path: Path,
    column_name: str,
    corrupt_value: str,
) -> None:
    repository = SqliteTaskExecutionResultRepository(
        tmp_path / f"{column_name}.sqlite"
    )

    portfolio_id = _uuid(100)
    _seed_portfolio(repository, portfolio_id)

    record = _record(execution_record_id=_uuid(510))
    repository.add(record)

    _update_execution_result_row(
        repository,
        record.execution_record_id,
        **{column_name: corrupt_value},
    )

    with pytest.raises(
        ValueError,
        match=column_name,
    ):
        repository.list_history(portfolio_id)

    repository.close()


def test_portfolio_column_snapshot_mismatch_is_rejected(
    tmp_path: Path,
) -> None:
    repository = SqliteTaskExecutionResultRepository(
        tmp_path / "portfolio-mismatch.sqlite"
    )

    original_portfolio_id = _uuid(100)
    corrupt_portfolio_id = _uuid(700)

    _seed_portfolio(repository, original_portfolio_id)
    _seed_portfolio(repository, corrupt_portfolio_id)

    record = _record(
        execution_record_id=_uuid(511),
        result=_result(portfolio_id=original_portfolio_id),
    )
    repository.add(record)

    _update_execution_result_row(
        repository,
        record.execution_record_id,
        portfolio_id=str(corrupt_portfolio_id),
    )

    with pytest.raises(
        ValueError,
        match="portfolio_id",
    ):
        repository.list_history(corrupt_portfolio_id)

    repository.close()


def test_succeeded_column_snapshot_mismatch_is_rejected(
    tmp_path: Path,
) -> None:
    repository = SqliteTaskExecutionResultRepository(
        tmp_path / "succeeded-mismatch.sqlite"
    )

    portfolio_id = _uuid(100)
    _seed_portfolio(repository, portfolio_id)

    record = _record(
        execution_record_id=_uuid(512),
        result=_result(succeeded=True),
    )
    repository.add(record)

    _update_execution_result_row(
        repository,
        record.execution_record_id,
        succeeded=0,
    )

    with pytest.raises(
        ValueError,
        match="succeeded",
    ):
        repository.list_history(portfolio_id)

    repository.close()


@pytest.mark.parametrize("invalid_succeeded", [-1, 2, 7])
def test_invalid_succeeded_storage_representation_is_rejected(
    tmp_path: Path,
    invalid_succeeded: int,
) -> None:
    repository = SqliteTaskExecutionResultRepository(
        tmp_path / f"succeeded-{invalid_succeeded}.sqlite"
    )

    portfolio_id = _uuid(100)
    _seed_portfolio(repository, portfolio_id)

    record = _record(execution_record_id=_uuid(513))
    repository.add(record)

    _update_execution_result_row(
        repository,
        record.execution_record_id,
        succeeded=invalid_succeeded,
    )

    with pytest.raises(
        ValueError,
        match="expected exactly 0 or 1",
    ):
        repository.list_history(portfolio_id)

    repository.close()


def test_foreign_key_integrity_error_is_not_misclassified_as_duplicate(
    tmp_path: Path,
) -> None:
    from sqlalchemy.exc import IntegrityError

    repository = SqliteTaskExecutionResultRepository(
        tmp_path / "foreign-key.sqlite"
    )

    # Deliberately do NOT seed the referenced portfolio.
    record = _record(
        execution_record_id=_uuid(514),
        result=_result(portfolio_id=_uuid(800)),
    )

    with pytest.raises(IntegrityError) as exc_info:
        repository.add(record)

    assert "FOREIGN KEY constraint failed" in str(exc_info.value.orig)
    assert not isinstance(
        exc_info.value,
        DuplicateTaskExecutionResultRecordError,
    )

    repository.close()
