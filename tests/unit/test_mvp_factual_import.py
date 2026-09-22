"""Focused tests for factual-first document import.

The initial import must mirror only what the document states. It must not
fabricate workstreams, work packages, subtasks, deliverables, dependencies or
next actions, and the preview must be materially smaller than a speculative
semantic decomposition.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from trajectory_os.mvp import importer, model, store


def _seed(root: object) -> None:
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=(model.Project(project_id="inventory-tool", name="Inventory Tool",
                                objective="recover"),),
        tasks=(),
    ))


def _speculative_llm(system: str, user: str) -> Mapping[str, Any]:
    del system, user
    return {"items": [
        {"kind": "PROJECT", "title": "Inventory Tool", "parent": None,
         "source_text": "Inventory Tool", "evidence": "FACT", "confidence": 0.9,
         "suggested_next_action": "Call the lawyer",
         "prerequisites": ["judgment"],
         "suggested_children": [
             {"title": "Stock audit", "kind": "WORKSTREAM",
              "rationale": "speculative"}],
         "dependencies": [
             {"source": "Inventory Tool", "target": "Stock audit",
              "rationale": "speculative", "evidence": "SUGGESTED",
              "confidence": 0.8}]},
        {"kind": "WORKSTREAM", "title": "Stock audit", "parent": "Inventory Tool",
         "evidence": "SUGGESTED", "needs_review": True},
        {"kind": "TASK", "title": "Collect stock counts", "parent": "Inventory Tool",
         "evidence": "FACT", "confidence": 0.8},
        {"kind": "DELIVERABLE", "title": "Complete legal file",
         "parent": "Inventory Tool", "evidence": "SUGGESTED", "needs_review": True},
        {"kind": "TASK", "title": "Organise the reorder", "parent": "Inventory Tool",
         "evidence": "FACT", "confidence": 0.7,
         "dependencies": [
             {"source": "Collect stock counts", "target": "Organise the reorder",
              "rationale": "speculative", "evidence": "SUGGESTED",
              "confidence": 0.9}]},
    ]}


def test_default_mode_is_factual(tmp_path: object) -> None:
    _seed(tmp_path)
    analysis = importer.analyze_document(
        str(tmp_path), "notes.txt", b"Inventory Tool", llm=_speculative_llm,
        engine="local")  # type: ignore[arg-type]
    assert analysis.mode == importer.MODE_FACTUAL
    assert analysis.engine == "local"


def test_factual_import_drops_speculative_wbs(tmp_path: object) -> None:
    _seed(tmp_path)
    analysis = importer.analyze_document(
        str(tmp_path), "notes.txt", b"Inventory Tool", llm=_speculative_llm,
        engine="local")  # type: ignore[arg-type]
    summary = analysis.to_dict()["summary"]
    # Only the explicitly-stated FACT items survive.
    assert summary["workstreams"] == 0
    assert summary["deliverables"] == 0
    assert summary["ai_suggested"] == 0
    assert summary["factual_items"] == 3
    assert summary["next_actions"] == 0
    assert summary["with_prerequisites"] == 0
    # No candidate carries speculative fields.
    for candidate in analysis.candidates:
        assert candidate.evidence != importer.SUGGESTED
        assert candidate.suggested_children == ()
        assert candidate.suggested_dependencies == ()
        assert candidate.suggested_next_action == ""
        assert candidate.prerequisites == ()


def test_factual_completion_report_has_zero_ai_items(tmp_path: object) -> None:
    _seed(tmp_path)
    analysis = importer.analyze_document(
        str(tmp_path), "notes.txt", b"Inventory Tool", llm=_speculative_llm,
        engine="local")  # type: ignore[arg-type]
    report = analysis.to_dict()["completion_report"]
    assert report["ai_generated_items"] == 0
    assert report["factual_items"] == 3
    assert report["likely_merges"] >= 1
    assert report["engine"] == "local"


def test_factual_deterministic_fallback_is_small_and_safe(
        tmp_path: object) -> None:
    _seed(tmp_path)
    text = (b"My Project:\n"
            b"- Do the first thing\n"
            b"- Do the second thing\n"
            b"Another Project:\n"
            b"- Do another thing\n")
    analysis = importer.analyze_document(
        str(tmp_path), "notes.txt", text, llm=None)
    assert analysis.mode == importer.MODE_FACTUAL
    assert analysis.engine == "factual"
    summary = analysis.to_dict()["summary"]
    assert summary["projects"] == 2
    assert summary["tasks"] == 3
    assert summary["workstreams"] == 0
    assert summary["ai_suggested"] == 0
    assert all(c.evidence == importer.FACT
               for c in analysis.candidates
               if c.kind in (importer.PROJECT, importer.TASK))


def test_factual_preview_still_imports(tmp_path: object) -> None:
    _seed(tmp_path)
    analysis = importer.analyze_document(
        str(tmp_path), "notes.txt", b"x", llm=_speculative_llm,
        engine="local")  # type: ignore[arg-type]
    decisions = [
        {"candidate_id": c.candidate_id, "action": "import"}
        for c in analysis.candidates
    ]
    result = importer.confirm_import(str(tmp_path), analysis, decisions)
    assert result["created_tasks"] >= 1
    reloaded = store.load_portfolio(str(tmp_path))
    assert reloaded is not None
    assert any(t.title == "Collect stock counts" for t in reloaded.tasks)


def test_factual_preview_materially_smaller_than_semantic(
        tmp_path: object) -> None:
    _seed(tmp_path)
    factual = importer.analyze_document(
        str(tmp_path), "notes.txt", b"x", llm=_speculative_llm,
        engine="local")  # type: ignore[arg-type]
    semantic = importer.analyze_document(
        str(tmp_path), "notes.txt", b"x", llm=_speculative_llm,
        mode=importer.MODE_SEMANTIC)  # type: ignore[arg-type]
    assert len(factual.candidates) < len(semantic.candidates)
    assert semantic.to_dict()["summary"]["ai_suggested"] > 0
