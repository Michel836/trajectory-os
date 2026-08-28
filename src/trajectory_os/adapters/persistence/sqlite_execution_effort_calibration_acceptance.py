"""SQLite persistence adapter for accepted calibrated estimate revisions (V1.21).

``SqliteCalibratedEstimateRevisionRepository`` persists a V1.21 acceptance
atomically: ONE SQLite transaction appends EXACTLY the passed V1.10
``ExecutionEffortEstimate`` row (through the exact, unchanged existing
V1.10 ``ExecutionEffortEstimateRow`` table and column mapping) AND
EXACTLY the passed V1.21 ``AcceptedCalibratedEstimateRevision``
provenance row (through the new, immutable
``accepted_calibrated_estimate_revisions`` table). One commit. There is
no two-call path, no two-commit path, and no partial state: if either
append fails after validation, the transaction rolls back and NEITHER
row is persisted.

Representation mapping is explicit at this boundary:

- UUID values are stored as 36-character text (RFC 4122 string form);
- ``entity_type`` is stored by its enum ``.value``;
- both durations are EXACT INTEGERs (never REAL);
- ``estimated_at`` is ISO-8601 aware text (same convention as the
  estimate row, offset preserved);
- the EXACT accepted V1.20 snapshot (V1.20 -> V1.19 -> V1.18 provenance
  chain) is stored as deterministic explicit JSON produced by the
  Pydantic JSON serialization of the genuine nested domain objects
  (``model_dump(mode="json")`` -\u003e JSON text). This is NOT a pickle and
  NOT an opaque binary blob: every core identifier and the accepted
  arithmetic is ALSO stored in the dedicated TEXT/INTEGER columns of the
  row, so plain estimates, plain provenance columns, and the nested
  snapshot all remain independently queryable and corruptible-visible.

Read-back reconstructs REAL domain values through normal strict Pydantic
validation (never ``model_construct``), including re-validation of the
stored JSON snapshot back into a genuine, fresh V1.20
``CalibratedEstimateRevisionProposal`` (with its nested V1.19/V1.18
objects re-included), so model-level invariants are revalidated on every
read.

Append-only V1.21 contract:

- ``add_accepted_revision()`` only INSERTs; it never updates or replaces
  existing rows;
- the same estimate id cannot be accepted twice: a second attempt with a
  duplicate estimate id raises
  :class:`DuplicateExecutionEffortEstimateError` (the exact existing
  V1.10 duplicate error, re-used so the estimate identity semantics stay
  singular and authoritative);
- the same estimate id cannot carry two different provenances: a second
  attempt with a duplicate provenance estimate link raises
  :class:`DuplicateCalibratedEstimateRevisionError`;
- plain V1.10 estimates (no accepted revision) remain valid: they simply
  have no provenance row, and ``get_provenance()`` returns ``None`` for
  them;
- no update/delete/correction API exists;
- the estimate append and the provenance append share ONE transaction and
  ONE commit; there is no ``NO_ACTION``/partial success state.

``estimate_id`` (the provenance row's primary key) links one-to-one into
``execution_effort_estimates.id`` and carries no SQLite-specific foreign
key by design: history must never be deleted by dropping/recreating the
estimate or entity snapshot tables during V0 development, and no
SQLite-specific NULL-PK or rowid behavior is ever relied upon.
``portfolio_id`` is a foreign key into ``portfolios.id`` with
``ON DELETE CASCADE`` (same convention as the estimate/observation
tables). ``project_id``/``entity_id``/``entity_type`` are explicit
immutable snapshot columns and carry no ``entities.id`` foreign keys:
historical provenance must survive the replacement or removal of entity
snapshot rows in later portfolio saves.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import event, insert, select
from sqlalchemy.engine import Engine, create_engine
from sqlalchemy.orm import Session

from trajectory_os.adapters.persistence.models import (
    AcceptedCalibratedEstimateRevisionRow,
    Base,
    ExecutionEffortEstimateRow,
)
from trajectory_os.adapters.persistence.sqlite_execution_effort_estimates import (
    DuplicateExecutionEffortEstimateError,
)
from trajectory_os.application.execution_effort_calibration_acceptance import (
    AcceptedCalibratedEstimateRevision,
)
from trajectory_os.application.execution_effort_calibration_entity_binding import (
    CalibratedEstimateRevisionProposal,
    CalibratedEstimateRevisionProposalStatus,
)
from trajectory_os.domain.entities import EntityType, SourceKind
from trajectory_os.domain.execution_effort_estimates import ExecutionEffortEstimate


class DuplicateCalibratedEstimateRevisionError(ValueError):
    """Raised when an estimate id already has an accepted-revision provenance row."""


def _to_text(value: UUID) -> str:
    return str(value)


def _to_domain_datetime(value: str) -> datetime:
    """Reconstruct a datetime from stored ISO-8601 text."""
    return datetime.fromisoformat(value)


def _row_to_domain(row: AcceptedCalibratedEstimateRevisionRow) -> (
    AcceptedCalibratedEstimateRevision
):
    """Reconstruct one strict immutable V1.21 provenance record from a row.

    The stored explicit JSON snapshot is FIRST re-validated into a
    genuine, fresh V1.20 ``CalibratedEstimateRevisionProposal`` (with its
    nested V1.19/V1.18 objects re-included by strict Pydantic
    validation), and only then is the outer V1.21 record built around
    it. The dedicated TEXT/INTEGER columns are re-validated against that
    genuine snapshot by the V1.21 model's own cross-field validator.
    """
    source_proposal = CalibratedEstimateRevisionProposal.model_validate_json(
        row.accepted_v120_snapshot,
    )
    return AcceptedCalibratedEstimateRevision(
        estimate_id=UUID(row.estimate_id),
        portfolio_id=UUID(row.portfolio_id),
        project_id=UUID(row.project_id),
        entity_id=UUID(row.entity_id),
        entity_type=EntityType(row.entity_type),
        candidate_duration_seconds=int(row.candidate_duration_seconds),
        calibrated_duration_seconds=int(row.calibrated_duration_seconds),
        estimated_at=_to_domain_datetime(row.estimated_at),
        source_proposal=source_proposal,
    )


class SqliteCalibratedEstimateRevisionRepository:
    """Atomic append of one V1.10 estimate + its V1.21 provenance record.

    The engine is owned by the repository; close it with ``close()`` when
    done. One connection is used per operation, so the repository can be
    shared freely from a single thread without ``check_same_thread=False``.
    """

    def __init__(self, database_path: str | Path) -> None:
        self._path = Path(database_path)
        self._engine = create_engine(f"sqlite:///{self._path.as_posix()}")

        @event.listens_for(self._engine, "connect")
        def _enable_foreign_keys(
            dbapi_connection: Any, connection_record: Any
        ) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

        # The pragma is registered before the first connection is opened,
        # so it is active for metadata.create_all() as well.
        Base.metadata.create_all(self._engine)

    @property
    def engine(self) -> Engine:
        """Expose the underlying engine for inspection (e.g. pragma checks)."""
        return self._engine

    def __enter__(self) -> SqliteCalibratedEstimateRevisionRepository:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the database connections owned by this repository."""
        self._engine.dispose()

    def _validate_payload(
        self,
        estimate: object,
        provenance: object,
    ) -> tuple[ExecutionEffortEstimate, AcceptedCalibratedEstimateRevision]:
        """Strictly validate BOTH inputs (before any write transaction opens).

        Each payload must be a genuine instance of the expected domain
        type and must survive ``model_validate(..., strict=True)`` of its
        own ``model_dump()`` round-trip, defeating ``model_construct()``
        bypass attempts. The V1.20 snapshot embedded in the provenance
        record is re-validated through the V1.21 record's own nested
        validation. Cross-object identity and status checks follow.
        """
        if not isinstance(estimate, ExecutionEffortEstimate):
            raise TypeError(
                "estimate must be a genuine ExecutionEffortEstimate (V1.10) "
                f"instance, got {type(estimate).__name__}"
            )
        if not isinstance(provenance, AcceptedCalibratedEstimateRevision):
            raise TypeError(
                "provenance must be a genuine "
                "AcceptedCalibratedEstimateRevision (V1.21) instance, "
                f"got {type(provenance).__name__}"
            )

        validated_estimate = ExecutionEffortEstimate.model_validate(
            estimate.model_dump(mode="python"), strict=True
        )
        validated_provenance = AcceptedCalibratedEstimateRevision.model_validate(
            provenance.model_dump(mode="python"), strict=True
        )

        if validated_estimate.id != validated_provenance.estimate_id:
            raise ValueError(
                "estimate.id and provenance.estimate_id must match exactly "
                f"({validated_estimate.id} != {validated_provenance.estimate_id})"
            )

        if (
            validated_provenance.source_proposal.status
            is not CalibratedEstimateRevisionProposalStatus.READY
        ):
            raise ValueError(
                "provenance.source_proposal.status must be READY exactly; "
                "a NO_EFFECTIVE_FACTOR snapshot is not an accepted revision"
            )

        if validated_estimate.source is not SourceKind.USER_CONFIRMED:
            raise ValueError(
                "estimate.source must be USER_CONFIRMED exactly for a "
                "V1.21 accepted calibrated estimate revision"
            )

        return validated_estimate, validated_provenance

    def add_accepted_revision(
        self,
        estimate: ExecutionEffortEstimate,
        provenance: AcceptedCalibratedEstimateRevision,
    ) -> None:
        """Atomically append the estimate AND its accepted-revision provenance.

        Both INSERTs and both duplicate pre-checks happen inside ONE
        ``Session`` transaction and are committed exactly once at the end
        (one commit). If either duplicate pre-check fires, or either
        INSERT fails, the transaction is aborted and NOTHING is persisted;
        an already-persisted estimate row is never updated or replaced.
        """
        validated_estimate, validated_provenance = self._validate_payload(
            estimate, provenance
        )

        stored_estimate_id = _to_text(validated_estimate.id)

        with Session(self._engine) as session:
            # Duplicate identity pre-checks (same transaction as the INSERTs).
            existing_estimate = session.scalar(
                select(ExecutionEffortEstimateRow.id).where(
                    ExecutionEffortEstimateRow.id == stored_estimate_id
                )
            )
            if existing_estimate is not None:
                raise DuplicateExecutionEffortEstimateError(
                    f"execution-effort estimate already exists: {stored_estimate_id}"
                )

            existing_provenance = session.scalar(
                select(AcceptedCalibratedEstimateRevisionRow.estimate_id).where(
                    AcceptedCalibratedEstimateRevisionRow.estimate_id == stored_estimate_id
                )
            )
            if existing_provenance is not None:
                raise DuplicateCalibratedEstimateRevisionError(
                    f"accepted calibrated estimate revision already exists: "
                    f"{stored_estimate_id}"
                )

            # Estimate append — same V1.10 table, same explicit mapping.
            session.execute(
                insert(ExecutionEffortEstimateRow).values(
                    id=stored_estimate_id,
                    portfolio_id=_to_text(validated_estimate.portfolio_id),
                    entity_id=_to_text(validated_estimate.entity_id),
                    duration_seconds=validated_estimate.duration_seconds,
                    estimated_at=validated_estimate.estimated_at.isoformat(),
                    source=validated_estimate.source.value,
                )
            )

            # Provenance append — new immutable table, exact snapshot JSON.
            session.execute(
                insert(AcceptedCalibratedEstimateRevisionRow).values(
                    estimate_id=stored_estimate_id,
                    portfolio_id=_to_text(validated_provenance.portfolio_id),
                    project_id=_to_text(validated_provenance.project_id),
                    entity_id=_to_text(validated_provenance.entity_id),
                    entity_type=validated_provenance.entity_type.value,
                    candidate_duration_seconds=(
                        validated_provenance.candidate_duration_seconds
                    ),
                    calibrated_duration_seconds=(
                        validated_provenance.calibrated_duration_seconds
                    ),
                    estimated_at=validated_provenance.estimated_at.isoformat(),
                    accepted_v120_snapshot=(
                        validated_provenance.source_proposal.model_dump_json()
                    ),
                )
            )

            # ONE commit for BOTH rows: atomic append, all-or-nothing.
            session.commit()

    def get_provenance(
        self, estimate_id: UUID
    ) -> AcceptedCalibratedEstimateRevision | None:
        """Load the stored V1.21 provenance for ``estimate_id``, or ``None``.

        Returns ``None`` for plain V1.10 estimates that were never
        accepted through V1.21 (no provenance row). The estimate itself is
        read through the existing, unchanged, V1.10 estimate read path
        (``SqliteExecutionEffortEstimateRepository``), not re-implemented
        here.
        """
        stored_estimate_id = _to_text(estimate_id)

        with Session(self._engine) as session:
            row = session.scalar(
                select(AcceptedCalibratedEstimateRevisionRow).where(
                    AcceptedCalibratedEstimateRevisionRow.estimate_id
                    == stored_estimate_id
                )
            )

        if row is None:
            return None

        return _row_to_domain(row)
