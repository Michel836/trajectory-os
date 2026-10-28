"""Phase 5.1 — project health and estimation coverage."""

from __future__ import annotations

from datetime import date

from trajectory_os.mvp import model, readiness, store, visualization


def _portfolio(project: model.Project, tasks: tuple[model.Task, ...]
               ) -> model.Portfolio:
    return model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="health",
        projects=(project,), tasks=tasks)


def _health(portfolio: model.Portfolio, today: date = date(2026, 1, 10)
            ) -> str:
    readiness_map = {item.task_id: item
                     for item in readiness.evaluate(portfolio)}
    project = portfolio.projects[0]
    return visualization.project_health(
        project, portfolio.tasks, readiness_map, today, portfolio.capacity)


def test_project_health_labels() -> None:
    empty = _portfolio(model.Project(project_id="p", name="P",
                                     objective="o"), ())
    assert _health(empty) == visualization.HEALTH_NO_NEXT_ACTION

    done = _portfolio(
        model.Project(project_id="p", name="P", objective="o"),
        (model.Task(task_id="t", project_id="p", title="T",
                    status=model.TS_COMPLETED),))
    assert _health(done) == visualization.HEALTH_DONE

    at_risk = _portfolio(
        model.Project(project_id="p", name="P", objective="o",
                      deadline="2026-01-12"),
        (model.Task(task_id="t", project_id="p", title="T"),))
    assert _health(at_risk) == visualization.HEALTH_AT_RISK

    stale = _portfolio(
        model.Project(project_id="p", name="P", objective="o"),
        (model.Task(task_id="t", project_id="p", title="T",
                    updated_at="2025-01-01T00:00:00Z"),))
    assert _health(stale) == visualization.HEALTH_STALE

    overloaded = _portfolio(
        model.Project(project_id="p", name="P", objective="o"),
        (model.Task(task_id="t1", project_id="p", title="T1",
                    estimated_minutes=1440,
                    updated_at="2026-01-09T00:00:00Z"),
         model.Task(task_id="t2", project_id="p", title="T2",
                    estimated_minutes=1440,
                    updated_at="2026-01-09T00:00:00Z")))
    assert _health(overloaded) == visualization.HEALTH_OVERLOADED

    on_track = _portfolio(
        model.Project(project_id="p", name="P", objective="o"),
        (model.Task(task_id="t", project_id="p", title="T",
                    estimated_minutes=30,
                    updated_at="2026-01-09T00:00:00Z"),))
    assert _health(on_track) == visualization.HEALTH_ON_TRACK


def test_views_expose_project_health_and_estimation_metrics(
        tmp_path: object) -> None:
    root = str(tmp_path)
    store.save_portfolio(root, _portfolio(
        model.Project(project_id="p", name="P", objective="o",
                      deadline="2026-01-12"),
        (model.Task(task_id="t", project_id="p", title="T"),)))
    views = visualization.build_views(root, today=date(2026, 1, 10))
    project = next(obj for obj in views["objects"]
                   if obj["kind"] == "project")
    assert project["health"] == visualization.HEALTH_AT_RISK
    health = views["portfolio_health"]
    assert health["counts"]["open_tasks"] == 1
    assert health["counts"]["open_tasks_with_estimate"] == 0
    assert health["project_health"][visualization.HEALTH_AT_RISK] == 1
    assert "AT_RISK" in visualization.PROJECT_HEALTH


def test_explain_project_is_deterministic_and_complete(
        tmp_path: object) -> None:
    from trajectory_os.mvp import changes, insights, signals

    root = str(tmp_path)
    store.save_portfolio(root, _portfolio(
        model.Project(project_id="p", name="P", objective="o",
                      deadline="2026-01-12"),
        (model.Task(task_id="t", project_id="p", title="T"),)))
    signals.create_signal(root, text="Échéance 15/11/2026.", source_ref="mail",
                          project_hint="p")
    changes.append_change(
        root, kind=changes.KIND_UPDATE, object_kind=changes.OBJECT_TASK,
        object_id="t", title="T", summary="estimation mise à jour",
        before={"estimated_minutes": None},
        after={"estimated_minutes": 30})
    explanation = insights.explain_project(root, "p",
                                           today=date(2026, 1, 10))
    assert explanation["canonical"] is False
    assert explanation["health"] == visualization.HEALTH_AT_RISK
    assert explanation["next_actions"]
    assert explanation["recent_changes"]
    assert explanation["signals"]


def test_explain_endpoint_and_ui() -> None:
    import tempfile

    from test_mvp_dashboard_api import _request, _server

    from trajectory_os.mvp import cockpit_ui

    root = tempfile.mkdtemp(prefix="phase5-explain-")
    store.save_portfolio(root, _portfolio(
        model.Project(project_id="p", name="P", objective="o"),
        (model.Task(task_id="t", project_id="p", title="T"),)))
    with _server(root) as port:
        status, result = _request(port, "GET", "/api/projects/p/explain")
        assert status == 200
        assert result["kind"] == "project_explanation"
        assert result["project"]["project_id"] == "p"
    js = cockpit_ui.PAGE_JS
    for marker in ("explainProject", "renderExplain", "explain-overlay",
                   "closeExplain"):
        assert marker in js, marker


def test_assistant_deterministic_and_grounded_local(tmp_path: object) -> None:
    from trajectory_os.mvp import assistant

    root = str(tmp_path)
    store.save_portfolio(root, _portfolio(
        model.Project(project_id="p", name="P", objective="o",
                      deadline="2026-01-12"),
        (model.Task(task_id="t", project_id="p", title="T"),)))
    request = assistant.AssistantRequest(
        kind=assistant.KIND_PROJECT, project_id="p")
    deterministic = assistant.run_assistant(
        root, request, backend=assistant.DeterministicBackend(),
        today=date(2026, 1, 10))
    assert deterministic["engine"] == assistant.ENGINE_DETERMINISTIC
    assert "P" in deterministic["answer"]
    assert deterministic["citations"]

    class FakeLlm:
        def complete_json(self, system, user, schema):
            return {"answer": "Résumé local", "citations": ["project"],
                    "unknowns": []}

    local = assistant.run_assistant(
        root, request, backend=assistant.LocalLlmBackend(FakeLlm()),
        today=date(2026, 1, 10))
    assert local["engine"] == assistant.ENGINE_LOCAL
    assert local["answer"] == "Résumé local"
    assert local["citations"] == ["project"]

    class InvalidLlm:
        def complete_json(self, system, user, schema):
            return {"answer": "Hallucination", "citations": ["missing"],
                    "unknowns": []}

    fallback = assistant.run_assistant(
        root, request, backend=assistant.LocalLlmBackend(InvalidLlm()),
        today=date(2026, 1, 10))
    assert fallback["deterministic_fallback"] is True
    assert fallback["engine"] == assistant.ENGINE_DETERMINISTIC


def test_assistant_weekly_and_endpoints(tmp_path: object) -> None:
    from test_mvp_dashboard_api import _request, _server

    from trajectory_os.mvp import assistant, cockpit_ui

    root = str(tmp_path)
    store.save_portfolio(root, _portfolio(
        model.Project(project_id="p", name="P", objective="o"),
        (model.Task(task_id="t", project_id="p", title="T"),)))
    weekly = assistant.run_assistant(
        root, assistant.AssistantRequest(kind=assistant.KIND_WEEKLY))
    assert "Revue hebdomadaire" in weekly["answer"]
    with _server(root) as port:
        status, result = _request(port, "POST", "/api/assistant/weekly", {})
        assert status == 200
        assert "answer" in result
        status, result = _request(port, "POST", "/api/assistant/project", {
            "project_id": "p"})
        assert status == 200
        assert "answer" in result
    js = cockpit_ui.PAGE_JS
    for marker in ("openAssistantProject", "openAssistantWeekly",
                   "assistant-overlay", "renderAssistantAnswer"):
        assert marker in js, marker
