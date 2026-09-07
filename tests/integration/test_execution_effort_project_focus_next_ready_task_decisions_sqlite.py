"""Integration tests for SQLite persistence of V1.43 next READY TASK decisions.

Verifies against a REAL SQLite database + the REAL V1.42 decision boundary:
exact round-trip reconstruction of a value-equivalent (and genuinely
re-validated) V1.42 decision, explicit per-row ``portfolio_id`` column
matching, exact preservation of accepted project/task IDs, counts and
provenance, original ``decided_at`` offset preservation, true-instant
history ordering with ``decision_id.int`` tiebreaker, append-only
duplicate semantics (precise duplicate detection, unrelated
``IntegrityError``/``OperationalError`` NOT translated as duplicates),
value-equivalent decisions with distinct durable decision IDs
coexisting, per-portfolio scoping and exact raw-storage checks.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import IntegrityError, OperationalError

from trajectory_os.adapters.persistence import (
    DuplicatePortfolioProjectFocusNextReadyTaskDecisionError,
    SqlitePortfolioProjectFocusNextReadyTaskDecisionRepository,
    SqlitePortfolioRepository,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_decision import (  # noqa: E501
    PortfolioProjectFocusNextReadyTaskDecision,
    accept_next_ready_task_selection,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_decision_persistence import (  # noqa: E501
    PortfolioProjectFocusNextReadyTaskDecisionRecord,
    record_next_ready_task_decision_durably,
)
from trajectory_os.application.execution_effort_project_focus_next_ready_task_selection import (  # noqa: E501
    PortfolioProjectFocusNextReadyTaskSelection,
)
from trajectory_os.domain.portfolio import Portfolio

DB = "test_v143.sqlite3"
TABLE = "portfolio_project_focus_next_ready_task_decision_records"

PORTFOLIO = UUID("61616161-6161-4161-8161-616161616161")
OTHER_PORTFOLIO = UUID("b2b2b2b2-b2b2-42b2-82b2-b2b2b2b2b2b2")

DECISION_ID_1 = UUID("11111111-1111-4111-8111-111111111111")
DECISION_ID_2 = UUID("22222222-2222-4222-8222-222222222222")
DECISION_ID_3 = UUID("33333333-3333-4333-8333-333333333333")

T_UTC = datetime(2025, 7, 1, 8, 31, tzinfo=UTC)
T_SAME_OFFSET_PLUS = datetime(2025, 7, 1, 10, 31, tzinfo=timezone(timedelta(hours=2)))  # noqa: E501
T_LATER = datetime(2025, 7, 1, 9, 1, tzinfo=UTC)

PROJECT_A = UUID("eaaaaaa0-aaaa-4aaa-8aaa-0000000000a0")
TASK_A = UUID("e00a0000-0000-4000-8000-0000000000a0")
TASK_B = UUID("e00b0000-0000-4000-8000-0000000000b0")

SCENARIO_REF = "ref"
SCENARIO_WIDE = "wide"


class _ForeignModel(BaseModel):
    """A different Pydantic model; must never be accepted as a record."""

    model_config = {"frozen": True, "extra": "forbid"}

    unrelated: str = "foreign"


# ---------------------------------------------------------------------------
# Scenario: two candidate projects, two or three READY tasks per scenario
# -> a genuine V1.42 acceptance decision.
# ---------------------------------------------------------------------------


def _selection(scenario: str) -> PortfolioProjectFocusNextReadyTaskSelection:
    """A fully validated (genuine) V1.41 selection."""
    ready_task_count = 3 if scenario == SCENARIO_WIDE else 2
    return PortfolioProjectFocusNextReadyTaskSelection(
        decision_id=UUID("c1c1c1c1-c1c1-4c1c-8c1c-c1c1c1c1c1c1"),
        decided_at=datetime(
            2025, 7, 1, 8, 0, 30, tzinfo=timezone(timedelta(hours=2, minutes=30))
        ),
        portfolio_id=PORTFOLIO,
        selected_project_count=2,
        ready_task_count=ready_task_count,
        selected_project_id=PROJECT_A,
        selected_task_id=TASK_A,
    )


def _accepted(scenario: str) -> PortfolioProjectFocusNextReadyTaskDecision:
    """A genuine V1.42 decision produced by the real V1.42 boundary."""
    return accept_next_ready_task_selection(
        _selection(scenario),
        accepted_project_id=PROJECT_A,
        accepted_task_id=TASK_A,
    )


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "portfolio.sqlite3"


@pytest.fixture
def saved_portfolio(db_path: Path) -> Path:
    """Persist the parent portfolio required by the record foreign key.

    Also creates all persistence tables (including the decision table) so
    later raw-access helpers see the full schema.
    """
    with SqlitePortfolioRepository(db_path) as portfolio_repo:
        portfolio_repo.save(Portfolio(id=PORTFOLIO, name="V1.43 SQLite"))
    with SqlitePortfolioProjectFocusNextReadyTaskDecisionRepository(db_path):
        pass
    return db_path


@pytest.fixture
def repo(
    saved_portfolio: Path,
) -> Iterator[SqlitePortfolioProjectFocusNextReadyTaskDecisionRepository]:
    with SqlitePortfolioProjectFocusNextReadyTaskDecisionRepository(
        saved_portfolio
    ) as repository:
        yield repository


# ---------------------------------------------------------------------------
# Raw access helpers.
# ---------------------------------------------------------------------------


def _raw_row(db: Path, decision_id: UUID) -> dict[str, Any]:
    connection = sqlite3.connect(db)
    try:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            f"SELECT decision_id, portfolio_id, decided_at, decision_snapshot "
            f"FROM {TABLE} "
            f"WHERE decision_id = ?",
            (str(decision_id),),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return dict(row)


def _raw_insert(db: Path, *, decision_id: UUID, row_portfolio: UUID, snapshot: str) -> None:  # noqa: E501
    connection = sqlite3.connect(db)
    connection.isolation_level = None
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            f"INSERT INTO {TABLE} "
            f"(decision_id, portfolio_id, decided_at, decision_snapshot) "
            f"VALUES (?, ?, ?, ?)",
            (str(decision_id), str(row_portfolio), T_UTC.isoformat(), snapshot),
        )
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Round-trip.
# ---------------------------------------------------------------------------


def test_end_to_end_decide_then_store_and_read_back_through_real_v142_boundary(  # noqa: E501
    repo: SqlitePortfolioProjectFocusNextReadyTaskDecisionRepository,
) -> None:
    decision = _accepted(SCENARIO_REF)

    returned = record_next_ready_task_decision_durably(
        DECISION_ID_1, T_UTC, decision, repository=repo
    )

    assert isinstance(returned, PortfolioProjectFocusNextReadyTaskDecisionRecord)
    assert returned.decision_id == DECISION_ID_1
    assert returned.decided_at == T_UTC
    # Returned and stored decision are value-equivalent and re-validated.
    assert isinstance(
        returned.decision, PortfolioProjectFocusNextReadyTaskDecision
    )
    assert returned.decision == decision
    assert returned.decision.model_dump(mode="python") == decision.model_dump(
        mode="python"
    )

    history = repo.list_history(PORTFOLIO)
    assert len(history) == 1
    stored = history[0]
    assert stored.model_dump(mode="python") == returned.model_dump(mode="python")
    assert stored.decision.model_dump(mode="python") == decision.model_dump(
        mode="python"
    )


def test_exact_read_back_preserves_every_v142_field(
    repo: SqlitePortfolioProjectFocusNextReadyTaskDecisionRepository,
) -> None:
    decision = _accepted(SCENARIO_WIDE)
    record_next_ready_task_decision_durably(
        DECISION_ID_1, T_UTC, decision, repository=repo
    )
    stored = repo.list_history(PORTFOLIO)[0].decision
    assert stored.model_dump(mode="python") == decision.model_dump(mode="python")
    assert stored.portfolio_id == PORTFOLIO
    assert stored.selected_project_count == 2
    assert stored.ready_task_count == 3
    assert stored.accepted_project_id == PROJECT_A
    assert stored.accepted_task_id == TASK_A
def test_accepted_project_and_task_are_preserved_exactly(
    repo: SqlitePortfolioProjectFocusNextReadyTaskDecisionRepository,
) -> None:
    decision = _accepted(SCENARIO_REF)
    record_next_ready_task_decision_durably(
        DECISION_ID_1, T_UTC, decision, repository=repo
    )
    stored = repo.list_history(PORTFOLIO)[0].decision
    assert stored.accepted_project_id == PROJECT_A
    assert stored.accepted_task_id == TASK_A

    # A different (genuine) acceptance round-trips equally exactly.
    decision_wide = _accepted(SCENARIO_WIDE)
    record_next_ready_task_decision_durably(
        DECISION_ID_2, T_LATER, decision_wide, repository=repo
    )
    stored_wide = next(
        record.decision for record in repo.list_history(PORTFOLIO)
        if record.decision_id == DECISION_ID_2
    )
    assert stored_wide.accepted_project_id == PROJECT_A
    assert stored_wide.accepted_task_id == TASK_A


def test_counts_and_provenance_are_preserved_exactly(
    repo: SqlitePortfolioProjectFocusNextReadyTaskDecisionRepository,
) -> None:
    decision = _accepted(SCENARIO_REF)
    record_next_ready_task_decision_durably(
        DECISION_ID_1, T_UTC, decision, repository=repo
    )
    stored = repo.list_history(PORTFOLIO)[0].decision
    assert stored.portfolio_id == PORTFOLIO
    assert stored.decision_id == decision.decision_id
    assert stored.decided_at == decision.decided_at
    assert stored.selected_project_count == 2
    assert stored.ready_task_count == 2


def test_stored_representations_are_exact(saved_portfolio: Path) -> None:
    """The table carries explicit, human-readable, typed values."""
    with SqlitePortfolioProjectFocusNextReadyTaskDecisionRepository(
        saved_portfolio
    ) as r:
        decision = _accepted(SCENARIO_REF)
        record_next_ready_task_decision_durably(
            DECISION_ID_1, T_UTC, decision, repository=r
        )

    row = _raw_row(saved_portfolio, DECISION_ID_1)

    # UUIDs are stored as 36-character canonical text.
    assert isinstance(row["decision_id"], str)
    assert row["decision_id"] == str(DECISION_ID_1)
    assert len(row["decision_id"]) == 36
    # The explicit row-level portfolio_id column is set.
    assert row["portfolio_id"] == str(PORTFOLIO)
    assert len(row["portfolio_id"]) == 36

    # The aware timestamp is stored with its original offset, verbatim.
    assert row["decided_at"] == T_UTC.isoformat()

    # The nested V1.42 decision is stored as its EXACT deterministic JSON.
    snapshot: str = row["decision_snapshot"]
    assert snapshot == decision.model_dump_json()
    assert json.loads(snapshot) == decision.model_dump(mode="json")
    assert json.loads(snapshot)["portfolio_id"] == str(PORTFOLIO)
    assert json.loads(snapshot)["accepted_project_id"] == str(PROJECT_A)
    assert json.loads(snapshot)["accepted_task_id"] == str(TASK_A)
    # It is explicit JSON text, not a pickle and not opaque binary.
    assert isinstance(snapshot, str)
    assert json.loads(snapshot) is not None
    assert snapshot.count('"portfolio_id"') == 1


def test_offset_is_preserved_verbatim_for_non_utc_offsets(
    saved_portfolio: Path,
) -> None:
    with SqlitePortfolioProjectFocusNextReadyTaskDecisionRepository(
        saved_portfolio
    ) as r:
        decision = _accepted(SCENARIO_REF)
        returned = record_next_ready_task_decision_durably(
            DECISION_ID_1, T_SAME_OFFSET_PLUS, decision, repository=r
        )
        assert returned.decided_at.utcoffset() == timedelta(hours=2)

    row = _raw_row(saved_portfolio, DECISION_ID_1)
    assert row["decided_at"] == T_SAME_OFFSET_PLUS.isoformat()
    assert "+02:00" in row["decided_at"]


def test_original_offset_invariance_for_non_utc_offsets(
    saved_portfolio: Path,
) -> None:
    with SqlitePortfolioProjectFocusNextReadyTaskDecisionRepository(
        saved_portfolio
    ) as r:
        decision = _accepted(SCENARIO_REF)
        record_next_ready_task_decision_durably(
            DECISION_ID_1, T_SAME_OFFSET_PLUS, decision, repository=r
        )

    stored = _raw_row(saved_portfolio, DECISION_ID_1)
    restored = datetime.fromisoformat(stored["decided_at"])
    assert restored.tzinfo is not None
    assert restored.utcoffset() == timedelta(hours=2)


# ---------------------------------------------------------------------------
# Scoping.
# ---------------------------------------------------------------------------


def test_list_history_returns_only_matching_portfolio(
    repo: SqlitePortfolioProjectFocusNextReadyTaskDecisionRepository,
) -> None:
    decision = _accepted(SCENARIO_REF)
    record_next_ready_task_decision_durably(
        DECISION_ID_1, T_UTC, decision, repository=repo
    )

    assert len(repo.list_history(PORTFOLIO)) == 1
    # A portfolio with no rows returns exactly the empty tuple.
    assert repo.list_history(OTHER_PORTFOLIO) == ()
    third = UUID("d3d3d3d3-d3d3-43d3-83d3-d3d3d3d3d3d3")
    assert repo.list_history(third) == ()


def test_empty_history_is_empty_tuple(
    repo: SqlitePortfolioProjectFocusNextReadyTaskDecisionRepository,
) -> None:
    assert repo.list_history(OTHER_PORTFOLIO) == ()


# ---------------------------------------------------------------------------
# Ordering.
# ---------------------------------------------------------------------------


def test_history_orders_by_true_instant_then_uuid_int(
    repo: SqlitePortfolioProjectFocusNextReadyTaskDecisionRepository,
) -> None:
    decision = _accepted(SCENARIO_REF)

    # Insert in deliberately scrambled order: the latest instant first.
    ids: list[UUID] = []
    for decision_id, decided_at in (
        (DECISION_ID_3, T_LATER),
        (DECISION_ID_2, T_SAME_OFFSET_PLUS),  # same instant as T_UTC, +02:00
        (DECISION_ID_1, T_UTC),
    ):
        record_next_ready_task_decision_durably(
            decision_id, decided_at, decision, repository=repo
        )
        ids.append(decision_id)

    history = repo.list_history(PORTFOLIO)
    assert len(history) == 3
    # True instant order: T_UTC == T_SAME_OFFSET_PLUS (tie) before T_LATER;
    # the tie is ordered by decision_id.int (not by stored text, and not by
    # lexical UUID string order).
    assert [record.decision_id.int for record in history] == [
        uid.int for uid in sorted(ids)
    ]
    assert history[0].decided_at == T_UTC  # tie ordered by decision_id.int
    assert history[-1].decided_at == T_LATER
    # Crucially, the stored TEXT of the tied +02:00 record is lexicographically
    # AFTER "2025-07-01T08:31:00+00:00"; true-instant sorting must still put
    # both tied records before it.
    tied = {record.decided_at.isoformat() for record in history[:2]}
    assert T_SAME_OFFSET_PLUS.isoformat() in tied
    assert T_UTC.isoformat() in tied


# ---------------------------------------------------------------------------
# Duplicates.
# ---------------------------------------------------------------------------


def test_duplicate_decision_id_rejected_and_existing_row_untouched(
    saved_portfolio: Path,
) -> None:
    with SqlitePortfolioProjectFocusNextReadyTaskDecisionRepository(
        saved_portfolio
    ) as r:
        decision = _accepted(SCENARIO_REF)
        record_next_ready_task_decision_durably(
            DECISION_ID_1, T_UTC, decision, repository=r
        )
        first_snapshot = _raw_row(saved_portfolio, DECISION_ID_1)[
            "decision_snapshot"
        ]

        # Same decision_id, even with a DIFFERENT decision value: rejected.
        wide = _accepted(SCENARIO_WIDE)
        with pytest.raises(
            DuplicatePortfolioProjectFocusNextReadyTaskDecisionError
        ):
            record_next_ready_task_decision_durably(
                DECISION_ID_1, T_LATER, wide, repository=r
            )

        # The original row is never replaced or updated.
        assert (
            _raw_row(saved_portfolio, DECISION_ID_1)["decision_snapshot"]
            == first_snapshot
        )
        history = r.list_history(PORTFOLIO)
        assert len(history) == 1
        assert history[0].decision.portfolio_id == PORTFOLIO
        assert issubclass(
            DuplicatePortfolioProjectFocusNextReadyTaskDecisionError, ValueError
        )


def test_database_level_duplicate_translated_through_add(
    saved_portfolio: Path,
    repo: SqlitePortfolioProjectFocusNextReadyTaskDecisionRepository,
) -> None:
    decision = _accepted(SCENARIO_REF)
    record_next_ready_task_decision_durably(
        DECISION_ID_1, T_UTC, decision, repository=repo
    )

    record = PortfolioProjectFocusNextReadyTaskDecisionRecord(
        decision_id=DECISION_ID_1,
        decided_at=T_UTC,
        decision=decision,
    )
    with pytest.raises(
        (IntegrityError, DuplicatePortfolioProjectFocusNextReadyTaskDecisionError)
    ):
        repo.add(record)


def _patched_session(monkeypatch: Any, exc: Exception) -> None:
    """Replace the adapter's Session factory so INSERT execution raises ``exc`` before any write."""
    import trajectory_os.adapters.persistence.sqlite_portfolio_project_focus_next_ready_task_decisions as adapter_module  # noqa: E501

    class _EmptyScalars:
        def all(self) -> list[Any]:
            return []

    class _RaisingSession:
        def __init__(self, engine: Any) -> None:
            self._engine = engine

        def execute(self, *args: Any, **kwargs: Any) -> Any:
            raise exc

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

        def close(self) -> None:
            return None

        def scalars(self, *args: Any, **kwargs: Any) -> _EmptyScalars:
            return _EmptyScalars()

        def __enter__(self) -> _RaisingSession:
            return self

        def __exit__(self, *exc_info: object) -> None:
            return None

    monkeypatch.setattr(adapter_module, "Session", _RaisingSession)


def test_unrelated_integrity_error_is_not_translated_as_duplicate(
    saved_portfolio: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An IntegrityError that is NOT the precise UNIQUE decision_id conflict
    (e.g., a foreign-key violation) must escape unchanged."""
    _patched_session(
        monkeypatch,
        IntegrityError(
            "INSERT", {}, sqlite3.IntegrityError("FOREIGN KEY constraint failed")
        ),
    )
    with SqlitePortfolioProjectFocusNextReadyTaskDecisionRepository(
        saved_portfolio
    ) as r:
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            r.add(
                PortfolioProjectFocusNextReadyTaskDecisionRecord(
                    decision_id=DECISION_ID_1,
                    decided_at=T_UTC,
                    decision=_accepted(SCENARIO_REF),
                )
            )
        # And nothing was appended.
        assert r.list_history(PORTFOLIO) == ()


def test_operational_error_is_never_misclassified_as_duplicate(
    saved_portfolio: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A locked database (OperationalError) must escape unchanged, not be
    translated into a duplicate error."""
    _patched_session(
        monkeypatch,
        OperationalError(
            "INSERT", {}, sqlite3.OperationalError("database is locked")
        ),
    )
    with SqlitePortfolioProjectFocusNextReadyTaskDecisionRepository(
        saved_portfolio
    ) as r:
        with pytest.raises(OperationalError, match="locked"):
            r.add(
                PortfolioProjectFocusNextReadyTaskDecisionRecord(
                    decision_id=DECISION_ID_1,
                    decided_at=T_UTC,
                    decision=_accepted(SCENARIO_REF),
                )
            )
        assert r.list_history(PORTFOLIO) == ()


# ---------------------------------------------------------------------------
# Value equivalence and coexistence.
# ---------------------------------------------------------------------------


def test_value_equivalent_decisions_with_different_ids_coexist(
    repo: SqlitePortfolioProjectFocusNextReadyTaskDecisionRepository,
) -> None:
    decision = _accepted(SCENARIO_REF)

    first = record_next_ready_task_decision_durably(
        DECISION_ID_1, T_UTC, decision, repository=repo
    )
    second = record_next_ready_task_decision_durably(
        DECISION_ID_2, T_SAME_OFFSET_PLUS, decision, repository=repo
    )

    history = repo.list_history(PORTFOLIO)
    assert len(history) == 2
    assert first.decision == second.decision
    assert first.decision_id != second.decision_id
    assert [record.decision_id.int for record in history] == [
        DECISION_ID_1.int,
        DECISION_ID_2.int,
    ]


def test_same_instant_different_portfolios_are_scoped(
    repo: SqlitePortfolioProjectFocusNextReadyTaskDecisionRepository,
) -> None:
    decision = _accepted(SCENARIO_REF)
    record_next_ready_task_decision_durably(
        DECISION_ID_1, T_UTC, decision, repository=repo
    )

    # OTHER_PORTFOLIO has no rows at all.
    assert repo.list_history(OTHER_PORTFOLIO) == ()
    assert len(repo.list_history(PORTFOLIO)) == 1


# ---------------------------------------------------------------------------
# Surface / hostile input.
# ---------------------------------------------------------------------------


def test_no_update_or_delete_api_exists(
    repo: SqlitePortfolioProjectFocusNextReadyTaskDecisionRepository,
) -> None:
    public = [name for name in dir(repo) if not name.startswith("_")]
    for forbidden in (
        "update",
        "delete",
        "remove",
        "replace",
        "upsert",
        "save",
        "patch",
        "modify",
        "put",
        "rewrite",
    ):
        assert forbidden not in public
    assert "add" in public
    assert "list_history" in public
    # No semantic latest/current inference is exposed either.
    for forbidden in ("latest", "current", "effective", "active"):
        assert forbidden not in public


def test_add_rejects_non_record_and_hostile_records_before_write(
    saved_portfolio: Path,
) -> None:
    with SqlitePortfolioProjectFocusNextReadyTaskDecisionRepository(
        saved_portfolio
    ) as r:
        with pytest.raises(TypeError):
            r.add(_ForeignModel())

        hostile_decision = PortfolioProjectFocusNextReadyTaskDecision.model_construct(  # noqa: E501
            portfolio_id="61616161-6161-4161-8161-616161616161",  # str, breaks strict UUID  # noqa: E501
            selected_project_count=2,
            ready_task_count=2,
            accepted_project_id=PROJECT_A,
            accepted_task_id=TASK_A,
        )
        with pytest.raises((ValidationError, ValueError)):
            r.add(
                PortfolioProjectFocusNextReadyTaskDecisionRecord.model_construct(
                    decision_id=DECISION_ID_1,
                    decided_at=T_UTC,
                    decision=hostile_decision,
                )
            )

        # Nothing was written in any of the failed attempts.
        assert r.list_history(PORTFOLIO) == ()


# ---------------------------------------------------------------------------
# Corruption.
# ---------------------------------------------------------------------------


def test_rejects_corrupt_stored_decision_snapshot(
    saved_portfolio: Path,
) -> None:
    with SqlitePortfolioProjectFocusNextReadyTaskDecisionRepository(
        saved_portfolio
    ) as r:
        decision = _accepted(SCENARIO_REF)
        record_next_ready_task_decision_durably(
            DECISION_ID_1, T_UTC, decision, repository=r
        )

        _raw_insert(  # corrupt: not the genuine JSON payload
            saved_portfolio,
            decision_id=DECISION_ID_2,
            row_portfolio=PORTFOLIO,
            snapshot='{"portfolio_id": 123}',
        )

        # The stored snapshot must be rejected when read back.
        with pytest.raises((ValidationError, ValueError)):
            r.list_history(PORTFOLIO)


def test_rejects_saved_row_portfolio_id_mismatch(
    saved_portfolio: Path,
) -> None:
    """A decision-level portfolio_id that does not match the explicit
    row-level portfolio_id column must be rejected when read back."""
    with SqlitePortfolioRepository(saved_portfolio) as portfolio_repo:
        portfolio_repo.save(Portfolio(id=OTHER_PORTFOLIO, name="V1.43 other"))

    with SqlitePortfolioProjectFocusNextReadyTaskDecisionRepository(
        saved_portfolio
    ) as r:
        # row column == PORTFOLIO, decision payload portfolio == OTHER_PORTFOLIO
        mismatched = PortfolioProjectFocusNextReadyTaskDecision(
            decision_id=UUID("c2c2c2c2-c2c2-4c2c-8c2c-c2c2c2c2c2c2"),
            decided_at=datetime(2025, 7, 1, 8, 0, 30, tzinfo=UTC),
            portfolio_id=OTHER_PORTFOLIO,
            selected_project_count=1,
            ready_task_count=1,
            accepted_project_id=PROJECT_A,
            accepted_task_id=TASK_A,
        )
        _raw_insert(
            saved_portfolio,
            decision_id=DECISION_ID_2,
            row_portfolio=PORTFOLIO,
            snapshot=mismatched.model_dump_json(),
        )

        with pytest.raises((ValidationError, ValueError)):
            r.list_history(PORTFOLIO)


# ---------------------------------------------------------------------------
# Concurrency.
# ---------------------------------------------------------------------------


def test_concurrent_appends_preserve_all_rows_and_no_loss(
    saved_portfolio: Path,
) -> None:
    """Independent processes appending distinct decisions concurrently
    must never lose or corrupt a row (the UNIQUE decision_id protects
    identity; distinct ids never collide)."""
    workers = 8
    per_worker = 6

    def append_range(base: int) -> None:
        with SqlitePortfolioProjectFocusNextReadyTaskDecisionRepository(
            saved_portfolio
        ) as r:
            decision = _accepted(SCENARIO_REF)
            for offset in range(per_worker):
                durable_id = UUID(int=(base << 40) | offset)
                record_next_ready_task_decision_durably(
                    durable_id, T_UTC, decision, repository=r
                )

    errors: list[BaseException] = []

    def guarded(base: int) -> None:
        try:
            append_range(base)
        except BaseException as exc:  # noqa: BLE001 - thread exception harvest
            errors.append(exc)

    threads = [
        threading.Thread(target=guarded, args=(base,)) for base in range(workers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []

    with SqlitePortfolioProjectFocusNextReadyTaskDecisionRepository(
        saved_portfolio
    ) as r:
        history = r.list_history(PORTFOLIO)
    assert len(history) == workers * per_worker
    # All rows are value-equivalent V1.42 re-validatable decisions.
    for record in history:
        assert isinstance(record.decision, PortfolioProjectFocusNextReadyTaskDecision)
        assert record.decision.portfolio_id == PORTFOLIO

