"""M064 — production-useful, provenance-first real input adapters.

Supported real inputs (a deliberately minimal, useful set):

* local files and folders (recursive, allow-listed text formats);
* Markdown / plain text;
* PDF-*derived* text (a ``.pdf.txt`` / ``.txt`` text export) — never OCR;
* CSV / TSV structured input (Excel-compatible exports);
* LifeOS / Obsidian markdown notes;
* URL / research evidence where the caller supplies the fetched text
  (this module never performs network I/O).

Every ingested source is persisted with a deterministic manifest carrying
``source_id``, ``origin``, location, timestamp, ``content_hash``,
``project_id``, ``mission_id``, ``sensitivity``, ``ingestion_status``,
duplicate status and stale/change status. Duplicates, changed sources and
unsupported formats are explicit results, never silent. Conversion
limitations are always recorded; there is **no silent OCR claim**.

An ingested source is *context/provenance*, never runtime truth: the manifest
is labelled ``canonical=false``. Secret-shaped assignments are redacted before
any content is persisted, and ``RESTRICTED`` content is never written to disk.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trajectory_os.intelligence import knowledge
from trajectory_os.realworld import model

# --- origins (closed set) -----------------------------------------------------

ORIGIN_LOCAL_FILE = "LOCAL_FILE"
ORIGIN_LOCAL_FOLDER = "LOCAL_FOLDER"
ORIGIN_MARKDOWN = "MARKDOWN"
ORIGIN_TEXT = "TEXT"
ORIGIN_PDF_TEXT = "PDF_TEXT"
ORIGIN_CSV = "CSV"
ORIGIN_EXCEL_COMPATIBLE = "EXCEL_COMPATIBLE"
ORIGIN_LIFEOS_NOTE = "LIFEOS_NOTE"
ORIGIN_URL = "URL"
ORIGIN_RESEARCH = "RESEARCH"

ORIGINS = frozenset({
    ORIGIN_LOCAL_FILE, ORIGIN_LOCAL_FOLDER, ORIGIN_MARKDOWN, ORIGIN_TEXT,
    ORIGIN_PDF_TEXT, ORIGIN_CSV, ORIGIN_EXCEL_COMPATIBLE,
    ORIGIN_LIFEOS_NOTE, ORIGIN_URL, ORIGIN_RESEARCH,
})

# --- ingestion statuses (closed set) -----------------------------------------

STATUS_INGESTED = "INGESTED"
STATUS_DUPLICATE = "DUPLICATE"
STATUS_CHANGED = "CHANGED"
STATUS_UNSUPPORTED = "UNSUPPORTED"
STATUS_FAILED = "FAILED"

INGESTION_STATUSES = frozenset({
    STATUS_INGESTED, STATUS_DUPLICATE, STATUS_CHANGED, STATUS_UNSUPPORTED,
    STATUS_FAILED,
})

# --- conversion limitations (explicit, bounded) ------------------------------

LIM_RAW_PDF = "RAW_PDF_BINARY_NOT_PARSED"
LIM_NO_OCR = "OCR_NOT_PERFORMED"
LIM_BINARY_SPREADSHEET = "BINARY_SPREADSHEET_SUPPLY_CSV_EXPORT"
LIM_BINARY_DOCUMENT = "BINARY_DOCUMENT_SUPPLY_TEXT_EXPORT"
LIM_IMAGE = "IMAGE_NOT_OCR_D"
LIM_UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
LIM_NETWORK_NOT_FETCHED = "NETWORK_FETCH_NOT_PERFORMED"
LIM_TRUNCATED = "CONTENT_TRUNCATED"
LIM_REDACTED = "SECRET_SHAPED_CONTENT_REDACTED"
LIM_RESTRICTED_NOT_PERSISTED = "RESTRICTED_CONTENT_NOT_PERSISTED"

#: Limitations that make a source structurally unsupported (never parsed).
UNSUPPORTED_LIMITATIONS = frozenset({
    LIM_RAW_PDF, LIM_NO_OCR, LIM_BINARY_SPREADSHEET, LIM_BINARY_DOCUMENT,
    LIM_IMAGE, LIM_UNSUPPORTED_FORMAT,
})

#: Content bound (characters) persisted per source.
MAX_CONTENT_CHARS = 200_000
MAX_SOURCES = 5_000

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

_TEXT_EXTENSIONS = frozenset({
    ".md", ".markdown", ".txt", ".text", ".csv", ".tsv", ".json",
    ".jsonl", ".yaml", ".yml",
})
_PDF_TEXT_EXTENSIONS = frozenset({".pdf.txt"})
_RAW_PDF_EXTENSIONS = frozenset({".pdf"})
_BINARY_SHEET_EXTENSIONS = frozenset({".xlsx", ".xls", ".xlsm", ".ods"})
_BINARY_DOC_EXTENSIONS = frozenset({".docx", ".doc", ".pptx", ".ppt"})
_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp",
                               ".bmp", ".tiff"})

MANIFEST_DOMAIN = "trajectory-os.realworld.ingest-manifest.v1"


@dataclass(frozen=True)
class SourceRecord:
    """One ingested (or explicitly rejected) source."""

    source_id: str
    origin: str
    location: str
    title: str
    content_hash: str
    project_id: str | None
    mission_id: str | None
    sensitivity: str
    ingestion_status: str
    char_count: int
    ingested_at: str
    modified_at: str | None = None
    duplicate_of: str | None = None
    changed_from: str | None = None
    conversion_limitations: tuple[str, ...] = ()
    parse_status: str = "OK"
    canonical: bool = False

    def validate(self) -> SourceRecord:
        if self.origin not in ORIGINS:
            model.intel_model.fail(model.intel_model.E_MALFORMED,
                                   f"unknown origin {self.origin!r}")
        if self.ingestion_status not in INGESTION_STATUSES:
            model.intel_model.fail(model.intel_model.E_MALFORMED,
                                   "unknown ingestion status")
        if self.sensitivity not in knowledge.SENSITIVITIES:
            model.intel_model.fail(model.intel_model.E_MALFORMED,
                                   "unknown sensitivity")
        if self.ingestion_status == STATUS_DUPLICATE and not self.duplicate_of:
            model.intel_model.fail(model.intel_model.E_MALFORMED,
                                   "duplicate requires duplicate_of")
        if self.ingestion_status == STATUS_CHANGED and not self.changed_from:
            model.intel_model.fail(model.intel_model.E_MALFORMED,
                                   "changed source requires changed_from")
        return self

    @property
    def is_usable(self) -> bool:
        return (self.ingestion_status in (STATUS_INGESTED, STATUS_CHANGED)
                and self.parse_status == "OK")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "origin": self.origin,
            "location": self.location,
            "title": self.title,
            "content_hash": self.content_hash,
            "project_id": self.project_id,
            "mission_id": self.mission_id,
            "sensitivity": self.sensitivity,
            "ingestion_status": self.ingestion_status,
            "char_count": self.char_count,
            "ingested_at": self.ingested_at,
            "modified_at": self.modified_at,
            "duplicate_of": self.duplicate_of,
            "changed_from": self.changed_from,
            "conversion_limitations": list(self.conversion_limitations),
            "parse_status": self.parse_status,
            "canonical": self.canonical,
        }


@dataclass(frozen=True)
class IngestedContent:
    """Redacted content kept in memory (and optionally persisted) for RAG."""

    source_id: str
    text: str
    persisted: bool


@dataclass(frozen=True)
class IngestionManifest:
    """The deterministic manifest over one ingestion run."""

    manifest_id: str
    generated_at: str
    records: tuple[SourceRecord, ...]
    contents: tuple[IngestedContent, ...] = field(default_factory=tuple)

    #: Labels the manifest as context, never canonical runtime truth.
    canonical: bool = False
    runtime_truth: bool = False

    @property
    def source_count(self) -> int:
        return len(self.records)

    def status_counts(self) -> dict[str, int]:
        counts = {status: 0 for status in sorted(INGESTION_STATUSES)}
        for record in self.records:
            counts[record.ingestion_status] = (
                counts.get(record.ingestion_status, 0) + 1)
        return counts

    @property
    def content_by_source(self) -> dict[str, str]:
        return {item.source_id: item.text for item in self.contents}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "realworld_version": model.REALWORLD_VERSION,
            "kind": "ingestion_manifest",
            "manifest_id": self.manifest_id,
            "generated_at": self.generated_at,
            "canonical": self.canonical,
            "runtime_truth": self.runtime_truth,
            "source_count": self.source_count,
            "status_counts": self.status_counts(),
            "records": [record.to_dict() for record in self.records],
        }

    def render_markdown(self) -> str:
        lines = [
            "# Ingestion manifest", "",
            f"- manifest id: `{self.manifest_id}`",
            f"- generated: {self.generated_at}",
            f"- sources: {self.source_count}",
            f"- canonical runtime truth: {self.runtime_truth}",
            "- status: "
            + ", ".join(f"{key}={value}"
                        for key, value in self.status_counts().items()),
            "",
            "| source | origin | status | sensitivity | hash | location |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for record in self.records:
            lines.append(
                f"| `{record.source_id[:12]}` | {record.origin} | "
                f"{record.ingestion_status} | {record.sensitivity} | "
                f"`{record.content_hash[:12]}` | {record.location} |")
        limitations = [
            (record.source_id, limitation)
            for record in self.records
            for limitation in record.conversion_limitations]
        lines += ["", "## Conversion limitations", ""]
        if limitations:
            for source_id, limitation in limitations:
                lines.append(f"- `{source_id[:12]}`: {limitation}")
        else:
            lines.append("- none recorded")
        return "\n".join(lines) + "\n"


def _stable_name(value: str) -> str:
    cleaned = _SAFE_NAME.sub("_", value).strip("._-")
    return cleaned[:96] or "source"


def _utc_from_timestamp(value: float) -> str:
    return (datetime.fromtimestamp(value, UTC).replace(microsecond=0)
            .isoformat().replace("+00:00", "Z"))


def _source_id(origin: str, location: str, content_hash: str) -> str:
    return model.digest(
        {"origin": origin, "location": location, "content_hash": content_hash},
        domain=MANIFEST_DOMAIN)[:32]


def _manifest_id(records: Sequence[SourceRecord]) -> str:
    material = [
        {
            "source_id": record.source_id,
            "origin": record.origin,
            "location": record.location,
            "content_hash": record.content_hash,
            "project_id": record.project_id,
            "mission_id": record.mission_id,
            "sensitivity": record.sensitivity,
            "ingestion_status": record.ingestion_status,
            "duplicate_of": record.duplicate_of,
            "changed_from": record.changed_from,
            "conversion_limitations": list(record.conversion_limitations),
            "parse_status": record.parse_status,
        }
        for record in records
    ]
    return model.digest(material, domain=MANIFEST_DOMAIN)


def _classify_origin(path: Path) -> tuple[str, str | None]:
    """Return ``(origin, conversion_limitation)`` for one local path."""
    name = path.name.lower()
    suffix = path.suffix.lower()
    if name.endswith(".pdf.txt") or suffix in _PDF_TEXT_EXTENSIONS:
        return ORIGIN_PDF_TEXT, None
    if suffix in _RAW_PDF_EXTENSIONS:
        return ORIGIN_LOCAL_FILE, LIM_RAW_PDF + ";" + LIM_NO_OCR
    if suffix in _BINARY_SHEET_EXTENSIONS:
        return ORIGIN_LOCAL_FILE, LIM_BINARY_SPREADSHEET
    if suffix in _BINARY_DOC_EXTENSIONS:
        return ORIGIN_LOCAL_FILE, LIM_BINARY_DOCUMENT
    if suffix in _IMAGE_EXTENSIONS:
        return ORIGIN_LOCAL_FILE, LIM_IMAGE + ";" + LIM_NO_OCR
    if suffix in (".csv", ".tsv"):
        return ORIGIN_CSV, None
    if suffix in (".md", ".markdown"):
        return ORIGIN_MARKDOWN, None
    if suffix in _TEXT_EXTENSIONS:
        return ORIGIN_TEXT, None
    return ORIGIN_LOCAL_FILE, LIM_UNSUPPORTED_FORMAT


def _read_text(path: Path) -> tuple[str | None, str | None]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, f"READ_ERROR:{type(exc).__name__}"
    return text, None


def _sensitivity_for(path: Path, override: str | None) -> str:
    if override is not None:
        if override not in knowledge.SENSITIVITIES:
            model.intel_model.fail(model.intel_model.E_MALFORMED,
                                   "unknown sensitivity override")
        return override
    text = path.name.lower()
    if any(marker in text for marker in ("secret", "private", "credential",
                                         "confidential")):
        return knowledge.SENS_CONFIDENTIAL
    return knowledge.SENS_INTERNAL


@dataclass(frozen=True)
class RemoteSource:
    """A caller-supplied URL/research source (never fetched here)."""

    location: str
    text: str
    title: str = ""
    origin: str = ORIGIN_URL
    sensitivity: str = knowledge.SENS_INTERNAL
    timestamp: str | None = None

    def validate(self) -> RemoteSource:
        if self.origin not in ORIGINS:
            model.intel_model.fail(model.intel_model.E_MALFORMED,
                                   "unknown remote origin")
        if not self.location:
            model.intel_model.fail(model.intel_model.E_MALFORMED,
                                   "remote location required")
        return self


def _build_record(
    *, origin: str, location: str, title: str, content: str | None,
    project_id: str | None, mission_id: str | None, sensitivity: str,
    timestamp: str, modified_at: str | None,
    limitation: str | None, parse_status: str,
    prior_by_location: Mapping[str, SourceRecord],
    seen_hashes: Mapping[str, str],
) -> tuple[SourceRecord, str | None]:
    """Build one record and return it plus the content to persist (or None)."""
    if content is None:
        content_hash = model.digest({"location": location, "absent": True},
                                    domain=MANIFEST_DOMAIN)
        return SourceRecord(
            source_id=_source_id(origin, location, content_hash),
            origin=origin, location=location, title=title,
            content_hash=content_hash, project_id=project_id,
            mission_id=mission_id, sensitivity=sensitivity,
            ingestion_status=STATUS_FAILED, char_count=0,
            ingested_at=timestamp, modified_at=modified_at,
            conversion_limitations=(limitation or LIM_UNSUPPORTED_FORMAT,),
            parse_status=parse_status).validate(), None

    limitations: list[str] = []
    if limitation:
        limitations.extend(limitation.split(";"))
    if any(item in UNSUPPORTED_LIMITATIONS for item in limitations):
        content_hash = model.digest(
            {"location": location, "unsupported": sorted(limitations)},
            domain=MANIFEST_DOMAIN)
        return SourceRecord(
            source_id=_source_id(origin, location, content_hash),
            origin=origin, location=location, title=title,
            content_hash=content_hash, project_id=project_id,
            mission_id=mission_id, sensitivity=sensitivity,
            ingestion_status=STATUS_UNSUPPORTED, char_count=0,
            ingested_at=timestamp, modified_at=modified_at,
            conversion_limitations=tuple(dict.fromkeys(limitations)),
            parse_status="UNSUPPORTED").validate(), None
    if len(content) > MAX_CONTENT_CHARS:
        content = content[:MAX_CONTENT_CHARS]
        limitations.append(LIM_TRUNCATED)
    redacted = model.redact_secrets(content)
    if redacted != content:
        limitations.append(LIM_REDACTED)
    content_hash = knowledge.hash_content(content)
    source_id = _source_id(origin, location, content_hash)

    status = STATUS_INGESTED
    duplicate_of: str | None = None
    changed_from: str | None = None
    prior = prior_by_location.get(location)
    if content_hash in seen_hashes:
        status = STATUS_DUPLICATE
        duplicate_of = seen_hashes[content_hash]
    elif prior is not None and prior.content_hash != content_hash:
        status = STATUS_CHANGED
        changed_from = prior.content_hash

    persist_content = sensitivity != knowledge.SENS_RESTRICTED
    if not persist_content:
        limitations.append(LIM_RESTRICTED_NOT_PERSISTED)

    record = SourceRecord(
        source_id=source_id, origin=origin, location=location, title=title,
        content_hash=content_hash, project_id=project_id,
        mission_id=mission_id, sensitivity=sensitivity,
        ingestion_status=status, char_count=len(content),
        ingested_at=timestamp, modified_at=modified_at,
        duplicate_of=duplicate_of, changed_from=changed_from,
        conversion_limitations=tuple(dict.fromkeys(limitations)),
        parse_status=parse_status).validate()
    return record, redacted


def _collect_files(
    paths: Sequence[str | Path],
) -> tuple[list[Path], list[str]]:
    files: list[Path] = []
    folders: list[str] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            folders.append(str(path))
            for candidate in sorted(path.rglob("*")):
                if candidate.is_file():
                    files.append(candidate)
        elif path.is_file():
            files.append(path)
    # de-duplicate while preserving deterministic order
    unique: list[Path] = []
    seen: set[str] = set()
    for path in files:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique, folders


def ingest_paths(
    paths: Sequence[str | Path], *, root: str,
    project_id: str | None = None, mission_id: str | None = None,
    prior_manifest: IngestionManifest | None = None,
    generated_at: str = "",
    origin_overrides: Mapping[str, str] | None = None,
    sensitivity_overrides: Mapping[str, str] | None = None,
    persist_content: bool = True,
) -> IngestionManifest:
    """Ingest local files/folders into a deterministic manifest."""
    stamp = generated_at or model.utc_now()
    files, folders = _collect_files(paths)
    if len(files) > MAX_SOURCES:
        model.intel_model.fail(model.intel_model.E_MALFORMED,
                               "too many sources")

    prior_by_location: dict[str, SourceRecord] = {}
    if prior_manifest is not None:
        for record in prior_manifest.records:
            prior_by_location[record.location] = record
    seen_hashes: dict[str, str] = {}
    records: list[SourceRecord] = []
    contents: list[IngestedContent] = []
    overrides = dict(origin_overrides or {})
    sens_overrides = dict(sensitivity_overrides or {})

    for path in files:
        origin, limitation = _classify_origin(path)
        origin = overrides.get(str(path), origin)
        try:
            modified_at = _utc_from_timestamp(path.stat().st_mtime)
        except OSError:
            modified_at = None
        sensitivity = _sensitivity_for(path, sens_overrides.get(str(path)))
        text, read_error = _read_text(path)
        if read_error is not None:
            record, _ = _build_record(
                origin=origin, location=str(path), title=path.name,
                content=None, project_id=project_id, mission_id=mission_id,
                sensitivity=sensitivity, timestamp=stamp,
                modified_at=modified_at, limitation=read_error,
                parse_status=read_error, prior_by_location=prior_by_location,
                seen_hashes=seen_hashes)
            records.append(record)
            continue
        assert text is not None
        record, persisted = _build_record(
            origin=origin, location=str(path), title=path.name,
            content=text, project_id=project_id, mission_id=mission_id,
            sensitivity=sensitivity, timestamp=stamp,
            modified_at=modified_at, limitation=limitation,
            parse_status="OK", prior_by_location=prior_by_location,
            seen_hashes=seen_hashes)
        records.append(record)
        if record.is_usable:
            seen_hashes.setdefault(record.content_hash, record.source_id)
        if persisted is not None and record.is_usable:
            contents.append(IngestedContent(
                source_id=record.source_id, text=persisted,
                persisted=(record.sensitivity
                           != knowledge.SENS_RESTRICTED)))
        if prior_by_location.get(str(path)) is None:
            prior_by_location[str(path)] = record

    for folder in folders:
        records.append(SourceRecord(
            source_id=_source_id(ORIGIN_LOCAL_FOLDER, folder,
                                 knowledge.hash_content(folder)),
            origin=ORIGIN_LOCAL_FOLDER, location=folder, title=folder,
            content_hash=knowledge.hash_content(folder),
            project_id=project_id, mission_id=mission_id,
            sensitivity=knowledge.SENS_PUBLIC,
            ingestion_status=STATUS_INGESTED, char_count=0,
            ingested_at=stamp, modified_at=None,
            parse_status="OK").validate())

    manifest = IngestionManifest(
        manifest_id=_manifest_id(records), generated_at=stamp,
        records=tuple(records), contents=tuple(contents))
    _persist_manifest(root, manifest, persist_content=persist_content)
    return manifest


def ingest_remote(
    sources: Sequence[RemoteSource], *, root: str,
    project_id: str | None = None, mission_id: str | None = None,
    prior_manifest: IngestionManifest | None = None,
    generated_at: str = "",
    persist_content: bool = True,
) -> IngestionManifest:
    """Ingest caller-supplied URL/research text (no network fetch)."""
    stamp = generated_at or model.utc_now()
    prior_by_location: dict[str, SourceRecord] = {}
    if prior_manifest is not None:
        for record in prior_manifest.records:
            prior_by_location[record.location] = record
    seen_hashes: dict[str, str] = {}
    records: list[SourceRecord] = []
    contents: list[IngestedContent] = []
    for source in sources:
        source.validate()
        record, persisted = _build_record(
            origin=source.origin, location=source.location,
            title=source.title or source.location, content=source.text,
            project_id=project_id, mission_id=mission_id,
            sensitivity=source.sensitivity, timestamp=stamp,
            modified_at=source.timestamp,
            limitation=LIM_NETWORK_NOT_FETCHED,
            parse_status="OK", prior_by_location=prior_by_location,
            seen_hashes=seen_hashes)
        records.append(record)
        if record.is_usable:
            seen_hashes.setdefault(record.content_hash, record.source_id)
        if persisted is not None and record.is_usable:
            contents.append(IngestedContent(
                source_id=record.source_id, text=persisted,
                persisted=(record.sensitivity
                           != knowledge.SENS_RESTRICTED)))
    manifest = IngestionManifest(
        manifest_id=_manifest_id(records), generated_at=stamp,
        records=tuple(records), contents=tuple(contents))
    _persist_manifest(root, manifest, persist_content=persist_content)
    return manifest


def _persist_manifest(root: str, manifest: IngestionManifest, *,
                      persist_content: bool) -> None:
    base = Path(root) / "ingest"
    model.write_json(str(base / "manifest.json"), manifest.to_dict())
    (base / "manifest.md").parent.mkdir(parents=True, exist_ok=True)
    (base / "manifest.md").write_text(manifest.render_markdown(),
                                      encoding="utf-8")
    if not persist_content:
        return
    content_dir = base / "content"
    content_dir.mkdir(parents=True, exist_ok=True)
    for item in manifest.contents:
        if item.persisted:
            (content_dir / f"{_stable_name(item.source_id)}.txt").write_text(
                item.text, encoding="utf-8")


def load_manifest(root: str) -> IngestionManifest | None:
    """Load the last persisted manifest (read-only), or ``None``."""
    document = model.read_json(str(Path(root) / "ingest" / "manifest.json"))
    if document is None:
        return None
    records = tuple(
        SourceRecord(
            source_id=str(item.get("source_id", "")),
            origin=str(item.get("origin", "")),
            location=str(item.get("location", "")),
            title=str(item.get("title", "")),
            content_hash=str(item.get("content_hash", "")),
            project_id=(str(item["project_id"])
                        if item.get("project_id") is not None else None),
            mission_id=(str(item["mission_id"])
                        if item.get("mission_id") is not None else None),
            sensitivity=str(item.get("sensitivity", "")),
            ingestion_status=str(item.get("ingestion_status", "")),
            char_count=int(item.get("char_count", 0)),
            ingested_at=str(item.get("ingested_at", "")),
            modified_at=(str(item["modified_at"])
                         if item.get("modified_at") is not None else None),
            duplicate_of=(str(item["duplicate_of"])
                          if item.get("duplicate_of") is not None else None),
            changed_from=(str(item["changed_from"])
                          if item.get("changed_from") is not None else None),
            conversion_limitations=tuple(
                str(x) for x in item.get("conversion_limitations", [])),
            parse_status=str(item.get("parse_status", "OK")),
            canonical=bool(item.get("canonical", False)),
        ).validate()
        for item in document.get("records", [])
        if isinstance(item, Mapping))
    return IngestionManifest(
        manifest_id=str(document.get("manifest_id", "")),
        generated_at=str(document.get("generated_at", "")),
        records=records, contents=())


def to_knowledge_adapters(
    manifest: IngestionManifest,
) -> tuple[knowledge.SourceAdapter, ...]:
    """Bridge usable ingested sources into the M060 knowledge workspace."""
    content = manifest.content_by_source
    adapters: list[knowledge.SourceAdapter] = []
    for record in manifest.records:
        text = content.get(record.source_id)
        if text is None or not record.is_usable:
            continue
        kind = knowledge.SK_TEXT
        if record.origin == ORIGIN_PDF_TEXT:
            kind = knowledge.SK_PDF_TEXT
        elif record.origin == ORIGIN_LIFEOS_NOTE:
            kind = knowledge.SK_LIFEOS_NOTE
        elif record.origin in (ORIGIN_URL, ORIGIN_RESEARCH):
            kind = knowledge.SK_RESEARCH
        elif record.origin in (ORIGIN_CSV, ORIGIN_EXCEL_COMPATIBLE):
            kind = knowledge.SK_TEXT
        adapters.append(knowledge.TextSource(
            record.location, record.title, text, source_kind=kind,
            timestamp=record.ingested_at, sensitivity=record.sensitivity))
    return tuple(adapters)


def unsupported_records(
    manifest: IngestionManifest,
) -> tuple[SourceRecord, ...]:
    """Return every explicitly unsupported/failed record."""
    return tuple(record for record in manifest.records
                 if record.ingestion_status in (STATUS_UNSUPPORTED,
                                                STATUS_FAILED))


__all__ = [
    "INGESTION_STATUSES",
    "LIM_BINARY_DOCUMENT",
    "LIM_BINARY_SPREADSHEET",
    "LIM_IMAGE",
    "LIM_NETWORK_NOT_FETCHED",
    "LIM_NO_OCR",
    "LIM_RAW_PDF",
    "LIM_UNSUPPORTED_FORMAT",
    "MAX_CONTENT_CHARS",
    "ORIGINS",
    "ORIGIN_CSV",
    "ORIGIN_EXCEL_COMPATIBLE",
    "ORIGIN_LIFEOS_NOTE",
    "ORIGIN_LOCAL_FILE",
    "ORIGIN_LOCAL_FOLDER",
    "ORIGIN_MARKDOWN",
    "ORIGIN_PDF_TEXT",
    "ORIGIN_RESEARCH",
    "ORIGIN_TEXT",
    "ORIGIN_URL",
    "STATUS_CHANGED",
    "STATUS_DUPLICATE",
    "STATUS_FAILED",
    "STATUS_INGESTED",
    "STATUS_UNSUPPORTED",
    "IngestedContent",
    "IngestionManifest",
    "RemoteSource",
    "SourceRecord",
    "ingest_paths",
    "ingest_remote",
    "load_manifest",
    "to_knowledge_adapters",
    "unsupported_records",
]
