"""M025 — operator-facing artifact/workspace provenance projection.

Derived only from the canonical workspace/artifact store: counts by kind, the
recorded artifacts with their lineage identities, and content-integrity
verification. Read-only; nothing here mutates state or invents provenance.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from trajectory_os.artifacts import engine, model, store


def status_document(root: str | Path, goal_id: str, *,
                   verify_content: bool = True) -> dict[str, Any]:
    """Complete machine-readable workspace/artifact document (read-only)."""
    manager = engine.WorkspaceManager(root)
    try:
        state = manager.reconstruct(goal_id)
    except (model.ArtifactError, OSError):
        return {
            "status": "UNAVAILABLE",
            "present": False,
            "goal_id": goal_id,
            "counts": {},
            "artifacts": [],
            "mission_workspaces": [],
        }
    if verify_content:
        ok, problems = manager.verify(goal_id)
    else:
        ok, problems = None, []
    counts: dict[str, int] = {kind: 0 for kind in sorted(model.ARTIFACT_KINDS)}
    for artifact in state.artifacts:
        counts[artifact.kind] = counts.get(artifact.kind, 0) + 1
    return {
        "status": "OK",
        "present": True,
        "goal_id": goal_id,
        "workspace_id": state.goal_workspace.workspace_id,
        "lineage_id": state.lineage_id,
        "counts": {
            "artifacts_total": len(state.artifacts),
            "missions_total": len(state.mission_workspaces),
            "by_kind": counts,
            "content_ok": ok,
        },
        "integrity": {
            "ok": ok,
            "problems": problems[:32],
            "verified": verify_content,
        },
        "artifacts": [
            {
                "artifact_id": artifact.artifact_id,
                "kind": artifact.kind,
                "name": artifact.name,
                "mission_id": artifact.mission_id,
                "phase_id": artifact.phase_id,
                "producer": artifact.producer,
                "size_bytes": artifact.size_bytes,
                "content_sha256": artifact.content_sha256,
                "parent_ids": list(artifact.parent_ids),
                "reuse_input_id": artifact.reuse_input_id,
                "proof_id": artifact.proof_id,
                "criterion_id": artifact.criterion_id,
                "created_at": artifact.created_at,
            }
            for artifact in state.artifacts
        ],
        "mission_workspaces": [
            {
                "workspace_id": workspace.workspace_id,
                "mission_id": workspace.mission_id,
            }
            for workspace in state.mission_workspaces
        ],
    }


def lineage_document(root: str | Path, goal_id: str,
                     artifact_id: str) -> dict[str, Any]:
    """Machine-readable lineage for one artifact (queried artifact first)."""
    manager = engine.WorkspaceManager(root)
    records = manager.lineage(goal_id, artifact_id)
    return {
        "status": "OK",
        "goal_id": goal_id,
        "artifact_id": artifact_id,
        "lineage": [record.to_dict() for record in records],
    }


def render_status(document: Mapping[str, Any]) -> str:
    if document.get("status") != "OK":
        return (f"artifacts : unavailable for goal "
                f"{document.get('goal_id')}")
    counts = document.get("counts", {})
    if not isinstance(counts, Mapping):
        counts = {}
    by_kind = counts.get("by_kind", {})
    if not isinstance(by_kind, Mapping):
        by_kind = {}
    integrity = document.get("integrity", {})
    if not isinstance(integrity, Mapping):
        integrity = {}
    lines = [
        f"goal      : {document.get('goal_id')}",
        f"workspace : {document.get('workspace_id')}",
        f"lineage   : {document.get('lineage_id')}",
        f"artifacts : total={counts.get('artifacts_total')} "
        f"missions={counts.get('missions_total')} "
        f"content_ok={integrity.get('ok')}",
        "by kind   : " + " ".join(
            f"{kind}={by_kind.get(kind, 0)}" for kind in sorted(by_kind)),
    ]
    problems = integrity.get("problems")
    if isinstance(problems, list) and problems:
        lines.append("problems  : " + "; ".join(str(p) for p in problems))
    artifacts = document.get("artifacts")
    if isinstance(artifacts, list) and artifacts:
        lines.append("records   :")
        for entry in artifacts[:64]:
            if not isinstance(entry, Mapping):
                continue
            parents = entry.get("parent_ids")
            parent_text = (f" parents={len(parents)}"
                           if isinstance(parents, list) and parents else "")
            lines.append(
                f"  - {entry.get('kind')} {entry.get('name')} "
                f"id={str(entry.get('artifact_id'))[:12]} "
                f"size={entry.get('size_bytes')}{parent_text}")
    return "\n".join(lines)


def render_lineage(document: Mapping[str, Any]) -> str:
    if document.get("status") != "OK":
        return f"lineage   : unavailable ({document.get('status')})"
    lines = [
        f"artifact  : {document.get('artifact_id')}",
        f"goal      : {document.get('goal_id')}",
        "lineage   :",
    ]
    lineage = document.get("lineage")
    if isinstance(lineage, list):
        for entry in lineage:
            if not isinstance(entry, Mapping):
                continue
            parents = entry.get("parent_ids")
            parent_text = (f" parents={len(parents)}"
                           if isinstance(parents, list) and parents else "")
            lines.append(
                f"  - {entry.get('kind')} {entry.get('name')} "
                f"id={str(entry.get('artifact_id'))[:12]}{parent_text}")
    return "\n".join(lines)


def resolve_artifact(root: str | Path, goal_id: str,
                     artifact_id: str) -> model.ArtifactRecord:
    """Cross-goal-safe artifact resolution (raises on leakage)."""
    return store.load_artifact(root, goal_id, artifact_id)
