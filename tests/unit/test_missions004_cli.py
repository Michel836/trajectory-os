"""Mission 004 (Issue #198) — operator CLI unit tests.

Covers argument parsing, canonical exit codes, fail-closed rejection,
machine-readable output, and policy parsing.  All runs are isolated in
tmp dirs and use an always-passing fake provider.
"""

from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

from trajectory_os.missions import cli


def _git_setup(tmp_path: pathlib.Path) -> str:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t.t",
         "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "b"],
        check=True)
    return str(repo)


def _head_of(repo: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True).stdout.strip()


def _fake_provider(tmp_path: pathlib.Path) -> str:
    p = tmp_path / "fp"
    p.write_text(
        "#!/bin/bash\n"
        "if [[ -n \"${TRAJECTORY_SUBRUN_RESULT_FILE:-}\" "
        "&& -n \"${TRAJECTORY_SUBRUN_ID:-}\" ]]; then\n"
        "  repo=\"$(pwd -P)\"\n"
        "  head=\"$(git -C \"$repo\" rev-parse HEAD 2>/dev/null || echo '')\"\n"
        "  if [[ -n \"$head\" ]]; then\n"
        "    run_id=\"run-$TRAJECTORY_SUBRUN_ID\"\n"
        "    rd=\"$repo/.trajectory-pi/runs/$run_id\"\n"
        "    mkdir -p \"$rd\"\n"
        "    printf 'run_id=%s\\nworkspace=%s\\nhead_before=%s\\n' "
        "\"$run_id\" \"$repo\" \"$head\" > \"$rd/meta.txt\"\n"
        "    : > \"$rd/worktree.patch\"\n"
        "    psha=\"$(sha256sum \"$rd/worktree.patch\" | awk '{print $1}')\"\n"
        "    printf '{\"schema_version\":1,\"subrun_id\":\"%s\","
        "\"status\":\"SUCCESS\","
        "\"agent_classification\":\"AGENT_COMPLETED\","
        "\"readiness\":\"READY_FOR_COMMIT\","
        "\"reason\":\"test fixture semantic success\","
        "\"attestation\":{\"schema_version\":1,\"subrun_id\":\"%s\","
        "\"run_id\":\"%s\",\"repo_head_before\":\"%s\","
        "\"repo_head_after\":\"%s\",\"patch_sha256\":\"%s\"}}\\n' "
        "\"$TRAJECTORY_SUBRUN_ID\" \"$TRAJECTORY_SUBRUN_ID\" "
        "\"$run_id\" \"$head\" \"$head\" \"$psha\" "
        "> \"$TRAJECTORY_SUBRUN_RESULT_FILE\"\n"
        "  fi\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8")
    p.chmod(0o755)
    return str(p)


@pytest.fixture()
def env(tmp_path: pathlib.Path) -> dict[str, str]:
    repo = _git_setup(tmp_path)
    return {
        "root": str(tmp_path / "root"),
        "repo": repo,
        "head": _head_of(repo),
        "provider": _fake_provider(tmp_path),
    }


def _args(env: dict[str, str], mid: str) -> list[str]:
    return [
        "--root", env["root"],
        "create", mid,
        "--objective", "test objective",
        "--repo", env["repo"], "--head", env["head"],
        "--pi-wrapper", env["provider"], "--model", "fake",
        "--validate", "true",
    ]


class TestParsePolicy:
    def test_valid(self) -> None:
        p = cli.parse_policy(["cpu_slots=2", "gpu_mem_bytes=1024"])
        assert p == {"cpu_slots": 2, "gpu_mem_bytes": 1024}

    def test_unknown_key(self) -> None:
        with pytest.raises(cli.UsageError):
            cli.parse_policy(["warp_drive=2"])

    def test_bad_int(self) -> None:
        with pytest.raises(cli.UsageError):
            cli.parse_policy(["cpu_slots=abc"])

    def test_negative(self) -> None:
        with pytest.raises(cli.UsageError):
            cli.parse_policy(["cpu_slots=-1"])


class TestStateExit:
    def test_mapping(self) -> None:
        assert cli._state_exit("COMPLETE") == cli.EXIT_OK
        assert cli._state_exit("BLOCKED") == cli.EXIT_REJECTED
        assert cli._state_exit("FAILED") == cli.EXIT_REJECTED
        assert cli._state_exit("RUNNING") == cli.EXIT_IN_FLIGHT
        assert cli._state_exit("REPAIRING") == cli.EXIT_IN_FLIGHT


class TestMainDispatch:
    def test_version(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["version"]) == 0
        out = capsys.readouterr().out
        assert "trajectory-pi-missions" in out

    def test_create_and_list(self, env: dict[str, str],
                             capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(_args(env, "m1")) == cli.EXIT_OK
        capsys.readouterr()
        assert cli.main(["--root", env["root"], "list"]) == cli.EXIT_OK
        out = capsys.readouterr()
        assert "m1" in out.out and "PLANNING" in out.out
        # status JSON is well-formed on the created (not run) mission
        code = cli.main(["--root", env["root"], "--json", "status", "m1"])
        assert code == cli.EXIT_OK


    def test_create_duplicate_fails_closed(self, env: dict[str, str],
                                          capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(_args(env, "m1")) == cli.EXIT_OK
        capsys.readouterr()
        assert cli.main(_args(env, "m1")) == cli.EXIT_REJECTED
        assert "already exists" in capsys.readouterr().err

    def test_unknown_id(self, env: dict[str, str],
                        capsys: pytest.CaptureFixture[str]) -> None:
        got = cli.main(["--root", env["root"], "status", "nope"])
        assert got == cli.EXIT_NOT_FOUND
        assert "not found" in capsys.readouterr().err

    def test_bad_id_rejected(self, env: dict[str, str],
                             capsys: pytest.CaptureFixture[str]) -> None:
        args = _args(env, "M1_UPPER")
        assert cli.main(args) == cli.EXIT_USAGE
        assert "invalid mission id" in capsys.readouterr().err

    def test_empty_root(self, tmp_path: pathlib.Path,
                        capsys: pytest.CaptureFixture[str]) -> None:
        root = tmp_path / "r"
        root.mkdir()
        (root / "missions").mkdir()
        got = cli.main(["--root", str(root), "list"])
        assert got == cli.EXIT_OK
        assert "no missions" in capsys.readouterr().out


class TestRunLifecycle:
    def test_run_to_complete_then_continue_ok(
            self, env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(_args(env, "m1")) == cli.EXIT_OK
        capsys.readouterr()
        code = cli.main(["--root", env["root"], "run", "m1"])
        assert code == cli.EXIT_OK
        out = capsys.readouterr().out
        assert "COMPLETE" in out
        # Terminal COMPLETE: continue is fine (exit 0); the mission ran 5
        # sub-runs (fresh-context per phase) — countable in the status.
        code = cli.main(["--root", env["root"], "continue", "m1"])
        assert code == cli.EXIT_OK

    def test_resuming_terminal_complete_is_noop_report(
            self, env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(_args(env, "m1")) == cli.EXIT_OK
        capsys.readouterr()
        assert cli.main(["--root", env["root"], "run", "m1"]) == cli.EXIT_OK
        capsys.readouterr()
        code = cli.main(["--root", env["root"], "resume", "m1"])
        assert code == cli.EXIT_OK
        out = capsys.readouterr().out
        assert "resumable=False" in out

    def test_evidence_command(self, env: dict[str, str],
                              capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(_args(env, "m1")) == cli.EXIT_OK
        capsys.readouterr()
        assert cli.main(["--root", env["root"], "run", "m1"]) == 0
        got = cli.main(["--root", env["root"], "evidence", "m1",
                        "implement"])
        assert got == cli.EXIT_OK
        out = capsys.readouterr().out
        assert "implement" in out and "PASSED" in out

    def test_note_command(
            self, env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(_args(env, "m1")) == cli.EXIT_OK
        capsys.readouterr()
        assert cli.main(["--root", env["root"], "run", "m1"]) == 0
        capsys.readouterr()
        got = cli.main(["--root", env["root"], "note", "m1", "--text",
                        "operator check"])
        assert got == cli.EXIT_OK
        out = capsys.readouterr().out
        assert "note recorded" in out

    def test_gpu_policy_flag(self, env: dict[str, str],
                             capsys: pytest.CaptureFixture[str]) -> None:
        # GPU-declared mission with full capacity evidence -> admitted.
        args = _args(env, "m2")
        args += ["--gpu"]
        assert cli.main(args) == cli.EXIT_OK
        capsys.readouterr()
        code = cli.main(["--root", env["root"], "run", "m2",
                         "--policy", "gpu_slots=1",
                         "gpu_mem_bytes=25769803776"])
        assert code == cli.EXIT_OK
        # GPU-declared mission, policy without GPU capacity -> fail-closed
        # BLOCKED (RESOURCE_UNAVAILABLE): one heavy GPU model at a time,
        # admitted only with capacity evidence.
        args3 = _args(env, "m3")
        args3 += ["--gpu"]
        assert cli.main(args3) == cli.EXIT_OK
        capsys.readouterr()
        code = cli.main(["--root", env["root"], "run", "m3",
                         "--policy", "cpu_slots=2", "ram_bytes=1073741824"])
        assert code == cli.EXIT_REJECTED
        assert "RESOURCE_UNAVAILABLE" in capsys.readouterr().out

    def test_policy_flag(self, env: dict[str, str],
                         capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(_args(env, "m4")) == cli.EXIT_OK
        capsys.readouterr()
        # capacity evidence supplied (no GPU declared) -> deterministic
        # admission passes
        code = cli.main(["--root", env["root"], "run", "m4",
                         "--policy", "cpu_slots=2", "ram_bytes=1073741824"])
        assert code == cli.EXIT_OK


class TestExitCodeValues:
    def test_status_codes_are_canonical(self) -> None:
        for code, name in [
            (0, "EXIT_OK"), (2, "EXIT_USAGE"), (3, "EXIT_REJECTED"),
            (4, "EXIT_NOT_FOUND"), (5, "EXIT_IN_FLIGHT"),
        ]:
            assert getattr(cli, name) == code, name


def test_main_returns_int_not_none(env: dict[str, str],
                                   capsys: pytest.CaptureFixture[str]) -> None:
    assert isinstance(cli.main(["version"]), int)
    capsys.readouterr()


class TestSharedFlagPositions:
    """--root / --json are accepted per-subcommand (after the command),
    not only globally — the natural operator form the module docstring
    implies.  Distinct dests keep the existing global flags byte-
    compatible; per-subcommand value wins on collision."""

    def test_json_after_subcommand(self, env: dict[str, str],
                                   capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(_args(env, "m1")) == cli.EXIT_OK
        capsys.readouterr()
        # Per-subcommand --json -> machine-readable payload, exit 0.
        code = cli.main(["--root", env["root"], "status", "m1", "--json"])
        assert code == cli.EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["summary"]["mission_state"] in (
            "COMPLETE", "PLANNING", "RUNNING", "RESOURCE_DEFERRED")

    def test_json_per_subcommand_unknown_id(self, env: dict[str, str],
                                            capsys: pytest.CaptureFixture[str]) -> None:
        # Canonical exit code is preserved when the flag is after the id.
        code = cli.main(["--root", env["root"], "status", "nope", "--json"])
        assert code == cli.EXIT_NOT_FOUND

    def test_root_only_per_subcommand(self, env: dict[str, str],
                                     capsys: pytest.CaptureFixture[str]) -> None:
        # No global --root, only per-subcommand --root after the command.
        assert cli.main(
            ["create", "m1", "--root", env["root"],
             "--objective", "test objective",
             "--repo", env["repo"], "--head", env["head"],
             "--pi-wrapper", env["provider"], "--model", "fake",
             "--validate", "true"],) == cli.EXIT_OK
        capsys.readouterr()
        code = cli.main(["list", "--root", env["root"]])
        assert code == cli.EXIT_OK
        assert "m1" in capsys.readouterr().out

    def test_collision_subcommand_root_wins(self, tmp_path: pathlib.Path,
                                           capsys: pytest.CaptureFixture[str]) -> None:
        real_root = tmp_path / "real"          # the mission actually lives here
        decoy_root = tmp_path / "decoy"        # global position (loses)
        real_root.mkdir()
        decoy_root.mkdir()
        (real_root / "missions").mkdir()
        (decoy_root / "missions").mkdir()
        repo = _git_setup(tmp_path)
        assert cli.main(
            ["--root", str(real_root), "create", "m1",
             "--objective", "x", "--repo", repo,
             "--pi-wrapper", str(_fake_provider(tmp_path)),
             "--model", "fake", "--validate", "true",
             "--root", str(real_root)]) == cli.EXIT_OK  # redundant, accepted
        capsys.readouterr()
        # Global root points at the EMPTY decoy; per-subcommand root must win
        # so the mission is found in the real root (exit 0, not exit 4).
        code = cli.main(["--root", str(decoy_root), "list",
                         "--root", str(real_root)])
        assert code == cli.EXIT_OK
        assert "m1" in capsys.readouterr().out

    def test_global_json_still_accepted(self, env: dict[str, str],
                                        capsys: pytest.CaptureFixture[str]) -> None:
        # Regression guard: the pre-existing global --json keeps working.
        assert cli.main(_args(env, "m1")) == cli.EXIT_OK
        capsys.readouterr()
        code = cli.main(["--root", env["root"], "--json", "status", "m1"])
        assert code == cli.EXIT_OK
        json.loads(capsys.readouterr().out)

    def test_version_has_no_shared_flags(self) -> None:
        # version is excluded from the per-subcommand shared flags by design;
        # the plain form still works and the global position stays compatible.
        assert cli.main(["version"]) == cli.EXIT_OK
        assert cli.main(["--json", "version"]) == cli.EXIT_OK


def test_status_payload_shape(env: dict[str, str],
                              capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(_args(env, "m1")) == cli.EXIT_OK
    capsys.readouterr()
    assert cli.main(["--root", env["root"], "run", "m1"]) == 0
    capsys.readouterr()
    assert cli.main(["--root", env["root"], "--json", "status",
                     "m1"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "OK"
    assert payload["rendered"]
    s = payload["summary"]
    assert s["mission_state"] == "COMPLETE"
    assert s["jobs"]["started"] == 5
    assert s["automatic"]["repair_budget"] >= 1
    assert s["lifecycle_events"] == {"reconstruction": 0, "resume": 0}
    # Mission 004: kind-based heavy accounting (plan/implement/review)
    assert s["model_heavy"]["subrun_count"] == 3
    assert s["model_heavy"]["phase_count"] == 3
