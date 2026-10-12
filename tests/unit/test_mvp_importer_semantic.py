"""Tests for semantic chunking, hierarchy, evidence, consolidation, and
anti-inflation behaviour of the importer."""

from __future__ import annotations

from trajectory_os.mvp import importer, model, store


def _seed(root: object) -> None:
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=(
            model.Project(project_id="inventory-tool", name="Inventory Tool",
                          objective="recover"),
            model.Project(project_id="training-application",
                          name="Sample Training Application", objective="train"),
            model.Project(project_id="budget-tracker",
                          name="Synthetic Budget Tracker", objective="budget"),
        ),
        tasks=(),
    ))


def test_chunking_splits_at_topic_boundaries() -> None:
    text = "\n".join(
        f"Section {i} :\n- " + ("desc " * 40).strip()
        for i in range(30))
    chunks = importer._chunk_text(text)
    assert len(chunks) > 1
    # No chunk exceeds the hard cap.
    assert all(len(c) <= importer.CHUNK_MAX_CHARS for c in chunks)
    assert all(chunks)


def test_rich_taxonomy_supported() -> None:
    assert {"AREA", "PROJECT", "WORKSTREAM", "WORK_PACKAGE", "TASK",
            "SUBTASK", "DELIVERABLE", "IDEA", "SOMEDAY"} <= importer.KINDS


def test_evidence_model_supported() -> None:
    assert {"FACT", "INFERRED", "SUGGESTED", "UNKNOWN"} == importer.EVIDENCES


def test_suggested_items_not_auto_accepted(tmp_path: object) -> None:
    _seed(tmp_path)
    portfolio = store.load_portfolio(str(tmp_path))
    assert portfolio is not None
    llm = lambda system, user: {"items": [  # noqa: E731
        {"kind": "PROJECT", "title": "Documentation Library", "parent": None,
         "source_text": "faire portfolio github", "evidence": "FACT",
         "needs_review": False, "confidence": 0.8,
         "suggested_children": [
             {"title": "Inventory the entries", "kind": "TASK",
              "rationale": "d'abord l'audit"},
         ]},
    ]}
    analysis = importer.analyze_document(
        str(tmp_path), "x.txt", b"x", mode="semantic", llm=llm)  # type: ignore[arg-type]
    # The AI child is present with evidence SUGGESTED and default action skip.
    child = next(c for c in analysis.candidates
                 if c.title == "Inventory the entries")
    assert child.evidence == "SUGGESTED"
    assert child.needs_review is True
    assert analysis.to_dict()["summary"]["ai_suggested"] >= 1


def test_hierarchy_projection_nests_children(tmp_path: object) -> None:
    _seed(tmp_path)
    llm = lambda system, user: {"items": [  # noqa: E731
        {"kind": "AREA", "title": "Finance", "parent": None,
         "source_text": "finance", "evidence": "INFERRED"},
        {"kind": "PROJECT", "title": "Budget Tracker", "parent": "Finance",
         "source_text": "budget", "evidence": "FACT"},
        {"kind": "TASK", "title": "Transfer", "parent": "Budget Tracker",
         "source_text": "transfer", "evidence": "FACT"},
    ]}
    analysis = importer.analyze_document(
        str(tmp_path), "x.txt", b"x", mode="semantic", llm=llm)  # type: ignore[arg-type]
    hierarchy = analysis.to_dict()["hierarchy"]
    root = hierarchy["roots"][0]
    assert root["title"] == "Finance"
    assert root["children"][0]["title"] == "Budget Tracker"
    assert root["children"][0]["children"][0]["title"] == "Transfer"


def test_area_with_project_children_does_not_become_project(
        tmp_path: object) -> None:
    _seed(tmp_path)
    portfolio = store.load_portfolio(str(tmp_path))
    assert portfolio is not None
    llm = lambda system, user: {"items": [  # noqa: E731
        {"kind": "AREA", "title": "Finance", "parent": None,
         "source_text": "finance", "evidence": "INFERRED"},
        {"kind": "PROJECT", "title": "Budget Tracker", "parent": "Finance",
         "source_text": "budget", "evidence": "FACT"},
    ]}
    analysis = importer.analyze_document(
        str(tmp_path), "x.txt", b"x", mode="semantic", llm=llm)  # type: ignore[arg-type]
    decisions = importer.parse_decisions([
        {"candidate_id": _cid(analysis, "Finance"), "action": "import"},
        {"candidate_id": _cid(analysis, "Budget Tracker"), "action": "import"},
    ])
    new_portfolio, entity_map = importer.apply_decisions(
        portfolio, analysis, decisions)
    # "Finance" (AREA) does not become a project; it becomes the domain of Budget Tracker.
    names = [p.name for p in new_portfolio.projects]
    assert "Finance" not in names
    assert "Budget Tracker" in names
    budget = next(p for p in new_portfolio.projects if p.name == "Budget Tracker")
    assert budget.domain == "Finance"
    assert "Finance" not in entity_map.values() or \
        entity_map[_cid(analysis, "Finance")] == "area"


def test_workstream_maps_to_task_field(tmp_path: object) -> None:
    _seed(tmp_path)
    portfolio = store.load_portfolio(str(tmp_path))
    assert portfolio is not None
    llm = lambda system, user: {"items": [  # noqa: E731
        {"kind": "PROJECT", "title": "GitHub", "parent": None,
         "source_text": "github", "evidence": "FACT"},
        {"kind": "WORKSTREAM", "title": "Audit", "parent": "GitHub",
         "source_text": "audit", "evidence": "INFERRED"},
        {"kind": "TASK", "title": "Inventory entries", "parent": "Audit",
         "source_text": "inventorier", "evidence": "FACT"},
    ]}
    analysis = importer.analyze_document(
        str(tmp_path), "x.txt", b"x", mode="semantic", llm=llm)  # type: ignore[arg-type]
    decisions = importer.parse_decisions([
        {"candidate_id": _cid(analysis, "GitHub"), "action": "import"},
        {"candidate_id": _cid(analysis, "Audit"), "action": "import"},
        {"candidate_id": _cid(analysis, "Inventory entries"),
         "action": "import"},
    ])
    new_portfolio, _ = importer.apply_decisions(portfolio, analysis, decisions)
    task = next(t for t in new_portfolio.tasks
                if t.title == "Inventory entries")
    assert task.workstream == "Audit"
    assert task.project_id in {p.project_id for p in new_portfolio.projects}


def test_semantic_duplicate_consolidation(tmp_path: object) -> None:
    _seed(tmp_path)
    llm = lambda system, user: {"items": [  # noqa: E731
        {"kind": "PROJECT", "title": "Inventory Tool", "parent": None,
         "source_text": "inventory-tool a", "evidence": "FACT"},
        {"kind": "PROJECT", "title": "Inventory Tool", "parent": None,
         "source_text": "inventory-tool b", "evidence": "FACT"},
    ]}
    analysis = importer.analyze_document(
        str(tmp_path), "x.txt", b"x", mode="semantic", llm=llm)  # type: ignore[arg-type]
    # The two titles normalize to the same key -> consolidated to one candidate.
    titles = [c.title for c in analysis.candidates]
    assert len(titles) == 1


def test_many_topics_do_not_hit_project_limit(tmp_path: object) -> None:
    _seed(tmp_path)
    portfolio = store.load_portfolio(str(tmp_path))
    assert portfolio is not None
    # 80 distinct projects (exceeds the old MAX_PROJECTS=64).
    items = [{"kind": "PROJECT", "title": f"Projet {i:02d}", "parent": None,
              "source_text": f"p{i}", "evidence": "FACT"} for i in range(80)]
    llm = lambda system, user, items=items: {"items": items}  # noqa: E731
    analysis = importer.analyze_document(
        str(tmp_path), "x.txt", b"x", mode="semantic", llm=llm)  # type: ignore[arg-type]
    decisions = [{"candidate_id": c.candidate_id, "action": "import"}
                 for c in analysis.candidates]
    result = importer.confirm_import(str(tmp_path), analysis, decisions)
    assert result["created_projects"] == 80
    reloaded = store.load_portfolio(str(tmp_path))
    assert reloaded is not None
    assert len(reloaded.projects) == 80 + 3  # + 3 seed projects


def _cid(analysis: importer.ImportAnalysis, title: str) -> str:
    return next(c.candidate_id for c in analysis.candidates
                if c.title == title)
