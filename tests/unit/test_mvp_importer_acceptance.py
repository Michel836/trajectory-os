"""Acceptance test: a flat multi-page brain-dump becomes an execution-ready WBS.

The source document is deliberately flat (headings + bullets). The fake LLM
stands in for the improved DeepSeek semantic engine and returns the deep
execution hierarchy the prompt now requires: AREA → PROJECT → WORKSTREAM →
WORK_PACKAGE → TASK → SUBTASK / DELIVERABLE, with next actions, prerequisites
and suggested dependencies. The test pins the *pipeline* behaviour: the
hierarchy is preserved, AI-added structure stays SUGGESTED, and the summary
counts expose every level.
"""

from __future__ import annotations

from pathlib import Path

from trajectory_os.mvp import importer, model, store

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / \
    "multipage-brain-dump.txt"

# Simulated semantic-engine response for the flat fixture (see fixture document).
_SIMULATED_ITEMS: list[dict[str, object]] = [
    # --- areas -----------------------------------------------------------------
    {"kind": "AREA", "title": "Community & learning", "parent": None,
     "evidence": "INFERRED"},
    {"kind": "AREA", "title": "Consulting practice", "parent": None,
     "evidence": "INFERRED"},
    {"kind": "AREA", "title": "Finance & planning", "parent": None,
     "evidence": "INFERRED"},
    {"kind": "AREA", "title": "Civic & admin", "parent": None,
     "evidence": "INFERRED"},
    {"kind": "AREA", "title": "Home lab & knowledge", "parent": None,
     "evidence": "INFERRED"},

    # --- Community Garden Planner (complex, deep) -------------------------------
    {"kind": "PROJECT", "title": "Community Garden Planner",
     "parent": "Community & learning",
     "evidence": "FACT", "source_text": "build the planning tool",
     "suggested_next_action": "Define the plot and volunteer data model",
     "prerequisites": ["repository access", "Python environment"]},
    {"kind": "WORKSTREAM", "title": "Plot data model",
     "parent": "Community Garden Planner",
     "evidence": "SUGGESTED", "needs_review": True},
    {"kind": "WORK_PACKAGE", "title": "Garden entities",
     "parent": "Plot data model", "evidence": "SUGGESTED", "needs_review": True},
    {"kind": "TASK", "title": "Define plot and volunteer entities",
     "parent": "Garden entities", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "SUBTASK", "title": "List fields per record",
     "parent": "Define plot and volunteer entities", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "DELIVERABLE", "title": "Validated garden data model",
     "parent": "Plot data model", "evidence": "SUGGESTED", "needs_review": True},
    {"kind": "WORKSTREAM", "title": "Calendar import",
     "parent": "Community Garden Planner", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "WORK_PACKAGE", "title": "Schedule importer",
     "parent": "Calendar import", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "TASK", "title": "Wire the schedule importer",
     "parent": "Schedule importer", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "DELIVERABLE", "title": "Working calendar import",
     "parent": "Calendar import", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "WORKSTREAM", "title": "Planning board & ranking",
     "parent": "Community Garden Planner", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "WORK_PACKAGE", "title": "Daily board",
     "parent": "Planning board & ranking", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "TASK", "title": "Build the planning board",
     "parent": "Daily board", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "TASK", "title": "Rank open tasks", "parent": "Daily board",
     "evidence": "SUGGESTED", "needs_review": True},
    {"kind": "DELIVERABLE", "title": "Usable planning board",
     "parent": "Planning board & ranking", "evidence": "SUGGESTED",
     "needs_review": True},

    # --- Fictional Consulting Portfolio (complex, deep) ------------------------
    {"kind": "PROJECT", "title": "Fictional Consulting Portfolio",
     "parent": "Consulting practice",
     "evidence": "FACT", "source_text": "assemble the portfolio",
     "suggested_next_action": "Collect the sample case studies",
     "prerequisites": ["access to sample documents"]},
    {"kind": "WORKSTREAM", "title": "Portfolio audit",
     "parent": "Fictional Consulting Portfolio", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "WORK_PACKAGE", "title": "Case studies",
     "parent": "Portfolio audit", "evidence": "SUGGESTED", "needs_review": True},
    {"kind": "TASK", "title": "Collect sample case studies",
     "parent": "Case studies", "evidence": "SUGGESTED", "needs_review": True},
    {"kind": "TASK", "title": "Review the service descriptions",
     "parent": "Portfolio audit", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "DELIVERABLE", "title": "Complete portfolio draft",
     "parent": "Portfolio audit", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "WORKSTREAM", "title": "Packaging",
     "parent": "Fictional Consulting Portfolio", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "WORK_PACKAGE", "title": "Proposal template",
     "parent": "Packaging", "evidence": "SUGGESTED", "needs_review": True},
    {"kind": "TASK", "title": "Organise the proposal template",
     "parent": "Proposal template", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "DELIVERABLE", "title": "Reusable proposal pack",
     "parent": "Packaging", "evidence": "SUGGESTED",
     "needs_review": True},

    # --- Language-Learning App (complex, deep) ---------------------------------
    {"kind": "PROJECT", "title": "Language-Learning App",
     "parent": "Community & learning", "evidence": "FACT",
     "source_text": "create the app",
     "suggested_next_action": "Design the lesson screen",
     "prerequisites": ["design sketch ready"]},
    {"kind": "WORKSTREAM", "title": "Lesson design",
     "parent": "Language-Learning App", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "WORK_PACKAGE", "title": "Screens & flow",
     "parent": "Lesson design", "evidence": "SUGGESTED", "needs_review": True},
    {"kind": "TASK", "title": "Design the lesson screen",
     "parent": "Screens & flow", "evidence": "SUGGESTED", "needs_review": True},
    {"kind": "DELIVERABLE", "title": "Approved lesson flow",
     "parent": "Lesson design", "evidence": "SUGGESTED", "needs_review": True},
    {"kind": "WORKSTREAM", "title": "Vocabulary review",
     "parent": "Language-Learning App", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "WORK_PACKAGE", "title": "Review list",
     "parent": "Vocabulary review", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "TASK", "title": "Build the vocabulary review flow",
     "parent": "Review list", "evidence": "SUGGESTED", "needs_review": True},
    {"kind": "WORKSTREAM", "title": "Progress tracking",
     "parent": "Language-Learning App", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "TASK", "title": "Add progress tracking",
     "parent": "Progress tracking", "evidence": "SUGGESTED",
     "needs_review": True,
     "dependencies": [
         {"source": "Design the lesson screen", "target": "Add progress tracking",
          "rationale": "lesson flow before progress", "evidence": "SUGGESTED",
          "confidence": 0.9}]},
    {"kind": "DELIVERABLE", "title": "Working study app",
     "parent": "Progress tracking", "evidence": "SUGGESTED",
     "needs_review": True},

    # --- Public Conference Preparation (complex, deep) -------------------------
    {"kind": "PROJECT", "title": "Public Conference Preparation",
     "parent": "Knowledge & docs", "evidence": "FACT",
     "source_text": "prepare the talk",
     "suggested_next_action": "Write the talk abstract",
     "prerequisites": ["conference account"]},
    {"kind": "WORKSTREAM", "title": "Talk audit",
     "parent": "Public Conference Preparation", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "WORK_PACKAGE", "title": "Session outline",
     "parent": "Talk audit", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "TASK", "title": "Outline the session",
     "parent": "Session outline", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "TASK", "title": "Identify reusable material",
     "parent": "Session outline", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "DELIVERABLE", "title": "Session outline draft",
     "parent": "Talk audit", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "WORKSTREAM", "title": "Material selection", "parent":
     "Public Conference Preparation", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "WORK_PACKAGE", "title": "Slide shortlist",
     "parent": "Material selection", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "TASK", "title": "Select the core slides",
     "parent": "Slide shortlist", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "WORKSTREAM", "title": "Packaging", "parent":
     "Public Conference Preparation", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "WORK_PACKAGE", "title": "Documentation",
     "parent": "Packaging", "evidence": "SUGGESTED", "needs_review": True},
    {"kind": "TASK", "title": "Improve the speaker notes",
     "parent": "Documentation", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "TASK", "title": "Add screenshots or demos",
     "parent": "Documentation", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "DELIVERABLE", "title": "Publishable talk materials",
     "parent": "Packaging", "evidence": "SUGGESTED", "needs_review": True},

    # --- Home Lab Backup (moderate) --------------------------------------------
    {"kind": "PROJECT", "title": "Home Lab Backup",
     "parent": "Home lab & knowledge",
     "evidence": "FACT", "source_text": "set up the backup routine",
     "suggested_next_action": "Configure the snapshot schedule"},
    {"kind": "WORKSTREAM", "title": "Backup routine", "parent": "Home Lab Backup",
     "evidence": "SUGGESTED", "needs_review": True},
    {"kind": "WORK_PACKAGE", "title": "Snapshot policy",
     "parent": "Backup routine", "evidence": "SUGGESTED", "needs_review": True},
    {"kind": "TASK", "title": "Configure the snapshot schedule",
     "parent": "Snapshot policy", "evidence": "SUGGESTED", "needs_review": True},
    {"kind": "TASK", "title": "Document the restore procedure",
     "parent": "Backup routine", "evidence": "SUGGESTED", "needs_review": True},
    {"kind": "DELIVERABLE", "title": "Working backup routine",
     "parent": "Backup routine", "evidence": "SUGGESTED", "needs_review": True},

    # --- Sample Training Application (moderate) --------------------------------
    {"kind": "PROJECT", "title": "Sample Training Application",
     "parent": "Community & learning", "evidence": "FACT",
     "source_text": "apply for the programme",
     "suggested_next_action": "Gather the sample certificates",
     "prerequisites": ["sample certificates"]},
    {"kind": "WORKSTREAM", "title": "Application", "parent":
     "Sample Training Application", "evidence": "SUGGESTED",
     "needs_review": True},
    {"kind": "WORK_PACKAGE", "title": "Certificates",
     "parent": "Application", "evidence": "SUGGESTED", "needs_review": True},
    {"kind": "TASK", "title": "Gather sample certificates",
     "parent": "Certificates", "evidence": "SUGGESTED", "needs_review": True},
    {"kind": "TASK", "title": "Shortlist the course options",
     "parent": "Application", "evidence": "SUGGESTED", "needs_review": True},
    {"kind": "TASK", "title": "Submit the application form",
     "parent": "Application", "evidence": "SUGGESTED", "needs_review": True,
     "dependencies": [
         {"source": "Gather sample certificates",
          "target": "Submit the application form",
          "rationale": "certificates required", "evidence": "SUGGESTED",
          "confidence": 0.85}]},
    {"kind": "DELIVERABLE", "title": "Submitted training application",
     "parent": "Application", "evidence": "SUGGESTED", "needs_review": True},

    # --- simple / flat projects --------------------------------------------------
    {"kind": "PROJECT", "title": "Generic Administrative Renewal",
     "parent": "Civic & admin", "evidence": "FACT",
     "source_text": "renew the document"},
    {"kind": "TASK", "title": "Collect the renewal forms",
     "parent": "Generic Administrative Renewal", "evidence": "FACT"},
    {"kind": "TASK", "title": "Book an appointment", "parent": "Generic "
     "Administrative Renewal", "evidence": "FACT"},
    {"kind": "PROJECT", "title": "Fictional Budget Planning",
     "parent": "Finance & planning", "evidence": "FACT",
     "source_text": "monthly budget"},
    {"kind": "TASK", "title": "List the budget categories", "parent":
     "Fictional Budget Planning", "evidence": "FACT"},
    {"kind": "PROJECT", "title": "Sample Equipment Maintenance",
     "parent": "Home lab & knowledge", "evidence": "FACT",
     "source_text": "equipment maintenance"},
    {"kind": "TASK", "title": "Inventory the devices", "parent":
     "Sample Equipment Maintenance", "evidence": "FACT"},
    {"kind": "PROJECT", "title": "Open-Source Documentation Library",
     "parent": "Home lab & knowledge", "evidence": "FACT",
     "source_text": "documentation library"},
    {"kind": "TASK", "title": "Choose the documentation tooling",
     "parent": "Open-Source Documentation Library", "evidence": "FACT"},
    {"kind": "PROJECT", "title": "Sample Certification Portfolio",
     "parent": "Consulting practice", "evidence": "FACT",
     "source_text": "certification portfolio"},
    {"kind": "TASK", "title": "List completed certifications",
     "parent": "Sample Certification Portfolio", "evidence": "FACT"},

    # --- someday / ideas --------------------------------------------------------
    {"kind": "SOMEDAY", "title": "Learn to play the ukulele", "parent": None,
     "evidence": "FACT"},
    {"kind": "SOMEDAY", "title": "Repaint the garden shed", "parent": None,
     "evidence": "FACT"},
    {"kind": "IDEA", "title": "Plant-swap mobile app", "parent": None,
     "evidence": "FACT"},
]


def _fake_deepseek(system: str, user: str) -> dict[str, object]:
    """Simulated DeepSeek: returns the full interpretation once.

    ``analyze_document`` calls the LLM once per chunk; a real LLM returns the
    items for *that* chunk only. The counter keeps the simulation faithful:
    one complete interpretation, no per-chunk duplication.
    """
    del system, user
    _fake_deepseek.calls += 1  # type: ignore[attr-defined]
    if _fake_deepseek.calls == 1:  # type: ignore[attr-defined]
        return {"items": _SIMULATED_ITEMS}
    return {"items": []}


_fake_deepseek.calls = 0  # type: ignore[attr-defined]


def test_flat_document_yields_execution_ready_wbs(tmp_path: object) -> None:
    _fake_deepseek.calls = 0  # type: ignore[attr-defined]
    portfolio = model.Portfolio(schema_version=model.SCHEMA_VERSION,
                                name="t", projects=(), tasks=())
    store.save_portfolio(tmp_path, portfolio)
    data = FIXTURE.read_bytes()
    analysis = importer.analyze_document(str(tmp_path), FIXTURE.name, data,
                                         mode="semantic", llm=_fake_deepseek)  # type: ignore[arg-type]
    summary = analysis.to_dict()["summary"]
    assert summary["areas"] >= 3
    assert summary["projects"] >= 10
    assert summary["workstreams"] >= 10
    assert summary["work_packages"] >= 6
    assert summary["subtasks"] >= 1
    assert summary["deliverables"] >= 8
    assert summary["ai_suggested"] >= 30
    assert summary["next_actions"] >= 6
    titles = {c.title for c in analysis.candidates}
    for required in ("Community Garden Planner",
                     "Fictional Consulting Portfolio",
                     "Language-Learning App", "Public Conference Preparation",
                     "Home Lab Backup", "Sample Training Application"):
        assert required in titles
    # Every AI-added item is SUGGESTED and needs review.
    for candidate in analysis.candidates:
        if candidate.evidence == "SUGGESTED":
            assert candidate.needs_review is True
