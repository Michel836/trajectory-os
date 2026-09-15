"""Mission 006 — bounded declarative input for ``start`` tests.

Covers the ``--objective-file`` and ``--spec`` surface:

* valid objective-file (whitespace stripped, canonical bounds applied);
* oversized / unreadable / non-regular objective-file (fail closed);
* valid spec (strict JSON object, known keys, typed values);
* malformed JSON / non-object JSON / unknown key / wrong type / oversized
  spec (all usage rejections);
* precedence: explicit CLI value > spec value > defaults (objective/model/repo);
* contradictions: ``--spec`` + ``--objective-file``, ``--objective`` +
  ``--objective-file``;
* every rejection writes no mission state and launches no provider;
* the launch provenance is persisted in the existing canonical event log.

All runs are isolated in tmp dirs with an always-passing fake provider that
records one marker line per invocation (same harness style as
``test_missions006_start_cli.py``).
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
from typing import Any

import pytest

from trajectory_os.missions import adapter, cli, model, store


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


def _write_spec(path: pathlib.Path, doc: Any) -> None:
    path.write_text(json.dumps(doc), encoding="utf-8")


def _status_payload(root: str, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    assert cli.main(["--root", root, "--json", "status", "m1"]) == cli.EXIT_OK
    doc: dict[str, Any] = json.loads(capsys.readouterr().out)
    summary = doc["summary"]  # machine summary from the canonical mission state
    assert isinstance(summary, dict)
    return summary


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


class TestObjectiveFile:
    def test_valid_objective_file_strips_and_completes(
            self, env: dict[str, str], tmp_path: pathlib.Path,
            capsys: pytest.CaptureFixture[str]) -> None:
        objective_path = tmp_path / "objective.txt"
        objective_path.write_text(
            "\n   fix the parser (from file)    \n", encoding="utf-8")
        code = cli.main(["--root", env["root"], "start", "m1",
                         "--objective-file", str(objective_path),
                         "--repo", env["repo"], "--head", env["head"],
                         "--pi-wrapper", env["provider"], "--model", "fake",
                         "--validate", "true"])
        assert code == cli.EXIT_OK
        capsys.readouterr()
        assert _marker_count(env["marker"]) == 3
        payload = _status_payload(env["root"], capsys)
        assert payload["objective"] == "fix the parser (from file)"
        # Launch provenance persisted in the existing canonical event log.
        mission, paths = store.load_mission(env["root"], "m1")
        events = [e for e in store.load_events(paths)
                  if e.get("event") == "launch"]
        assert len(events) == 1
        assert events[0]["objective_source"] == "objective_file"
        assert events[0]["objective_file"] == str(objective_path)
        assert events[0]["spec_file"] is None

    def test_oversized_objective_file_rejected_no_state(
            self, env: dict[str, str], tmp_path: pathlib.Path,
            capsys: pytest.CaptureFixture[str]) -> None:
        # Beyond the hard byte cap (MAX_OBJECTIVE_LEN * 4 bytes).
        objective_path = tmp_path / "big.txt"
        objective_path.write_text("a" * 40000, encoding="utf-8")
        code = cli.main(["--root", env["root"], "start", "m1",
                         "--objective-file", str(objective_path),
                         "--repo", env["repo"], "--validate", "true"])
        assert code == cli.EXIT_USAGE
        err = capsys.readouterr().err
        assert "exceeds" in err and "16384" in err
        assert not pathlib.Path(env["root"], "missions", "m1").exists()
        assert _marker_count(env["marker"]) == 0

    def test_length_beyond_canonical_objective_bounds_rejected(
            self, env: dict[str, str], tmp_path: pathlib.Path,
            capsys: pytest.CaptureFixture[str]) -> None:
        # Within the byte cap but beyond the canonical 4096-char bound.
        objective_path = tmp_path / "long.txt"
        objective_path.write_text("a" * 5000, encoding="utf-8")
        code = cli.main(["--root", env["root"], "start", "m1",
                         "--objective-file", str(objective_path),
                         "--repo", env["repo"], "--validate", "true"])
        assert code == cli.EXIT_USAGE
        assert "objective must be 1..4096 chars" in capsys.readouterr().err
        assert not pathlib.Path(env["root"], "missions", "m1").exists()
        assert _marker_count(env["marker"]) == 0

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permissions")
    def test_unreadable_objective_file_rejected_no_state(
            self, env: dict[str, str], tmp_path: pathlib.Path,
            capsys: pytest.CaptureFixture[str]) -> None:
        objective_path = tmp_path / "secret.txt"
        objective_path.write_text("secret objective", encoding="utf-8")
        objective_path.chmod(0)
        code = cli.main(["--root", env["root"], "start", "m1",
                         "--objective-file", str(objective_path),
                         "--repo", env["repo"], "--validate", "true"])
        assert code == cli.EXIT_USAGE
        assert "unreadable" in capsys.readouterr().err
        assert not pathlib.Path(env["root"], "missions", "m1").exists()
        assert _marker_count(env["marker"]) == 0

    def test_non_regular_objective_file_rejected_no_state(
            self, env: dict[str, str], tmp_path: pathlib.Path,
            capsys: pytest.CaptureFixture[str]) -> None:
        directory = tmp_path / "dir.txt"
        directory.mkdir()
        code = cli.main(["--root", env["root"], "start", "m1",
                         "--objective-file", str(directory),
                         "--repo", env["repo"], "--validate", "true"])
        assert code == cli.EXIT_USAGE
        assert "regular file" in capsys.readouterr().err
        assert not pathlib.Path(env["root"], "missions", "m1").exists()
        assert _marker_count(env["marker"]) == 0

    def test_missing_objective_file_rejected_no_state(
            self, env: dict[str, str], tmp_path: pathlib.Path,
            capsys: pytest.CaptureFixture[str]) -> None:
        code = cli.main(["--root", env["root"], "start", "m1",
                         "--objective-file", str(tmp_path / "absent.txt"),
                         "--repo", env["repo"], "--validate", "true"])
        assert code == cli.EXIT_USAGE
        assert "missing" in capsys.readouterr().err
        assert not pathlib.Path(env["root"], "missions", "m1").exists()
        assert _marker_count(env["marker"]) == 0


class TestSpec:
    def test_valid_spec_creates_and_completes(
            self, env: dict[str, str], tmp_path: pathlib.Path,
            capsys: pytest.CaptureFixture[str]) -> None:
        spec_path = tmp_path / "spec.json"
        _write_spec(spec_path, {
            "objective": "spec objective",
            "repo": env["repo"],
            "head": env["head"],
            "pi_wrapper": env["provider"],
            "model": "specmodel",
            "validate": "true",
            "session_subruns": 1,
        })
        code = cli.main(["--root", env["root"], "start", "m1",
                         "--spec", str(spec_path)])
        assert code == cli.EXIT_IN_FLIGHT  # bounded session of 1
        capsys.readouterr()
        assert _marker_count(env["marker"]) == 1
        payload = _status_payload(env["root"], capsys)
        assert payload["objective"] == "spec objective"
        # spec model landed in the canonical phase commands
        mission, _ = store.load_mission(env["root"], "m1")
        assert any("specmodel" in str(p.command) for p in mission.phases)
        assert mission.baseline_revision == env["head"]
        events = [e for e in store.load_events(
            store.mission_paths(env["root"], "m1"))
            if e.get("event") == "launch"]
        assert len(events) == 1
        assert events[0]["objective_source"] == "spec"
        assert events[0]["spec_file"] == str(spec_path)
        # The unchanged run path resumes the persisted mission to COMPLETE.
        assert cli.main(["--root", env["root"], "run", "m1"]) == cli.EXIT_OK
        capsys.readouterr()
        assert _marker_count(env["marker"]) == 3

    def test_malformed_json_rejected_no_state(
            self, env: dict[str, str], tmp_path: pathlib.Path,
            capsys: pytest.CaptureFixture[str]) -> None:
        spec_path = tmp_path / "spec.json"
        spec_path.write_text("{not json", encoding="utf-8")
        code = cli.main(["--root", env["root"], "start", "m1",
                         "--spec", str(spec_path)])
        assert code == cli.EXIT_USAGE
        assert "not valid JSON" in capsys.readouterr().err
        assert not pathlib.Path(env["root"], "missions", "m1").exists()
        assert _marker_count(env["marker"]) == 0

    def test_non_object_json_rejected_no_state(
            self, env: dict[str, str], tmp_path: pathlib.Path,
            capsys: pytest.CaptureFixture[str]) -> None:
        spec_path = tmp_path / "spec.json"
        spec_path.write_text("[1, 2, 3]", encoding="utf-8")
        code = cli.main(["--root", env["root"], "start", "m1",
                         "--spec", str(spec_path)])
        assert code == cli.EXIT_USAGE
        assert "JSON object" in capsys.readouterr().err
        assert not pathlib.Path(env["root"], "missions", "m1").exists()
        assert _marker_count(env["marker"]) == 0

    def test_unknown_key_rejected_no_state(
            self, env: dict[str, str], tmp_path: pathlib.Path,
            capsys: pytest.CaptureFixture[str]) -> None:
        spec_path = tmp_path / "spec.json"
        _write_spec(spec_path, {"objective": "x", "bogus_key": True})
        code = cli.main(["--root", env["root"], "start", "m1",
                         "--spec", str(spec_path)])
        assert code == cli.EXIT_USAGE
        err = capsys.readouterr().err
        assert "unknown key" in err and "bogus_key" in err
        assert not pathlib.Path(env["root"], "missions", "m1").exists()
        assert _marker_count(env["marker"]) == 0

    def test_wrong_type_rejected_no_state(
            self, env: dict[str, str], tmp_path: pathlib.Path,
            capsys: pytest.CaptureFixture[str]) -> None:
        spec_path = tmp_path / "spec.json"
        _write_spec(spec_path, {"objective": "x", "model": 42})
        code = cli.main(["--root", env["root"], "start", "m1",
                         "--spec", str(spec_path)])
        assert code == cli.EXIT_USAGE
        err = capsys.readouterr().err
        assert "model" in err and "non-empty string" in err
        assert not pathlib.Path(env["root"], "missions", "m1").exists()
        assert _marker_count(env["marker"]) == 0

        # Bool is not a valid integer (bool subclasses int in Python).
        _write_spec(spec_path, {"objective": "x", "repair_budget": True})
        code = cli.main(["--root", env["root"], "start", "m1",
                         "--spec", str(spec_path)])
        assert code == cli.EXIT_USAGE
        assert "integer" in capsys.readouterr().err
        assert not pathlib.Path(env["root"], "missions", "m1").exists()
        assert _marker_count(env["marker"]) == 0

    def test_spec_without_objective_rejected_no_state(
            self, env: dict[str, str], tmp_path: pathlib.Path,
            capsys: pytest.CaptureFixture[str]) -> None:
        spec_path = tmp_path / "spec.json"
        _write_spec(spec_path, {"model": "specmodel"})
        code = cli.main(["--root", env["root"], "start", "m1",
                         "--spec", str(spec_path)])
        assert code == cli.EXIT_USAGE
        assert "objective is required" in capsys.readouterr().err
        assert not pathlib.Path(env["root"], "missions", "m1").exists()
        assert _marker_count(env["marker"]) == 0

    def test_oversized_spec_rejected_no_state(
            self, env: dict[str, str], tmp_path: pathlib.Path,
            capsys: pytest.CaptureFixture[str]) -> None:
        spec_path = tmp_path / "spec.json"
        # Padding inside a known string field keeps the JSON syntactically
        # valid but pushes the file past the hard byte cap.
        _write_spec(spec_path, {"objective": "x",
                                "validate": "true",
                                "consolidate": "pad" * (cli.SPEC_MAX_BYTES)})
        code = cli.main(["--root", env["root"], "start", "m1",
                         "--spec", str(spec_path)])
        assert code == cli.EXIT_USAGE
        assert "hard cap" in capsys.readouterr().err
        assert not pathlib.Path(env["root"], "missions", "m1").exists()
        assert _marker_count(env["marker"]) == 0


class TestPrecedence:
    def test_cli_objective_model_repo_win_over_spec(
            self, env: dict[str, str], tmp_path: pathlib.Path,
            capsys: pytest.CaptureFixture[str]) -> None:
        # Second repository: the spec's repo (loses to the CLI repo).
        repo_b = tmp_path / "repo_b"
        repo_b.mkdir()
        subprocess.run(["git", "init", "-q", str(repo_b)], check=True)
        subprocess.run(
            ["git", "-C", str(repo_b), "-c", "user.email=t@t.t",
             "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "b"],
            check=True)

        spec_path = tmp_path / "spec.json"
        _write_spec(spec_path, {
            "objective": "from spec",
            "repo": str(repo_b),
            "model": "specmodel",
        })
        code = cli.main(["--root", env["root"], "start", "m1",
                         "--spec", str(spec_path),
                         "--objective", "from cli",   # wins
                         "--model", "cliparser",      # wins
                         "--repo", env["repo"],       # wins over repo_b
                         "--head", env["head"],
                         "--pi-wrapper", env["provider"],
                         "--validate", "true"])
        assert code == cli.EXIT_OK
        capsys.readouterr()
        payload = _status_payload(env["root"], capsys)
        assert payload["objective"] == "from cli"
        mission, _ = store.load_mission(env["root"], "m1")
        assert mission.repo_root == env["repo"]       # CLI repo won
        assert mission.baseline_revision == env["head"]
        heavy_phases = [p for p in mission.phases if model.is_model_heavy(p.kind)]
        assert heavy_phases
        for phase in heavy_phases:
            assert "cliparser" in str(phase.command)
            assert "specmodel" not in str(phase.command)

    def test_spec_model_applies_when_absent_from_cli(
            self, env: dict[str, str], tmp_path: pathlib.Path,
            capsys: pytest.CaptureFixture[str]) -> None:
        spec_path = tmp_path / "spec.json"
        _write_spec(spec_path, {
            "objective": "obj",
            "repo": env["repo"],
            "model": "specmodel",
        })
        code = cli.main(["--root", env["root"], "start", "m1",
                         "--spec", str(spec_path),
                         "--head", env["head"],
                         "--objective", "obj",          # CLI objective wins
                         "--pi-wrapper", env["provider"],
                         "--validate", "true"])
        assert code == cli.EXIT_OK
        capsys.readouterr()
        mission, _ = store.load_mission(env["root"], "m1")
        assert mission.objective == "obj"
        assert any("specmodel" in str(p.command) for p in mission.phases)


class TestNormalizedLaunchEvidence:
    """Mission 006 — persisted normalized launch configuration.

    The launch event must persist the *effective* launch values (CLI >
    spec > default resolution) so later inspection does not depend on
    mutable/deleted external spec/objective files.
    """

    @staticmethod
    def _launch_event(root: str) -> dict[str, Any]:
        paths = store.mission_paths(root, "m1")
        events = [e for e in store.load_events(paths)
                  if e.get("event") == "launch"]
        assert len(events) == 1
        return events[0]

    def test_cli_override_is_persisted(
            self, env: dict[str, str], tmp_path: pathlib.Path,
            capsys: pytest.CaptureFixture[str]) -> None:
        spec_path = tmp_path / "spec.json"
        _write_spec(spec_path, {
            "objective": "obj",
            "model": "specmodel",
            "gpu_mem": 111,
            "time_budget": 555,
            "repair_budget": 1,
        })
        code = cli.main(["--root", env["root"], "start", "m1",
                         "--spec", str(spec_path),
                         "--repo", env["repo"],
                         "--head", env["head"],
                         "--model", "cliparser",      # wins over spec
                         "--gpu-mem", "222",          # wins over spec
                         "--time-budget", "666",      # wins over spec
                         "--repair-budget", "2",      # wins over spec
                         "--pi-wrapper", env["provider"],
                         "--validate", "true"])   # wins over spec default
        assert code == cli.EXIT_OK
        capsys.readouterr()
        normalized = self._launch_event(env["root"])["normalized"]
        assert normalized["model"] == "cliparser"
        assert normalized["gpu_mem"] == 222
        assert normalized["time_budget"] == 666
        assert normalized["repair_budget"] == 2
        assert normalized["repo"] == env["repo"]
        assert normalized["head"] == env["head"]
        assert normalized["pi_wrapper"] == env["provider"]
        assert normalized["validate"] == ["true"]

    def test_spec_derived_value_persisted_when_cli_absent(
            self, env: dict[str, str], tmp_path: pathlib.Path,
            capsys: pytest.CaptureFixture[str]) -> None:
        spec_path = tmp_path / "spec.json"
        _write_spec(spec_path, {
            "objective": "obj",
            "model": "specmodel",
            "gpu_mem": 333,
            "time_budget": 444,
            "repair_budget": 3,
            "subrun_budget": 8,
            "policy": ["cpu_slots=1"],
            "session_subruns": 8,
        })
        code = cli.main(["--root", env["root"], "start", "m1",
                         "--spec", str(spec_path),
                         "--repo", env["repo"],
                         "--head", env["head"],
                         "--pi-wrapper", env["provider"],
                         "--validate", "true"])
        assert code == cli.EXIT_OK
        capsys.readouterr()
        normalized = self._launch_event(env["root"])["normalized"]
        assert normalized["model"] == "specmodel"
        assert normalized["gpu_mem"] == 333
        assert normalized["time_budget"] == 444
        assert normalized["repair_budget"] == 3
        assert normalized["subrun_budget"] == 8
        assert normalized["policy"] == ["cpu_slots=1"]
        assert normalized["session_subruns"] == 8

    def test_defaults_persisted_deterministically(
            self, env: dict[str, str], tmp_path: pathlib.Path,
            capsys: pytest.CaptureFixture[str]) -> None:
        spec_path = tmp_path / "spec.json"
        _write_spec(spec_path, {"objective": "obj"})
        code = cli.main(["--root", env["root"], "start", "m1",
                         "--spec", str(spec_path),
                         "--repo", env["repo"],
                         "--head", env["head"],
                         "--pi-wrapper", env["provider"],
                         "--validate", "true"])
        assert code == cli.EXIT_OK
        capsys.readouterr()
        normalized = self._launch_event(env["root"])["normalized"]
        # Exactly the bounded key set, stable primitives/lists only.
        expected = {
            "repo": env["repo"],
            "head": env["head"],
            "pi_wrapper": env["provider"],
            "model": adapter.DEFAULT_MODEL,
            "validate": ["true"],
            "consolidate": None,
            "gpu": False,
            "gpu_mem": cli._DEFAULT_GPU_MEM_BYTES,
            "time_budget": model.DEFAULT_TIME_BUDGET_S,
            "subrun_budget": None,
            "repair_budget": model.MAX_REPAIR_ROUNDS,
            "policy": [],
            "session_subruns": None,
        }
        assert normalized == expected
        # No objective text duplicated in the launch event (mission state
        # persists the canonical objective; objective_source only).
        event = self._launch_event(env["root"])
        assert "objective" not in event
        assert "objective" not in normalized
        assert event["objective_source"] == "spec"

    def test_source_mutation_cannot_change_persisted_evidence(
            self, env: dict[str, str], tmp_path: pathlib.Path,
            capsys: pytest.CaptureFixture[str]) -> None:
        spec_path = tmp_path / "spec.json"
        _write_spec(spec_path, {
            "objective": "original objective",
            "model": "specmodel",
            "gpu_mem": 333,
        })
        code = cli.main(["--root", env["root"], "start", "m1",
                         "--spec", str(spec_path),
                         "--repo", env["repo"],
                         "--head", env["head"],
                         "--pi-wrapper", env["provider"],
                         "--validate", "true"])
        assert code == cli.EXIT_OK
        capsys.readouterr()
        before = self._launch_event(env["root"])
        # Mutate then delete the external spec: persisted evidence must
        # be unchanged (it does not depend on the file any more).
        spec_path.write_text(json.dumps({
            "objective": "tampered objective",
            "model": "othermodel", "gpu_mem": 999}) + "\n",
            encoding="utf-8")
        spec_path.unlink()
        after = self._launch_event(env["root"])
        assert before == after
        assert after["normalized"]["model"] == "specmodel"
        assert after["normalized"]["gpu_mem"] == 333
        assert after["spec_file"] == str(spec_path)


class TestContradictions:
    def test_spec_and_objective_file_rejected_no_state(
            self, env: dict[str, str], tmp_path: pathlib.Path,
            capsys: pytest.CaptureFixture[str]) -> None:
        spec_path = tmp_path / "spec.json"
        _write_spec(spec_path, {"objective": "x"})
        objective_path = tmp_path / "objective.txt"
        objective_path.write_text("obj", encoding="utf-8")
        code = cli.main(["--root", env["root"], "start", "m1",
                         "--spec", str(spec_path),
                         "--objective-file", str(objective_path)])
        assert code == cli.EXIT_USAGE
        assert "contradictory" in capsys.readouterr().err
        assert not pathlib.Path(env["root"], "missions", "m1").exists()
        assert _marker_count(env["marker"]) == 0

    def test_objective_and_objective_file_rejected_no_state(
            self, env: dict[str, str], tmp_path: pathlib.Path,
            capsys: pytest.CaptureFixture[str]) -> None:
        objective_path = tmp_path / "objective.txt"
        objective_path.write_text("obj", encoding="utf-8")
        code = cli.main(["--root", env["root"], "start", "m1",
                         "--objective", "from cli",
                         "--objective-file", str(objective_path)])
        assert code == cli.EXIT_USAGE
        assert "contradictory" in capsys.readouterr().err
        assert not pathlib.Path(env["root"], "missions", "m1").exists()
        assert _marker_count(env["marker"]) == 0

    def test_spec_objective_loses_to_cli_objective(
            self, env: dict[str, str], tmp_path: pathlib.Path,
            capsys: pytest.CaptureFixture[str]) -> None:
        # Allowed combination: objective from both spec and CLI — the
        # explicit CLI value wins (not a contradiction).
        spec_path = tmp_path / "spec.json"
        _write_spec(spec_path, {"objective": "from spec",
                                "repo": env["repo"],
                                "model": "specmodel"})
        code = cli.main(["--root", env["root"], "start", "m1",
                         "--spec", str(spec_path),
                         "--objective", "from cli",
                         "--head", env["head"],
                         "--pi-wrapper", env["provider"],
                         "--validate", "true"])
        assert code == cli.EXIT_OK
        capsys.readouterr()
        payload = _status_payload(env["root"], capsys)
        assert payload["objective"] == "from cli"


class TestPR203ReviewFixes:
    """Blocking PR #203 review findings — focused regressions.

    1. ``--objective-file`` persists ``objective_source="objective_file"``
       (not ``"cli"``), preserving CLI-over-spec precedence;
    2. invalid ``--policy`` / ``--session-subruns`` are rejected *before*
       mission creation (no mission state, no provider invocation,
       canonical usage exit code);
    3. launch-provenance persistence failure is controlled (no uncaught
       exception, no provider invocation, mission state intact and
       reconstructable).
    """

    def test_objective_file_persists_objective_source(
            self, env: dict[str, str], tmp_path: pathlib.Path,
            capsys: pytest.CaptureFixture[str]) -> None:
        objective_path = tmp_path / "objective.txt"
        objective_path.write_text("from objective file", encoding="utf-8")
        code = cli.main(["--root", env["root"], "start", "m1",
                         "--objective-file", str(objective_path),
                         "--repo", env["repo"], "--head", env["head"],
                         "--pi-wrapper", env["provider"], "--model", "fake",
                         "--validate", "true"])
        assert code == cli.EXIT_OK
        capsys.readouterr()
        events = [e for e in store.load_events(
            store.mission_paths(env["root"], "m1"))
            if e.get("event") == "launch"]
        assert len(events) == 1
        assert events[0]["objective_source"] == "objective_file"

    def test_invalid_policy_rejected_no_mission_no_provider(
            self, env: dict[str, str], capsys: pytest.CaptureFixture[str]
            ) -> None:
        code = cli.main(["--root", env["root"], "start", "m1",
                         "--objective", "obj",
                         "--repo", env["repo"], "--head", env["head"],
                         "--pi-wrapper", env["provider"], "--model", "fake",
                         "--validate", "true",
                         "--policy", "bogus_key=1"])
        assert code == cli.EXIT_USAGE
        assert "bogus_key" in capsys.readouterr().err
        assert not pathlib.Path(env["root"], "missions", "m1").exists()
        assert _marker_count(env["marker"]) == 0

    def test_invalid_session_subruns_rejected_no_mission_no_provider(
            self, env: dict[str, str], capsys: pytest.CaptureFixture[str]
            ) -> None:
        for bound in (0, model.MAX_SESSION_SUBRUNS + 1):
            code = cli.main(["--root", env["root"], "start", "m1",
                             "--objective", "obj",
                             "--repo", env["repo"], "--head", env["head"],
                             "--pi-wrapper", env["provider"],
                             "--model", "fake", "--validate", "true",
                             "--session-subruns", str(bound)])
            assert code == cli.EXIT_USAGE
            assert "out of bounds" in capsys.readouterr().err
            # Rejected before create: no mission state, no provider start.
            assert not pathlib.Path(env["root"], "missions", "m1").exists()
            assert _marker_count(env["marker"]) == 0

    def test_launch_event_persistence_failure_is_controlled(
            self, env: dict[str, str], tmp_path: pathlib.Path,
            capsys: pytest.CaptureFixture[str],
            monkeypatch: pytest.MonkeyPatch) -> None:
        objective_path = tmp_path / "objective.txt"
        objective_path.write_text("obj", encoding="utf-8")
        real_append = store.append_event

        def failing_append(paths: Any, event: Any) -> None:
            if event.get("event") == "launch":
                raise OSError("simulated persistence failure")
            real_append(paths, event)

        monkeypatch.setattr(cli.store, "append_event", failing_append)
        code = cli.main(["--root", env["root"], "start", "m1",
                         "--objective-file", str(objective_path),
                         "--repo", env["repo"], "--head", env["head"],
                         "--pi-wrapper", env["provider"], "--model", "fake",
                         "--validate", "true"])
        assert code == cli.EXIT_REJECTED
        err = capsys.readouterr().err
        assert "launch provenance persistence failed" in err
        # No uncaught exception; start never claims success.
        # No sub-run was launched.
        assert _marker_count(env["marker"]) == 0
        # Mission state is intact and reconstructable (created event
        # persisted; no launch event; load/status work).
        paths = store.mission_paths(env["root"], "m1")
        mission, _ = store.load_mission(env["root"], "m1")
        assert mission.mission_id == "m1"
        events = store.load_events(paths)
        assert any(e.get("event") == "created" for e in events)
        assert not any(e.get("event") == "launch" for e in events)
        assert cli.main(["--root", env["root"], "status", "m1"]) \
            == cli._state_exit(mission.mission_state)

    def test_cli_objective_still_beats_spec_objective_for_source(
            self, env: dict[str, str], tmp_path: pathlib.Path) -> None:
        # Precedence semantics preserved: explicit CLI objective over spec
        # objective is source "cli" (regression guard for finding 1).
        spec_path = tmp_path / "spec.json"
        _write_spec(spec_path, {"objective": "from spec",
                                "repo": env["repo"],
                                "model": "specmodel"})
        code = cli.main(["--root", env["root"], "start", "m1",
                         "--spec", str(spec_path),
                         "--objective", "from cli",
                         "--head", env["head"],
                         "--pi-wrapper", env["provider"],
                         "--validate", "true"])
        assert code == cli.EXIT_OK
        events = [e for e in store.load_events(
            store.mission_paths(env["root"], "m1"))
            if e.get("event") == "launch"]
        assert events[0]["objective_source"] == "cli"
        assert events[0]["objective_file"] is None
