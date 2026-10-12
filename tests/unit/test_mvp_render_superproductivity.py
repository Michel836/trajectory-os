"""Unit tests for MVP cockpit rendering and Super Productivity export."""

from __future__ import annotations

from trajectory_os.mvp import (
    dataset,
    engine,
    priority,
    render,
    superproductivity,
)


def _cockpit(tmp_path: object) -> engine.Cockpit:
    root = str(tmp_path)
    dataset.write_seed(root)
    return engine.build_cockpit(root)


def test_render_text_has_required_sections(tmp_path: object) -> None:
    text = render.render_text(_cockpit(tmp_path))
    for section in ("PORTFOLIO", "TOP READY TASKS", "TODAY",
                    "BLOCKED / WAITING", "PROJECTS",
                    "PLANNED VS ACTUAL"):
        assert section in text


def test_render_html_is_complete(tmp_path: object) -> None:
    html = render.render_html(_cockpit(tmp_path))
    assert html.startswith("<!doctype html>")
    assert "</html>" in html
    assert "Today" in html
    assert "Blocked / waiting" in html
    assert "Projects" in html


def test_sp_document_shape(tmp_path: object) -> None:
    root = str(tmp_path)
    dataset.write_seed(root)
    from trajectory_os.mvp import store

    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    ranked = priority.rank_ready_tasks(portfolio)
    document = superproductivity.build_sp_document(portfolio, ranked[:3])
    assert document["project"]["title"] == "TrajectoryOS"
    assert len(document["tasks"]) == 3
    assert all("id" in task and "title" in task
               for task in document["tasks"])


def test_export_ready_writes_file(tmp_path: object) -> None:
    root = str(tmp_path)
    dataset.write_seed(root)
    target = str(tmp_path / "sp.json")
    result = superproductivity.export_ready(root, target=target, limit=3)
    assert result["exported"] == 3
    from pathlib import Path

    assert Path(target).is_file()
