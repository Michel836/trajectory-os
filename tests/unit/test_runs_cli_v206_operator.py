"""V2.06 operator CLI activation (Mission 001-A).

Focused, bounded tests for the operator-facing subcommands activated on top
of the existing V1.97–V2.06 core:

* advertised commands (``model.CLI_COMMANDS``) and parser commands agree;
* ``ops`` / ``observe`` are strictly read-only;
* ``reconstruct`` is explicit and deterministic (idempotent no-op second run);
* ``supervisor`` is bounded (cycle bound, no launch when not eligible);
* dependency / resource metadata (V2.03/V2.04) survive operator submission;
* malformed input fails closed (non-zero exit, state untouched).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

from trajectory_os.runs import cli, model, spec, store

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _roots(tmp_path: Path, name: str) -> tuple[Path, Path]:
    state_root = tmp_path / f"state-{name}"
    runs_root = tmp_path / f"runs-{name}"
    state_root.mkdir()
    runs_root.mkdir()
    return state_root, runs_root


def _enqueue(state_root: Path, job_id: str, spec_obj: spec.JobSpec) -> None:
    paths = store.state_paths(state_root)
    doc = store.QueueDoc.load(paths["queue"])
    doc.enqueue_spec(spec_obj)
    doc.save(paths["queue"])


def _state_fingerprint(state_root: Path) -> dict[str, str]:
    """Deterministic fingerprint of the durable state tree (bytes only)."""
    digests: dict[str, str] = {}
    for path in sorted(state_root.rglob("*")):
        if path.is_file():
            digests[str(path.relative_to(state_root))] = path.read_bytes().hex()
    return digests


def _queue_doc(state_root: Path) -> dict[str, Any]:
    paths = store.state_paths(state_root)
    return json.loads(paths["queue"].read_text(encoding="utf-8"))


def _run(argv: list[str], capsys: object) -> tuple[int, dict[str, Any]]:
    code = cli.main(argv)
    out = cast(str, capsys.readouterr().out)  # type: ignore[attr-defined]
    try:
        doc = json.loads(out)
    except json.JSONDecodeError:
        doc = {"_raw": out}
    return code, doc


# ---------------------------------------------------------------------------
# 1) advertised commands and parser commands are consistent
# ---------------------------------------------------------------------------


def test_advertised_commands_match_parser() -> None:
    parser = cli.build_parser()
    sub = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)][0]
    assert set(sub.choices) == set(model.CLI_COMMANDS)


def test_version_reports_tool_surface(capsys: object) -> None:
    code, doc = _run(["version", "--json"], capsys)
    assert code == 0
    assert doc["version"] == model.CLI_TOOL_VERSION
    assert "V1.99" in doc["capabilities"]
    assert "V2.00" in doc["capabilities"]


def test_version_capability_history_is_chronological(capsys: object) -> None:
    """The advertised capability history must be ordered chronologically
    (V1.99 before V2.00, etc.), not in insertion order."""
    code, doc = _run(["version", "--json"], capsys)
    assert code == 0
    keys = list(doc["capabilities"])
    def version_key(k: str) -> tuple[int, ...]:
        return tuple(int(part) for part in k[1:].split("."))

    assert len(keys) >= 2
    assert keys == sorted(keys, key=version_key), keys


# ---------------------------------------------------------------------------
# 2) ops / observe are strictly read-only
# ---------------------------------------------------------------------------


def test_ops_is_read_only(tmp_path: Path, capsys: object) -> None:
    state_root, runs_root = _roots(tmp_path, "ops")
    _enqueue(state_root, "job1", spec.build_spec("job1", ["true"], spec.EXEC_AD_HOC))
    before = _state_fingerprint(state_root)

    code, doc = _run(
        ["ops", "--state-root", str(state_root), "--runs-root", str(runs_root), "--json"],
        capsys,
    )
    assert code == 0
    assert doc["schema_version"] == model.OPS_SCHEMA_VERSION
    assert _state_fingerprint(state_root) == before  # no mutation at all


def test_observe_is_read_only(tmp_path: Path, capsys: object) -> None:
    state_root, runs_root = _roots(tmp_path, "observe")
    _enqueue(state_root, "job1", spec.build_spec("job1", ["true"], spec.EXEC_AD_HOC))
    before = _state_fingerprint(state_root)

    code, _ = _run(
        ["observe", "--state-root", str(state_root), "--runs-root", str(runs_root), "--json"],
        capsys,
    )
    assert code == 0
    assert _state_fingerprint(state_root) == before


# ---------------------------------------------------------------------------
# 3) reconstruction is explicit and deterministic
# ---------------------------------------------------------------------------


def test_reconstruction_is_explicit_and_deterministic(tmp_path: Path, capsys: object) -> None:
    state_root, runs_root = _roots(tmp_path, "reconstruct")
    _enqueue(
        state_root,
        "job1",
        spec.build_spec("job1", ["true"], spec.EXEC_AD_HOC, depends_on=["job-never"]),
    )
    argv = ["reconstruct", "--state-root", str(state_root), "--runs-root", str(runs_root), "--json"]

    code1, first = _run(argv, capsys)
    assert code1 == 0
    assert first["kind"] == "reconstruction"
    assert first["reconstructed"] is True
    assert first["buckets"]["dependency_blocked"] == 1

    # Deterministic + idempotent: a second explicit run yields the same report.
    code2, second = _run(argv, capsys)
    assert code2 == 0
    assert second["reconstructed"] is True
    assert first["buckets"] == second["buckets"]
    assert first["jobs"] == second["jobs"]
    assert first["mutations"] == second["mutations"]


# ---------------------------------------------------------------------------
# 4) supervisor is bounded
# ---------------------------------------------------------------------------


def test_supervisor_is_bounded_and_honest(tmp_path: Path, capsys: object) -> None:
    state_root, runs_root = _roots(tmp_path, "supervisor")
    # One queued job whose prerequisite never exists: never eligible, so the
    # session must terminate without launching anything.
    _enqueue(
        state_root,
        "job1",
        spec.build_spec("job1", ["true"], spec.EXEC_AD_HOC, depends_on=["job-never"]),
    )
    code, summary = _run(
        [
            "supervisor",
            "--state-root", str(state_root),
            "--runs-root", str(runs_root),
            "--cycles", "3",
            "--json",
        ],
        capsys,
    )
    assert code == 0
    assert summary["totals"]["launched"] == 0
    assert summary["stop"]["reason"] == model.STOP_NO_ELIGIBLE_WORK
    assert 1 <= summary["cycles"]["executed"] <= 3
    assert summary["cycles"]["bounded_by"] == model.MAX_SUPERVISOR_CYCLES


def test_supervisor_rejects_unbounded_cycles(tmp_path: Path, capsys: object) -> None:
    state_root, _ = _roots(tmp_path, "sup-limit")
    code, _ = _run(
        [
            "supervisor",
            "--state-root", str(state_root),
            "--cycles", str(model.MAX_SUPERVISOR_CYCLES + 1),
            "--json",
        ],
        capsys,
    )
    assert code == cli.EXIT_USAGE


# ---------------------------------------------------------------------------
# 5) dependency / resource metadata survives operator submission
# ---------------------------------------------------------------------------


def test_depends_on_and_resources_survive_enqueue(tmp_path: Path, capsys: object) -> None:
    state_root, _ = _roots(tmp_path, "meta")
    code, _ = _run(
        [
            "enqueue",
            "--state-root", str(state_root),
            "--depends-on", "job0,job00",
            "--resources", "cpu_slots=2,ram_bytes=1048576,gpu=false",
            "job1", "--", "/bin/true",
        ],
        capsys,
    )
    assert code == 0
    entry = _queue_doc(state_root)["entries"][0]
    assert entry["job_id"] == "job1"
    assert entry["command"] == ["/bin/true"]
    assert entry["spec"]["depends_on"] == ["job0", "job00"]
    resources = entry["spec"]["resources"]
    assert resources["cpu_slots"] == 2
    assert resources["ram_bytes"] == 1048576
    assert resources["gpu"] is False
    # Re-validation from the persisted spec stays valid (canonical store path).
    spec.JobSpec.from_dict(entry["spec"]).validate()


# ---------------------------------------------------------------------------
# 6) malformed input fails closed
# ---------------------------------------------------------------------------


def test_malformed_resources_fail_closed_without_mutation(tmp_path: Path, capsys: object) -> None:
    state_root, _ = _roots(tmp_path, "badres")
    for resources_flag in (
        "cpu_slots=0",      # below policy minimum
        "unknown_dim=1",    # unknown dimension
        "cpu_slots=oops",   # non-integer
        "cpu_slots=2,cpu_slots=3",  # duplicate dimension
        "gpu=garbage",      # malformed boolean must fail closed
    ):
        code, _ = _run(
            [
                "enqueue",
                "--state-root", str(state_root),
                "--resources", resources_flag,
                "job1", "--", "/bin/true",
            ],
            capsys,
        )
        assert code in (cli.EXIT_USAGE, cli.EXIT_REJECTED), resources_flag
    queue_path = store.state_paths(state_root)["queue"]
    if queue_path.exists():
        assert _queue_doc(state_root)["entries"] == []  # nothing persisted


def test_malformed_store_fails_closed_across_operator_commands(
    tmp_path: Path,
    capsys: object,
) -> None:
    state_root, runs_root = _roots(tmp_path, "malformed-operators")
    paths = store.state_paths(state_root)
    paths["queue"].parent.mkdir(parents=True, exist_ok=True)
    paths["queue"].write_text("{not-json", encoding="utf-8")

    commands = (
        ["observe", "--state-root", str(state_root), "--json"],
        ["reap", "--state-root", str(state_root), "--json"],
        [
            "reconstruct",
            "--state-root", str(state_root),
            "--runs-root", str(runs_root),
            "--json",
        ],
        [
            "supervisor",
            "--state-root", str(state_root),
            "--runs-root", str(runs_root),
            "--cycles", "1",
            "--json",
        ],
    )

    for argv in commands:
        code, doc = _run(argv, capsys)
        assert code == cli.EXIT_REJECTED, (argv, doc)

        if argv[0] == "reconstruct":
            # Reconstruction has its own deterministic fail-closed schema.
            assert doc["code"] == "RECONSTRUCTION_MALFORMED", (argv, doc)
        elif argv[0] == "supervisor":
            # Supervisor preserves its structured session summary while
            # surfacing malformed durable state as a rejected CLI outcome.
            assert doc["stop"]["reason"] == model.STOP_STATE_MALFORMED, (argv, doc)
            assert doc["any_unproven_residual"] is True, (argv, doc)
        else:
            assert doc.get("malformed") is True, (argv, doc)
            assert "code" in doc, (argv, doc)

    assert paths["queue"].read_text(encoding="utf-8") == "{not-json"


def test_self_dependency_fails_closed(tmp_path: Path, capsys: object) -> None:
    state_root, _ = _roots(tmp_path, "selfdep")
    code, _ = _run(
        [
            "enqueue",
            "--state-root", str(state_root),
            "--depends-on", "job1",
            "job1", "--", "/bin/true",
        ],
        capsys,
    )
    assert code == cli.EXIT_REJECTED
    queue_path = store.state_paths(state_root)["queue"]
    if queue_path.exists():
        assert _queue_doc(state_root)["entries"] == []


def test_malformed_queue_fails_closed_in_ops_and_enqueue(tmp_path: Path, capsys: object) -> None:
    state_root, _ = _roots(tmp_path, "malformed")
    paths = store.state_paths(state_root)
    paths["queue"].parent.mkdir(parents=True, exist_ok=True)
    paths["queue"].write_text("{not-json", encoding="utf-8")

    code1, doc1 = _run(
        ["ops", "--state-root", str(state_root), "--json"], capsys
    )
    assert code1 == cli.EXIT_REJECTED
    assert doc1.get("malformed") is True

    code2, _ = _run(
        [
            "enqueue", "--state-root", str(state_root),
            "job1", "--", "/bin/true",
        ],
        capsys,
    )
    assert code2 == cli.EXIT_REJECTED
    assert paths["queue"].read_text(encoding="utf-8") == "{not-json"  # untouched
