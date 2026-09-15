"""Mission 006 — canonical ``start`` subcommand (create-then-run) tests.

``start ID`` composes the existing create path (validate + persist) with
the existing run path — one command, not a second orchestration.  Covers:

* successful one-command start (create then run to COMPLETE);
* create failure -> no provider invocation / no sub-run launch;
* existing mission rejected (start never silently resumes it);
* canonical exit-code preservation through the composition (0/2/3/5);
* global and per-subcommand ``--root`` / ``--json`` compatibility.

All runs are isolated in tmp dirs and use an always-passing fake provider
that records one marker line per invocation, so "no provider invocation"
and "no sub-run launched" are assertable from filesystem evidence.
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


def _marker_provider(tmp_path: pathlib.Path) -> tuple[str, pathlib.Path]:
    """Fake pi-wrapper (always exit 0) recording one marker line per call."""
    marker = tmp_path / "provider-invocations"
    script = tmp_path / "fp"
    script.write_text(
        "#!/bin/bash\n"
        "echo invoked >> " + str(marker) + "\n"
        "if [[ -n \"${TRAJECTORY_SUBRUN_RESULT_FILE:-}\" "
        "&& -n \"${TRAJECTORY_SUBRUN_ID:-}\" ]]; then\n"
        "  printf '{\"schema_version\":1,"
        "\"subrun_id\":\"%s\","
        "\"status\":\"SUCCESS\","
        "\"agent_classification\":\"AGENT_COMPLETED\","
        "\"readiness\":\"READY_FOR_COMMIT\","
        "\"reason\":\"test fixture semantic success\"}\\n' "
        "\"$TRAJECTORY_SUBRUN_ID\" "
        "> \"$TRAJECTORY_SUBRUN_RESULT_FILE\"\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8")
    script.chmod(0o755)
    return str(script), marker


def _marker_count(marker: str) -> int:
    p = pathlib.Path(marker)
    if not p.is_file():
        return 0
    return len(p.read_text(encoding="utf-8").strip().splitlines())


def _json_docs(text: str) -> list[dict]:
    """Decode consecutive JSON documents (``start --json``: create + run)."""
    docs: list[dict] = []
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(text):
        while idx < len(text) and text[idx].isspace():
            idx += 1
        if idx >= len(text):
            break
        doc, idx = decoder.raw_decode(text, idx)
        if not isinstance(doc, dict):
            raise AssertionError(f"non-object JSON document: {doc!r}")
        docs.append(doc)
    return docs


@pytest.fixture()
def env(tmp_path: pathlib.Path) -> dict[str, str]:
    repo = _git_setup(tmp_path)
    provider, marker = _marker_provider(tmp_path)
    return {
        "root": str(tmp_path / "root"),
        "repo": repo,
        "head": _head_of(repo),
        "provider": provider,
        "marker": str(marker),
    }


def _create_flags(env: dict[str, str]) -> list[str]:
    """The same create options ``create`` accepts (start must accept them)."""
    return [
        "--objective", "test objective",
        "--repo", env["repo"], "--head", env["head"],
        "--pi-wrapper", env["provider"], "--model", "fake",
        "--validate", "true",
    ]


class TestStartSuccess:
    def test_one_command_start_creates_and_completes(
            self, env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
        code = cli.main(["--root", env["root"],
                         "start", "m1", *_create_flags(env)])
        assert code == cli.EXIT_OK
        out = capsys.readouterr().out
        # Both stages visible: create persisted, then run completed.
        assert "created" in out and "COMPLETE" in out
        # The three model-heavy phases ran via the provider
        # (plan / implement / review); validate + consolidate ran "true".
        assert _marker_count(env["marker"]) == 3
        # Canonical state: COMPLETE with all five sub-runs started.
        code = cli.main(["--root", env["root"], "--json", "status", "m1"])
        assert code == cli.EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["summary"]["mission_state"] == "COMPLETE"
        assert payload["summary"]["jobs"]["started"] == 5

    def test_start_threads_run_policy_and_session_options(
            self, env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
        # Session bound honored through the composition: exactly one
        # sub-run, then the bounded session stops (canonical code 5).
        code = cli.main(["--root", env["root"],
                         "start", "m1", *_create_flags(env),
                         "--policy", "cpu_slots=2", "ram_bytes=1073741824",
                         "--session-subruns", "1"])
        assert code == cli.EXIT_IN_FLIGHT
        out = capsys.readouterr().out
        assert "session_bound" in out
        assert _marker_count(env["marker"]) == 1
        # The unchanged run path resumes the persisted mission to COMPLETE.
        code = cli.main(["--root", env["root"], "run", "m1"])
        assert code == cli.EXIT_OK
        assert "COMPLETE" in capsys.readouterr().out
        assert _marker_count(env["marker"]) == 3


class TestStartFailClosed:
    def test_create_failure_existing_planning_launches_nothing(
            self, env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
        # Mission already exists (PLANNING): start must fail closed,
        # launch no sub-run, and NOT silently resume it.
        assert cli.main(["--root", env["root"], "create", "m1",
                         *_create_flags(env)]) == cli.EXIT_OK
        capsys.readouterr()
        assert _marker_count(env["marker"]) == 0

        code = cli.main(["--root", env["root"], "start", "m1",
                         *_create_flags(env)])
        assert code == cli.EXIT_REJECTED
        assert "already exists" in capsys.readouterr().err
        # No provider invocation: create failed before the run path.
        assert _marker_count(env["marker"]) == 0

        # State unchanged — start never resumed the existing mission.
        # (``status --json`` documents the canonical state; exit code is
        # reserved for the machine report, so it reports OK here.)
        code = cli.main(["--root", env["root"], "--json", "status", "m1"])
        assert code == cli.EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["summary"]["mission_state"] == "PLANNING"
        assert payload["summary"]["jobs"]["started"] == 0

    def test_create_failure_existing_complete_rejected(
            self, env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
        # An existing terminal mission is rejected the same way: no
        # re-run, no provider activity.
        assert cli.main(["--root", env["root"], "start", "m1",
                         *_create_flags(env)]) == cli.EXIT_OK
        capsys.readouterr()
        before = _marker_count(env["marker"])
        assert before == 3

        code = cli.main(["--root", env["root"], "start", "m1",
                         *_create_flags(env)])
        assert code == cli.EXIT_REJECTED
        assert "already exists" in capsys.readouterr().err
        assert _marker_count(env["marker"]) == before

    def test_create_usage_failure_writes_nothing(
            self, env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
        # Usage-level create failure: nothing persisted, no sub-run launch.
        code = cli.main(["--root", env["root"], "start", "M1_BAD",
                         *_create_flags(env)])
        assert code == cli.EXIT_USAGE
        assert "invalid mission id" in capsys.readouterr().err
        assert not pathlib.Path(env["root"], "missions", "M1_BAD").exists()
        assert _marker_count(env["marker"]) == 0

    def test_missing_objective_is_usage_error(
            self, env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
        code = cli.main(["--root", env["root"], "start", "m1",
                         "--repo", env["repo"], "--validate", "true"])
        assert code == cli.EXIT_USAGE
        capsys.readouterr()
        assert not pathlib.Path(env["root"], "missions", "m1").exists()
        assert _marker_count(env["marker"]) == 0


class TestStartExitCodes:
    """Canonical exit-code preservation through the create->run composition."""

    def test_canonical_codes(self, env: dict[str, str],
                             capsys: pytest.CaptureFixture[str]) -> None:
        # 0 — create + run to COMPLETE in one command.
        assert cli.main(["--root", env["root"], "start", "ok1",
                         *_create_flags(env)]) == 0
        capsys.readouterr()
        # 2 — usage error (invalid id), rejected before any state.
        assert cli.main(["--root", env["root"], "start", "BAD",
                         *_create_flags(env)]) == 2
        capsys.readouterr()
        # 3 — create rejected (existing mission): fail closed.
        assert cli.main(["--root", env["root"], "start", "ok1",
                         *_create_flags(env)]) == 3
        capsys.readouterr()
        # 3 — create OK, run stage fail-closed (GPU declared, no capacity
        # evidence): the run rejection propagates through start, and the
        # block happens before any model-heavy sub-run launches.
        before = _marker_count(env["marker"])
        assert cli.main(["--root", env["root"], "start", "blk1",
                         *_create_flags(env), "--gpu"]) == 3
        out = capsys.readouterr().out
        assert "created" in out            # create stage fully persisted
        assert "RESOURCE_UNAVAILABLE" in out
        assert _marker_count(env["marker"]) == before

        # 5 — bounded session leaves the mission in-flight after create+run.
        assert cli.main(["--root", env["root"], "start", "flight1",
                         *_create_flags(env),
                         "--session-subruns", "1"]) == 5
        capsys.readouterr()


class TestStartSharedFlagPositions:
    """``--root`` / ``--json`` are accepted globally and per-subcommand
    for ``start`` (same merge rules as the existing subcommands)."""

    def _check_docs(self, docs: list[dict]) -> None:
        # Two JSON documents in sequence: the create payload, then the
        # run report — both valid, both for the same mission.
        assert len(docs) == 2
        created, report = docs
        assert created["status"] == "CREATED"
        assert created["mission_id"] == "m1"
        assert report["mission_id"] == "m1"
        assert report["mission_state"] == "COMPLETE"
        assert report["stop"] in ("complete", "terminal_after_subrun")

    def test_global_root_and_json(
            self, env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
        code = cli.main(["--root", env["root"], "--json",
                         "start", "m1", *_create_flags(env)])
        assert code == cli.EXIT_OK
        self._check_docs(_json_docs(capsys.readouterr().out))

    def test_per_subcommand_root_and_json(
            self, env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
        # No global flags at all: only the per-subcommand position.
        code = cli.main(["start", "m1", *_create_flags(env),
                         "--root", env["root"], "--json"])
        assert code == cli.EXIT_OK
        self._check_docs(_json_docs(capsys.readouterr().out))

    def test_collision_subcommand_root_wins(
            self, tmp_path: pathlib.Path,
            capsys: pytest.CaptureFixture[str]) -> None:
        real_root = tmp_path / "real"   # the mission actually lives here
        decoy_root = tmp_path / "decoy"  # global position (loses)
        for root in (real_root, decoy_root):
            (root / "missions").mkdir(parents=True)
        repo = _git_setup(tmp_path)
        provider, marker = _marker_provider(tmp_path)

        code = cli.main(["--root", str(decoy_root),
                         "start", "m1",
                         "--objective", "test objective",
                         "--repo", repo,
                         "--pi-wrapper", provider, "--model", "fake",
                         "--validate", "true",
                         "--root", str(real_root)])
        assert code == cli.EXIT_OK
        capsys.readouterr()
        # Per-subcommand root won: mission recorded under the real root,
        # the decoy root stayed untouched, exit 0 (not not-found exit 4).
        assert cli.main(["list", "--root", str(real_root)]) == cli.EXIT_OK
        assert "m1" in capsys.readouterr().out
        assert cli.main(["list", "--root", str(decoy_root)]) == cli.EXIT_OK
        assert "no missions" in capsys.readouterr().out
        assert len(marker.read_text(encoding="utf-8").splitlines()) == 3
