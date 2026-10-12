"""Unit tests for the MVP dependency graph and readiness engine."""

from __future__ import annotations

from trajectory_os.mvp import graph, model, readiness


def _portfolio(tasks: tuple[model.Task, ...],
               projects: tuple[model.Project, ...] | None = None,
               resources: tuple[model.Resource, ...] = ()) -> model.Portfolio:
    projects = projects or (
        model.Project(project_id="p1", name="P1", objective="o"),
    )
    return model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t", projects=projects,
        tasks=tasks, resources=resources)


def test_downstream_and_transitive_unblocks() -> None:
    tasks = (
        model.Task(task_id="a", project_id="p1", title="a"),
        model.Task(task_id="b", project_id="p1", title="b",
                   dependencies=("a",)),
        model.Task(task_id="c", project_id="p1", title="c",
                   dependencies=("b",)),
    )
    g = graph.build_graph(_portfolio(tasks=tasks))
    assert g.downstream("a") == ("b",)
    assert g.transitive_unblocks("a") == 2
    assert g.transitive_unblocks("c") == 0


def test_topological_order_respects_dependencies() -> None:
    tasks = (
        model.Task(task_id="c", project_id="p1", title="c",
                   dependencies=("b",)),
        model.Task(task_id="b", project_id="p1", title="b",
                   dependencies=("a",)),
        model.Task(task_id="a", project_id="p1", title="a"),
    )
    g = graph.build_graph(_portfolio(tasks=tasks))
    order = g.topological_order()
    assert order.index("a") < order.index("b") < order.index("c")


def test_readiness_ready_and_blocked() -> None:
    tasks = (
        model.Task(task_id="a", project_id="p1", title="a"),
        model.Task(task_id="b", project_id="p1", title="b",
                   dependencies=("a",)),
    )
    states = {item.task_id: item for item in readiness.evaluate(
        _portfolio(tasks=tasks))}
    assert states["a"].ready is True
    assert states["a"].state == readiness.RS_READY
    assert states["b"].state == readiness.RS_BLOCKED
    assert any("dependency not complete" in reason
               for reason in states["b"].reasons)


def test_readiness_blocked_by_task() -> None:
    tasks = (
        model.Task(task_id="blocker", project_id="p1", title="blocker"),
        model.Task(task_id="victim", project_id="p1", title="victim",
                   blocked_by=("blocker",)),
    )
    states = {item.task_id: item for item in readiness.evaluate(
        _portfolio(tasks=tasks))}
    assert states["victim"].state == readiness.RS_BLOCKED
    assert states["victim"].blockers == ("blocker",)


def test_readiness_waiting_and_resource() -> None:
    tasks = (
        model.Task(task_id="w", project_id="p1", title="w",
                   waiting_for=("a reply",)),
        model.Task(task_id="r", project_id="p1", title="r",
                   resources=("tool",)),
    )
    resources = (model.Resource(resource_id="tool", name="Tool",
                                available=False),)
    states = {item.task_id: item for item in readiness.evaluate(
        _portfolio(tasks=tasks, resources=resources))}
    assert states["w"].state == readiness.RS_WAITING
    assert states["r"].state == readiness.RS_BLOCKED
    assert states["r"].missing_resources == ("tool",)


def test_readiness_project_closed() -> None:
    projects = (model.Project(project_id="p1", name="P1", objective="o",
                              status=model.PS_DEFERRED),)
    tasks = (model.Task(task_id="a", project_id="p1", title="a"),)
    states = {item.task_id: item for item in readiness.evaluate(
        _portfolio(tasks=tasks, projects=projects))}
    assert states["a"].state == readiness.RS_PROJECT_CLOSED
    assert states["a"].ready is False


def test_completed_task_not_ready() -> None:
    tasks = (model.Task(task_id="a", project_id="p1", title="a",
                        status=model.TS_COMPLETED),)
    states = readiness.evaluate(_portfolio(tasks=tasks))
    assert states[0].state == readiness.RS_COMPLETED
    assert states[0].ready is False
