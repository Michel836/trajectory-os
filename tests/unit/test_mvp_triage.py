"""Tests for deterministic triage suggestions (B1).

Suggestions are conservative and human-applied: a project-like large task,
a project that is really a task, or a project with no open task. Nothing is
ever applied automatically.
"""

from __future__ import annotations

from trajectory_os.mvp import model, triage


def _portfolio(projects: tuple[model.Project, ...],
               tasks: tuple[model.Task, ...]) -> model.Portfolio:
    return model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=projects, tasks=tasks)


def test_project_like_large_task_is_suggested_for_promotion() -> None:
    portfolio = _portfolio(
        projects=(model.Project(project_id="p1", name="P1",
                                objective="deliver the work"),),
        tasks=(model.Task(task_id="t1", project_id="p1",
                          title="Lancer un projet de refonte",
                          estimated_minutes=300),))
    actions = {s.action for s in triage.suggest(portfolio)}
    promote = next(s for s in triage.suggest(portfolio)
                   if s.action == triage.PROMOTE)
    assert promote.ref_id == "t1"
    assert triage.PROMOTE in actions


def test_small_or_plain_task_is_not_suggested() -> None:
    portfolio = _portfolio(
        projects=(model.Project(project_id="p1", name="P1", objective="o"),),
        tasks=(model.Task(task_id="t1", project_id="p1",
                          title="Appeler le dentiste",
                          estimated_minutes=10),))
    assert all(s.ref_id != "t1" for s in triage.suggest(portfolio))


def test_task_of_same_named_project_is_not_suggested() -> None:
    portfolio = _portfolio(
        projects=(model.Project(project_id="p1", name="Lancer un projet",
                                objective="o"),),
        tasks=(model.Task(task_id="t1", project_id="p1",
                          title="Lancer un projet",
                          estimated_minutes=300),))
    assert all(s.action != triage.PROMOTE for s in triage.suggest(portfolio))


def test_single_task_project_is_suggested_for_demotion() -> None:
    portfolio = _portfolio(
        projects=(model.Project(project_id="p1", name="P1", objective="P1"),),
        tasks=(model.Task(task_id="t1", project_id="p1", title="Do it"),))
    suggestion = next(s for s in triage.suggest(portfolio)
                      if s.action == triage.DEMOTE)
    assert suggestion.ref_id == "p1"


def test_project_with_objective_is_not_suggested_for_demotion() -> None:
    portfolio = _portfolio(
        projects=(model.Project(project_id="p1", name="P1",
                                objective="a distinct outcome"),),
        tasks=(model.Task(task_id="t1", project_id="p1", title="Do it"),))
    assert all(s.action != triage.DEMOTE for s in triage.suggest(portfolio))


def test_empty_project_is_suggested_for_parking() -> None:
    portfolio = _portfolio(
        projects=(model.Project(project_id="p1", name="Empty",
                                objective="o"),),
        tasks=())
    suggestion = next(s for s in triage.suggest(portfolio)
                      if s.action == triage.PARK)
    assert suggestion.ref_id == "p1"


def test_parked_project_is_not_suggested() -> None:
    portfolio = _portfolio(
        projects=(model.Project(project_id="p1", name="Idea",
                                objective="o", status=model.PS_IDEA),),
        tasks=())
    assert triage.suggest(portfolio) == ()
