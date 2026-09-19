"""M025 — durable workspace/artifact store (atomic, strict, leak-proof).

Persistence layout (all under ``<root>``)::

    workspaces/<goal_id>/.workspace.json                 per-goal workspace
    workspaces/<goal_id>/missions/<mission_id>/.workspace.json
    workspaces/<goal_id>/missions/<mission_id>/artifacts/<name>   content
    artifacts/<goal_id>/records/<artifact_id>.json       artifact provenance
    artifacts/<goal_id>/index.json                       ordered id index

Rules:

* every JSON write is atomic (temp file + fsync + rename);
* every read is strict: unsupported schema, malformed records and identity
  mismatches fail closed — data is never guessed or repaired;
* artifacts are *namespaced by goal*: a record stored under one goal is never
  implicitly visible to another goal (``resolve_record`` rejects any request
  that is not the owning goal unless an explicit ``shared_with`` grant
  exists), so implicit cross-goal leakage is structurally impossible;
* lineage reconstruction is bounded and fail-closed on missing parents,
  cross-goal parents or cycles.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from trajectory_os.artifacts import model
from trajectory_os.runs.store import atomic_write_json

#: Chunk size for streaming content hashing (1 MiB).
_HASH_CHUNK = 1024 * 1024


def goal_workspace_path(root: str | Path, goal_id: str) -> Path:
    return Path(root) / "workspaces" / goal_id


def mission_workspace_path(root: str | Path, goal_id: str,
                           mission_id: str) -> Path:
    return goal_workspace_path(root, goal_id) / "missions" / mission_id


def workspace_marker(path: Path) -> Path:
    return path / ".workspace.json"


def artifact_content_dir(root: str | Path, goal_id: str,
                         mission_id: str | None) -> Path:
    if mission_id is not None:
        return mission_workspace_path(root, goal_id, mission_id) / "artifacts"
    return goal_workspace_path(root, goal_id) / "artifacts"


def artifact_records_dir(root: str | Path, goal_id: str) -> Path:
    return Path(root) / "artifacts" / goal_id / "records"


def artifact_record_path(root: str | Path, goal_id: str,
                         artifact_id: str) -> Path:
    return artifact_records_dir(root, goal_id) / f"{artifact_id}.json"


def artifact_index_path(root: str | Path, goal_id: str) -> Path:
    return Path(root) / "artifacts" / goal_id / "index.json"


# --- hashing ------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, *, max_bytes: int | None = None) -> tuple[str, int]:
    """Stream-hash a file; return ``(sha256, size_bytes)``."""
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if max_bytes is not None and size > max_bytes:
                raise model.ArtifactError(
                    model.E_MALFORMED,
                    f"artifact exceeds {max_bytes} bytes")
            hasher.update(chunk)
    return hasher.hexdigest(), size


# --- workspaces ---------------------------------------------------------------


def ensure_workspace(record: model.WorkspaceRecord) -> Path:
    """Create (idempotently) the workspace directory + provenance marker."""
    path = Path(record.path)
    path.mkdir(parents=True, exist_ok=True)
    marker = workspace_marker(path)
    if marker.is_file():
        existing = load_workspace(marker)
        if existing.workspace_id != record.workspace_id:
            raise model.ArtifactError(
                model.E_IDENTITY_MISMATCH,
                f"workspace marker mismatch at {marker}")
        return path
    atomic_write_json(marker, record.to_dict())
    return path


def load_workspace(marker: Path) -> model.WorkspaceRecord:
    try:
        raw = marker.read_text(encoding="utf-8")
        doc = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise model.ArtifactError(model.E_MALFORMED, str(exc)) from exc
    return model.WorkspaceRecord.from_dict(doc)


# --- artifact records ---------------------------------------------------------


def _load_index(root: str | Path, goal_id: str) -> list[str]:
    path = artifact_index_path(root, goal_id)
    if not path.is_file():
        return []
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise model.ArtifactError(model.E_MALFORMED, str(exc)) from exc
    if not isinstance(doc, dict) \
            or doc.get("schema_version") != model.SCHEMA_VERSION:
        raise model.ArtifactError(model.E_UNSUPPORTED_VERSION, "artifact index")
    ids = doc.get("artifact_ids")
    if not isinstance(ids, list) or any(
            not isinstance(item, str) for item in ids):
        raise model.ArtifactError(model.E_MALFORMED, "artifact index ids")
    return sorted(set(ids))


def _write_index(root: str | Path, goal_id: str, ids: list[str]) -> None:
    atomic_write_json(artifact_index_path(root, goal_id), {
        "schema_version": model.SCHEMA_VERSION,
        "artifact_ids": sorted(set(ids)),
    })


def write_artifact(root: str | Path, record: model.ArtifactRecord) -> Path:
    """Atomically persist an artifact record and update the goal index.

    Idempotent for an identical record; a conflicting record with the same id
    (impossible unless content/provenance changed) fails closed.
    """
    path = artifact_record_path(root, record.goal_id, record.artifact_id)
    if path.is_file():
        existing = load_artifact(root, record.goal_id, record.artifact_id)
        if existing.identity_payload() != record.identity_payload():
            raise model.ArtifactError(
                model.E_IDENTITY_MISMATCH,
                f"artifact {record.artifact_id} already exists")
        return path
    atomic_write_json(path, record.to_dict())
    ids = _load_index(root, record.goal_id)
    ids.append(record.artifact_id)
    _write_index(root, record.goal_id, ids)
    return path


def load_artifact(root: str | Path, goal_id: str,
                  artifact_id: str) -> model.ArtifactRecord:
    """Strictly load one artifact from the requesting goal's namespace."""
    path = artifact_record_path(root, goal_id, artifact_id)
    if not path.is_file():
        raise model.ArtifactError(
            model.E_NOT_FOUND, f"{goal_id}/{artifact_id}")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise model.ArtifactError(model.E_MALFORMED, str(exc)) from exc
    record = model.ArtifactRecord.from_dict(doc)
    if record.goal_id != goal_id:
        raise model.ArtifactError(
            model.E_CROSS_GOAL_LEAK,
            f"artifact {artifact_id} belongs to {record.goal_id!r}, "
            f"requested by {goal_id!r}")
    return record


def list_artifacts(root: str | Path, goal_id: str) -> list[model.ArtifactRecord]:
    return [load_artifact(root, goal_id, artifact_id)
            for artifact_id in _load_index(root, goal_id)]


def resolve_record(
    requesting_goal: str,
    record: model.ArtifactRecord,
) -> model.ArtifactRecord:
    """Enforce explicit sharing when crossing a goal boundary (fail closed)."""
    if record.goal_id == requesting_goal:
        return record
    if requesting_goal in record.shared_with:
        return record
    raise model.ArtifactError(
        model.E_CROSS_GOAL_LEAK,
        f"artifact {record.artifact_id} of goal {record.goal_id!r} is not "
        f"shared with {requesting_goal!r}")


def lineage(
    root: str | Path,
    goal_id: str,
    artifact_id: str,
    *,
    maximum: int = model.MAX_PARENTS * model.MAX_PARENTS,
) -> tuple[model.ArtifactRecord, ...]:
    """Reconstruct an artifact's lineage (queried artifact first, safe).

    The order is deterministic: the queried artifact first, then its direct
    parents, then their ancestors. Fails closed on a missing parent, an
    unshared cross-goal parent, a cycle or an unbounded graph.
    """
    ordered: list[model.ArtifactRecord] = []
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[tuple[str, bool]] = [(artifact_id, False)]
    steps = 0

    while stack:
        current_id, expanded = stack.pop()
        steps += 1
        if steps > maximum:
            raise model.ArtifactError(model.E_MALFORMED, "lineage too large")
        if expanded:
            visiting.discard(current_id)
            visited.add(current_id)
            continue
        if current_id in visited:
            continue
        if current_id in visiting:
            raise model.ArtifactError(
                model.E_LINEAGE_CYCLE, f"cycle at {current_id}")
        record = load_artifact(root, goal_id, current_id)
        resolve_record(goal_id, record)
        visiting.add(current_id)
        ordered.append(record)
        stack.append((current_id, True))
        # Parents first: push in reverse so deterministic order is preserved.
        for parent_id in reversed(record.parent_ids):
            if parent_id not in visited:
                stack.append((parent_id, False))
    return tuple(ordered)


def validate_parents(
    root: str | Path,
    goal_id: str,
    parent_ids: tuple[str, ...],
) -> None:
    """Every parent must exist and be visible to the requesting goal."""
    for parent_id in parent_ids:
        parent = load_artifact(root, goal_id, parent_id)
        try:
            resolve_record(goal_id, parent)
        except model.ArtifactError as exc:
            raise model.ArtifactError(
                model.E_CROSS_GOAL_LEAK_PARENT,
                f"parent {parent_id}: {exc.detail}") from exc
