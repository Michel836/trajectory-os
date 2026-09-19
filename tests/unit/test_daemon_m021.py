"""M021 — pure daemon model/store unit tests (fail closed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from trajectory_os.daemon import engine, model
from trajectory_os.daemon import store as daemon_store
from trajectory_os.portfolio import model as portfolio_model


def _cycle(number: int = 1, decision_id: str = "a" * 64) -> model.DaemonCycle:
    return model.DaemonCycle(
        cycle=number, portfolio_id="p-1", decision_id=decision_id,
        status="DISPATCHED", selected=("g-a",), completed_goals=(),
        created_at="2026-01-01T00:00:00Z")


def test_config_validation_bounds() -> None:
    model.DaemonConfig().validate()
    with pytest.raises(model.DaemonError):
        model.DaemonConfig(max_cycles=0).validate()
    with pytest.raises(model.DaemonError):
        model.DaemonConfig(session_subruns=0).validate()
    config = model.DaemonConfig(max_cycles=3)
    assert model.DaemonConfig.from_dict(config.to_dict()).max_cycles == 3


def test_cycle_and_state_roundtrip() -> None:
    cycle = _cycle()
    assert model.DaemonCycle.from_dict(cycle.to_dict()) == cycle
    state = model.DaemonState.initial(started_at="2026-01-01T00:00:00Z")
    document = state.to_dict()
    document.update({
        "cycles_executed": 1, "portfolio_id": "p-1",
        "decision_ids": [cycle.decision_id], "last_cycle": cycle.to_dict()})
    restored = model.DaemonState.from_dict(document)
    assert restored.cycles_executed == 1
    assert restored.last_cycle == cycle


def test_state_rejects_unknown_status_and_bad_decision() -> None:
    state = model.DaemonState.initial(started_at="t").to_dict()
    state["status"] = "BOGUS"
    with pytest.raises(model.DaemonError):
        model.DaemonState.from_dict(state)
    state = model.DaemonState.initial(started_at="t").to_dict()
    state["decision_ids"] = ["not-a-digest"]
    with pytest.raises(model.DaemonError):
        model.DaemonState.from_dict(state)


def test_stop_request_roundtrip_and_malformed(tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    assert engine.is_stop_requested(root) is False
    engine.request_stop(root, reason="halt", requested_at="2026-01-01T00:00:00Z")
    read = engine.read_stop_request(root)
    assert read is not None and read["reason"] == "halt"
    engine.clear_stop(root)
    assert engine.is_stop_requested(root) is False
    path = daemon_store.daemon_paths(root)["stop"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    malformed = engine.read_stop_request(root)
    assert malformed is not None and malformed["reason"] == "UNREADABLE"


def test_store_reconstruct_requires_state(tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    with pytest.raises(model.DaemonError):
        daemon_store.reconstruct(root)


def test_store_reconstruct_detects_cycle_mismatch(tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    paths = daemon_store.daemon_paths(root)
    state = model.DaemonState.initial(started_at="t")
    daemon_store.save_state(paths, state)
    daemon_store.append_cycle(paths, _cycle())
    with pytest.raises(model.DaemonError):
        daemon_store.reconstruct(root)


def test_daemon_dependency_validation_fails_closed() -> None:
    cfg = model.DaemonConfig(
        dependencies=portfolio_model.PortfolioDependencies.normalize(
            {"g-b": ["g-a"]}))
    assert cfg.dependencies.for_goal("g-b") == ("g-a",)
