"""Focused tests for the Week + Tomorrow planning sidecar.

The planning layer is presentation-only: it lives in ``planning.json`` and must
never mutate the validated portfolio. These tests pin the Tomorrow plan
contract, the 3..5 weekly-outcome contract, the morning Today handoff, the
explicit-only carry-over rule and reload persistence.
"""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta

import pytest

from trajectory_os.mvp import dashboard, model, planning, store, visualization

MONDAY = date(2026, 1, 5)
TUESDAY = date(2026, 1, 6)
WEDNESDAY = date(2026, 1, 7)
NEXT_MONDAY = date(2026, 1, 12)


def _seed(root: str) -> None:
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="planning-test",
        projects=(
            model.Project(project_id="p1", name="Website",
                          objective="Launch the site",
                          urgency=model.U_HIGH, impact=model.I_HIGH),
            model.Project(project_id="p2", name="Garden",
                          objective="Prepare the beds"),
        ),
        tasks=(
            model.Task(task_id="a", project_id="p1", title="Write copy",
                       estimated_minutes=30, urgency=model.U_HIGH,
                       impact=model.I_HIGH,
                       deadline=(MONDAY + timedelta(days=2)).isoformat()),
            model.Task(task_id="b", project_id="p1", title="Design hero",
                       estimated_minutes=60),
            model.Task(task_id="c", project_id="p1", title="Ship site",
                       estimated_minutes=45, dependencies=("a",)),
            model.Task(task_id="d", project_id="p2", title="Buy seeds",
                       estimated_minutes=15),
            model.Task(task_id="e", project_id="p2", title="Water beds",
                       estimated_minutes=20),
        ),
    ))


def _save_plan(root: str, *, day: str = TUESDAY.isoformat(),
               main: tuple[str, ...] = ("a", "b", "c"),
               optional: tuple[str, ...] = ("d",)) -> planning.PlanningState:
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    state = planning.load_planning(root)
    state = planning.plan_tomorrow(
        state, portfolio, day=day, main_task_ids=main,
        optional_task_ids=optional, saved_at=planning.utc_timestamp())
    return planning.save_planning(root, state)


def _portfolio_bytes(root: str) -> bytes:
    return store.default_portfolio_path(root).read_bytes()


@contextmanager
def _server(root: str) -> Iterator[int]:
    app = dashboard.Dashboard(root)
    server = dashboard.create_server(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(port: int, method: str, path: str,
             body: dict[str, object] | None = None,
             ) -> tuple[int, dict[str, object]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    payload = json.dumps(body) if body is not None else None
    headers = {"Content-Type": "application/json"} if payload else {}
    conn.request(method, path, body=payload, headers=headers)
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        data = {"_raw": raw.decode("utf-8", "replace")}
    return response.status, data


# --- A — save Tomorrow plan ---------------------------------------------------


def test_a_save_tomorrow_plan(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    state = _save_plan(root)
    assert state.tomorrow_plan is not None
    assert state.tomorrow_plan.date == TUESDAY.isoformat()
    assert state.tomorrow_plan.main_task_ids == ("a", "b", "c")
    assert state.tomorrow_plan.optional_task_ids == ("d",)
    # reloaded from disk independently
    assert planning.load_planning(root).tomorrow_plan == state.tomorrow_plan


# --- B — 3 main + up to 2 optional validation ---------------------------------


def test_b_tomorrow_bounds_and_validation(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    state = planning.empty_planning()

    # exactly 3 main + 2 optional is allowed
    ok = planning.plan_tomorrow(
        state, portfolio, day=TUESDAY.isoformat(),
        main_task_ids=("a", "b", "c"), optional_task_ids=("d", "e"))
    assert ok.tomorrow_plan is not None

    # 4 main rejected
    with pytest.raises(planning.PlanningError):
        planning.plan_tomorrow(state, portfolio, day=TUESDAY.isoformat(),
                               main_task_ids=("a", "b", "c", "d"),
                               optional_task_ids=())

    # 3 optional rejected
    with pytest.raises(planning.PlanningError):
        planning.plan_tomorrow(state, portfolio, day=TUESDAY.isoformat(),
                               main_task_ids=("a",),
                               optional_task_ids=("b", "c", "d"))

    # duplicate across main/optional rejected
    with pytest.raises(planning.PlanningError):
        planning.plan_tomorrow(state, portfolio, day=TUESDAY.isoformat(),
                               main_task_ids=("a",),
                               optional_task_ids=("a", "b"))

    # unknown task rejected
    with pytest.raises(planning.PlanningError):
        planning.plan_tomorrow(state, portfolio, day=TUESDAY.isoformat(),
                               main_task_ids=("zzz",),
                               optional_task_ids=())

    # terminal task rejected
    completed = model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="planning-test",
        projects=portfolio.projects,
        tasks=tuple(
            model.Task.from_dict({**t.to_dict(),
                                  "status": model.TS_COMPLETED})
            if t.task_id == "a" else t for t in portfolio.tasks))
    with pytest.raises(planning.PlanningError):
        planning.plan_tomorrow(state, completed, day=TUESDAY.isoformat(),
                               main_task_ids=("a",),
                               optional_task_ids=())

    # empty selection clears the plan
    cleared = planning.plan_tomorrow(
        ok, portfolio, day=TUESDAY.isoformat(),
        main_task_ids=(), optional_task_ids=())
    assert cleared.tomorrow_plan is None


# --- C — morning Today handoff ------------------------------------------------


def test_c_morning_today_handoff(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    _save_plan(root)
    views = visualization.build_views(root, today=TUESDAY)
    handoff = views["today_handoff"]
    assert handoff["active"] is True
    assert [item["task_id"] for item in handoff["main"]] == ["a", "b", "c"]
    assert [item["task_id"] for item in handoff["optional"]] == ["d"]
    # not shown once the planned day is past
    stale = visualization.build_views(root, today=WEDNESDAY)["today_handoff"]
    assert stale["active"] is False


# --- D — planned task becomes blocked ----------------------------------------


def test_d_planned_task_becomes_blocked(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    _save_plan(root)
    # the human blocks a planned task in the portfolio (external change)
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    blocked = model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="planning-test",
        projects=portfolio.projects,
        tasks=tuple(
            model.Task.from_dict({**t.to_dict(), "status": model.TS_BLOCKED})
            if t.task_id == "a" else t for t in portfolio.tasks))
    store.save_portfolio(root, blocked)
    handoff = visualization.build_views(root, today=TUESDAY)["today_handoff"]
    item = next(i for i in handoff["main"] if i["task_id"] == "a")
    assert item["blocked"] is True
    assert item["ready"] is False
    assert item["blocked_reason"]
    assert handoff["blocked_count"] >= 1


# --- E — no portfolio mutation from planning ---------------------------------


def test_e_planning_never_mutates_portfolio(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    before = _portfolio_bytes(root)
    state = _save_plan(root)
    state = planning.plan_week(
        state, week_start=planning.monday_of(MONDAY).isoformat(),
        outcomes=[
            {"title": "Ship landing page", "project_id": "p1"},
            {"title": "Prepare garden", "project_id": "p2"},
            {"title": "Clear inbox"},
        ])
    planning.save_planning(root, state)
    outcome_id = state.week_plan.outcomes[0].outcome_id  # type: ignore[union-attr]
    state = planning.set_outcome_state(state, outcome_id,
                                       planning.OUTCOME_DONE)
    state = planning.carry_outcomes(state, [outcome_id],
                                    new_week_start=NEXT_MONDAY.isoformat())
    planning.save_planning(root, state)
    visualization.build_views(root, today=MONDAY)
    planning.build_review(root, today=MONDAY)
    planning.build_tomorrow_view(root, today=MONDAY, target=TUESDAY)
    assert _portfolio_bytes(root) == before


# --- F — unfinished task does not auto-rollover ------------------------------


def test_f_unfinished_does_not_auto_rollover(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    _save_plan(root, day=MONDAY.isoformat(), main=("a", "b", "c"),
               optional=())
    review = planning.build_review(root, today=MONDAY)
    assert {item["task_id"] for item in review["carry_candidates"]} >= {
        "a", "b", "c"}
    # reviewing does not create tomorrow's plan
    state = planning.load_planning(root)
    assert state.tomorrow_plan is not None
    assert state.tomorrow_plan.date == MONDAY.isoformat()
    assert planning.build_tomorrow_view(
        root, today=MONDAY, target=TUESDAY)["saved_plan"] is None


# --- G — weekly plan save/load ------------------------------------------------


def test_g_week_plan_save_and_load(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    week_start = planning.monday_of(MONDAY).isoformat()
    state = planning.plan_week(
        planning.empty_planning(), week_start=week_start,
        outcomes=[
            {"title": "Ship landing page", "project_id": "p1",
             "note": "copy first"},
            {"title": "Prepare garden", "project_id": "p2"},
            {"title": "Weekly review"},
        ], note="deep work week")
    planning.save_planning(root, state)
    loaded = planning.load_planning(root)
    assert loaded.week_plan is not None
    assert loaded.week_plan.note == "deep work week"
    assert [o.title for o in loaded.week_plan.outcomes] == [
        "Ship landing page", "Prepare garden", "Weekly review"]
    view = planning.build_week_view(root, today=MONDAY)
    assert view["outcome_count"] == 3
    assert view["week_end"] == (MONDAY + timedelta(days=6)).isoformat()
    assert view["outcomes"][0]["project"] == "Website"
    assert "tasks" in view["outcomes"][0]["progress_signal"]


# --- H — 3..5 weekly outcomes validation -------------------------------------


def test_h_week_outcome_bounds(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    week_start = planning.monday_of(MONDAY).isoformat()
    state = planning.empty_planning()
    for count in (1, 2, 6):
        with pytest.raises(planning.PlanningError):
            planning.plan_week(state, week_start=week_start,
                               outcomes=[{"title": f"o{i}"}
                                         for i in range(count)])
    for count in (3, 4, 5):
        planned = planning.plan_week(
            state, week_start=week_start,
            outcomes=[{"title": f"o{i}"} for i in range(count)])
        assert planned.week_plan is not None
        assert len(planned.week_plan.outcomes) == count
    # empty clears the week plan
    cleared = planning.plan_week(
        state, week_start=week_start, outcomes=[])
    assert cleared.week_plan is None


# --- I — weekly carry requires human action ----------------------------------


def test_i_carry_requires_human_action(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    week_start = planning.monday_of(MONDAY).isoformat()
    state = planning.plan_week(
        planning.empty_planning(), week_start=week_start,
        outcomes=[{"title": "o1"}, {"title": "o2"}, {"title": "o3"}])
    planning.save_planning(root, state)
    outcome_id = state.week_plan.outcomes[0].outcome_id  # type: ignore[union-attr]

    # merely looking at the next week does not carry anything
    next_week = planning.build_week_view(
        root, today=NEXT_MONDAY, week_start=NEXT_MONDAY.isoformat())
    assert next_week["outcome_count"] == 0
    assert planning.load_planning(root).week_plan.week_start == week_start  # type: ignore[union-attr]

    # an explicit human carry creates the next week's draft
    carried = planning.carry_outcomes(state, [outcome_id],
                                      new_week_start=NEXT_MONDAY.isoformat())
    assert carried.week_plan is not None
    assert carried.week_plan.week_start == NEXT_MONDAY.isoformat()
    assert carried.week_plan.outcomes[0].carried_from == week_start
    assert carried.week_plan.outcomes[0].state == planning.OUTCOME_OPEN

    # closing is also explicit
    closed = planning.set_outcome_state(
        state, outcome_id, planning.OUTCOME_DONE)
    assert closed.week_plan is not None
    assert closed.week_plan.outcome_by_id(outcome_id).state \
        == planning.OUTCOME_DONE


# --- J — reload persistence ---------------------------------------------------


def test_j_reload_persistence(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    _save_plan(root)
    state = planning.load_planning(root)
    state = planning.plan_week(
        state, week_start=planning.monday_of(MONDAY).isoformat(),
        outcomes=[{"title": "o1"}, {"title": "o2"}, {"title": "o3"}])
    planning.save_planning(root, state)
    reloaded = planning.load_planning(root)
    assert reloaded.to_dict() == state.to_dict()
    # survives a fresh Dashboard instance (same data root)
    app = dashboard.Dashboard(root)
    view = app.planning_view()
    assert view["planning"] == state.to_dict()
    assert view["planning"]["tomorrow_plan"]["date"] == TUESDAY.isoformat()


# --- K — Today baseline preserved below planned tasks ------------------------


def test_k_today_baseline_preserved(tmp_path: object) -> None:
    from trajectory_os.mvp import priority, scheduler

    root = str(tmp_path)
    _seed(root)
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    ranked = priority.rank_ready_tasks(portfolio, today=TUESDAY)
    expected = [item.task_id
                for item in scheduler.plan_day(portfolio, ranked,
                                               TUESDAY).planned]
    _save_plan(root)
    views = visualization.build_views(root, today=TUESDAY)
    baseline = [item["task_id"] for item in views["today_plan"]["planned"]]
    assert baseline == expected
    assert views["today_handoff"]["active"] is True
    assert len(views["today_handoff"]["main"]) == 3


# --- L — synthetic end-of-day review flow ------------------------------------


def test_l_end_of_day_review_flow(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    _save_plan(root, day=TUESDAY.isoformat(), main=("a", "b"),
               optional=("d",))
    # record a completed outcome for task a
    from trajectory_os.mvp import outcomes

    outcomes.record_and_save(root, task_id="a",
                             outcome=model.O_COMPLETED,
                             actual_minutes=25,
                             recorded_at=TUESDAY.isoformat() + "T09:00:00Z")
    review = planning.build_review(root, today=TUESDAY)
    assert "a" in {item["task_id"] for item in review["completed"]}
    unfinished = {item["task_id"] for item in review["unfinished_planned"]}
    assert "b" in unfinished and "d" in unfinished
    carry = {item["task_id"] for item in review["carry_candidates"]}
    assert "b" in carry and "d" in carry
    assert review["weekly_outcomes"] == []

    # the user decides: carry b as main, d as optional
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    state = planning.load_planning(root)
    state = planning.plan_tomorrow(
        state, portfolio, day=WEDNESDAY.isoformat(),
        main_task_ids=("b",), optional_task_ids=("d",))
    planning.save_planning(root, state)
    assert planning.load_planning(root).tomorrow_plan.date \
        == WEDNESDAY.isoformat()
    assert planning.build_tomorrow_view(
        root, today=TUESDAY, target=WEDNESDAY)["saved_plan"] is not None


# --- HTTP wiring --------------------------------------------------------------


def test_planning_http_roundtrip(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    real_today = date.today()
    real_tomorrow = real_today + timedelta(days=1)
    week_start = planning.monday_of(real_today).isoformat()
    with _server(root) as port:
        status, result = _request(port, "POST", "/api/planning/tomorrow", {
            "date": real_tomorrow.isoformat(),
            "main_task_ids": ["a", "b", "c"],
            "optional_task_ids": ["d"],
        })
        assert status == 200
        assert result["planning"]["tomorrow_plan"]["main_task_ids"] == [
            "a", "b", "c"]
        status, result = _request(port, "POST", "/api/planning/week", {
            "week_start": week_start,
            "outcomes": [{"title": "o1"}, {"title": "o2"},
                         {"title": "o3"}],
        })
        assert status == 200
        assert result["week"]["outcome_count"] == 3
        _, planning_view = _request(port, "GET", "/api/planning")
        assert planning_view["tomorrow"]["saved_plan"]["date"] \
            == real_tomorrow.isoformat()
        _, week_view = _request(port, "GET", "/api/week")
        assert week_view["status"] == "OK"
        assert week_view["outcome_count"] == 3
        _, review = _request(port, "GET", "/api/review")
        assert review["status"] == "OK"
        # invalid bounds are rejected with the fail-closed error code
        status, result = _request(port, "POST", "/api/planning/week", {
            "week_start": week_start,
            "outcomes": [{"title": "only one"}],
        })
        assert status == 409
        assert result["error"] == model.E_MALFORMED


def test_review_exposes_weekly_signals(tmp_path: object) -> None:
    root = str(tmp_path)
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=(
            model.Project(project_id="p1", name="P1", objective="o"),
            model.Project(project_id="p2", name="P2", objective="o"),
        ),
        tasks=(
            model.Task(task_id="t1", project_id="p1", title="Due soon",
                       estimated_minutes=60,
                       deadline=(MONDAY + timedelta(days=1)).isoformat()),
        ),
    ))
    store.append_outcome(root, model.OutcomeRecord(
        task_id="t1", outcome=model.O_COMPLETED,
        recorded_at="2026-01-06T10:00:00Z", actual_minutes=90))
    review = planning.build_review(root, today=MONDAY)
    assert [item["task_id"] for item in review["at_risk"]] == ["t1"]
    # p1 has a ready task and must NOT be flagged; p2 has none and must be.
    assert {p["project_id"] for p in review["stale_projects"]} == {"p2"}
    # Estimation accuracy reflects the ledger: 60 estimated, 90 actual.
    assert review["estimation"]["completed_with_actual"] == 1
    assert review["estimation"]["mean_absolute_error_minutes"] == 30.0
