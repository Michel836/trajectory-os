"""M064–M071 — bundled integration test (privacy-safe demo end to end)."""

from __future__ import annotations

from pathlib import Path

from trajectory_os.operator import cli as operator_cli
from trajectory_os.realworld import dogfood, portfolio


def test_privacy_safe_demo_end_to_end(tmp_path: Path) -> None:
    evidence = dogfood.run_privacy_safe_demo(
        str(tmp_path), generated_at="2026-01-01T00:00:00Z",
        run_acceptance_matrix=False, fixture_count=30)
    # History is fixture-labelled, never presented as real.
    assert evidence.history_source == "FIXTURE"
    assert evidence.real_row_count == 0
    assert evidence.fixture_row_count > 0
    # The five scenarios produced outputs.
    assert evidence.career_artifacts
    assert evidence.life_sciences_artifacts
    assert evidence.operations["next_actions"]
    assert evidence.model_refresh["state"] in (
        "PROMOTE", "NO_PROMOTION", "INSUFFICIENT_DATA")
    # The cockpit and portfolio are persisted.
    assert (tmp_path / "realworld" / "cockpit" / "dogfood-cockpit" /
            "cockpit.txt").is_file()
    assert (tmp_path / "realworld" / "portfolio" /
            "executive-overview.md").is_file()
    assert evidence.marker == dogfood.MARKER
    assert (tmp_path / "realworld" / "portfolio" /
            "build-in-public.json").is_file()


def test_cli_parser_recognizes_demo_portfolio() -> None:
    parser = operator_cli.build_parser()
    args = parser.parse_args(["demo", "portfolio", "--root", "/tmp/x"])
    assert args.command == "demo"
    assert args.demo_command == "portfolio"
    assert args.root == "/tmp/x"


def test_cli_parser_recognizes_realworld_commands() -> None:
    parser = operator_cli.build_parser()
    args = parser.parse_args(
        ["realworld", "ingest", "--root", "/tmp/x", "--path", "/tmp/y"])
    assert args.command == "realworld"
    assert args.realworld_command == "ingest"


def test_architecture_chain_is_stable() -> None:
    assert portfolio.ARCHITECTURE_CHAIN[0] == "Objective"
    assert portfolio.ARCHITECTURE_CHAIN[-1] == "Learning"
    assert len(portfolio.ARCHITECTURE_CHAIN) == 9
