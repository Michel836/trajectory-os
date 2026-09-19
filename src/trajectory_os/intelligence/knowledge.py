"""M060 — provenance-first retrieval for non-code knowledge work.

The knowledge workspace indexes text, notes, PDF-derived text, prior
artifacts and research evidence behind small adapters, and retrieves chunks
with an explicit, auditable evidence chain:

    query -> ranked chunk -> document -> source identity -> content hash

Design invariants:

* **provenance-first** — every retrieved chunk exposes its document id,
  source identity, content hash, ordinal and retrieval score/rank;
* **stale/missing semantics** — an index is compared against freshly loaded
  sources and each source is ``FRESH`` / ``STALE`` / ``MISSING`` / ``NEW``;
* **contradictions are representable** — conflicting chunks are recorded as
  explicit contradiction records, never silently resolved;
* **RAG is context, never canonical truth** — a retrieval trace is labelled
  ``canonical=False`` and an empty result is ``NO_EVIDENCE``; model knowledge
  is never substituted for a missing source fact;
* **no vendor lock-in** — retrieval is deterministic BM25 over a plain,
  serialisable index; adapters are the only integration point;
* **sensitive boundaries** — every document carries a sensitivity label and
  retrieval can be restricted to an allowed set.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from trajectory_os.intelligence import model

#: Source kinds (closed, extensible set).
SK_TEXT = "TEXT"
SK_PDF_TEXT = "PDF_TEXT"
SK_PROJECT_NOTE = "PROJECT_NOTE"
SK_LIFEOS_NOTE = "LIFEOS_NOTE"
SK_ARTIFACT = "ARTIFACT"
SK_RESEARCH = "RESEARCH"
SK_EMAIL = "EMAIL"

SOURCE_KINDS = frozenset({
    SK_TEXT, SK_PDF_TEXT, SK_PROJECT_NOTE, SK_LIFEOS_NOTE, SK_ARTIFACT,
    SK_RESEARCH, SK_EMAIL,
})

#: Sensitivity labels (closed set, least to most restrictive).
SENS_PUBLIC = "PUBLIC"
SENS_INTERNAL = "INTERNAL"
SENS_CONFIDENTIAL = "CONFIDENTIAL"
SENS_RESTRICTED = "RESTRICTED"

SENSITIVITIES = (SENS_PUBLIC, SENS_INTERNAL, SENS_CONFIDENTIAL,
                 SENS_RESTRICTED)

#: Staleness statuses (closed set).
FRESH = "FRESH"
STALE = "STALE"
MISSING = "MISSING"
NEW = "NEW"

#: Retrieval outcome labels.
OK = "OK"
NO_EVIDENCE = "NO_EVIDENCE"

#: Contradiction kinds (closed set).
CT_NUMERIC = "NUMERIC_MISMATCH"
CT_DIRECT = "DIRECT_CONFLICT"
CT_TEMPORAL = "TEMPORAL_CONFLICT"
CT_SCOPE = "SCOPE_DIFFERENCE"

CONTRADICTION_KINDS = frozenset({CT_NUMERIC, CT_DIRECT, CT_TEMPORAL, CT_SCOPE})

#: Bounded limits.
MAX_DOCUMENTS = 10_000
MAX_CHUNKS = 200_000
MAX_CHUNK_CHARS = 1_200
MAX_CONTENT_CHARS = 2_000_000
MAX_QUERY_CHARS = 2_000
MAX_TOP_K = 50
MAX_CONTRADICTIONS = 10_000

_TOKEN_RE = re.compile(r"[a-z0-9_]+")


@dataclass(frozen=True)
class KnowledgeDocument:
    """One source document with explicit identity and provenance."""

    document_id: str
    source_kind: str
    source_ref: str
    title: str
    content: str
    content_hash: str
    timestamp: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)
    sensitivity: str = SENS_INTERNAL

    def validate(self) -> KnowledgeDocument:
        if self.source_kind not in SOURCE_KINDS:
            model.fail(model.E_MALFORMED,
                       f"unknown source_kind {self.source_kind!r}")
        if self.sensitivity not in SENSITIVITIES:
            model.fail(model.E_MALFORMED, "unknown sensitivity")
        if not self.document_id or not self.source_ref:
            model.fail(model.E_MALFORMED, "document identity required")
        if len(self.content) > MAX_CONTENT_CHARS:
            model.fail(model.E_MALFORMED, "content exceeds bound")
        if self.content_hash != hash_content(self.content):
            model.fail(model.E_IDENTITY_MISMATCH, self.document_id)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "source_kind": self.source_kind,
            "source_ref": self.source_ref,
            "title": self.title,
            "content_hash": self.content_hash,
            "timestamp": self.timestamp,
            "metadata": dict(sorted(self.metadata.items())),
            "sensitivity": self.sensitivity,
            "content_chars": len(self.content),
        }

    @staticmethod
    def build(*, source_kind: str, source_ref: str, title: str,
              content: str, timestamp: str | None = None,
              metadata: Mapping[str, str] | None = None,
              sensitivity: str = SENS_INTERNAL) -> KnowledgeDocument:
        document_id = model.digest(
            {"source_kind": source_kind, "source_ref": source_ref,
             "content_hash": hash_content(content)},
            domain=model.MATERIAL_DOMAIN)
        return KnowledgeDocument(
            document_id=document_id, source_kind=source_kind,
            source_ref=source_ref, title=title, content=content,
            content_hash=hash_content(content), timestamp=timestamp,
            metadata=dict(metadata or {}),
            sensitivity=sensitivity).validate()

    @staticmethod
    def from_dict(mapping: Mapping[str, Any],
                  content: str) -> KnowledgeDocument:
        return KnowledgeDocument(
            document_id=str(mapping.get("document_id", "")),
            source_kind=str(mapping.get("source_kind", "")),
            source_ref=str(mapping.get("source_ref", "")),
            title=str(mapping.get("title", "")),
            content=content,
            content_hash=str(mapping.get("content_hash", "")),
            timestamp=(str(mapping["timestamp"])
                       if mapping.get("timestamp") is not None else None),
            metadata={
                str(k): str(v) for k, v in model.require_mapping(
                    mapping.get("metadata", {}), "metadata").items()},
            sensitivity=str(mapping.get("sensitivity", SENS_INTERNAL)),
        ).validate()


def hash_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Chunk:
    """One indexed chunk with a stable identity and provenance."""

    chunk_id: str
    document_id: str
    ordinal: int
    text: str
    content_hash: str
    source_kind: str
    source_ref: str
    sensitivity: str
    timestamp: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "ordinal": self.ordinal,
            "content_hash": self.content_hash,
            "source_kind": self.source_kind,
            "source_ref": self.source_ref,
            "sensitivity": self.sensitivity,
            "timestamp": self.timestamp,
            "text_chars": len(self.text),
        }


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _chunk_text(text: str, *, maximum: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split into paragraph-ish chunks, never dropping content."""
    if not text:
        return [""]
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text)
                  if p.strip()]
    if not paragraphs:
        paragraphs = [text]
    chunks: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= maximum:
            chunks.append(paragraph)
            continue
        start = 0
        while start < len(paragraph):
            chunks.append(paragraph[start:start + maximum])
            start += maximum
    return chunks


def build_chunks(document: KnowledgeDocument) -> tuple[Chunk, ...]:
    chunks: list[Chunk] = []
    for ordinal, text in enumerate(_chunk_text(document.content)):
        chunk_hash = hash_content(text)
        chunk_id = model.digest(
            {"document_id": document.document_id, "ordinal": ordinal,
             "content_hash": chunk_hash, "source_ref": document.source_ref},
            domain=model.MATERIAL_DOMAIN)
        chunks.append(Chunk(
            chunk_id=chunk_id, document_id=document.document_id,
            ordinal=ordinal, text=text, content_hash=chunk_hash,
            source_kind=document.source_kind,
            source_ref=document.source_ref,
            sensitivity=document.sensitivity,
            timestamp=document.timestamp))
    return tuple(chunks)


@dataclass(frozen=True)
class KnowledgeIndex:
    """Serialisable BM25 index over deterministic chunks."""

    index_id: str
    documents: tuple[KnowledgeDocument, ...]
    chunks: tuple[Chunk, ...]
    #: Number of retrieval units (chunks) containing each term -- BM25's
    #: document frequency when the chunk is the retrieval unit. It is
    #: intentionally chunk-level, not deduplicated by source document.
    document_frequency: Mapping[str, int]
    chunk_lengths: Mapping[str, int]
    average_length: float
    generated_at: str

    @property
    def document_count(self) -> int:
        return len(self.documents)

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "kind": "knowledge_index",
            "index_id": self.index_id,
            "generated_at": self.generated_at,
            "document_count": self.document_count,
            "chunk_count": self.chunk_count,
            "average_length": self.average_length,
            "documents": [
                {**document.to_dict(), "content": document.content}
                for document in self.documents],
            "chunks": [
                {**chunk.to_dict(), "text": chunk.text}
                for chunk in self.chunks],
            "document_frequency": dict(sorted(self.document_frequency.items())),
            "chunk_lengths": dict(sorted(self.chunk_lengths.items())),
        }

    @staticmethod
    def from_dict(mapping: Mapping[str, Any]) -> KnowledgeIndex:
        model.check_version(mapping, "knowledge index")
        documents: list[KnowledgeDocument] = []
        for item in model.require_list(
                mapping.get("documents", []), "documents"):
            document_mapping = model.require_mapping(item, "document")
            documents.append(KnowledgeDocument.from_dict(
                document_mapping, str(document_mapping.get("content", ""))))
        chunks = tuple(_chunk_from_dict(model.require_mapping(item, "chunk"),
                                        str(model.require_mapping(
                                            item, "chunk").get("text", "")))
                       for item in model.require_list(
                           mapping.get("chunks", []), "chunks"))
        document_frequency = {
            str(k): int(v) for k, v in model.require_mapping(
                mapping.get("document_frequency", {}),
                "document_frequency").items()}
        chunk_lengths = {
            str(k): int(v) for k, v in model.require_mapping(
                mapping.get("chunk_lengths", {}), "chunk_lengths").items()}
        index = KnowledgeIndex(
            index_id=str(mapping.get("index_id", "")),
            documents=tuple(documents), chunks=chunks,
            document_frequency=document_frequency,
            chunk_lengths=chunk_lengths,
            average_length=float(mapping.get("average_length", 0.0)),
            generated_at=str(mapping.get("generated_at", "")))
        expected = index.compute_id()
        stored = mapping.get("index_id")
        if stored is not None and stored != expected:
            model.fail(model.E_IDENTITY_MISMATCH, str(stored))
        return index

    def compute_id(self) -> str:
        return model.digest(
            {"chunks": [chunk.chunk_id for chunk in self.chunks]},
            domain=model.MATERIAL_DOMAIN)


def _chunk_from_dict(mapping: Mapping[str, Any], text: str) -> Chunk:
    return Chunk(
        chunk_id=str(mapping.get("chunk_id", "")),
        document_id=str(mapping.get("document_id", "")),
        ordinal=int(mapping.get("ordinal", 0)),
        text=text,
        content_hash=str(mapping.get("content_hash", "")),
        source_kind=str(mapping.get("source_kind", "")),
        source_ref=str(mapping.get("source_ref", "")),
        sensitivity=str(mapping.get("sensitivity", SENS_INTERNAL)),
        timestamp=(str(mapping["timestamp"])
                   if mapping.get("timestamp") is not None else None),
    )


class SourceAdapter(Protocol):
    """Extensible adapter: produce documents from any external source."""

    def load(self) -> Sequence[KnowledgeDocument]: ...


@dataclass
class TextSource:
    """Adapter for an in-memory text/note/PDF-derived source."""

    source_ref: str
    title: str
    content: str
    source_kind: str = SK_TEXT
    timestamp: str | None = None
    sensitivity: str = SENS_INTERNAL
    metadata: Mapping[str, str] = field(default_factory=dict)

    def load(self) -> Sequence[KnowledgeDocument]:
        return (KnowledgeDocument.build(
            source_kind=self.source_kind, source_ref=self.source_ref,
            title=self.title, content=self.content, timestamp=self.timestamp,
            metadata=self.metadata, sensitivity=self.sensitivity),)


@dataclass
class FileSource:
    """Adapter that reads a bounded text file (never a directory or binary)."""

    path: str
    source_kind: str = SK_TEXT
    title: str | None = None
    sensitivity: str = SENS_INTERNAL
    maximum_bytes: int = MAX_CONTENT_CHARS

    def load(self) -> Sequence[KnowledgeDocument]:
        target = Path(self.path)
        if not target.is_file():
            model.fail(model.E_MALFORMED, f"source missing: {self.path}")
        raw = target.read_bytes()[: self.maximum_bytes]
        content = raw.decode("utf-8", errors="replace")
        return (KnowledgeDocument.build(
            source_kind=self.source_kind, source_ref=self.path,
            title=self.title or target.name, content=content,
            sensitivity=self.sensitivity),)


def build_index(
    adapters: Iterable[SourceAdapter], *, generated_at: str = "",
    clock: Callable[[], str] | None = None,
) -> KnowledgeIndex:
    """Build a deterministic index from any collection of adapters."""
    documents: list[KnowledgeDocument] = []
    chunks: list[Chunk] = []
    for adapter in adapters:
        for document in adapter.load():
            if document.document_id in {d.document_id for d in documents}:
                continue
            documents.append(document.validate())
            if len(documents) > MAX_DOCUMENTS:
                model.fail(model.E_MALFORMED, "document limit exceeded")
            chunks.extend(build_chunks(document))
            if len(chunks) > MAX_CHUNKS:
                model.fail(model.E_MALFORMED, "chunk limit exceeded")
    document_frequency: dict[str, int] = {}
    chunk_lengths: dict[str, int] = {}
    for chunk in chunks:
        tokens = _tokenize(chunk.text)
        chunk_lengths[chunk.chunk_id] = len(tokens)
        for token in set(tokens):
            document_frequency[token] = document_frequency.get(token, 0) + 1
    average = (sum(chunk_lengths.values()) / len(chunk_lengths)
               if chunk_lengths else 0.0)
    stamp = generated_at or (clock() if clock is not None else model.utc_now())
    index = KnowledgeIndex(
        index_id="", documents=tuple(documents), chunks=tuple(chunks),
        document_frequency=document_frequency, chunk_lengths=chunk_lengths,
        average_length=average, generated_at=stamp)
    return _with_id(index)


def _with_id(index: KnowledgeIndex) -> KnowledgeIndex:
    from dataclasses import replace

    return replace(index, index_id=index.compute_id())


#: BM25 parameters (explicit, documented).
BM25_K1 = 1.5
BM25_B = 0.75


@dataclass(frozen=True)
class RetrievedChunk:
    chunk: Chunk
    score: float
    rank: int
    matched_terms: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk": self.chunk.to_dict(),
            "score": round(self.score, 6),
            "rank": self.rank,
            "matched_terms": list(self.matched_terms),
            "citation": {
                "chunk_id": self.chunk.chunk_id,
                "document_id": self.chunk.document_id,
                "source_kind": self.chunk.source_kind,
                "source_ref": self.chunk.source_ref,
                "content_hash": self.chunk.content_hash,
                "ordinal": self.chunk.ordinal,
            },
        }


@dataclass(frozen=True)
class RetrievalTrace:
    query: str
    status: str
    results: tuple[RetrievedChunk, ...]
    allowed_sensitivities: tuple[str, ...]
    index_id: str
    canonical: bool
    caveat: str
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "kind": "retrieval_trace",
            "query": self.query,
            "status": self.status,
            "results": [r.to_dict() for r in self.results],
            "allowed_sensitivities": list(self.allowed_sensitivities),
            "index_id": self.index_id,
            "canonical": self.canonical,
            "caveat": self.caveat,
            "generated_at": self.generated_at,
        }

    def render_markdown(self) -> str:
        lines = [f"# Retrieval: {self.query}", "",
                 f"- status: {self.status}",
                 f"- canonical: {self.canonical}",
                 f"- caveat: {self.caveat}", ""]
        for result in self.results:
            source = result.chunk.source_ref
            lines.append(
                f"{result.rank}. `{source}` "
                f"(score {result.score:.3f}) — "
                f"{result.chunk.text[:160].strip()}")
        return "\n".join(lines) + "\n"


def retrieve(
    index: KnowledgeIndex, query: str, *,
    top_k: int = 5,
    allowed_sensitivities: Sequence[str] = SENSITIVITIES,
    generated_at: str = "",
    clock: Callable[[], str] | None = None,
) -> RetrievalTrace:
    """Deterministic BM25 retrieval restricted to allowed sensitivities."""
    if len(query) > MAX_QUERY_CHARS:
        model.fail(model.E_MALFORMED, "query exceeds bound")
    top_k = max(1, min(top_k, MAX_TOP_K))
    allowed = tuple(allowed_sensitivities)
    for label in allowed:
        if label not in SENSITIVITIES:
            model.fail(model.E_MALFORMED, f"unknown sensitivity {label!r}")
    tokens = _tokenize(query)
    query_terms = set(tokens)
    total = index.chunk_count
    scored: list[RetrievedChunk] = []
    for chunk in index.chunks:
        if chunk.sensitivity not in allowed:
            continue
        chunk_tokens = _tokenize(chunk.text)
        length = len(chunk_tokens) or 1
        frequencies: dict[str, int] = {}
        for token in chunk_tokens:
            frequencies[token] = frequencies.get(token, 0) + 1
        score = 0.0
        matched: list[str] = []
        for term in query_terms:
            frequency = frequencies.get(term, 0)
            if frequency == 0:
                continue
            matched.append(term)
            df = index.document_frequency.get(term, 0)
            idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
            denominator = frequency + BM25_K1 * (
                1 - BM25_B + BM25_B * length
                / (index.average_length or 1.0))
            score += idf * (frequency * (BM25_K1 + 1)) / denominator
        if score > 0:
            scored.append(RetrievedChunk(
                chunk=chunk, score=score, rank=0,
                matched_terms=tuple(sorted(matched))))
    scored.sort(key=lambda item: (-item.score, item.chunk.chunk_id))
    ranked = tuple(
        RetrievedChunk(chunk=item.chunk, score=item.score, rank=position + 1,
                       matched_terms=item.matched_terms)
        for position, item in enumerate(scored[:top_k]))
    stamp = generated_at or (clock() if clock is not None else model.utc_now())
    status = OK if ranked else NO_EVIDENCE
    caveat = (
        "retrieved context only; not canonical runtime truth and not a "
        "substitute for a missing source fact"
        if ranked else
        "no source evidence matched the query; no model knowledge may be "
        "substituted for a source fact")
    return RetrievalTrace(
        query=query, status=status, results=ranked,
        allowed_sensitivities=allowed, index_id=index.index_id,
        canonical=False, caveat=caveat, generated_at=stamp)


# --- staleness ----------------------------------------------------------------


@dataclass(frozen=True)
class SourceStatus:
    source_ref: str
    status: str
    indexed_hash: str | None
    live_hash: str | None
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"source_ref": self.source_ref, "status": self.status,
                "indexed_hash": self.indexed_hash, "live_hash": self.live_hash,
                "detail": self.detail}


def assess_staleness(
    index: KnowledgeIndex,
    live_adapters: Iterable[SourceAdapter],
) -> tuple[SourceStatus, ...]:
    """Compare indexed sources against freshly loaded live sources."""
    indexed: dict[str, str] = {
        document.source_ref: document.content_hash
        for document in index.documents}
    live: dict[str, str] = {}
    for adapter in live_adapters:
        for document in adapter.load():
            live[document.source_ref] = document.content_hash
    statuses: list[SourceStatus] = []
    for source_ref, indexed_hash in sorted(indexed.items()):
        live_hash = live.get(source_ref)
        if live_hash is None:
            statuses.append(SourceStatus(
                source_ref, MISSING, indexed_hash, None,
                "source no longer present in the live adapter set"))
        elif live_hash == indexed_hash:
            statuses.append(SourceStatus(
                source_ref, FRESH, indexed_hash, live_hash,
                "indexed content matches live source"))
        else:
            statuses.append(SourceStatus(
                source_ref, STALE, indexed_hash, live_hash,
                "live source content differs from the indexed hash"))
    for source_ref, live_hash in sorted(live.items()):
        if source_ref not in indexed:
            statuses.append(SourceStatus(
                source_ref, NEW, None, live_hash,
                "live source not present in the index"))
    return tuple(statuses)


# --- contradictions -----------------------------------------------------------


@dataclass(frozen=True)
class Contradiction:
    contradiction_id: str
    kind: str
    left_chunk_id: str
    right_chunk_id: str
    description: str
    resolution: str

    def validate(self) -> Contradiction:
        if self.kind not in CONTRADICTION_KINDS:
            model.fail(model.E_MALFORMED, f"unknown kind {self.kind!r}")
        if not self.left_chunk_id or not self.right_chunk_id:
            model.fail(model.E_MALFORMED, "both chunk ids required")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "contradiction_id": self.contradiction_id,
            "kind": self.kind,
            "left_chunk_id": self.left_chunk_id,
            "right_chunk_id": self.right_chunk_id,
            "description": self.description,
            "resolution": self.resolution,
        }


def record_contradiction(
    index: KnowledgeIndex, *, left_chunk_id: str, right_chunk_id: str,
    kind: str = CT_DIRECT, description: str = "",
    resolution: str = "UNRESOLVED_REQUIRES_HUMAN_REVIEW",
) -> Contradiction:
    """Represent a contradiction explicitly (never silently resolved)."""
    known = {chunk.chunk_id for chunk in index.chunks}
    if left_chunk_id not in known or right_chunk_id not in known:
        model.fail(model.E_MALFORMED, "contradiction references unknown chunk")
    contradiction_id = model.digest(
        {"left": left_chunk_id, "right": right_chunk_id, "kind": kind},
        domain=model.MATERIAL_DOMAIN)
    return Contradiction(
        contradiction_id=contradiction_id, kind=kind,
        left_chunk_id=left_chunk_id, right_chunk_id=right_chunk_id,
        description=description, resolution=resolution).validate()


# --- persistence --------------------------------------------------------------


def index_path(root: str) -> str:
    return f"{root}/knowledge/index.json"


def persist_index(root: str, index: KnowledgeIndex) -> str:
    model.write_json(index_path(root), index.to_dict())
    return index_path(root)


def load_index(root: str) -> KnowledgeIndex | None:
    document = model.read_json(index_path(root))
    if document is None:
        return None
    return KnowledgeIndex.from_dict(document)


def persist_trace(root: str, trace: RetrievalTrace) -> str:
    stamp = trace.generated_at.replace(":", "").replace("-", "")
    path = f"{root}/knowledge/traces/{stamp}-{trace.index_id[:12]}.json"
    model.write_json(path, trace.to_dict())
    return path


__all__ = [
    "BM25_B",
    "BM25_K1",
    "CT_DIRECT",
    "CT_NUMERIC",
    "CT_SCOPE",
    "CT_TEMPORAL",
    "Chunk",
    "Contradiction",
    "FileSource",
    "FRESH",
    "KnowledgeDocument",
    "KnowledgeIndex",
    "MISSING",
    "NEW",
    "NO_EVIDENCE",
    "OK",
    "RetrievalTrace",
    "RetrievedChunk",
    "SENSITIVITIES",
    "SENS_CONFIDENTIAL",
    "SENS_INTERNAL",
    "SENS_PUBLIC",
    "SENS_RESTRICTED",
    "SK_ARTIFACT",
    "SK_EMAIL",
    "SK_LIFEOS_NOTE",
    "SK_PDF_TEXT",
    "SK_PROJECT_NOTE",
    "SK_RESEARCH",
    "SK_TEXT",
    "SOURCE_KINDS",
    "STALE",
    "SourceAdapter",
    "SourceStatus",
    "TextSource",
    "assess_staleness",
    "build_chunks",
    "build_index",
    "hash_content",
    "index_path",
    "load_index",
    "persist_index",
    "persist_trace",
    "record_contradiction",
    "retrieve",
]
