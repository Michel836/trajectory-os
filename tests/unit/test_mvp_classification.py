"""Tests for the shared, deterministic classification contract (ADR-033).

The taxonomy and the text classifier are the single source of truth used by
both Quick Capture and the document importer. These tests pin the mapping from
a taxonomy kind to its durable entity, the re-export by ``importer``, and the
conservative behaviour of ``classify_text`` (explicit markers win; a line with
no signal is reported as ambiguous, never asserted).
"""

from __future__ import annotations

from trajectory_os.mvp import classification, importer


def test_taxonomy_entity_mapping() -> None:
    assert classification.entity_of(classification.PROJECT) == "project"
    assert classification.entity_of(classification.AREA) == "project"
    assert classification.entity_of(classification.IDEA) == "project"
    assert classification.entity_of(classification.SOMEDAY) == "project"
    assert classification.entity_of(classification.TASK) == "task"
    assert classification.entity_of(classification.SUBTASK) == "task"
    assert classification.entity_of(classification.WORKSTREAM) == "grouping"
    assert classification.entity_of(classification.REFERENCE) == "note"


def test_inbox_kinds_are_project_like() -> None:
    assert frozenset({
        classification.IDEA, classification.SOMEDAY}) == \
        classification.INBOX_KINDS
    assert classification.INBOX_KINDS <= classification.PROJECT_LIKE
    for kind in classification.INBOX_KINDS:
        assert classification.is_inbox(kind) is True
        assert classification.is_project_like(kind) is True
    assert classification.is_inbox(classification.PROJECT) is False


def test_importer_reexports_the_same_taxonomy() -> None:
    # The importer keeps module-level names for backward compatibility, but
    # they must be the very same object as the shared contract.
    assert importer.PROJECT_LIKE is classification.PROJECT_LIKE
    assert importer.TASK_LIKE is classification.TASK_LIKE
    assert importer.KINDS == classification.KINDS
    assert importer.normalize_text is classification.normalize_text


def test_normalize_text() -> None:
    assert classification.normalize_text("Préparer  Portfolio APEC") == \
        "preparer portfolio apec"
    assert classification.normalize_text("  Refonte du site ! ") == \
        "refonte du site"


def test_classify_project_from_marker() -> None:
    result = classification.classify_text("Refonte du site web")
    assert result.kind == classification.PROJECT
    assert result.entity == "project"
    assert result.ambiguous is False
    assert result.inbox is False


def test_classify_task_from_action_verb() -> None:
    result = classification.classify_text("Appeler le dentiste")
    assert result.kind == classification.TASK
    assert result.entity == "task"
    assert result.inbox is False


def test_classify_someday_and_idea_are_parked() -> None:
    someday = classification.classify_text("Someday voyager au Japon")
    assert someday.kind == classification.SOMEDAY
    assert someday.inbox is True
    idea = classification.classify_text("Idée de podcast tech")
    assert idea.kind == classification.IDEA
    assert idea.inbox is True


def test_someday_wins_over_project_marker() -> None:
    # A vague future notion is parked, never turned into a project.
    result = classification.classify_text("Someday lancer un projet de ferme")
    assert result.kind == classification.SOMEDAY
    assert result.inbox is True


def test_classify_without_signal_is_ambiguous() -> None:
    result = classification.classify_text("Le grand livre rouge")
    assert result.kind == classification.TASK  # conservative default
    assert result.ambiguous is True
    assert result.confidence < 0.5
    assert result.reasons


def test_classify_empty_title_is_ambiguous() -> None:
    result = classification.classify_text("   ")
    assert result.ambiguous is True
    assert result.confidence == 0.0
    assert result.to_dict()["reasons"]


def test_classification_to_dict_shape() -> None:
    payload = classification.classify_text("Lancer un projet").to_dict()
    assert set(payload) == {
        "kind", "entity", "confidence", "ambiguous", "inbox", "reasons"}
    assert isinstance(payload["reasons"], list)
