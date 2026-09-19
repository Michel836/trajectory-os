"""M062 — read-only dashboard/API and LifeOS projection for intelligence.

This module does **not** duplicate any decision logic. It reads the durable
artifacts produced by M056–M062 and renders one coherent, read-only summary
for the local API/dashboard, plus LifeOS-compatible markdown notes.

The LifeOS writer refuses to write inside the canonical state root, mirroring
the M028 boundary: intelligence projections are advisory sinks and never
mutate canonical state.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from trajectory_os.intelligence import adaptive, decision, ml, model, routing
from trajectory_os.intelligence import dataset as dataset_module


def _optional(path: str) -> dict[str, Any] | None:
    return model.read_json(path)


def build_intelligence_projection(
    root: str | Path, *, clock: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Build the read-only intelligence projection (missing parts are null)."""
    root_str = str(root)
    learning = dataset_module.load_dataset(root_str)
    report = ml.load_report(root_str)
    recommendation = routing.load_recommendation(root_str)
    comparison = _optional(adaptive.comparison_path(root_str))
    return {
        "schema_version": model.SCHEMA_VERSION,
        "intelligence_version": model.INTELLIGENCE_VERSION,
        "generated_at": (clock() if clock is not None else model.utc_now()),
        "read_only": True,
        "dataset": _dataset_summary(learning),
        "ml": _ml_summary(report),
        "routing": (recommendation.to_dict()
                    if recommendation is not None else None),
        "scheduler": comparison,
        "decisions": decision.decision_summary(root_str),
        "workflows": _workflow_summaries(root_str),
    }


def _dataset_summary(
    learning: dataset_module.LearningDataset | None,
) -> dict[str, Any] | None:
    if learning is None:
        return None
    quality = learning.quality
    return {
        "dataset_id": learning.dataset_id,
        "source_kind": learning.source_kind,
        "row_count": learning.row_count,
        "real_rows": quality.real_rows,
        "fixture_rows": quality.fixture_rows,
        "trainable_targets": list(quality.trainable_targets),
        "untrainable_targets": dict(quality.untrainable_targets),
        "leakage_free": bool(
            learning.split_metadata.get("leakage_free", False)),
        "built_at": learning.built_at,
    }


def _ml_summary(report: ml.TrainingReport | None) -> dict[str, Any] | None:
    if report is None:
        return None
    return {
        "dataset_id": report.dataset_id,
        "feature_set": report.feature_set,
        "generated_at": report.generated_at,
        "targets": [
            {
                "target": evaluation.target,
                "status": evaluation.status,
                "reason": evaluation.reason,
                "selected_algorithm": evaluation.selected_algorithm,
                "model_id": evaluation.model_id or None,
            }
            for evaluation in report.evaluations],
    }


def _workflow_summaries(root: str) -> list[dict[str, Any]]:
    directory = Path(root) / "workflows"
    if not directory.is_dir():
        return []
    summaries: list[dict[str, Any]] = []
    for child in sorted(directory.iterdir()):
        if not child.is_dir():
            continue
        artifact = model.read_json(child / "artifact.json")
        if artifact is None:
            continue
        summaries.append({
            "workflow_id": artifact.get("workflow_id"),
            "family": artifact.get("family"),
            "title": artifact.get("title"),
            "inputs_are_fixture": artifact.get("inputs_are_fixture"),
            "generated_at": artifact.get("generated_at"),
            "next_action_count": len(artifact.get("next_actions", [])),
        })
    return summaries


# --- LifeOS projection --------------------------------------------------------


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def build_lifeos_notes(root: str | Path) -> tuple[dict[str, Any], ...]:
    """Collect LifeOS note documents without writing anything."""
    root_str = str(root)
    notes: list[dict[str, Any]] = []
    for decision_id in decision.list_decisions(root_str):
        snapshot = decision.load_decision(root_str, decision_id)
        if snapshot is None:
            continue
        notes.append({
            "note_id": f"decision:{decision_id}",
            "kind": "DECISION",
            "title": snapshot.question,
            "markdown": decision.to_lifeos_note(snapshot),
            "project_id": None,
            "mission_id": None,
        })
    directory = Path(root_str) / "workflows"
    if directory.is_dir():
        for child in sorted(directory.iterdir()):
            note_document = model.read_json(child / "lifeos-note.json")
            if note_document is None:
                continue
            notes.append({
                "note_id": f"workflow:{note_document.get('workflow_id')}",
                "kind": "WORKFLOW",
                "title": note_document.get("title"),
                "markdown": note_document.get("markdown", ""),
                "project_id": note_document.get("project_id"),
                "mission_id": note_document.get("mission_id"),
            })
    return tuple(notes)


def write_lifeos_notes(
    root: str | Path, target_dir: str | Path,
) -> tuple[dict[str, Any], ...]:
    """Write LifeOS notes to an external target (never inside the root)."""
    root_path = Path(root)
    target = Path(target_dir)
    if _inside(target, root_path):
        model.fail(model.E_MALFORMED,
                   "LifeOS target must be outside the canonical root")
    written: list[dict[str, Any]] = []
    for note in build_lifeos_notes(root_path):
        safe_name = str(note["note_id"]).replace(":", "-").replace("/", "-")
        path = target / f"{safe_name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(note["markdown"]), encoding="utf-8")
        written.append({**note, "path": str(path)})
    return tuple(written)


__all__ = [
    "build_intelligence_projection",
    "build_lifeos_notes",
    "write_lifeos_notes",
]
