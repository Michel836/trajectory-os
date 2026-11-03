"""Phase 2.1 — portfolio backups, restore and single-writer lock."""

from __future__ import annotations

import multiprocessing
import time
from dataclasses import replace

import pytest

from trajectory_os.mvp import model, mutations, store


def _seed(root: object, name: str = "safety") -> None:
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name=name,
        projects=(model.Project(project_id="p1", name="P1", objective="o"),),
        tasks=(model.Task(task_id="t1", project_id="p1", title="T1"),),
    ))


def test_backup_and_restore_roundtrip(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root, "original")
    backup = store.backup_portfolio(root, reason="manual")
    assert backup["name"].startswith("portfolio-")
    assert backup["size"] > 0

    changed = store.load_portfolio(root)
    assert changed is not None
    store.save_portfolio(root, replace(changed, name="changed"))

    result = store.restore_portfolio(root, name=str(backup["name"]))
    assert result["restored"] == backup["name"]
    assert result["portfolio"] == "original"
    assert result["pre_restore_backup"]
    restored = store.load_portfolio(root)
    assert restored is not None
    assert restored.name == "original"
    assert any(item["name"] == result["pre_restore_backup"]
               for item in store.list_backups(root))


def test_backup_retention_is_bounded(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    for index in range(5):
        store.backup_portfolio(root, reason=f"r{index}", keep=3)
    backups = store.list_backups(root)
    assert len(backups) == 3
    # Newest first: the three most recent reasons are kept.
    assert all("-r" in item["name"] for item in backups)


def test_maybe_daily_backup_is_idempotent(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    first = store.maybe_daily_backup(root)
    assert first is not None
    assert first["reason"] == "daily"
    assert store.maybe_daily_backup(root) is None


def test_restore_rejects_unknown_and_path_traversal(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with pytest.raises(model.MvpError):
        store.restore_portfolio(root, name="../portfolio.json")
    with pytest.raises(model.MvpError):
        store.restore_portfolio(root, name="does-not-exist.json")
    with pytest.raises(model.MvpError):
        store.restore_portfolio(root)


def test_portfolio_lock_is_reentrant(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with store.portfolio_lock(root), store.portfolio_lock(root):
        pass


def test_portfolio_lock_blocks_another_process(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    ctx = multiprocessing.get_context("fork")
    start = ctx.Event()
    acquired = ctx.Event()

    def child() -> None:
        start.wait(5)
        with store.portfolio_lock(root):
            acquired.set()

    process = ctx.Process(target=child)
    process.start()
    try:
        with store.portfolio_lock(root):
            start.set()
            time.sleep(0.3)
            assert not acquired.is_set()
        assert acquired.wait(5)
    finally:
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(2)
    assert process.exitcode == 0


def test_bulk_estimate_creates_a_backup(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    mutations.estimate_tasks(root, [
        {"task_id": "t1", "estimated_minutes": 30},
    ])
    reasons = {item["name"] for item in store.list_backups(root)}
    assert any("bulk-estimate" in name for name in reasons)
