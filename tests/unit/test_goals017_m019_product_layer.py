"""Missions 017-019 — pure unit tests for the visible operator product layer.

These cover the deterministic, side-effect-free helpers: CLI parser shape,
provider/locality attribution, elapsed formatting, mission objective
bounding and run-configuration bounds. End-to-end behavior is covered by
``tests/integration/test_goals017_m019_visible_operator.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import trajectory_os.goals as goals
from trajectory_os.goals import cli, launch, runner, snapshot, tui
from trajectory_os.missions import model as mission_model


def test_cli_parser_exposes_required_commands() -> None:
    parser = cli.build_parser()
    subparsers = next(
        action for action in parser._actions
        if action.dest == "command")
    commands = set(subparsers.choices)
    for required in (
            "start", "resume", "stop", "status", "inspect", "explain", "why",
            "dashboard", "evidence", "proof", "tui", "list", "version"):
        assert required in commands, required


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["version"]) == cli.EXIT_OK
    assert "trajectory-pi-goal" in capsys.readouterr().out


def test_cli_rejects_unknown_command() -> None:
    assert cli.main(["definitely-not-a-command"]) == cli.EXIT_USAGE


def test_cli_stop_unknown_goal(tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    assert cli.main(["--root", root, "stop", "g-missing"]) == cli.EXIT_NOT_FOUND


def test_attribution_remote_and_local() -> None:
    remote = snapshot._attribution("deepseek-flash", "trajectory-pi")
    assert remote["provider"] == "deepseek"
    assert remote["locality"] == "remote"
    local = snapshot._attribution("qwen3.8-dev3090", "trajectory-pi")
    assert local["provider"] == "ollama"
    assert local["locality"] == "local"
    unknown = snapshot._attribution(None, None)
    assert unknown["model"] is None
    assert unknown["locality"] is None


def test_command_helpers() -> None:
    command = ["scripts/trajectory-pi", "--mode", "IMPLEMENT",
               "--model", "qwen3.8-dev3090", "--", "x"]
    assert snapshot._command_value(command, "--model") == "qwen3.8-dev3090"
    assert snapshot._command_value(command, "--missing") is None
    assert snapshot._command_agent(command) == "trajectory-pi"
    assert snapshot._command_agent([]) is None


def test_elapsed_formatting() -> None:
    assert tui._elapsed_text(None) == "-"
    assert tui._elapsed_text(5) == "5s"
    assert tui._elapsed_text(65) == "1m05s"
    assert tui._elapsed_text(3661) == "1h01m01s"


def test_elapsed_formatting_accepts_float_durations() -> None:
    # ``elapsed_seconds`` may be a float (e.g. a time.time() difference).
    assert tui._elapsed_text(5.0) == "5s"
    assert tui._elapsed_text(65.9) == "1m05s"
    assert tui._elapsed_text(3661.5) == "1h01m01s"


def test_elapsed_formatting_is_fail_safe() -> None:
    assert tui._elapsed_text(float("nan")) == "-"
    assert tui._elapsed_text(float("inf")) == "-"
    assert tui._elapsed_text(float("-inf")) == "-"
    assert tui._elapsed_text(-1) == "-"
    assert tui._elapsed_text(-1.5) == "-"
    assert tui._elapsed_text(True) == "-"
    assert tui._elapsed_text("65") == "-"
    assert tui._elapsed_text(None) == "-"


class _Node:
    def __init__(self, title: str) -> None:
        self.title = title


def test_mission_objective_is_bounded() -> None:
    objective = launch.mission_objective(
        _Node("t" * 5000), "o" * 5000)  # type: ignore[arg-type]
    assert len(objective) <= mission_model.MAX_OBJECTIVE_LEN
    assert objective.startswith("t")


def test_run_config_validation_bounds() -> None:
    runner.GoalRunConfig().validate()
    with pytest.raises(ValueError):
        runner.GoalRunConfig(max_cycles=0).validate()
    with pytest.raises(ValueError):
        runner.GoalRunConfig(session_subruns=0).validate()
    with pytest.raises(ValueError):
        runner.GoalRunConfig(
            session_subruns=mission_model.MAX_SESSION_SUBRUNS + 1).validate()


def test_package_exports_modules() -> None:
    for name in ("cli", "launch", "runner", "snapshot", "tui"):
        assert name in goals.__all__
