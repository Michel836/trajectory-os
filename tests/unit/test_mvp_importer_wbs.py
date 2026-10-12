"""Focused tests for semantic WBS execution-hierarchy depth.

These tests pin the behaviours that turn a flat source document into a useful,
reviewable execution hierarchy:

* a complex project receives a deep WBS (WORKSTREAM → WORK_PACKAGE → TASK →
  SUBTASK / DELIVERABLE);
* a simple project is not mechanically over-expanded;
* every AI-added item stays a suggestion (SUGGESTED + needs_review) and is
  never silently accepted;
* deliverables attach to the right workstream/task when flattened;
* suggested dependencies remain unconfirmed suggestions;
* executable projects get a concrete immediate next action;
* the preview retains FACT / INFERRED / SUGGESTED / UNKNOWN evidence labels.
"""

from __future__ import annotations

from trajectory_os.mvp import importer, model, store


def _seed(root: object) -> model.Portfolio:
    portfolio = model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t", projects=(), tasks=(),
    )
    store.save_portfolio(root, portfolio)
    return portfolio


def _analyze(root: str, items: list[dict[str, object]]) -> importer.ImportAnalysis:
    llm = lambda system, user: {"items": items}  # noqa: E731
    return importer.analyze_document(root, "notes.txt", b"x",
                                     mode="semantic", llm=llm)  # type: ignore[arg-type]


def _cid(analysis: importer.ImportAnalysis, title: str) -> str:
    return next(c.candidate_id for c in analysis.candidates
                if c.title == title)


def _import_all(analysis: importer.ImportAnalysis,
                portfolio: model.Portfolio) -> tuple[model.Portfolio, dict[str, str]]:
    decisions = [{"candidate_id": c.candidate_id, "action": "import"}
                 for c in analysis.candidates]
    return importer.apply_decisions(portfolio, analysis,
                                    importer.parse_decisions(decisions))


def test_meaningful_wbs_expansion(tmp_path: object) -> None:
    _seed(tmp_path)
    items: list[dict[str, object]] = [
        {"kind": "PROJECT", "title": "Trajectory_OS", "parent": None,
         "evidence": "FACT", "source_text": "construire l'OS"},
        {"kind": "WORKSTREAM", "title": "Core engine",
         "parent": "Trajectory_OS", "evidence": "SUGGESTED",
         "needs_review": True},
        {"kind": "WORK_PACKAGE", "title": "Data model",
         "parent": "Core engine", "evidence": "SUGGESTED",
         "needs_review": True},
        {"kind": "TASK", "title": "Define entities", "parent": "Data model",
         "evidence": "SUGGESTED", "needs_review": True},
        {"kind": "SUBTASK", "title": "List entities",
         "parent": "Define entities", "evidence": "SUGGESTED",
         "needs_review": True},
        {"kind": "DELIVERABLE", "title": "Validated data model",
         "parent": "Core engine", "evidence": "SUGGESTED",
         "needs_review": True},
    ]
    analysis = _analyze(str(tmp_path), items)
    summary = analysis.to_dict()["summary"]
    assert summary["workstreams"] == 1
    assert summary["work_packages"] == 1
    assert summary["subtasks"] == 1
    assert summary["deliverables"] == 1
    root = analysis.to_dict()["hierarchy"]["roots"][0]
    assert root["title"] == "Trajectory_OS"
    workstream = root["children"][0]
    assert workstream["kind"] == "WORKSTREAM"
    assert any(child["kind"] == "WORK_PACKAGE"
               for child in workstream["children"])


def test_suggestions_remain_suggestions(tmp_path: object) -> None:
    _seed(tmp_path)
    items: list[dict[str, object]] = [
        {"kind": "PROJECT", "title": "Documentation Library", "parent": None,
         "evidence": "FACT",
         "suggested_children": [
             {"title": "Inventory the entries", "kind": "TASK",
              "rationale": "d'abord l'audit"},
         ]},
    ]
    analysis = _analyze(str(tmp_path), items)
    child = next(c for c in analysis.candidates
                 if c.title == "Inventory the entries")
    assert child.evidence == "SUGGESTED"
    assert child.needs_review is True
    # AI suggestions are never silently accepted in the preview.
    child_dict = next(c for c in analysis.to_dict()["candidates"]
                      if c["title"] == "Inventory the entries")
    assert child_dict["suggested_action"] == "skip"


def test_simple_project_not_over_expanded(tmp_path: object) -> None:
    _seed(tmp_path)
    items: list[dict[str, object]] = [
        {"kind": "PROJECT", "title": "Renew passport", "parent": None,
         "evidence": "FACT"},
        {"kind": "TASK", "title": "Book appointment",
         "parent": "Renew passport", "evidence": "FACT"},
        {"kind": "TASK", "title": "Gather photo",
         "parent": "Renew passport", "evidence": "FACT"},
    ]
    analysis = _analyze(str(tmp_path), items)
    summary = analysis.to_dict()["summary"]
    assert summary["workstreams"] == 0
    assert summary["work_packages"] == 0
    assert summary["subtasks"] == 0
    # A concrete next action is still assigned without inventing levels.
    project = next(c for c in analysis.candidates if c.kind == "PROJECT")
    assert project.suggested_next_action.startswith("Start with:")


def test_complex_project_receives_deeper_hierarchy(tmp_path: object) -> None:
    _seed(tmp_path)
    items: list[dict[str, object]] = [
        {"kind": "PROJECT", "title": "Inventory Tool", "parent": None,
         "evidence": "FACT"},
        {"kind": "WORKSTREAM", "title": "Stock audit", "parent": "Inventory Tool",
         "evidence": "SUGGESTED", "needs_review": True},
        {"kind": "WORK_PACKAGE", "title": "Documents",
         "parent": "Stock audit", "evidence": "SUGGESTED",
         "needs_review": True},
        {"kind": "TASK", "title": "Collect stock counts", "parent": "Documents",
         "evidence": "SUGGESTED", "needs_review": True},
        {"kind": "SUBTASK", "title": "List stock records",
         "parent": "Collect stock counts", "evidence": "SUGGESTED",
         "needs_review": True},
        {"kind": "WORKSTREAM", "title": "Reorder planning",
         "parent": "Inventory Tool", "evidence": "SUGGESTED",
         "needs_review": True},
        {"kind": "WORK_PACKAGE", "title": "Reorder plan",
         "parent": "Reorder planning", "evidence": "SUGGESTED",
         "needs_review": True},
        {"kind": "TASK", "title": "Draft reorder plan",
         "parent": "Reorder plan", "evidence": "SUGGESTED",
         "needs_review": True},
    ]
    analysis = _analyze(str(tmp_path), items)
    summary = analysis.to_dict()["summary"]
    assert summary["workstreams"] == 2
    assert summary["work_packages"] == 2


def test_deliverables_attached_correctly(tmp_path: object) -> None:
    portfolio = _seed(tmp_path)
    items: list[dict[str, object]] = [
        {"kind": "PROJECT", "title": "Documentation Library", "parent": None,
         "evidence": "FACT", "source_text": "github portfolio"},
        {"kind": "WORKSTREAM", "title": "Packaging",
         "parent": "Documentation Library", "evidence": "INFERRED"},
        {"kind": "DELIVERABLE", "title": "Publishable project pages",
         "parent": "Packaging", "evidence": "INFERRED"},
        {"kind": "TASK", "title": "Improve README",
         "parent": "Publishable project pages", "evidence": "FACT"},
    ]
    analysis = _analyze(str(tmp_path), items)
    new_portfolio, _ = _import_all(analysis, portfolio)
    task = next(t for t in new_portfolio.tasks
                if t.title == "Improve README")
    assert task.workstream == "Packaging"
    assert task.deliverable == "Publishable project pages"


def test_dependency_suggestions_remain_unconfirmed(tmp_path: object) -> None:
    portfolio = _seed(tmp_path)
    items: list[dict[str, object]] = [
        {"kind": "PROJECT", "title": "Garden planning", "parent": None,
         "evidence": "FACT", "source_text": "garden planning"},
        {"kind": "TASK", "title": "Update the plans", "parent": "Garden planning",
         "evidence": "FACT"},
        {"kind": "TASK", "title": "Apply to roles", "parent": "Garden planning",
         "evidence": "FACT",
         "dependencies": [
             {"source": "Update the plans", "target": "Apply to roles",
              "rationale": "CV first", "evidence": "SUGGESTED",
              "confidence": 0.9},
         ]},
    ]
    analysis = _analyze(str(tmp_path), items)
    dep = next(c.suggested_dependencies[0]
               for c in analysis.candidates if c.suggested_dependencies)
    assert dep.source_title == "Update the plans"
    assert dep.target_title == "Apply to roles"
    assert dep.evidence == "SUGGESTED"
    assert dep.confidence == 0.9
    new_portfolio, _ = _import_all(analysis, portfolio)
    apply_task = next(t for t in new_portfolio.tasks
                      if t.title == "Apply to roles")
    # The suggested dependency is NOT turned into a confirmed task dependency.
    assert apply_task.dependencies == ()


def test_immediate_next_action_generation(tmp_path: object) -> None:
    _seed(tmp_path)
    # (a) a concrete LLM-supplied next action is preserved verbatim.
    items: list[dict[str, object]] = [
        {"kind": "PROJECT", "title": "Documentation Library", "parent": None,
         "evidence": "FACT",
         "suggested_next_action":
             "Inventory public GitHub repositories and mark each "
             "keep/private/archive"},
        {"kind": "TASK", "title": "Inventory repositories",
         "parent": "Documentation Library", "evidence": "FACT"},
    ]
    analysis = _analyze(str(tmp_path), items)
    project = next(c for c in analysis.candidates if c.kind == "PROJECT")
    assert project.suggested_next_action == (
        "Inventory public GitHub repositories and mark each "
        "keep/private/archive")

    # (b) a missing next action is derived deterministically from the first
    # task so it names a real, concrete step (never "work on project").
    items2: list[dict[str, object]] = [
        {"kind": "PROJECT", "title": "CRM Practice", "parent": None,
         "evidence": "FACT"},
        {"kind": "TASK", "title": "Configure daily review",
         "parent": "CRM Practice", "evidence": "FACT"},
    ]
    analysis2 = _analyze(str(tmp_path), items2)
    project2 = next(c for c in analysis2.candidates if c.kind == "PROJECT")
    assert project2.suggested_next_action == "Start with: Configure daily review"


def test_preview_retains_evidence_labels(tmp_path: object) -> None:
    _seed(tmp_path)
    items: list[dict[str, object]] = [
        {"kind": "AREA", "title": "Career", "parent": None,
         "evidence": "INFERRED"},
        {"kind": "PROJECT", "title": "Garden planning", "parent": "Career",
         "evidence": "FACT"},
        {"kind": "WORKSTREAM", "title": "Positioning",
         "parent": "Garden planning", "evidence": "SUGGESTED",
         "needs_review": True},
    ]
    analysis = _analyze(str(tmp_path), items)
    doc = analysis.to_dict()
    by_title = {c["title"]: c for c in doc["candidates"]}
    assert by_title["Career"]["evidence"] == "INFERRED"
    assert by_title["Garden planning"]["evidence"] == "FACT"
    assert by_title["Positioning"]["evidence"] == "SUGGESTED"
    # The hierarchy projection retains the same evidence labels per node.
    root = doc["hierarchy"]["roots"][0]
    assert root["evidence"] == "INFERRED"
    assert root["children"][0]["evidence"] == "FACT"
    assert root["children"][0]["children"][0]["evidence"] == "SUGGESTED"


def test_draft_round_trip_preserves_new_fields(tmp_path: object) -> None:
    _seed(tmp_path)
    items: list[dict[str, object]] = [
        {"kind": "PROJECT", "title": "Training", "parent": None,
         "evidence": "FACT",
         "suggested_next_action": "Gather certificates",
         "prerequisites": ["certificates"]},
        {"kind": "TASK", "title": "Gather certificates",
         "parent": "Training", "evidence": "FACT",
         "dependencies": [
             {"source": "Training", "target": "Gather certificates",
              "rationale": "scope first", "evidence": "SUGGESTED",
              "confidence": 0.6},
         ]},
    ]
    analysis = _analyze(str(tmp_path), items)
    draft_id = importer.save_draft(str(tmp_path), analysis)
    reloaded = importer.load_draft(str(tmp_path), draft_id)
    project = next(c for c in reloaded.candidates if c.kind == "PROJECT")
    assert project.suggested_next_action == "Gather certificates"
    assert project.prerequisites == ("certificates",)
    task = next(c for c in reloaded.candidates if c.kind == "TASK")
    assert task.suggested_dependencies[0].confidence == 0.6
    assert task.suggested_dependencies[0].evidence == "SUGGESTED"


def test_project_next_action_flows_to_first_task(tmp_path: object) -> None:
    portfolio = _seed(tmp_path)
    items: list[dict[str, object]] = [
        {"kind": "PROJECT", "title": "Documentation Library", "parent": None,
         "evidence": "FACT", "source_text": "github",
         "suggested_next_action": "Inventory public repositories"},
        {"kind": "TASK", "title": "Inventory repositories",
         "parent": "Documentation Library", "evidence": "FACT",
         "source_text": "inventory"},
        {"kind": "TASK", "title": "Select projects",
         "parent": "Documentation Library", "evidence": "FACT",
         "source_text": "select"},
    ]
    analysis = _analyze(str(tmp_path), items)
    new_portfolio, _ = _import_all(analysis, portfolio)
    tasks = {t.title: t for t in new_portfolio.tasks}
    # The project's concrete next action lands on the first task; later tasks
    # keep their own (source) next action rather than being overwritten.
    assert tasks["Inventory repositories"].next_action == \
        "Inventory public repositories"
    assert tasks["Select projects"].next_action == "select"
