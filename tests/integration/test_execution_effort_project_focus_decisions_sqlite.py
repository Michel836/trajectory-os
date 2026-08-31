"""Integration tests for V1.35 durable focus-decision SQLite persistence.

Covers the full real V1.33/V1.34 semantic chain into SQLite: the EXACT
accepted V1.34 decision built through ``accept_portfolio_effort_focus_decision``
persists byte-exact via ``record_portfolio_effort_focus_decision_durably``;
the stored snapshot is the deterministic Pydantic JSON of the genuine
nested decision; UUIDs are 36-character text; the caller's UTC offset is
preserved verbatim; history is ordered by true chronological instant then
``decision_id.int``; duplicates by ``decision_id`` are rejected while
value-equivalent decisions with different ids coexist; hostile
``model_construct()`` payloads and non-record inputs are rejected before
any write; corrupt/mismatched stored rows raise precise errors; the empty
history is exactly ``()``; and no update/delete/replace/upsert/save/patch
API exists.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import IntegrityError

from trajectory_os.adapters.persistence import (
    SqlitePortfolioProjectEffortFocusDecisionRepository,
    SqlitePortfolioRepository,
)
from trajectory_os.adapters.persistence.sqlite_portfolio_project_focus_decisions import (  # noqa: E501
    DuplicatePortfolioProjectEffortFocusDecisionError,
)
from trajectory_os.application import (
    PortfolioProjectEffortFocusDecisionRecord,
    accept_portfolio_effort_focus_decision,
    record_portfolio_effort_focus_decision_durably,
)
from trajectory_os.application.execution_effort_project_focus_decision import (
    PortfolioProjectEffortFocusDecision,
)
from trajectory_os.application.execution_effort_project_focus_scenario_set import (
    PortfolioProjectEffortFocusScenario,
    PortfolioProjectEffortFocusScenarioSet,
)
from trajectory_os.domain.portfolio import Portfolio

PORTFOLIO = UUID("71717171-7171-4171-8171-717171717171")
OTHER_PORTFOLIO = UUID("71717171-7171-4171-8171-717171717172")

DECISION_ID_1 = UUID("73737373-7373-4373-8373-737373737373")
DECISION_ID_2 = UUID("74747474-7474-4474-8474-747474747474")
DECISION_ID_3 = UUID("75757575-7575-4575-8575-757575757575")
DECISION_ID_4 = UUID("76767676-7676-4676-8676-767676767676")
DECISION_ID_5 = UUID("77777777-7777-4777-8777-777777777777")

# Two distinct wall-clock texts for the SAME true instant (offset-aware),
# plus one strictly later instant. The lexical text order is
# T_SAME_OFFSET_PLUS > T_LATER > T_UTC, but the true instant order is
# T_UTC == T_SAME_OFFSET_PLUS < T_LATER — a lexical sort would be wrong.
T_UTC = datetime(2025, 7, 1, 8, 30, tzinfo=UTC)
T_SAME_OFFSET_PLUS = datetime(
    2025, 7, 1, 10, 30, tzinfo=timezone(timedelta(hours=2))
)
T_LATER = datetime(2025, 7, 1, 8, 31, tzinfo=UTC)

TABLE = "portfolio_project_effort_focus_decision_records"


def _scenario(
    requested_limit: int,
    count: int,
    count_delta: int,
    selected_dur: int | None,
    selected_delta: int | None,
    remaining_dur: int | None,
    remaining_delta: int | None,
) -> PortfolioProjectEffortFocusScenario:
    return PortfolioProjectEffortFocusScenario(
        requested_limit=requested_limit,
        selected_project_count=count,
        selected_project_count_delta=count_delta,
        selected_duration_seconds=selected_dur,
        selected_duration_delta_seconds=selected_delta,
        remaining_duration_seconds=remaining_dur,
        remaining_duration_delta_seconds=remaining_delta,
    )


def _set(
    *scenarios: PortfolioProjectEffortFocusScenario,
) -> PortfolioProjectEffortFocusScenarioSet:
    return PortfolioProjectEffortFocusScenarioSet(
        portfolio_id=PORTFOLIO,
        source_project_count=3,
        total_duration_seconds=10000,
        reference_requested_limit=1,
        reference_selected_project_count=1,
        reference_selected_duration_seconds=6000,
        reference_remaining_duration_seconds=4000,
        scenarios=scenarios,
    )


def _accepted(scenario: PortfolioProjectEffortFocusScenario) -> (
    PortfolioProjectEffortFocusDecision
):
    """Accept the EXPLICIT scenario through the genuine V1.34 boundary."""
    return accept_portfolio_effort_focus_decision(
        _set(
            _scenario(1, 1, 0, 6000, 0, 4000, 0),
            _scenario(2, 2, 1, 9000, 3000, 1000, -3000),
            _scenario(3, 3, 2, 10000, 4000, 0, -4000),
        ),
        scenario,
    )


SCENARIO_REF = _scenario(1, 1, 0, 6000, 0, 4000, 0)
SCENARIO_WIDE = _scenario(2, 2, 1, 9000, 3000, 1000, -3000)


class _ForeignModel(BaseModel):
    """A different Pydantic model; must never be accepted by ``add``."""

    field: str = "foreign"


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "v135.db"


@pytest.fixture()
def saved_portfolio(db_path: Path) -> Path:
    """Persist the portfolio required by the durable record foreign key."""
    with SqlitePortfolioRepository(db_path) as portfolio_repo:
        portfolio_repo.save(Portfolio(id=PORTFOLIO, name="V1.35 SQLite"))
    return db_path


@pytest.fixture()
def repo(saved_portfolio: Path) -> SqlitePortfolioProjectEffortFocusDecisionRepository:
    repository = SqlitePortfolioProjectEffortFocusDecisionRepository(saved_portfolio)
    yield repository
    repository.close()


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


def _raw_insert(db: Path, *, decision_id: UUID, row_portfolio: UUID, snapshot: str) -> None:
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


def test_end_to_end_record_through_real_v134_boundary_and_sqlite(
    repo: SqlitePortfolioProjectEffortFocusDecisionRepository,
) -> None:
    decision = _accepted(SCENARIO_REF)

    returned = record_portfolio_effort_focus_decision_durably(
        DECISION_ID_1, T_UTC, decision, repository=repo
    )

    assert isinstance(returned, PortfolioProjectEffortFocusDecisionRecord)
    assert returned.decision_id == DECISION_ID_1
    assert returned.decided_at == T_UTC
    assert returned.decision == decision

    history = repo.list_history(PORTFOLIO)
    assert len(history) == 1
    stored = history[0]
    assert stored.model_dump(mode="python") == returned.model_dump(mode="python")
    assert stored.decision.model_dump(mode="python") == decision.model_dump(
        mode="python"
    )


def test_stored_representations_are_exact(
    saved_portfolio: Path,
) -> None:
    """The table carries explicit, human-readable, typed values."""
    with SqlitePortfolioProjectEffortFocusDecisionRepository(saved_portfolio) as r:
        decision = _accepted(SCENARIO_REF)
        record_portfolio_effort_focus_decision_durably(
            DECISION_ID_1, T_UTC, decision, repository=r
        )

    row = _raw_row(saved_portfolio, DECISION_ID_1)

    # UUIDs are stored as 36-character canonical text.
    assert isinstance(row["decision_id"], str)
    assert row["decision_id"] == str(DECISION_ID_1)
    assert len(row["decision_id"]) == 36
    assert row["portfolio_id"] == str(PORTFOLIO)
    assert len(row["portfolio_id"]) == 36

    # The aware timestamp is stored with its original offset, verbatim.
    assert row["decided_at"] == T_UTC.isoformat()

    # The nested V1.34 decision is stored as its EXACT deterministic JSON.
    snapshot: str = row["decision_snapshot"]
    assert snapshot == decision.model_dump_json()
    assert json.loads(snapshot) == decision.model_dump(mode="json")
    assert json.loads(snapshot)["portfolio_id"] == str(PORTFOLIO)
    # It is explicit JSON text, not a pickle and not opaque binary.
    assert isinstance(snapshot, str)
    assert json.loads(snapshot) is not None
    assert snapshot.count('"portfolio_id"') == 1


def test_offset_is_preserved_verbatim_for_non_utc_offsets(
    saved_portfolio: Path,
) -> None:
    with SqlitePortfolioProjectEffortFocusDecisionRepository(
        saved_portfolio
    ) as r:
        decision = _accepted(SCENARIO_REF)
        returned = record_portfolio_effort_focus_decision_durably(
            DECISION_ID_1, T_SAME_OFFSET_PLUS, decision, repository=r
        )
        assert returned.decided_at.utcoffset() == timedelta(hours=2)

    row = _raw_row(saved_portfolio, DECISION_ID_1)
    assert row["decided_at"] == T_SAME_OFFSET_PLUS.isoformat()
    assert "+02:00" in row["decided_at"]


def test_empty_history_is_empty_tuple(
    repo: SqlitePortfolioProjectEffortFocusDecisionRepository,
) -> None:
    assert repo.list_history(OTHER_PORTFOLIO) == ()


# ---------------------------------------------------------------------------
# Ordering.
# ---------------------------------------------------------------------------


def test_history_orders_by_true_instant_then_uuid_int(
    repo: SqlitePortfolioProjectEffortFocusDecisionRepository,
) -> None:
    decision = _accepted(SCENARIO_REF)

    # Insert in deliberately scrambled order: the latest instant first.
    ids: list[UUID] = []
    for decision_id, decided_at in (
        (DECISION_ID_3, T_LATER),
        (DECISION_ID_2, T_SAME_OFFSET_PLUS),  # same instant as T_UTC, +02:00
        (DECISION_ID_1, T_UTC),
    ):
        record_portfolio_effort_focus_decision_durably(
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
    with SqlitePortfolioProjectEffortFocusDecisionRepository(
        saved_portfolio
    ) as r:
        decision = _accepted(SCENARIO_REF)
        record_portfolio_effort_focus_decision_durably(
            DECISION_ID_1, T_UTC, decision, repository=r
        )
        first_snapshot = _raw_row(saved_portfolio, DECISION_ID_1)[
            "decision_snapshot"
        ]

        # Same decision_id, even with a DIFFERENT decision value: rejected.
        wide = _accepted(SCENARIO_WIDE)
        with pytest.raises(DuplicatePortfolioProjectEffortFocusDecisionError):
            record_portfolio_effort_focus_decision_durably(
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
        assert history[0].decision.source_project_count == 3
        assert issubclass(
            DuplicatePortfolioProjectEffortFocusDecisionError, ValueError
        )


def test_database_level_duplicate_translated_through_add(
    saved_portfolio: Path,
) -> None:
    """A DB-level PK/UNIQUE constraint violation reached through add()
    is translated into DuplicatePortfolioProjectEffortFocusDecisionError,
    and the original row remains unchanged."""
    with SqlitePortfolioProjectEffortFocusDecisionRepository(
        saved_portfolio
    ) as r:
        decision = _accepted(SCENARIO_REF)
        record = PortfolioProjectEffortFocusDecisionRecord(
            decision_id=DECISION_ID_1,
            decided_at=T_UTC,
            decision=decision,
        )
        r.add(record)
        first_snapshot = _raw_row(saved_portfolio, DECISION_ID_1)[
            "decision_snapshot"
        ]

        # Second add with the SAME decision_id directly through the adapter.
        record_dup = PortfolioProjectEffortFocusDecisionRecord(
            decision_id=DECISION_ID_1,
            decided_at=T_LATER,
            decision=_accepted(SCENARIO_WIDE),
        )
        with pytest.raises(DuplicatePortfolioProjectEffortFocusDecisionError) as exc_info:
            r.add(record_dup)

        # The error message carries the stored decision_id.
        assert str(DECISION_ID_1) in str(exc_info.value)
        # Exception chaining: the original IntegrityError is preserved.
        assert exc_info.value.__cause__ is not None
        assert isinstance(exc_info.value.__cause__, IntegrityError)

        # The original row is completely untouched.
        assert (
            _raw_row(saved_portfolio, DECISION_ID_1)["decision_snapshot"]
            == first_snapshot
        )
        assert (
            _raw_row(saved_portfolio, DECISION_ID_1)["decided_at"]
            == T_UTC.isoformat()
        )
        history = r.list_history(PORTFOLIO)
        assert len(history) == 1
        assert history[0].decision_id == DECISION_ID_1


def test_two_repository_instances_concurrent_add_deterministic(
    saved_portfolio: Path,
) -> None:
    """Two separate repository instances (separate engines/connections)
    race on the same decision_id: exactly one INSERT wins, the other
    receives the translated duplicate error. Deterministic because
    SQLite serializes writers via its write lock."""
    repo_a = SqlitePortfolioProjectEffortFocusDecisionRepository(saved_portfolio)
    repo_b = SqlitePortfolioProjectEffortFocusDecisionRepository(saved_portfolio)
    try:
        decision = _accepted(SCENARIO_REF)
        record = PortfolioProjectEffortFocusDecisionRecord(
            decision_id=DECISION_ID_2,
            decided_at=T_UTC,
            decision=decision,
        )

        outcome_a: list[BaseException | None] = []
        outcome_b: list[BaseException | None] = []

        def _do_add(
            repo: SqlitePortfolioProjectEffortFocusDecisionRepository,
            outcome: list[BaseException | None],
        ) -> None:
            try:
                repo.add(record)
                outcome.append(None)
            except BaseException as e:  # noqa: BLE001
                outcome.append(e)

        thread = threading.Thread(target=_do_add, args=(repo_b, outcome_b))
        thread.start()
        try:
            _do_add(repo_a, outcome_a)
        finally:
            thread.join()

        # Exactly one succeeded, one failed with the translated error.
        results = outcome_a[0], outcome_b[0]
        successes = sum(1 for o in results if o is None)
        failures = [o for o in results if o is not None]

        assert successes == 1
        assert len(failures) == 1
        assert isinstance(
            failures[0], DuplicatePortfolioProjectEffortFocusDecisionError
        )
        # The surviving row is intact.
        assert len(repo_a.list_history(PORTFOLIO)) == 1
        survivor = repo_a.list_history(PORTFOLIO)[0]
        assert survivor.decision_id == DECISION_ID_2
        assert survivor.decision.portfolio_id == PORTFOLIO
    finally:
        repo_a.close()
        repo_b.close()


def test_value_equivalent_decisions_with_different_ids_coexist(
    repo: SqlitePortfolioProjectEffortFocusDecisionRepository,
) -> None:
    decision = _accepted(SCENARIO_REF)

    first = record_portfolio_effort_focus_decision_durably(
        DECISION_ID_1, T_UTC, decision, repository=repo
    )
    second = record_portfolio_effort_focus_decision_durably(
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
    repo: SqlitePortfolioProjectEffortFocusDecisionRepository,
) -> None:
    decision = _accepted(SCENARIO_REF)
    record_portfolio_effort_focus_decision_durably(
        DECISION_ID_1, T_UTC, decision, repository=repo
    )

    # OTHER_PORTFOLIO has no rows at all.
    assert repo.list_history(OTHER_PORTFOLIO) == ()
    assert len(repo.list_history(PORTFOLIO)) == 1


# ---------------------------------------------------------------------------
# Surface.
# ---------------------------------------------------------------------------


def test_no_update_or_delete_api_exists(
    repo: SqlitePortfolioProjectEffortFocusDecisionRepository,
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


def test_add_rejects_non_record_and_hostile_records_before_write(
    saved_portfolio: Path,
) -> None:
    with SqlitePortfolioProjectEffortFocusDecisionRepository(
        saved_portfolio
    ) as r:
        with pytest.raises(TypeError):
            r.add(_ForeignModel())

        hostile_decision = PortfolioProjectEffortFocusDecision.model_construct(
            portfolio_id=PORTFOLIO,
            source_project_count=3,
            reference_requested_limit=1,
            reference_selected_project_count=9,  # breaks the invariant
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
        with pytest.raises((ValidationError, ValueError)):
            r.add(
                PortfolioProjectEffortFocusDecisionRecord.model_construct(
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


def test_mismatching_explicit_portfolio_column_raises(
    saved_portfolio: Path,
) -> None:
    with SqlitePortfolioRepository(saved_portfolio) as portfolio_repo:
        portfolio_repo.save(Portfolio(id=OTHER_PORTFOLIO, name="V1.35 other"))

    decision = _accepted(SCENARIO_REF)
    # The nested snapshot declares PORTFOLIO; the row column says OTHER.
    _raw_insert(
        saved_portfolio,
        decision_id=DECISION_ID_5,
        row_portfolio=OTHER_PORTFOLIO,
        snapshot=decision.model_dump_json(),
    )
    with SqlitePortfolioProjectEffortFocusDecisionRepository(
        saved_portfolio
    ) as r, pytest.raises(ValueError):
        r.list_history(OTHER_PORTFOLIO)


def test_corrupted_snapshot_raises_precise_error(saved_portfolio: Path) -> None:
    _raw_insert(
        saved_portfolio,
        decision_id=DECISION_ID_5,
        row_portfolio=PORTFOLIO,
        snapshot="this is not json",
    )
    with SqlitePortfolioProjectEffortFocusDecisionRepository(
        saved_portfolio
    ) as r, pytest.raises(ValidationError):
        r.list_history(PORTFOLIO)

    decision = _accepted(SCENARIO_REF)
    corrupted = json.loads(decision.model_dump_json())
    corrupted["portfolio_id"] = 123
    _raw_insert(
        saved_portfolio,
        decision_id=DECISION_ID_4,
        row_portfolio=PORTFOLIO,
        snapshot=json.dumps(corrupted),
    )
    with SqlitePortfolioProjectEffortFocusDecisionRepository(
        saved_portfolio
    ) as r, pytest.raises(ValidationError):
        r.list_history(PORTFOLIO)


def test_foreign_key_enforced_on_insert(saved_portfolio: Path) -> None:
    missing = UUID("79797979-7979-4979-8979-797979797979")
    decision = _accepted(SCENARIO_REF).model_copy(
        update={"portfolio_id": missing}
    )
    record = PortfolioProjectEffortFocusDecisionRecord(
        decision_id=DECISION_ID_5, decided_at=T_UTC, decision=decision
    )
    with SqlitePortfolioProjectEffortFocusDecisionRepository(
        saved_portfolio
    ) as r:
        with pytest.raises(IntegrityError):
            r.add(record)
        # The failed attempt left no history behind.
        assert r.list_history(missing) == ()


def test_context_manager_and_pragmas(saved_portfolio: Path) -> None:
    with SqlitePortfolioProjectEffortFocusDecisionRepository(saved_portfolio) as r:
        decision = _accepted(SCENARIO_REF)
        returned = record_portfolio_effort_focus_decision_durably(
            DECISION_ID_1, T_UTC, decision, repository=r
        )
        assert returned.decision_id == DECISION_ID_1
        assert len(r.list_history(PORTFOLIO)) == 1
        with r.engine.connect() as connection:
            pragma = connection.exec_driver_sql("PRAGMA foreign_keys").fetchone()
        assert pragma[0] == 1


def test_repository_rejects_wrong_type_input(saved_portfolio: Path) -> None:
    with SqlitePortfolioProjectEffortFocusDecisionRepository(
        saved_portfolio
    ) as r:
        with pytest.raises(TypeError):
            r.add(_ForeignModel())
        assert r.list_history(PORTFOLIO) == ()
