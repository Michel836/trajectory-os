"""Mission 004 (Issue #198) — operator CLI focused test: create without
--validate must fall back to the deterministic default validator
(fail-safe default; previously crashed with TypeError)."""

from __future__ import annotations

import json
import pathlib
import subprocess

from trajectory_os.missions import adapter, cli


def test_create_without_validate_uses_default_validator(
        tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t.t",
                    "-c", "user.name=t", "commit", "-q", "--allow-empty",
                    "-m", "x"], check=True)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          check=True, capture_output=True,
                          text=True).stdout.strip()

    code = cli.main([
        "--root", str(tmp_path / "root"), "create", "defval",
        "--objective", "no explicit validator",
        "--repo", str(repo), "--head", head,
        "--pi-wrapper", "pi", "--model", "sonoma",
        # intentionally no --validate
    ])
    assert code == cli.EXIT_OK
    state = json.loads((tmp_path / "root" / "missions" / "defval"
                        / "mission.json").read_text())
    validate = next(p for p in state["phases"] if p["phase_id"] == "validate")
    assert validate["command"], "default validator must be persisted"
    # Absent --validate -> canonical default validator (quality gate).
    assert validate["command"] == list(adapter.DEFAULT_VALIDATE_COMMAND)
