"""Tests for on-demand AI enrichment: SUGGESTED, reviewable, never auto-applied."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from trajectory_os.mvp import enrichment, model, store


def _seed(root: object) -> model.Portfolio:
    portfolio = model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=(model.Project(project_id="git", name="Documentation Library",
                                objective="Publish a portfolio"),),
        tasks=(
            model.Task(task_id="update-cv", project_id="git",
                       title="Update the plans"),
            model.Task(task_id="apply", project_id="git",
                       title="Apply to roles"),
        ),
    )
    store.save_portfolio(root, portfolio)
    return portfolio


def _llm_for(result: dict[str, Any]):
    def _llm(system: str, user: str) -> Mapping[str, Any]:
        del system, user
        return result
    return _llm


def test_enrichment_returns_suggested_and_does_not_mutate(
        tmp_path: object) -> None:
    _seed(tmp_path)
    before = store.load_portfolio(str(tmp_path))
    llm = _llm_for({"suggestions": [
        {"kind": "NEXT_ACTION", "title": "Audit public repositories",
         "detail": "start with the public ones"}]})
    result = enrichment.generate(
        str(tmp_path), "git", enrichment.ENRICH_NEXT_ACTIONS,
        llm=llm, engine="local")  # type: ignore[arg-type]
    assert result.engine == "local"
    assert len(result.suggestions) == 1
    assert result.suggestions[0].evidence == enrichment.SUGGESTED
    assert result.suggestions[0].state == enrichment.PENDING
    # Nothing entered the portfolio automatically.
    assert store.load_portfolio(str(tmp_path)) == before
    # It is stored separately as a sidecar.
    stored = enrichment.load_suggestions(str(tmp_path), "git")
    assert len(stored) == 1


def test_generate_wbs_stays_separate_from_portfolio(tmp_path: object) -> None:
    _seed(tmp_path)
    llm = _llm_for({"suggestions": [
        {"kind": "WORKSTREAM", "title": "Packaging", "detail": "group"},
        {"kind": "TASK", "title": "Improve READMEs", "detail": "docs"}]})
    result = enrichment.generate(
        str(tmp_path), "git", enrichment.ENRICH_WBS, llm=llm,
        engine="local")  # type: ignore[arg-type]
    assert {s.kind for s in result.suggestions} == {"WORKSTREAM", "TASK"}
    portfolio = store.load_portfolio(str(tmp_path))
    assert portfolio is not None
    assert len(portfolio.tasks) == 2  # unchanged


def test_accept_next_action_creates_task(tmp_path: object) -> None:
    _seed(tmp_path)
    llm = _llm_for({"suggestions": [
        {"kind": "NEXT_ACTION", "title": "Audit public repositories",
         "detail": "start with the public ones"}]})
    result = enrichment.generate(
        str(tmp_path), "git", enrichment.ENRICH_NEXT_ACTIONS, llm=llm,
        engine="local")  # type: ignore[arg-type]
    sid = result.suggestions[0].suggestion_id
    outcome = enrichment.accept(str(tmp_path), "git", sid)
    assert outcome["portfolio_changed"] is True
    portfolio = store.load_portfolio(str(tmp_path))
    assert portfolio is not None
    created = next(t for t in portfolio.tasks
                   if t.title == "Audit public repositories")
    assert created.next_action == "Audit public repositories"
    stored = enrichment.load_suggestions(str(tmp_path), "git")
    assert stored[0].state == enrichment.ACCEPTED


def test_accept_wbs_creates_workstream_task(tmp_path: object) -> None:
    _seed(tmp_path)
    llm = _llm_for({"suggestions": [
        {"kind": "WORKSTREAM", "title": "Packaging", "detail": "group"}]})
    result = enrichment.generate(
        str(tmp_path), "git", enrichment.ENRICH_WBS, llm=llm,
        engine="local")  # type: ignore[arg-type]
    enrichment.accept(str(tmp_path), "git", result.suggestions[0].suggestion_id)
    portfolio = store.load_portfolio(str(tmp_path))
    assert portfolio is not None
    created = next(t for t in portfolio.tasks if t.title == "Packaging")
    assert created.workstream == "Packaging"


def test_reject_does_not_change_portfolio(tmp_path: object) -> None:
    _seed(tmp_path)
    before = store.load_portfolio(str(tmp_path))
    llm = _llm_for({"suggestions": [
        {"kind": "DELIVERABLE", "title": "Public portfolio page",
         "detail": "visible proof"}]})
    result = enrichment.generate(
        str(tmp_path), "git", enrichment.ENRICH_DELIVERABLES, llm=llm,
        engine="local")  # type: ignore[arg-type]
    enrichment.reject(
        str(tmp_path), "git", result.suggestions[0].suggestion_id)
    assert store.load_portfolio(str(tmp_path)) == before
    stored = enrichment.load_suggestions(str(tmp_path), "git")
    assert stored[0].state == enrichment.REJECTED


def test_dependency_accept_uses_existing_tasks(tmp_path: object) -> None:
    _seed(tmp_path)
    llm = _llm_for({"suggestions": [
        {"kind": "DEPENDENCY", "title": "Update the plans before applying",
         "detail": "CV first", "source_title": "Update the plans",
         "target_title": "Apply to roles"}]})
    result = enrichment.generate(
        str(tmp_path), "git", enrichment.ENRICH_DEPENDENCIES, llm=llm,
        engine="local")  # type: ignore[arg-type]
    enrichment.accept(str(tmp_path), "git", result.suggestions[0].suggestion_id)
    portfolio = store.load_portfolio(str(tmp_path))
    assert portfolio is not None
    apply_task = next(t for t in portfolio.tasks if t.task_id == "apply")
    assert apply_task.dependencies == ("update-cv",)


def test_dependency_accept_fails_if_task_missing(tmp_path: object) -> None:
    _seed(tmp_path)
    llm = _llm_for({"suggestions": [
        {"kind": "DEPENDENCY", "title": "Missing link", "detail": "x",
         "source_title": "No such task", "target_title": "Apply to roles"}]})
    result = enrichment.generate(
        str(tmp_path), "git", enrichment.ENRICH_DEPENDENCIES, llm=llm,
        engine="local")  # type: ignore[arg-type]
    with pytest.raises(enrichment.EnrichmentError):
        enrichment.accept(
            str(tmp_path), "git", result.suggestions[0].suggestion_id)


def test_unknown_enrichment_kind_rejected(tmp_path: object) -> None:
    _seed(tmp_path)
    with pytest.raises(enrichment.EnrichmentError):
        enrichment.generate(str(tmp_path), "git", "make_coffee",
                            llm=_llm_for({"suggestions": []}),
                            engine="local")  # type: ignore[arg-type]


def test_enrichment_rejects_deepseek_pro(tmp_path: object) -> None:
    _seed(tmp_path)
    with pytest.raises(enrichment.EnrichmentError):
        enrichment.generate(
            str(tmp_path), "git", enrichment.ENRICH_WBS,
            llm=_llm_for({"suggestions": []}), engine="deepseek-pro")


def test_enrichment_rejects_wrong_kind(tmp_path: object) -> None:
    _seed(tmp_path)
    llm = _llm_for({"suggestions": [
        {"kind": "DELIVERABLE", "title": "Nope", "detail": ""}]})
    with pytest.raises(enrichment.EnrichmentError):
        enrichment.generate(
            str(tmp_path), "git", enrichment.ENRICH_NEXT_ACTIONS, llm=llm,
            engine="local")  # type: ignore[arg-type]
