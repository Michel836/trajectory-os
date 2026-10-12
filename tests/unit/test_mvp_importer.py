"""Unit tests for the MVP document → portfolio semantic importer."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from trajectory_os.mvp import importer, model, store


def _seed(root: object) -> None:
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=(
            model.Project(project_id="inventory-tool", name="Inventory Tool — reconciliation",
                          objective="Recover funds"),
            model.Project(project_id="training-application",
                          name="Sample Training Application",
                          objective="Get funded training"),
        ),
        tasks=(model.Task(task_id="orc.pay", project_id="inventory-tool",
                          title="Organise the reorder"),),
    ))


def _root(tmp_path: object) -> str:
    return str(tmp_path)


_FAKE_LLM_RESULT: dict[str, Any] = {
    "items": [
        {"kind": "PROJECT", "title": "Inventory Tool",
         "parent": None, "source_text": "Inventory Tool : activation entreprise",
         "evidence": "FACT", "merge_project_id": "inventory-tool",
         "needs_review": False, "confidence": 0.9},
        {"kind": "PROJECT", "title": "Sample Workshop",
         "parent": None, "source_text": "Sample workshop: profile",
         "evidence": "FACT", "merge_project_id": None,
         "suggested_status": "DEFERRED", "needs_review": False,
         "confidence": 0.7},
        {"kind": "TASK", "title": "Enable the workspace",
         "parent": "Inventory Tool", "source_text": "workspace activation",
         "evidence": "FACT", "merge_project_id": None,
         "needs_review": False, "confidence": 0.8},
        {"kind": "TASK", "title": "Scan the ledgers",
         "parent": "Sample Workshop", "source_text": "les scanner",
         "evidence": "FACT", "needs_review": False,
         "confidence": 0.6},
        {"kind": "QUESTION", "title": "Which tools?",
         "parent": None, "source_text": "Quel seraient les outils",
         "evidence": "FACT", "needs_review": True,
         "confidence": 0.4},
    ],
}


def _fake_llm(system: str, user: str) -> Mapping[str, Any]:
    del system, user
    return _FAKE_LLM_RESULT


def test_analyze_with_llm_no_mutation(tmp_path: object) -> None:
    _seed(tmp_path)
    before = store.load_portfolio(_root(tmp_path))
    analysis = importer.analyze_document(
        _root(tmp_path), "notes.txt", b"x",
        mode="semantic", llm=_fake_llm)  # type: ignore[arg-type]
    assert analysis.engine == "llm"
    assert len(analysis.candidates) == 5
    after = store.load_portfolio(_root(tmp_path))
    assert after == before  # preview must not mutate


def test_analyze_fallback_marks_review(tmp_path: object) -> None:
    _seed(tmp_path)
    text = (b"Inventory Tool :\n"
            b"- workspace activation\n"
            b"- paiement\n"
            b"Sample workshop :\n"
            b"- profil\n")
    analysis = importer.analyze_document(
        _root(tmp_path), "notes.txt", text, mode="semantic", llm=None)
    assert analysis.engine == "fallback"
    assert all(c.needs_review for c in analysis.candidates)
    kinds = {c.kind for c in analysis.candidates}
    assert importer.PROJECT in kinds
    assert importer.TASK in kinds


def test_duplicate_detection_matches_existing_project(tmp_path: object) -> None:
    _seed(tmp_path)
    analysis = importer.analyze_document(
        _root(tmp_path), "notes.txt", b"x", mode="semantic", llm=_fake_llm)  # type: ignore[arg-type]
    matches = {m.candidate_id: m for m in analysis.matches}
    orc = next(c for c in analysis.candidates if c.title == "Inventory Tool")
    assert orc.candidate_id in matches
    assert matches[orc.candidate_id].matched_project_id == "inventory-tool"


def test_parse_decisions_fail_closed(tmp_path: object) -> None:
    _seed(tmp_path)
    with pytest.raises(importer.PortfolioImportError):
        importer.parse_decisions([{"candidate_id": "c000", "action": "NOPE"}])
    with pytest.raises(importer.PortfolioImportError):
        importer.parse_decisions([{"action": "import"}])


def test_apply_decisions_merge_and_new(tmp_path: object) -> None:
    _seed(tmp_path)
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    analysis = importer.analyze_document(
        _root(tmp_path), "notes.txt", b"x", mode="semantic", llm=_fake_llm)  # type: ignore[arg-type]
    decisions = importer.parse_decisions([
        {"candidate_id": _cid(analysis, "Inventory Tool"), "action": "import",
         "merge_project_id": "inventory-tool"},
        {"candidate_id": _cid(analysis, "Sample Workshop"),
         "action": "import"},
        {"candidate_id": _cid(analysis, "Enable the workspace"),
         "action": "import"},
        {"candidate_id": _cid(analysis, "Scan the ledgers"),
         "action": "import"},
        {"candidate_id": _cid(analysis, "Which tools?"), "action": "skip"},
    ])
    new_portfolio, entity_map = importer.apply_decisions(
        portfolio, analysis, decisions)
    # No duplicate project created for Inventory Tool.
    assert len(new_portfolio.projects) == 3  # original 2 + Sample Workshop
    assert "sample-workshop" in {p.project_id for p in new_portfolio.projects}
    # Tasks attach to the right projects.
    tasks = {t.title: t for t in new_portfolio.tasks}
    assert tasks["Enable the workspace"].project_id == "inventory-tool"
    assert tasks["Scan the ledgers"].project_id == "sample-workshop"
    # Question candidate skipped.
    assert entity_map[_cid(analysis, "Which tools?")] == "skipped"


def test_apply_decisions_does_not_invent_values(tmp_path: object) -> None:
    _seed(tmp_path)
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    analysis = importer.analyze_document(
        _root(tmp_path), "notes.txt", b"x", mode="semantic", llm=_fake_llm)  # type: ignore[arg-type]
    decisions = importer.parse_decisions([
        {"candidate_id": _cid(analysis, "Sample Workshop"), "action": "import"},
    ])
    new_portfolio, _ = importer.apply_decisions(portfolio, analysis, decisions)
    project = next(p for p in new_portfolio.projects
                   if p.name == "Sample Workshop")
    assert project.deadline is None  # no invented deadline
    # A conservative non-active suggestion (DEFERRED) is honored, never a
    # completion state; urgency/impact fall back to neutral, not invented.
    assert project.status == model.PS_DEFERRED
    assert project.urgency == model.U_MEDIUM
    assert project.impact == model.I_MEDIUM


def test_project_without_suggestion_defaults_to_deferred(tmp_path: object) -> None:
    _seed(tmp_path)
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    llm = lambda system, user: {"items": [  # noqa: E731
        {"kind": "PROJECT", "title": "New project", "parent": None,
         "source_text": "header", "evidence": "FACT", "needs_review": True,
         "confidence": 0.5},
    ]}
    analysis = importer.analyze_document(
        _root(tmp_path), "notes.txt", b"x", mode="semantic", llm=llm)  # type: ignore[arg-type]
    decisions = importer.parse_decisions([
        {"candidate_id": _cid(analysis, "New project"),
         "action": "import"},
    ])
    new_portfolio, _ = importer.apply_decisions(portfolio, analysis, decisions)
    project = next(p for p in new_portfolio.projects
                   if p.name == "New project")
    # New projects are deferred by default (no aggressive auto-activation).
    assert project.status == model.PS_DEFERRED


def test_someday_kind_defaults_to_deferred(tmp_path: object) -> None:
    _seed(tmp_path)
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    llm = lambda system, user: {"items": [  # noqa: E731
        {"kind": "SOMEDAY", "title": "Study spiral", "parent": None,
         "source_text": "Sample learning project", "evidence": "FACT",
         "needs_review": True, "confidence": 0.5},
    ]}
    analysis = importer.analyze_document(
        _root(tmp_path), "notes.txt", b"x", mode="semantic", llm=llm)  # type: ignore[arg-type]
    decisions = importer.parse_decisions([
        {"candidate_id": _cid(analysis, "Study spiral"), "action": "import"},
    ])
    new_portfolio, _ = importer.apply_decisions(portfolio, analysis, decisions)
    project = next(p for p in new_portfolio.projects
                   if p.name == "Study spiral")
    assert project.status == model.PS_DEFERRED


def test_apply_decisions_invalid_merge_fails(tmp_path: object) -> None:
    _seed(tmp_path)
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    analysis = importer.analyze_document(
        _root(tmp_path), "notes.txt", b"x", mode="semantic", llm=_fake_llm)  # type: ignore[arg-type]
    decisions = importer.parse_decisions([
        {"candidate_id": _cid(analysis, "Inventory Tool"), "action": "import",
         "merge_project_id": "does-not-exist"},
    ])
    with pytest.raises(importer.PortfolioImportError):
        importer.apply_decisions(portfolio, analysis, decisions)


def test_confirm_import_persists_and_backs_up(tmp_path: object) -> None:
    _seed(tmp_path)
    analysis = importer.analyze_document(
        _root(tmp_path), "notes.txt", b"x", mode="semantic", llm=_fake_llm)  # type: ignore[arg-type]
    result = importer.confirm_import(_root(tmp_path), analysis, [
        {"candidate_id": _cid(analysis, "Inventory Tool"), "action": "import",
         "merge_project_id": "inventory-tool"},
        {"candidate_id": _cid(analysis, "Sample Workshop"), "action": "import"},
        {"candidate_id": _cid(analysis, "Enable the workspace"),
         "action": "import"},
        {"candidate_id": _cid(analysis, "Scan the ledgers"),
         "action": "import", "project_id": "sample-workshop"},
    ])
    assert result["created_projects"] == 1
    assert result["created_tasks"] == 2
    from pathlib import Path

    assert Path(result["backup"]).is_file()
    assert Path(result["artifact"]).is_file()
    # Persistence after restart.
    reloaded = store.load_portfolio(_root(tmp_path))
    assert reloaded is not None
    assert "sample-workshop" in reloaded.project_map()
    assert any(t.title == "Enable the workspace" for t in reloaded.tasks)


def test_provenance_preserved_in_artifact(tmp_path: object) -> None:
    _seed(tmp_path)
    analysis = importer.analyze_document(
        _root(tmp_path), "notes.txt", b"x", mode="semantic", llm=_fake_llm)  # type: ignore[arg-type]
    result = importer.confirm_import(_root(tmp_path), analysis, [
        {"candidate_id": _cid(analysis, "Sample Workshop"), "action": "import"},
    ])
    import json
    from pathlib import Path

    artifact = json.loads(Path(result["artifact"]).read_text(encoding="utf-8"))
    assert artifact["source_name"] == "notes.txt"
    assert artifact["kind"] == "mvp_import"
    assert any(c["title"] == "Sample Workshop" for c in artifact["candidates"])
    assert "sample-workshop" in artifact["entity_map"].values()


def _cid(analysis: importer.ImportAnalysis, title: str) -> str:
    return next(c.candidate_id for c in analysis.candidates
                if c.title == title)
