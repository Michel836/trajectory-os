"""Time-of-day aware capacity (V0 active capacity model).

A day's ``capacity`` is the nominal whole-day budget. When the plan is built
for *today*, the scheduler must only expose and pack the capacity that remains
inside the explicit working window, so a cockpit rebuilt at 15:00 never claims
the whole day is still available. Future days keep the nominal budget. These
tests pin the end-to-end contract across the scheduler, the view payload, the
text renderer and the cockpit UI wiring.
"""

from __future__ import annotations

from datetime import date, datetime

from trajectory_os.mvp import (
    cockpit_ui,
    engine,
    model,
    render,
    store,
    visualization,
)

TODAY = date(2026, 10, 12)
AFTERNOON = datetime(2026, 10, 12, 15, 0)


def _portfolio() -> model.Portfolio:
    tasks = tuple(
        model.Task(task_id=f"t{i}", project_id="p1", title=f"task {i}",
                   estimated_minutes=60) for i in range(10))
    return model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=(model.Project(project_id="p1", name="P", objective="o"),),
        tasks=tasks,
        capacity=model.Capacity(minutes_per_day=360, buffer_ratio=0.30),
    )


def _seed(root: str) -> None:
    store.save_portfolio(root, _portfolio())


def test_build_views_exposes_remaining_capacity_for_today(
        tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    views = visualization.build_views(root, today=TODAY, now=AFTERNOON)
    plan = views["today_plan"]
    assert plan["is_today"] is True
    assert plan["total_capacity"] == 360
    assert plan["plan_capacity"] == 252  # nominal, preserved
    assert plan["remaining_plan_capacity"] == 84
    assert plan["remaining_buffer_minutes"] == 36
    assert plan["planned_minutes"] == 60


def test_build_views_keeps_future_days_nominal(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    views = visualization.build_views(root, today=TODAY, now=AFTERNOON)
    days = views["heatmap"]["planned_load"]["days"]
    assert days[0]["is_today"] is True
    assert days[0]["remaining_plan_capacity"] == 84
    assert days[1]["is_today"] is False
    assert days[1]["remaining_plan_capacity"] == days[1]["plan_capacity"]
    assert days[1]["plan_capacity"] == 252


def test_build_cockpit_keeps_future_days_nominal(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    cockpit = engine.build_cockpit(root, today=TODAY, now=AFTERNOON,
                                   persist=False)
    days = cockpit.week_plan["days"]
    assert days[0]["remaining_plan_capacity"] == 84
    assert days[1]["is_today"] is False
    assert days[1]["remaining_plan_capacity"] == days[1]["plan_capacity"]
    assert days[1]["plan_capacity"] == 252


def test_cockpit_remaining_capacity_is_injected_and_persisted_read_only(
        tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    cockpit = engine.build_cockpit(root, today=TODAY, now=AFTERNOON,
                                   persist=False)
    assert cockpit.today_plan["remaining_plan_capacity"] == 84
    # A cockpit rebuilt later the same day has strictly less capacity left.
    later = engine.build_cockpit(root, today=TODAY,
                                 now=datetime(2026, 10, 12, 16, 0),
                                 persist=False)
    assert later.today_plan["remaining_plan_capacity"] < 84


def test_render_text_reports_remaining_capacity(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    cockpit = engine.build_cockpit(root, today=TODAY, now=AFTERNOON,
                                   persist=False)
    text = render.render_text(cockpit)
    assert "remaining 1h24m" in text
    assert "planned 1h / 1h24m" in text


def test_cockpit_ui_uses_the_shared_capacity_label() -> None:
    source = cockpit_ui.PAGE_JS
    assert "function capacityLabel(" in source
    for renderer in ("renderSummary", "renderToday"):
        start = source.index("function " + renderer + "(")
        end = source.index("\nfunction ", start + 1)
        assert "capacityLabel(" in source[start:end], renderer
    # The today view must show the remaining ceiling, not the nominal one.
    label = source.index("function capacityLabel(")
    label_end = source.index("\nfunction ", label + 1)
    block = source[label:label_end]
    assert "remaining_plan_capacity" in block
    assert "remaining_buffer_minutes" in block
