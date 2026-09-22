"""Unit tests for the MVP seed dataset and CLI (end-to-end demo path)."""

from __future__ import annotations

from datetime import date

from trajectory_os.mvp import cli, dataset, model


def test_seed_has_representative_projects() -> None:
    portfolio = dataset.build_portfolio(today=date(2026, 9, 20))
    assert 10 <= len(portfolio.projects) <= 15
    assert len(portfolio.tasks) > 10
    # The seed exercises every layer: dependencies, blockers, waiting, resources.
    assert any(t.dependencies for t in portfolio.tasks)
    assert any(t.blocked_by for t in portfolio.tasks)
    assert any(t.waiting_for for t in portfolio.tasks)
    assert any(t.resources for t in portfolio.tasks)


def test_cli_init_plan_record_flow(tmp_path: object) -> None:
    root = str(tmp_path)
    assert cli.main(["init", "--root", root, "--today", "2026-09-20"]) == 0
    assert cli.main(["plan", "--root", root, "--today", "2026-09-20",
                     "--json"]) == 0
    assert cli.main(["record", "--root", root, "--task", "docs.index",
                     "--outcome", "COMPLETED", "--minutes", "200"]) == 0
    assert cli.main(["ready", "--root", root, "--today", "2026-09-20",
                     "--json"]) == 0
    assert cli.main(["blocked", "--root", root, "--today", "2026-09-20",
                     "--json"]) == 0
    assert cli.main(["wbs", "--root", root, "--json"]) == 0


def test_cli_demo_writes_html(tmp_path: object) -> None:
    root = str(tmp_path / "demo")
    assert cli.main(["demo", "--root", root, "--today", "2026-09-20"]) == 0
    from pathlib import Path

    assert (Path(root) / "portfolio.json").is_file()
    assert (Path(root) / "cockpit.html").is_file()
    assert (Path(root) / "cockpit.json").is_file()


def test_cli_load_validates(tmp_path: object) -> None:
    root = str(tmp_path)
    source = tmp_path / "portfolio.json"
    source.write_text(
        '{"schema_version": 1, "name": "x", "projects": [], "tasks": []}',
        encoding="utf-8")
    assert cli.main(["load", "--root", root, str(source)]) == 0
    loaded = dataset_module_load(root)
    assert loaded is not None


def dataset_module_load(root: str) -> model.Portfolio | None:
    from trajectory_os.mvp import store
    return store.load_portfolio(root)
