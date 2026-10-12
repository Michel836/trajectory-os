"""MVP — derived, deterministic search memory built from import artifacts.

The V0 Trajectory Mirror persists the *source* vocabulary of an import in the
artifact under ``<root>/imports/*.json`` (``candidates[*].title``,
``.description`` and ``.source_text``), while the operational Project/Task
keeps only a short provenance note. This module bridges the two without
polluting the business model or duplicating the import memory into
``portfolio.json``: it reads the artifacts and returns a *derived* index
(entity -> imported vocabulary) that the visualization layer attaches to its
normalised objects. Global search then stays a pure presentation filter.

Design constraints:

* read-only, deterministic and dependency-free;
* missing / unreadable / malformed / incomplete / stale artifacts are ignored
  and never break the dashboard;
* only real operational entities are enriched: ``skipped`` and ``area`` are
  never turned into fake attachments, and ``merged:<project_id>`` enriches the
  target project;
* the persisted import artifact remains the single source of truth for the
  imported vocabulary.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trajectory_os.intelligence import model as intel_model

#: Defensive bounds so a huge or hostile import history cannot bloat a payload.
MAX_ARTIFACTS = 500
MAX_VOCAB_CHARS = 20000
MAX_SOURCES = 50

#: ``entity_map`` sentinels produced by ``importer.apply_decisions``.
SKIPPED = "skipped"
AREA = "area"
MERGED_PREFIX = "merged:"

PROJECT = "project"
TASK = "task"


@dataclass(frozen=True)
class EntityMemory:
    """Derived imported vocabulary attached to one operational entity."""

    text: str
    sources: tuple[str, ...]


@dataclass(frozen=True)
class ImportSearchIndex:
    """Entity-keyed derived search memory (empty when no artifact applies)."""

    entries: Mapping[tuple[str, str], EntityMemory]

    def __bool__(self) -> bool:
        return bool(self.entries)

    def get(self, kind: str, ref_id: str) -> EntityMemory | None:
        return self.entries.get((kind, ref_id))


def build_index(
    root: str | Path,
    *,
    project_ids: Iterable[str],
    task_ids: Iterable[str],
) -> ImportSearchIndex:
    """Read ``<root>/imports/*.json`` and derive the search memory.

    The function never raises for a bad artifact: anything that cannot be read
    or interpreted is skipped so search keeps working with the data available.
    """
    directory = Path(root) / "imports"
    projects = {str(p) for p in project_ids}
    tasks = {str(t) for t in task_ids}
    try:
        paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
    except OSError:
        return ImportSearchIndex({})
    vocab: dict[tuple[str, str], list[str]] = {}
    sources: dict[tuple[str, str], list[str]] = {}
    for path in paths[:MAX_ARTIFACTS]:
        data = intel_model.read_json(path)
        if not isinstance(data, Mapping) or data.get("kind") != "mvp_import":
            continue
        candidates = _candidate_text(data.get("candidates"))
        entity_map = data.get("entity_map")
        if not candidates or not isinstance(entity_map, Mapping):
            continue
        source_name = _clean(data.get("source_name")) or path.stem
        for candidate_id, raw in entity_map.items():
            key = _resolve(raw, projects, tasks)
            if key is None:
                continue
            text = candidates.get(_clean(candidate_id))
            if not text:
                continue
            vocab.setdefault(key, []).append(text)
            bucket = sources.setdefault(key, [])
            if source_name not in bucket:
                bucket.append(source_name)
    entries: dict[tuple[str, str], EntityMemory] = {}
    for key, chunks in vocab.items():
        text = _join(chunks)
        if not text:
            continue
        entries[key] = EntityMemory(
            text=text,
            sources=tuple(sorted(sources.get(key, ()))[:MAX_SOURCES]),
        )
    return ImportSearchIndex(entries)


def _candidate_text(raw: Any) -> dict[str, str]:
    """Map ``candidate_id -> searchable imported vocabulary`` (defensively)."""
    out: dict[str, str] = {}
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        candidate_id = _clean(item.get("candidate_id"))
        if not candidate_id:
            continue
        out[candidate_id] = _join((
            _clean(item.get("title")),
            _clean(item.get("description")),
            _clean(item.get("source_text")),
        ))
    return out


def _resolve(
    raw: Any,
    project_ids: set[str],
    task_ids: set[str],
) -> tuple[str, str] | None:
    """Resolve an ``entity_map`` value to a real ``(kind, ref_id)`` or ``None``."""
    value = _clean(raw)
    if not value or value in (SKIPPED, AREA):
        return None
    if value.startswith(MERGED_PREFIX):
        target = value[len(MERGED_PREFIX):]
        if target in project_ids:
            return (PROJECT, target)
        return None
    if value in project_ids:
        return (PROJECT, value)
    if value in task_ids:
        return (TASK, value)
    return None


def _join(parts: Iterable[str]) -> str:
    """Join distinct non-empty fragments, bounded by ``MAX_VOCAB_CHARS``."""
    seen: set[str] = set()
    kept: list[str] = []
    total = 0
    for part in parts:
        part = str(part).strip()
        if not part or part in seen:
            continue
        seen.add(part)
        total += len(part) + 1
        if total > MAX_VOCAB_CHARS:
            break
        kept.append(part)
    return " ".join(kept)


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


__all__ = [
    "AREA", "MERGED_PREFIX", "PROJECT", "SKIPPED", "TASK",
    "EntityMemory", "ImportSearchIndex", "build_index",
]
