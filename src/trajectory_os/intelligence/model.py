"""M056–M063 — shared provenance, identity and fail-closed primitives.

The single most important invariant of the intelligence layer is that a
value is never detached from how it was obtained. Every observation, model
metric, retrieval score and decision input carries an explicit
:class:`Provenance` label drawn from a closed set:

``MEASURED``
    Read directly from a canonical Trajectory_OS artifact (a real run's
    ``meta.txt``/``lifecycle.json``/telemetry document).
``DERIVED``
    Deterministically computed from two or more ``MEASURED`` values.
``INFERRED``
    Produced by a model or heuristic from other values; never a measurement.
``UNAVAILABLE``
    Absent. An ``UNAVAILABLE`` value always carries a bounded reason and is
    never estimated, averaged or fabricated.

Nothing in this module performs I/O beyond atomic writes, reads a clock
outside an injected callable, or touches a release Git surface.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

#: Schema version of the durable intelligence documents.
SCHEMA_VERSION = 1

#: Human/machine intelligence bundle version string (additive).
INTELLIGENCE_VERSION = "m056-m063.1"

# --- provenance labels (closed set) ------------------------------------------

MEASURED = "MEASURED"
DERIVED = "DERIVED"
INFERRED = "INFERRED"
UNAVAILABLE = "UNAVAILABLE"

PROVENANCE_LABELS = frozenset({MEASURED, DERIVED, INFERRED, UNAVAILABLE})

#: Labels that assert a concrete value exists.
CONCRETE_PROVENANCE = frozenset({MEASURED, DERIVED, INFERRED})

# --- source kinds (closed set) ------------------------------------------------

SOURCE_REAL = "REAL"
SOURCE_FIXTURE = "FIXTURE"

SOURCE_KINDS = frozenset({SOURCE_REAL, SOURCE_FIXTURE})

# --- stable fail-closed error codes ------------------------------------------

E_MALFORMED = "MALFORMED_INTELLIGENCE"
E_UNSUPPORTED_VERSION = "UNSUPPORTED_INTELLIGENCE_SCHEMA"
E_MISSING_REASON = "UNAVAILABLE_WITHOUT_REASON"
E_MISSING_VALUE = "CONCRETE_VALUE_ABSENT"
E_MISSING_SOURCE = "CONCRETE_VALUE_WITHOUT_SOURCE"
E_UNKNOWN_PROVENANCE = "UNKNOWN_PROVENANCE_LABEL"
E_IDENTITY_MISMATCH = "INTELLIGENCE_IDENTITY_MISMATCH"
E_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
E_LEAKAGE = "DATASET_SPLIT_LEAKAGE"
E_ROUTE_UNCERTAIN = "ROUTE_EVIDENCE_NOT_COMPARABLE"

# --- bounded limits -----------------------------------------------------------

MAX_STR_LEN = 256
MAX_REF_LEN = 512
MAX_REASON_LEN = 512
MAX_ROWS = 100_000
MAX_FIELD_NAME_LEN = 64

#: Domain separator for every intelligence identity digest.
PROVENANCE_DOMAIN = "trajectory-os.intelligence.provenance.v1"
MATERIAL_DOMAIN = "trajectory-os.intelligence.material.v1"


class IntelligenceError(Exception):
    """An intelligence-layer contract violation (fail closed)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def fail(code: str, detail: str = "") -> NoReturn:
    raise IntelligenceError(code, detail)


# --- deterministic canonical helpers -----------------------------------------


def canonical_json(payload: object) -> str:
    """Deterministic JSON text: sorted keys, compact separators, ASCII."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        default=str)


def digest(payload: object, *, domain: str) -> str:
    """SHA-256 hex digest of ``domain || 0x00 || canonical_json``."""
    material = (domain.encode("utf-8") + b"\x00"
                + canonical_json(payload).encode("utf-8"))
    return hashlib.sha256(material).hexdigest()


def utc_now() -> str:
    """Canonical UTC timestamp (seconds precision, ``Z`` suffix)."""
    return (datetime.now(UTC).replace(microsecond=0).isoformat()
            .replace("+00:00", "Z"))


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomic JSON write (temp file + ``os.replace``), fail closed."""
    from trajectory_os.observability import store as obs_store

    obs_store.write_json(path, payload)


def write_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    """Atomic JSON write to ``path`` (creating parents), fail closed."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_json(target, payload)


def read_json(path: str | Path) -> dict[str, Any] | None:
    """Read an optional JSON object; missing/unreadable/malformed -> None."""
    from trajectory_os.observability import store as obs_store

    if not Path(path).is_file():
        return None
    try:
        document = obs_store.read_json(Path(path))
    except obs_store.CanonicalStoreError:
        return None
    return document


def append_jsonl(path: str | Path, record: Mapping[str, Any]) -> None:
    """Append one canonical JSON line, fsynced, creating parents."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = canonical_json(dict(record))
    with target.open("ab") as handle:
        handle.write(line.encode("utf-8") + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Read an append-only JSONL file; missing file -> empty list."""
    target = Path(path)
    if not target.is_file():
        return []
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            document = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(document, dict):
            out.append(document)
    return out


# --- provenance ---------------------------------------------------------------


@dataclass(frozen=True)
class Provenance:
    """How one value was obtained (never detached from the value)."""

    field_name: str
    source: str
    source_ref: str
    reason: str | None = None

    def validate(self) -> Provenance:
        _require_str(self.field_name, "provenance", "field_name",
                     maximum=MAX_FIELD_NAME_LEN)
        if self.source not in PROVENANCE_LABELS:
            fail(E_UNKNOWN_PROVENANCE, f"{self.field_name}={self.source!r}")
        if self.source == UNAVAILABLE:
            if not self.reason:
                fail(E_MISSING_REASON, self.field_name)
        else:
            if not (isinstance(self.source_ref, str) and self.source_ref):
                fail(E_MISSING_SOURCE, self.field_name)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field_name,
            "source": self.source,
            "source_ref": self.source_ref,
            "reason": self.reason,
        }

    @staticmethod
    def from_dict(data: object) -> Provenance:
        if not isinstance(data, Mapping):
            fail(E_MALFORMED, "provenance must be an object")
        return Provenance(
            field_name=str(data.get("field", "")),
            source=str(data.get("source", "")),
            source_ref=str(data.get("source_ref", "") or ""),
            reason=(str(data["reason"])
                    if data.get("reason") is not None else None),
        ).validate()


@dataclass(frozen=True)
class ProvenanceMap:
    """Ordered, validated mapping of field name -> provenance."""

    entries: tuple[Provenance, ...] = field(default_factory=tuple)

    def get(self, field_name: str) -> Provenance | None:
        for entry in self.entries:
            if entry.field_name == field_name:
                return entry
        return None

    def require(self, field_name: str) -> Provenance:
        entry = self.get(field_name)
        if entry is None:
            fail(E_MISSING_SOURCE, field_name)
        return entry

    def with_entry(self, provenance: Provenance) -> ProvenanceMap:
        kept = tuple(e for e in self.entries
                     if e.field_name != provenance.field_name)
        return ProvenanceMap(entries=(*kept, provenance.validate()))

    def to_list(self) -> list[dict[str, Any]]:
        return [entry.to_dict()
                for entry in sorted(self.entries, key=lambda e: e.field_name)]

    @staticmethod
    def from_list(document: object) -> ProvenanceMap:
        if not isinstance(document, Sequence) or isinstance(
                document, (str, bytes, bytearray)):
            fail(E_MALFORMED, "provenance must be a list")
        entries = tuple(Provenance.from_dict(item) for item in document)
        seen: set[str] = set()
        for entry in entries:
            if entry.field_name in seen:
                fail(E_MALFORMED, f"duplicate provenance {entry.field_name}")
            seen.add(entry.field_name)
        return ProvenanceMap(entries=entries)

    def concat(self, other: ProvenanceMap) -> ProvenanceMap:
        merged = self
        for entry in other.entries:
            merged = merged.with_entry(entry)
        return merged


def measured(field_name: str, source_ref: str) -> Provenance:
    return Provenance(field_name=field_name, source=MEASURED,
                      source_ref=source_ref)


def derived(field_name: str, source_ref: str) -> Provenance:
    return Provenance(field_name=field_name, source=DERIVED,
                      source_ref=source_ref)


def inferred(field_name: str, source_ref: str) -> Provenance:
    return Provenance(field_name=field_name, source=INFERRED,
                      source_ref=source_ref)


def unavailable(field_name: str, reason: str) -> Provenance:
    return Provenance(field_name=field_name, source=UNAVAILABLE,
                      source_ref="", reason=reason)


# --- strict primitive helpers -------------------------------------------------


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_str(value: object, path: str, detail: str, *,
                 maximum: int = MAX_STR_LEN) -> str:
    if not isinstance(value, str) or not value:
        fail(E_MALFORMED, f"{path}: {detail}")
    if len(value) > maximum:
        fail(E_MALFORMED, f"{path}: {detail} exceeds {maximum} chars")
    return value


def _optional_str(value: object, detail: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _require_str(value, "value", detail, maximum=maximum)


def _optional_number(value: object, detail: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail(E_MALFORMED, detail)
    return float(value)


def require_mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        fail(E_MALFORMED, f"{path} must be an object")
    return value


def require_list(value: object, path: str) -> list[Any]:
    if not isinstance(value, list):
        fail(E_MALFORMED, f"{path} must be a list")
    return value


def _require_int(value: object, path: str, detail: str, *,
                 minimum: int = 0, maximum: int | None = None) -> int:
    if not _is_int(value):
        fail(E_MALFORMED, f"{path}: {detail}")
    assert isinstance(value, int)
    if value < minimum or (maximum is not None and value > maximum):
        fail(E_MALFORMED, f"{path}: {detail} out of bounds {value}")
    return value


def check_version(document: Mapping[str, Any], path: str) -> None:
    if "schema_version" not in document:
        # Fail closed: a document without an explicit schema version is
        # not silently assumed to be current.
        fail(E_UNSUPPORTED_VERSION, f"{path}: missing schema_version")
    version = document["schema_version"]
    if version != SCHEMA_VERSION:
        fail(E_UNSUPPORTED_VERSION, f"{path}: schema_version={version!r}")


__all__ = [
    "CONCRETE_PROVENANCE",
    "DERIVED",
    "E_IDENTITY_MISMATCH",
    "E_INSUFFICIENT_DATA",
    "E_LEAKAGE",
    "E_MALFORMED",
    "E_MISSING_REASON",
    "E_MISSING_SOURCE",
    "E_MISSING_VALUE",
    "E_ROUTE_UNCERTAIN",
    "E_UNKNOWN_PROVENANCE",
    "E_UNSUPPORTED_VERSION",
    "INFERRED",
    "INTELLIGENCE_VERSION",
    "IntelligenceError",
    "MATERIAL_DOMAIN",
    "MAX_ROWS",
    "MEASURED",
    "PROVENANCE_DOMAIN",
    "PROVENANCE_LABELS",
    "Provenance",
    "ProvenanceMap",
    "SCHEMA_VERSION",
    "SOURCE_FIXTURE",
    "SOURCE_KINDS",
    "SOURCE_REAL",
    "UNAVAILABLE",
    "append_jsonl",
    "canonical_json",
    "check_version",
    "derived",
    "digest",
    "fail",
    "inferred",
    "measured",
    "read_json",
    "read_jsonl",
    "require_list",
    "require_mapping",
    "unavailable",
    "utc_now",
    "write_json",
]
