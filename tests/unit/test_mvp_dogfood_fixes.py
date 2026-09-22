"""Focused regression tests from the representative multi-page import regression run (D1/D2/D3).

These lock in the targeted fixes identified by the first representative multi-page
import run:

* **D1** — a selected LLM engine that fails on every chunk must fail the
  job closed instead of silently returning deterministic fallback results;
  partial failure is allowed and reported; the warning is no longer
  misleading.
* **D2** — ``source_text`` provenance is required and never empty.
* **D3** — a conservative deterministic post-pass re-attaches existing
  candidates to explicitly listed source headings without inventing WBS.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from trajectory_os.mvp import import_jobs, importer, model, store


def _seed(root: object) -> None:
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=(model.Project(project_id="inventory-tool", name="Inventory Tool",
                                objective="recover"),),
        tasks=(),
    ))


def _failing_llm(system: str, user: str) -> Mapping[str, Any]:
    del system, user
    raise importer.PortfolioImportError("simulated engine outage")


def _flaky_llm() -> Any:
    state = {"calls": 0}

    def llm(system: str, user: str) -> Mapping[str, Any]:
        del system, user
        state["calls"] += 1
        if state["calls"] == 1:
            raise importer.PortfolioImportError("simulated chunk outage")
        return {"items": [{
            "kind": "TASK", "title": "Chunk ok",
            "source_text": "chunk ok", "evidence": "FACT"}]}

    return llm


# --- D1 — no silent total fallback -------------------------------------------


def test_total_llm_failure_raises_and_does_not_fall_back(
        tmp_path: object) -> None:
    _seed(tmp_path)
    with pytest.raises(importer.PortfolioImportError):
        importer.analyze_document(
            str(tmp_path), "notes.txt", b"Project:\n- do a thing\n",
            llm=_failing_llm, engine="local")  # type: ignore[arg-type]


def test_total_llm_failure_marks_job_failed(tmp_path: object) -> None:
    import time

    _seed(tmp_path)
    manager = import_jobs.ImportJobManager(
        str(tmp_path), llm=_failing_llm)  # type: ignore[arg-type]
    job = manager.start(
        "notes.txt", b"Project:\n- do a thing\n", engine="local",
        mode=importer.MODE_FACTUAL)
    deadline = time.monotonic() + 5
    finished = manager.get(job.job_id)
    while time.monotonic() < deadline:
        finished = manager.get(job.job_id)
        if finished.status in (import_jobs.JOB_COMPLETED,
                               import_jobs.JOB_FAILED,
                               import_jobs.JOB_CANCELLED):
            break
        time.sleep(0.02)
    assert finished.status == import_jobs.JOB_FAILED
    assert finished.stage == importer.STAGE_FAILED
    assert finished.analysis is None
    assert finished.draft_id is None
    assert finished.error and "engine" in finished.error
    assert "fail" in finished.message.lower()


def test_partial_llm_failure_is_reported_but_not_fatal(
        tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importer, "CHUNK_TARGET_CHARS", 1)
    monkeypatch.setattr(importer, "CHUNK_MAX_CHARS", 1)
    _seed(tmp_path)
    text = (b"Project A :\n- first thing\n"
            b"Project B :\n- second thing\n")
    analysis = importer.analyze_document(
        str(tmp_path), "notes.txt", text, llm=_flaky_llm(),
        engine="local")  # type: ignore[arg-type]
    assert analysis.llm_calls >= 1
    assert analysis.fallback_count == 1
    assert analysis.chunk_failures == 1
    assert "fallback" in analysis.warning
    assert "review flagged items" not in analysis.warning


def test_total_failure_warning_never_claims_review_items(
        tmp_path: object) -> None:
    _seed(tmp_path)
    # The deterministic engine (llm=None) is not a silent fallback and keeps
    # its empty warning: the misleading "review flagged items" text is gone.
    analysis = importer.analyze_document(
        str(tmp_path), "notes.txt", b"Project:\n- thing\n", llm=None)
    assert analysis.fallback_count == 0
    assert analysis.warning == ""


# --- D2 — source_text provenance ---------------------------------------------


def _no_source_llm(system: str, user: str) -> Mapping[str, Any]:
    del system, user
    return {"items": [
        {"kind": "PROJECT", "title": "Sample Language Project", "parent": None,
         "evidence": "FACT"},
        {"kind": "TASK", "title": "Build a search index", "parent": "Sample Language Project",
         "evidence": "INFERRED"},
    ]}


def test_source_text_is_a_required_schema_field() -> None:
    required = importer._LLM_SCHEMA["properties"]["items"]["items"]["required"]
    assert "source_text" in required


def test_source_text_and_evidence_are_preserved(tmp_path: object) -> None:
    _seed(tmp_path)
    analysis = importer.analyze_document(
        str(tmp_path), "notes.txt", b"Project:\n- build a search index\n",
        llm=_no_source_llm, engine="local")  # type: ignore[arg-type]
    by_title = {c.title: c for c in analysis.candidates}
    # Omitted source_text defaults to the title (never an empty string).
    assert by_title["Sample Language Project"].source_text == "Sample Language Project"
    assert by_title["Build a search index"].source_text == "Build a search index"
    assert all(c.source_text for c in analysis.candidates)
    # Evidence classification survives the factual sanitizer.
    assert by_title["Sample Language Project"].evidence == importer.FACT
    assert by_title["Build a search index"].evidence == importer.INFERRED


def test_factual_prompt_requires_source_text() -> None:
    assert "source_text" in importer._FACTUAL_SYSTEM_PROMPT


# --- D3 — conservative source hierarchy ---------------------------------------


_HIERARCHY_DOC = (
    b"Sample Language Project :\n"
    b"- scan the handbook\n"
    b"- build a small app\n"
    b"Sample Workshop :\n"
    b"- faire le scan de tous les elements\n"
)


def _hierarchy_llm(system: str, user: str) -> Mapping[str, Any]:
    del system, user
    return {"items": [
        {"kind": "PROJECT", "title": "Sample Language Project", "parent": None,
         "source_text": "Sample Language Project :", "evidence": "FACT"},
        {"kind": "TASK", "title": "Scan the handbook", "parent": None,
         "source_text": "scan the handbook", "evidence": "FACT"},
        {"kind": "TASK", "title": "Build a small app", "parent": None,
         "source_text": "build a small app", "evidence": "FACT"},
        {"kind": "PROJECT", "title": "Sample Workshop", "parent": None,
         "source_text": "Sample Workshop :", "evidence": "FACT"},
        {"kind": "TASK", "title": "Scan all the items",
         "parent": None, "source_text": "faire le scan de tous les elements",
         "evidence": "FACT"},
    ]}


def test_source_hierarchy_attaches_explicit_list_items(tmp_path: object) -> None:
    _seed(tmp_path)
    analysis = importer.analyze_document(
        str(tmp_path), "notes.txt", _HIERARCHY_DOC, llm=_hierarchy_llm,
        engine="local")  # type: ignore[arg-type]
    by_title = {c.title: c for c in analysis.candidates}
    language_project = by_title["Sample Language Project"]
    workshop = by_title["Sample Workshop"]
    assert by_title["Scan the handbook"].parent_id == language_project.candidate_id
    assert by_title["Build a small app"].parent_id == language_project.candidate_id
    assert by_title["Scan all the items"].parent_id == \
        workshop.candidate_id
    hierarchy = analysis.to_dict()["hierarchy"]
    assert {root["title"] for root in hierarchy["roots"]} == {
        "Sample Language Project", "Sample Workshop"}


def test_source_hierarchy_preserves_explicit_parent(tmp_path: object) -> None:
    _seed(tmp_path)

    def llm(system: str, user: str) -> Mapping[str, Any]:
        del system, user
        return {"items": [
            {"kind": "PROJECT", "title": "Project A", "parent": None,
             "source_text": "Project A :", "evidence": "FACT"},
            {"kind": "PROJECT", "title": "Project B", "parent": None,
             "source_text": "Project B :", "evidence": "FACT"},
            {"kind": "TASK", "title": "Task B", "parent": "Project B",
             "source_text": "task b", "evidence": "FACT"},
        ]}

    analysis = importer.analyze_document(
        str(tmp_path), "notes.txt",
        b"Project A :\n- task b\nProject B :\n- task b\n", llm=llm,
        engine="local")  # type: ignore[arg-type]
    by_title = {c.title: c for c in analysis.candidates}
    # An explicit hierarchy is never overridden by the post-pass.
    assert by_title["Task B"].parent_id == by_title["Project B"].candidate_id


def test_source_hierarchy_never_invents_wbs(tmp_path: object) -> None:
    _seed(tmp_path)
    analysis = importer.analyze_document(
        str(tmp_path), "notes.txt", _HIERARCHY_DOC, llm=_hierarchy_llm,
        engine="local")  # type: ignore[arg-type]
    summary = analysis.to_dict()["summary"]
    assert summary["workstreams"] == 0
    assert summary["work_packages"] == 0
    assert summary["subtasks"] == 0
    assert summary["deliverables"] == 0
    assert summary["ai_suggested"] == 0
    # The post-pass only re-parents existing candidates.
    assert len(analysis.candidates) == 5


def test_source_hierarchy_confirms_into_project(tmp_path: object) -> None:
    _seed(tmp_path)
    analysis = importer.analyze_document(
        str(tmp_path), "notes.txt", _HIERARCHY_DOC, llm=_hierarchy_llm,
        engine="local")  # type: ignore[arg-type]
    decisions = [
        {"candidate_id": c.candidate_id, "action": "import"}
        for c in analysis.candidates
    ]
    result = importer.confirm_import(str(tmp_path), analysis, decisions)
    assert result["created_projects"] == 2
    assert result["created_tasks"] == 3
    reloaded = store.load_portfolio(str(tmp_path))
    assert reloaded is not None
    language_project = next(p for p in reloaded.projects
                   if p.name == "Sample Language Project")
    attached = [t for t in reloaded.tasks if t.project_id == language_project.project_id]
    assert {t.title for t in attached} == {
        "Scan the handbook", "Build a small app"}


def test_merge_bound_area_is_demoted_to_project(tmp_path: object) -> None:
    _seed(tmp_path)

    def llm(system: str, user: str) -> Mapping[str, Any]:
        del system, user
        return {"items": [
            {"kind": "AREA", "title": "Archive", "parent": None,
             "source_text": "Archive :", "evidence": "FACT",
             "merge_project_id": "inventory-tool"},
            {"kind": "AREA", "title": "Logistics", "parent": None,
             "source_text": "Logistics :", "evidence": "FACT"},
        ]}

    analysis = importer.analyze_document(
        str(tmp_path), "notes.txt", b"Archive :\n- profile\nLogistics :\n- fitness\n",
        llm=llm, engine="local")  # type: ignore[arg-type]
    kinds = {c.title: c.kind for c in analysis.candidates}
    assert kinds["Archive"] == importer.PROJECT
    # An unbound broad domain stays an AREA (no blanket redesign).
    assert kinds["Logistics"] == importer.AREA


def test_factual_mode_still_produces_zero_ai_items(tmp_path: object) -> None:
    _seed(tmp_path)
    analysis = importer.analyze_document(
        str(tmp_path), "notes.txt", _HIERARCHY_DOC, llm=_hierarchy_llm,
        engine="local")  # type: ignore[arg-type]
    report = analysis.to_dict()["completion_report"]
    assert report["ai_generated_items"] == 0
    assert report["factual_items"] == 5
