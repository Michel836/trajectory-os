"""M024 — durable local resource reservations (atomic, strict, fail closed).

The reservation document is the *only* authoritative record of currently
admitted local workloads. Every write is atomic (temp file + fsync + rename);
every read is strict (unsupported schema, malformed records and unknown roles
fail closed). The store never probes hardware and never makes an admission
decision — it only persists decisions made by
:class:`trajectory_os.resources.arbiter.ResourceArbiter`.
"""

from __future__ import annotations

import json
from pathlib import Path

from trajectory_os.resources import model
from trajectory_os.runs.store import atomic_write_json

#: Relative path of the reservation document under the state root.
RESERVATIONS_REL = "resources/reservations.json"


class MalformedReservationsError(Exception):
    """Malformed persisted reservations (fail closed, never repaired)."""

    def __init__(self, detail: str = "") -> None:
        super().__init__(f"RESOURCE_RESERVATIONS_MALFORMED: {detail}")
        self.code = "RESOURCE_RESERVATIONS_MALFORMED"
        self.detail = detail


def reservations_path(root: str | Path) -> Path:
    return Path(root) / RESERVATIONS_REL


def load_reservations(root: str | Path) -> dict[str, model.Reservation]:
    """Strictly load the persisted reservations (empty when absent)."""
    path = reservations_path(root)
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        doc = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MalformedReservationsError(str(exc)) from exc
    if not isinstance(doc, dict):
        raise MalformedReservationsError("root must be an object")
    if doc.get("schema_version") != model.SCHEMA_VERSION:
        raise MalformedReservationsError("unsupported schema version")
    records = doc.get("reservations")
    if not isinstance(records, list):
        raise MalformedReservationsError("reservations must be a list")
    loaded: dict[str, model.Reservation] = {}
    for record in records:
        reservation = model.Reservation.from_dict(record)
        if not reservation.job_id:
            raise MalformedReservationsError("reservation missing job_id")
        if reservation.job_id in loaded:
            raise MalformedReservationsError(
                f"duplicate reservation {reservation.job_id!r}")
        loaded[reservation.job_id] = reservation
    return loaded


def save_reservations(
    root: str | Path, reservations: dict[str, model.Reservation],
) -> None:
    """Atomically persist the current reservations (deterministic order)."""
    ordered = [reservations[key].to_dict() for key in sorted(reservations)]
    atomic_write_json(reservations_path(root), {
        "schema_version": model.SCHEMA_VERSION,
        "reservations": ordered,
    })
