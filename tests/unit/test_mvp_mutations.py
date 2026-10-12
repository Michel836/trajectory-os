"""Unit tests for MVP validated mutations (create/update/dependencies)."""

from __future__ import annotations

import pytest

from trajectory_os.mvp import model, mutations, readiness, store


def _seed(root: object) -> None:
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=(model.Project(project_id="p1", name="P1",
                                objective="objective one"),),
        tasks=(
            model.Task(task_id="a", project_id="p1", title="A",
                       estimated_minutes=30),
            model.Task(task_id="b", project_id="p1", title="B",
                       dependencies=("a",), estimated_minutes=20),
            model.Task(task_id="c", project_id="p1", title="C",
                       estimated_minutes=10),
        ),
    ))


def _root(tmp_path: object) -> str:
    return str(tmp_path)


# --- project mutations --------------------------------------------------------


def test_create_project_generates_slug(tmp_path: object) -> None:
    _seed(tmp_path)
    result = mutations.create_project(
        _root(tmp_path), {"name": "New Project", "objective": "Do it"})
    assert result["project_id"] == "new-project"
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    assert any(p.name == "New Project" for p in portfolio.projects)


def test_create_project_with_explicit_id(tmp_path: object) -> None:
    _seed(tmp_path)
    mutations.create_project(_root(tmp_path), {
        "project_id": "custom-id", "name": "Custom", "objective": "o",
        "status": model.PS_WAITING, "urgency": model.U_HIGH,
        "impact": model.I_HIGH, "deadline": "2030-01-01",
    })
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    project = portfolio.project_map()["custom-id"]
    assert project.status == model.PS_WAITING
    assert project.urgency == model.U_HIGH
    assert project.deadline == "2030-01-01"


def test_update_project_fields_and_defer(tmp_path: object) -> None:
    _seed(tmp_path)
    mutations.update_project(_root(tmp_path), "p1", {
        "name": "Renamed", "objective": "new objective",
        "impact": model.I_HIGH, "urgency": model.U_LOW,
        "deadline": "2031-05-05",
    })
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    project = portfolio.project_map()["p1"]
    assert project.name == "Renamed"
    assert project.objective == "new objective"
    assert project.impact == model.I_HIGH
    assert project.deadline == "2031-05-05"
    # defer (archive)
    mutations.update_project(_root(tmp_path), "p1",
                             {"status": model.PS_DEFERRED})
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    assert portfolio.project_map()["p1"].status == model.PS_DEFERRED
    # reactivate
    mutations.update_project(_root(tmp_path), "p1",
                             {"status": model.PS_ACTIVE})
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    assert portfolio.project_map()["p1"].status == model.PS_ACTIVE


def test_update_project_can_clear_deadline(tmp_path: object) -> None:
    _seed(tmp_path)
    mutations.update_project(_root(tmp_path), "p1",
                             {"deadline": "2030-01-01"})
    mutations.update_project(_root(tmp_path), "p1", {"deadline": ""})
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    assert portfolio.project_map()["p1"].deadline is None


# --- task mutations -----------------------------------------------------------


def test_create_task_with_defaults(tmp_path: object) -> None:
    _seed(tmp_path)
    result = mutations.create_task(_root(tmp_path), {
        "project_id": "p1", "title": "Write the thing",
        "estimated_minutes": 40,
    })
    assert result["project_id"] == "p1"
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    task = portfolio.task_map()[result["task_id"]]
    assert task.title == "Write the thing"
    assert task.status == model.TS_TODO
    assert task.estimated_minutes == 40


def test_update_task_fields(tmp_path: object) -> None:
    _seed(tmp_path)
    mutations.update_task(_root(tmp_path), "a", {
        "title": "A renamed", "status": model.TS_IN_PROGRESS,
        "estimated_minutes": 45, "deadline": "2030-02-02",
        "impact": model.I_HIGH, "urgency": model.U_CRITICAL,
        "next_action": "Open the editor",
        "waiting_for": ["a reply"], "blocked_by": [],
    })
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    task = portfolio.task_map()["a"]
    assert task.title == "A renamed"
    assert task.status == model.TS_IN_PROGRESS
    assert task.estimated_minutes == 45
    assert task.deadline == "2030-02-02"
    assert task.urgency == model.U_CRITICAL
    assert task.next_action == "Open the editor"
    assert task.waiting_for == ("a reply",)


def test_update_task_reassigns_project(tmp_path: object) -> None:
    _seed(tmp_path)
    mutations.create_project(_root(tmp_path),
                             {"name": "Other", "objective": "o"})
    mutations.update_task(_root(tmp_path), "a", {"project_id": "other"})
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    assert portfolio.task_map()["a"].project_id == "other"


def test_update_task_status_derives_project_completed(tmp_path: object) -> None:
    _seed(tmp_path)
    mutations.update_task(_root(tmp_path), "a",
                          {"status": model.TS_COMPLETED})
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    assert portfolio.project_map()["p1"].status == model.PS_ACTIVE
    mutations.update_task(_root(tmp_path), "b",
                          {"status": model.TS_COMPLETED})
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    assert portfolio.project_map()["p1"].status == model.PS_ACTIVE
    mutations.update_task(_root(tmp_path), "c",
                          {"status": model.TS_COMPLETED})
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    assert portfolio.project_map()["p1"].status == model.PS_COMPLETED


# --- dependency mutations -----------------------------------------------------


def test_add_and_remove_dependency(tmp_path: object) -> None:
    _seed(tmp_path)
    result = mutations.add_dependency(_root(tmp_path), "b", "a")
    assert result["added"] is False  # b already depends on a
    result = mutations.add_dependency(_root(tmp_path), "c", "a")
    assert result["added"] is True
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    assert "a" in portfolio.task_map()["c"].dependencies
    removed = mutations.remove_dependency(_root(tmp_path), "c", "a")
    assert removed["removed"] is True
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    assert "a" not in portfolio.task_map()["c"].dependencies


def test_remove_dependency_releases_task(tmp_path: object) -> None:
    _seed(tmp_path)
    # b is blocked by unfinished a; removing the edge makes b ready.
    before = {item.task_id: item for item in readiness.evaluate(
        store.load_portfolio(_root(tmp_path)))}  # type: ignore[arg-type]
    assert before["b"].ready is False
    mutations.remove_dependency(_root(tmp_path), "b", "a")
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    after = {item.task_id: item for item in readiness.evaluate(portfolio)}
    assert after["b"].ready is True


# --- invalid mutations fail closed --------------------------------------------


def test_create_task_unknown_project_fails(tmp_path: object) -> None:
    _seed(tmp_path)
    with pytest.raises(model.MvpError) as exc:
        mutations.create_task(_root(tmp_path),
                              {"project_id": "missing", "title": "x"})
    assert exc.value.code == model.E_UNKNOWN_PROJECT


def test_update_task_unknown_field_fails(tmp_path: object) -> None:
    _seed(tmp_path)
    with pytest.raises(model.MvpError) as exc:
        mutations.update_task(_root(tmp_path), "a", {"nonsense": 1})
    assert exc.value.code == model.E_MALFORMED


def test_update_task_invalid_status_fails(tmp_path: object) -> None:
    _seed(tmp_path)
    with pytest.raises(model.MvpError):
        mutations.update_task(_root(tmp_path), "a", {"status": "NOPE"})


def test_add_dependency_self_reference_fails(tmp_path: object) -> None:
    _seed(tmp_path)
    with pytest.raises(model.MvpError) as exc:
        mutations.add_dependency(_root(tmp_path), "a", "a")
    assert exc.value.code == model.E_SELF_REFERENCE


def test_add_dependency_cycle_fails(tmp_path: object) -> None:
    _seed(tmp_path)
    with pytest.raises(model.MvpError) as exc:
        mutations.add_dependency(_root(tmp_path), "a", "b")
    assert exc.value.code == model.E_DEPENDENCY_CYCLE


def test_create_project_duplicate_id_fails(tmp_path: object) -> None:
    _seed(tmp_path)
    with pytest.raises(model.MvpError) as exc:
        mutations.create_project(_root(tmp_path), {
            "project_id": "p1", "name": "Dup", "objective": "o"})
    assert exc.value.code == model.E_DUPLICATE_ID


def test_empty_title_fails(tmp_path: object) -> None:
    _seed(tmp_path)
    with pytest.raises(model.MvpError):
        mutations.create_task(_root(tmp_path),
                              {"project_id": "p1", "title": "  "})


def test_update_task_malformed_project_id_fails_closed(tmp_path: object) -> None:
    _seed(tmp_path)
    with pytest.raises(model.MvpError):
        mutations.update_task(_root(tmp_path), "a", {"project_id": 123})


# --- recomputation and persistence -------------------------------------------


def test_mutation_persists_after_reload(tmp_path: object) -> None:
    _seed(tmp_path)
    mutations.create_project(_root(tmp_path),
                             {"name": "Persisted", "objective": "o"})
    mutations.create_task(_root(tmp_path),
                          {"project_id": "persisted", "title": "Task X"})
    reloaded = store.load_portfolio(_root(tmp_path))
    assert reloaded is not None
    assert "persisted" in reloaded.project_map()
    assert any(t.project_id == "persisted" for t in reloaded.tasks)


def test_task_status_mutation_recomputes_downstream_readiness(
        tmp_path: object) -> None:
    _seed(tmp_path)
    # b depends on a (TODO), so b is blocked.
    before = readiness.evaluate(store.load_portfolio(_root(tmp_path)))  # type: ignore[arg-type]
    assert next(i for i in before if i.task_id == "b").ready is False
    # Completing a via a direct status edit recomputes readiness -> b ready.
    mutations.update_task(_root(tmp_path), "a",
                          {"status": model.TS_COMPLETED})
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    after = readiness.evaluate(portfolio)
    assert next(i for i in after if i.task_id == "b").ready is True


# --- duplicate protection on manual creation ----------------------------------


def _seed_french(tmp_path: object) -> None:
    store.save_portfolio(_root(tmp_path), model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=(
            model.Project(project_id="p1", name="Recherche emploi",
                          objective="trouver un poste"),
            model.Project(project_id="p2", name="Autre projet",
                          objective="autre"),
        ),
        tasks=(),
    ))


def test_create_task_unique_then_duplicate_conflicts(tmp_path: object) -> None:
    _seed_french(tmp_path)
    title = "Fixer 3 à 5 types de postes cibles"
    first = mutations.create_task(_root(tmp_path),
                                  {"project_id": "p1", "title": title})
    assert first["task_id"]
    with pytest.raises(model.MvpError) as exc:
        mutations.create_task(_root(tmp_path),
                              {"project_id": "p1", "title": title})
    assert exc.value.code == model.E_DUPLICATE_OBJECT
    duplicate = exc.value.metadata["duplicate"]
    assert duplicate["id"] == "task:" + first["task_id"]
    assert duplicate["type"] == "task"
    assert duplicate["title"] == title
    assert duplicate["project_id"] == "p1"
    # nothing was created by the rejected attempt
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    assert sum(1 for t in portfolio.tasks if t.project_id == "p1") == 1


def test_create_task_same_normalized_title_conflicts(tmp_path: object) -> None:
    _seed_french(tmp_path)
    mutations.create_task(_root(tmp_path),
                          {"project_id": "p1",
                           "title": "Préparer Portfolio APEC"})
    with pytest.raises(model.MvpError) as exc:
        mutations.create_task(_root(tmp_path),
                              {"project_id": "p1",
                               "title": "  preparer   portfolio apec "})
    assert exc.value.code == model.E_DUPLICATE_OBJECT


def test_create_task_same_title_other_project_allowed(tmp_path: object) -> None:
    _seed_french(tmp_path)
    title = "Fixer 3 à 5 types de postes cibles"
    mutations.create_task(_root(tmp_path),
                          {"project_id": "p1", "title": title})
    second = mutations.create_task(_root(tmp_path),
                                   {"project_id": "p2", "title": title})
    assert second["project_id"] == "p2"


def test_create_task_allow_duplicate_bypasses_and_keeps_id_unique(
        tmp_path: object) -> None:
    _seed_french(tmp_path)
    title = "Fixer 3 à 5 types de postes cibles"
    first = mutations.create_task(_root(tmp_path),
                                  {"project_id": "p1", "title": title})
    second = mutations.create_task(_root(tmp_path),
                                   {"project_id": "p1", "title": title,
                                    "allow_duplicate": True})
    assert second["task_id"] != first["task_id"]
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    assert sum(1 for t in portfolio.tasks if t.project_id == "p1") == 2


def test_create_project_same_normalized_name_conflicts(tmp_path: object) -> None:
    _seed_french(tmp_path)
    with pytest.raises(model.MvpError) as exc:
        mutations.create_project(_root(tmp_path), {
            "name": "  recherche   EMPLOI ", "objective": "o"})
    assert exc.value.code == model.E_DUPLICATE_OBJECT
    duplicate = exc.value.metadata["duplicate"]
    assert duplicate["id"] == "project:p1"
    assert duplicate["type"] == "project"


def test_create_project_allow_duplicate_bypasses(tmp_path: object) -> None:
    _seed_french(tmp_path)
    result = mutations.create_project(_root(tmp_path), {
        "name": "Recherche emploi", "objective": "o",
        "allow_duplicate": True})
    assert result["project_id"] != "p1"


def test_historical_duplicates_remain_loadable(tmp_path: object) -> None:
    # A portfolio created before this protection may already contain duplicates.
    store.save_portfolio(_root(tmp_path), model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=(
            model.Project(project_id="p1", name="Same", objective="o"),
            model.Project(project_id="p2", name="Same", objective="o"),
        ),
        tasks=(
            model.Task(task_id="a", project_id="p1", title="Dup"),
            model.Task(task_id="b", project_id="p1", title="Dup"),
        ),
    ))
    loaded = store.load_portfolio(_root(tmp_path))
    assert loaded is not None
    assert len(loaded.projects) == 2
    assert len(loaded.tasks) == 2


# --- project / task conversion & parking --------------------------------------


def test_park_project_moves_it_to_the_inbox(tmp_path: object) -> None:
    _seed(tmp_path)
    result = mutations.park_project(_root(tmp_path), "p1", {})
    assert result == {"project_id": "p1", "status": model.PS_IDEA,
                      "parked": True}
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    project = portfolio.project_map()["p1"]
    assert project.status == model.PS_IDEA
    assert project.open is False
    assert project.parked is True


def test_park_project_someday_and_unpark(tmp_path: object) -> None:
    _seed(tmp_path)
    mutations.park_project(_root(tmp_path), "p1",
                           {"kind": model.PS_SOMEDAY})
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    assert portfolio.project_map()["p1"].status == model.PS_SOMEDAY
    # An explicit status is how the project leaves the Inbox.
    result = mutations.park_project(_root(tmp_path), "p1",
                                    {"status": model.PS_ACTIVE})
    assert result["parked"] is False
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    assert portfolio.project_map()["p1"].status == model.PS_ACTIVE


def test_park_project_rejects_unknown_kind_and_project(
        tmp_path: object) -> None:
    _seed(tmp_path)
    with pytest.raises(model.MvpError) as exc:
        mutations.park_project(_root(tmp_path), "p1", {"kind": "BOGUS"})
    assert exc.value.code == model.E_MALFORMED
    with pytest.raises(model.MvpError) as exc:
        mutations.park_project(_root(tmp_path), "missing", {})
    assert exc.value.code == model.E_UNKNOWN_PROJECT


def test_parked_project_tasks_are_not_ready(tmp_path: object) -> None:
    _seed(tmp_path)
    mutations.park_project(_root(tmp_path), "p1", {})
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    states = {item.task_id: item for item in readiness.evaluate(portfolio)}
    assert states["a"].ready is False
    assert states["a"].state == readiness.RS_PROJECT_CLOSED


def test_promote_task_creates_project_and_moves_task(
        tmp_path: object) -> None:
    _seed(tmp_path)
    result = mutations.promote_task(_root(tmp_path), "a",
                                    {"name": "Alpha effort"})
    assert result["previous_project_id"] == "p1"
    new_id = str(result["project_id"])
    assert new_id != "p1"
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    assert sorted(p.project_id for p in portfolio.projects) == \
        sorted(["p1", new_id])
    assert portfolio.task_map()["a"].project_id == new_id
    assert portfolio.project_map()[new_id].name == "Alpha effort"
    # No data is lost: the original project still owns its other tasks.
    assert portfolio.task_map()["b"].project_id == "p1"


def test_promote_task_derives_name_from_title(tmp_path: object) -> None:
    _seed(tmp_path)
    result = mutations.promote_task(_root(tmp_path), "a", {})
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    assert portfolio.project_map()[str(result["project_id"])].name == "A"


def test_promote_task_rejects_unknown_task(tmp_path: object) -> None:
    _seed(tmp_path)
    with pytest.raises(model.MvpError) as exc:
        mutations.promote_task(_root(tmp_path), "missing", {})
    assert exc.value.code == model.E_UNKNOWN_ID


def test_promote_task_blocks_duplicate_project(tmp_path: object) -> None:
    _seed(tmp_path)
    with pytest.raises(model.MvpError) as exc:
        mutations.promote_task(_root(tmp_path), "a", {"name": "P1"})
    assert exc.value.code == model.E_DUPLICATE_OBJECT


def test_demote_project_moves_tasks_and_removes_project(
        tmp_path: object) -> None:
    _seed(tmp_path)
    promoted = mutations.promote_task(_root(tmp_path), "a", {})
    new_id = str(promoted["project_id"])
    result = mutations.demote_project(_root(tmp_path), new_id,
                                      {"target_project_id": "p1"})
    assert result == {"project_id": new_id, "target_project_id": "p1",
                      "moved_tasks": 1}
    portfolio = store.load_portfolio(_root(tmp_path))
    assert portfolio is not None
    assert [p.project_id for p in portfolio.projects] == ["p1"]
    assert portfolio.task_map()["a"].project_id == "p1"


def test_demote_project_validation(tmp_path: object) -> None:
    _seed(tmp_path)
    with pytest.raises(model.MvpError) as exc:
        mutations.demote_project(_root(tmp_path), "p1",
                                 {"target_project_id": "p1"})
    assert exc.value.code == model.E_MALFORMED
    with pytest.raises(model.MvpError) as exc:
        mutations.demote_project(_root(tmp_path), "p1",
                                 {"target_project_id": "missing"})
    assert exc.value.code == model.E_UNKNOWN_PROJECT
    with pytest.raises(model.MvpError) as exc:
        mutations.demote_project(_root(tmp_path), "missing",
                                 {"target_project_id": "p1"})
    assert exc.value.code == model.E_UNKNOWN_PROJECT
    with pytest.raises(model.MvpError) as exc:
        mutations.demote_project(_root(tmp_path), "p1", {})
    assert exc.value.code == model.E_MALFORMED
