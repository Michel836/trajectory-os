"""Tests for the decisions register (C2)."""

from __future__ import annotations

import pytest

from trajectory_os.mvp import model, mutations, store


def _seed(root: str) -> None:
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=(model.Project(project_id="p1", name="P1", objective="o"),),
        tasks=()))


def test_create_and_decide_decision(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    created = mutations.create_decision(root, {
        "title": "Choose a hosting provider", "question": "Which host?",
        "options": ["A", "B"], "project_id": "p1"})
    decision_id = str(created["decision_id"])
    assert created["status"] == model.DS_OPEN
    result = mutations.decide(root, decision_id, {
        "choice": "A", "rationale": "cheaper and local"})
    assert result["status"] == model.DS_DECIDED
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    decision = portfolio.decisions[0]
    assert decision.choice == "A"
    assert decision.rationale == "cheaper and local"
    assert decision.settled is True
    assert decision.decided_at  # stamped on settle


def test_settled_decision_requires_a_choice(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with pytest.raises(model.MvpError):
        mutations.create_decision(root, {
            "title": "x", "status": model.DS_DECIDED})


def test_decision_project_link_must_exist(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with pytest.raises(model.MvpError) as exc:
        mutations.create_decision(root, {
            "title": "x", "project_id": "missing"})
    assert exc.value.code == model.E_UNKNOWN_PROJECT


def test_deferred_decision_is_not_settled(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    created = mutations.create_decision(root, {
        "title": "Revisit vendor", "status": model.DS_DEFERRED})
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    assert portfolio.decisions[0].settled is False
    assert created["status"] == model.DS_DEFERRED


def test_delete_decision(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    created = mutations.create_decision(root, {"title": "x"})
    mutations.delete_decision(root, str(created["decision_id"]))
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    assert portfolio.decisions == ()


def test_portfolio_without_decisions_loads() -> None:
    data = model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=(model.Project(project_id="p1", name="P", objective="o"),),
        tasks=()).to_dict()
    data.pop("decisions")
    assert model.Portfolio.from_dict(data).decisions == ()
