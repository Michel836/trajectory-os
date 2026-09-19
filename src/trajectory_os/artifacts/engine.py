"""M025 — persistent workspace materialization and artifact provenance.

:class:`WorkspaceManager` gives every goal and mission a durable workspace and
turns generated/imported files into explicit, content-addressed artifacts
with lineage. It refuses implicit cross-goal leakage: parents must be visible
to the owning goal and content is namespaced under the goal workspace.

The manager performs no Git operation and writes only inside the product-owned
state root.
"""

from __future__ import annotations

import contextlib
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trajectory_os.artifacts import identity, model, store

#: Content sub-directory layout by kind (deterministic, human-friendly).
KIND_SUBDIR: dict[str, str] = {
    model.AK_FILE: "files",
    model.AK_REPORT: "reports",
    model.AK_DATASET: "datasets",
    model.AK_MODEL: "models",
    model.AK_INTERMEDIATE: "intermediate",
}


def utc_now_iso() -> str:
    return (datetime.now(UTC).replace(microsecond=0).isoformat() + "Z")


@dataclass(frozen=True)
class WorkspaceState:
    """One reconstructed, read-only goal workspace state."""

    goal_id: str
    goal_workspace: model.WorkspaceRecord
    mission_workspaces: tuple[model.WorkspaceRecord, ...]
    artifacts: tuple[model.ArtifactRecord, ...]
    lineage_id: str = ""

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "goal_id": self.goal_id,
            "artifact_ids": [a.artifact_id for a in self.artifacts],
            "mission_workspace_ids": [
                w.workspace_id for w in self.mission_workspaces],
        }

    def compute_lineage_id(self) -> str:
        return identity.lineage_id(self.identity_payload())

    def to_dict(self) -> dict[str, object]:
        return {
            **self.identity_payload(),
            "goal_workspace": self.goal_workspace.to_dict(),
            "mission_workspaces": [
                w.to_dict() for w in self.mission_workspaces],
            "artifacts": [a.to_dict() for a in self.artifacts],
            "lineage_id": self.lineage_id,
        }


class WorkspaceManager:
    """Operational manager for persistent workspaces and artifacts."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    # -- workspaces ------------------------------------------------------------

    def ensure_goal_workspace(
        self, goal_id: str, *, created_at: str | None = None,
    ) -> model.WorkspaceRecord:
        path = store.goal_workspace_path(self._root, goal_id)
        record = model.WorkspaceRecord.build(
            goal_id=goal_id, mission_id=None, path=str(path),
            created_at=created_at or utc_now_iso(), kind="goal")
        store.ensure_workspace(record)
        return record

    def ensure_mission_workspace(
        self, goal_id: str, mission_id: str, *,
        created_at: str | None = None,
    ) -> model.WorkspaceRecord:
        self.ensure_goal_workspace(goal_id, created_at=created_at)
        path = store.mission_workspace_path(self._root, goal_id, mission_id)
        record = model.WorkspaceRecord.build(
            goal_id=goal_id, mission_id=mission_id, path=str(path),
            created_at=created_at or utc_now_iso(), kind="mission")
        store.ensure_workspace(record)
        return record

    # -- artifacts -------------------------------------------------------------

    def record_artifact(
        self,
        *,
        goal_id: str,
        kind: str,
        name: str,
        producer: str = model.PRODUCER_RUNNER,
        content: bytes | None = None,
        source_path: str | Path | None = None,
        mission_id: str | None = None,
        phase_id: str | None = None,
        subrun_id: str | None = None,
        parent_ids: tuple[str, ...] = (),
        shared_with: tuple[str, ...] = (),
        reuse_input_id: str | None = None,
        proof_id: str | None = None,
        criterion_id: str | None = None,
        created_at: str | None = None,
    ) -> model.ArtifactRecord:
        """Materialize one artifact and persist its provenance + lineage."""
        self._validate_name(name)
        if content is not None and source_path is not None:
            raise model.ArtifactError(
                model.E_MALFORMED, "provide content or source_path, not both")
        if (content is None) == (source_path is None):
            raise model.ArtifactError(
                model.E_MALFORMED, "content or source_path is required")
        store.validate_parents(self._root, goal_id, tuple(parent_ids))

        if mission_id is not None:
            self.ensure_mission_workspace(goal_id, mission_id,
                                          created_at=created_at)
        else:
            self.ensure_goal_workspace(goal_id, created_at=created_at)
        content_dir = store.artifact_content_dir(
            self._root, goal_id, mission_id)
        target = content_dir / KIND_SUBDIR.get(kind, "files")
        target.mkdir(parents=True, exist_ok=True)

        # Stage, hash, then promote to a content-addressed destination so two
        # artifacts that share a human name but differ in content can never
        # alias the same path (which would break provenance verification).
        staging = target / f".{name}.{os.getpid()}.staging"
        try:
            if source_path is not None:
                source = Path(source_path)
                if not source.is_file():
                    raise model.ArtifactError(
                        model.E_MALFORMED,
                        f"source artifact missing: {source}")
                shutil.copy2(source, staging)
            else:
                assert content is not None
                staging.write_bytes(content)
            digest, size = store.sha256_file(
                staging, max_bytes=model.MAX_ARTIFACT_BYTES)
            destination = target / f"{digest[:16]}__{name}"
            if destination.exists():
                staging.unlink(missing_ok=True)
            else:
                os.replace(staging, destination)
        except BaseException:
            with contextlib.suppress(OSError):
                staging.unlink(missing_ok=True)
            raise

        rel_path = str(destination.relative_to(self._root))
        record = model.ArtifactRecord.build(
            goal_id=goal_id, mission_id=mission_id, phase_id=phase_id,
            subrun_id=subrun_id, kind=kind, name=name, rel_path=rel_path,
            size_bytes=size, content_sha256=digest, producer=producer,
            created_at=created_at or utc_now_iso(),
            parent_ids=tuple(parent_ids), shared_with=tuple(shared_with),
            reuse_input_id=reuse_input_id, proof_id=proof_id,
            criterion_id=criterion_id)
        store.write_artifact(self._root, record)
        return record

    def load_artifact(self, goal_id: str,
                      artifact_id: str) -> model.ArtifactRecord:
        return store.load_artifact(self._root, goal_id, artifact_id)

    def lineage(self, goal_id: str,
                artifact_id: str) -> tuple[model.ArtifactRecord, ...]:
        return store.lineage(self._root, goal_id, artifact_id)

    # -- reconstruction --------------------------------------------------------

    def reconstruct(self, goal_id: str) -> WorkspaceState:
        goal_path = store.goal_workspace_path(self._root, goal_id)
        goal_record = store.load_workspace(store.workspace_marker(goal_path))
        missions: list[model.WorkspaceRecord] = []
        missions_dir = goal_path / "missions"
        if missions_dir.is_dir():
            for child in sorted(missions_dir.iterdir()):
                marker = store.workspace_marker(child)
                if child.is_dir() and marker.is_file():
                    missions.append(store.load_workspace(marker))
        artifacts = tuple(store.list_artifacts(self._root, goal_id))
        base = WorkspaceState(
            goal_id=goal_id, goal_workspace=goal_record,
            mission_workspaces=tuple(missions), artifacts=artifacts)
        return WorkspaceState(
            goal_id=goal_id, goal_workspace=goal_record,
            mission_workspaces=tuple(missions), artifacts=artifacts,
            lineage_id=base.compute_lineage_id())

    # -- verification ----------------------------------------------------------

    def verify(self, goal_id: str) -> tuple[bool, list[str]]:
        """Verify every recorded artifact content still matches its digest."""
        problems: list[str] = []
        try:
            artifacts = store.list_artifacts(self._root, goal_id)
        except model.ArtifactError as exc:
            return False, [f"{exc.code}:{exc.detail}"]
        for record in artifacts:
            path = self._root / record.rel_path
            if not path.is_file():
                problems.append(f"MISSING_CONTENT:{record.artifact_id}")
                continue
            try:
                digest, size = store.sha256_file(path)
            except (OSError, model.ArtifactError):
                problems.append(f"UNREADABLE_CONTENT:{record.artifact_id}")
                continue
            if digest != record.content_sha256 or size != record.size_bytes:
                problems.append(f"CONTENT_MISMATCH:{record.artifact_id}")
        return (not problems), problems

    @staticmethod
    def _validate_name(name: str) -> None:
        if not isinstance(name, str) or not name:
            raise model.ArtifactError(model.E_MALFORMED, "name is required")
        if len(name) > model.MAX_NAME_LEN:
            raise model.ArtifactError(model.E_MALFORMED, "name too long")
        if name in (".", "..") or "/" in name or "\\" in name \
                or "\x00" in name:
            raise model.ArtifactError(
                model.E_MALFORMED,
                f"name must be a plain file name: {name!r}")
