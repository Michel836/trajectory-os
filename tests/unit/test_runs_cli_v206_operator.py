"""V2.06 operator CLI activation (Mission 001-A).

Focused, bounded tests for the operator-facing subcommands activated on top
of the existing V1.97–V2.06 core:

* advertised commands (``model.CLI_COMMANDS``) and parser commands agree;
* ``ops`` / ``observe`` are strictly read-only;
* ``reconstruct`` is explicit and deterministic (idempotent no-op second run);
* ``supervisor`` is bounded (cycle bound, no launch when not eligible);
* dependency / resource metadata (V2.03/V2.04) survive operator submission;
* malformed input fails closed (non-zero exit, state untouched);
* canonical provenance/execution flags (Mission 001-D) round-trip through the
  persisted spec, and the closure-report contract rejects fabricated proofs.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

from trajectory_os.runs import cli, closure_report, model, spec, store

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


# ---------------------------------------------------------------------------
# 7) Mission 001-D (A/G): canonical provenance/execution flags + closure
#    report contract (fail-closed, no fabricated positive proof)
# ---------------------------------------------------------------------------


def test_enqueue_canonical_flags_round_trip_persisted_spec(
    tmp_path: Path, capsys: object
) -> None:
    state_root, _ = _roots(tmp_path, "canon")
    code, _ = _run(
        [
            "enqueue",
            "--state-root", str(state_root),
            "--execution-class", spec.EXEC_READ_ONLY,
            "--workspace-policy", spec.WORKSPACE_SHARED_READ_ONLY,
            "--runner", spec.RUNNER_TRAJECTORY_PI,
            "--repo-root", "/srv/repo",
            "--source-revision", "abc123",
            "--source-checkout", "/srv/repo",
            "--permit-failed-prereqs",
            "job1", "--", "/bin/true",
        ],
        capsys,
    )
    assert code == 0
    entry = _queue_doc(state_root)["entries"][0]
    s = entry["spec"]
    assert s["execution_class"] == spec.EXEC_READ_ONLY
    assert s["workspace_policy"] == spec.WORKSPACE_SHARED_READ_ONLY
    assert s["runner"] == spec.RUNNER_TRAJECTORY_PI
    assert s["repo_root"] == "/srv/repo"
    assert s["source_revision"] == "abc123"
    assert s["source_checkout"] == "/srv/repo"
    assert s["permit_failed_prereqs"] is True
    # Re-validation from the persisted spec stays valid (canonical store path).
    spec.JobSpec.from_dict(s).validate()


def test_enqueue_defaults_remain_backward_compatible(
    tmp_path: Path, capsys: object
) -> None:
    state_root, _ = _roots(tmp_path, "legacy")
    code, _ = _run(
        ["enqueue", "--state-root", str(state_root), "job1", "--", "/bin/true"],
        capsys,
    )
    assert code == 0
    s = _queue_doc(state_root)["entries"][0]["spec"]
    assert s["execution_class"] == spec.EXEC_AD_HOC
    assert s["workspace_policy"] == spec.WORKSPACE_ISOLATED
    assert s["runner"] == spec.RUNNER_GENERIC
    assert s["repo_root"] is None
    assert s["source_revision"] is None
    assert s["source_checkout"] is None
    assert s["permit_failed_prereqs"] is False
    spec.JobSpec.from_dict(s).validate()


def test_enqueue_preserves_command_args_matching_option_names(
    tmp_path: Path, capsys: object
) -> None:
    """PR #193 finding: a command argument after `--` that equals an enqueue
    option name is a legitimate user argument — it must be accepted AND
    preserved verbatim (no option-leak false rejection)."""
    state_root, _ = _roots(tmp_path, "payload-flags")
    code, _ = _run(
        [
            "enqueue",
            "--state-root", str(state_root),
            "--max-attempts", "2",
            "job1",
            "--",
            "echo", "--max-attempts", "3", "--json", "--capacity", "4",
        ],
        capsys,
    )
    assert code == 0
    entry = _queue_doc(state_root)["entries"][0]
    assert entry["command"] == ["echo", "--max-attempts", "3", "--json", "--capacity", "4"]
    s = entry["spec"]
    # The real option (before the job id) was parsed and validated, not lost.
    assert s["command"] == ["echo", "--max-attempts", "3", "--json", "--capacity", "4"]
    spec.JobSpec.from_dict(s).validate()


def test_enqueue_actual_options_before_job_id_still_validated(
    tmp_path: Path, capsys: object
) -> None:
    """The payload after the job id is data (never re-scanned for options);
    actual enqueue options before the job id are still parsed and the
    canonical validation (e.g. max_attempts bounds) still applies."""
    state_root, _ = _roots(tmp_path, "option-validation")
    # Out-of-range max_attempts is still rejected even with a payload after `--`.
    code, _ = _run(
        [
            "enqueue",
            "--state-root", str(state_root),
            "--max-attempts", "999",
            "job1",
            "--",
            "echo", "--max-attempts", "3",
        ],
        capsys,
    )
    assert code in (cli.EXIT_USAGE, cli.EXIT_REJECTED)
    queue_path = store.state_paths(state_root)["queue"]
    if queue_path.exists():
        assert _queue_doc(state_root)["entries"] == []  # nothing persisted


def test_malformed_canonical_flags_fail_closed_without_mutation(
    tmp_path: Path, capsys: object
) -> None:
    state_root, _ = _roots(tmp_path, "badflags")
    single_bad: list[tuple[str, str]] = [
        ("execution-class", "turbo"),
        ("workspace-policy", "wet"),
        ("runner", "nope"),
    ]
    for flag, value in single_bad:
        code, _ = _run(
            [
                "enqueue",
                "--state-root", str(state_root),
                f"--{flag}", value,
                "job1", "--", "/bin/true",
            ],
            capsys,
        )
        assert code in (cli.EXIT_USAGE, cli.EXIT_REJECTED), flag

    # Canonical safety rule: a mutating job must be isolated (and sourced).
    code, _ = _run(
        [
            "enqueue",
            "--state-root", str(state_root),
            "--execution-class", spec.EXEC_MUTATING,
            "--workspace-policy", spec.WORKSPACE_SHARED_READ_ONLY,
            "--repo-root", "/srv/repo",
            "--source-revision", "abc123",
            "--source-checkout", "/srv/repo",
            "job1", "--", "/bin/true",
        ],
        capsys,
    )
    assert code in (cli.EXIT_USAGE, cli.EXIT_REJECTED)

    queue_path = store.state_paths(state_root)["queue"]
    if queue_path.exists():
        assert _queue_doc(state_root)["entries"] == []  # nothing persisted


def _valid_closure_report() -> dict[str, Any]:
    lifecycle = {
        stage: {"status": closure_report.STATUS_MEASURED, "note": "unit"}
        for stage in (
            "enqueue", "start", "observe", "completion", "reap",
            "reconstruction",
        )
    }
    return {
        "schema_version": closure_report.CLOSURE_REPORT_SCHEMA_VERSION,
        "mission": "001-D",
        "baseline_commit": "f67098e74a42454a71051e4665346f7cbc9b9e89",
        "closure_branch": "mission/001-closure-evidence",
        "production_path_proof": {
            "status": closure_report.PROOF_PROVEN,
            "jobs_created": 2,
            "jobs_started": 2,
            "jobs_completed": 2,
            "max_concurrent_observed": 2,
            "concurrency_claim": closure_report.PROOF_PROVEN,
            "evidence": ["/tmp/proof/raw/production_path.json"],
        },
        "conflict_proof": {
            "status": closure_report.PROOF_PROVEN,
            "candidate_job": "job-b",
            "active_job": "job-a",
            "reason_code": model.ERR_SAME_WORKTREE_CONFLICT,
            "candidate_execution_class": spec.EXEC_MUTATING,
            "active_execution_class": spec.EXEC_MUTATING,
            "source_checkout": "/tmp/proof/src",
        },
        "lifecycle": lifecycle,
        "final_state": {"status": closure_report.PROOF_PROVEN, "note": "all terminal"},
        "quality_state": {
            "status": "PASS",
            "command": "bash scripts/quality.sh",
        },
        "review_state": "pending",
        "evidence": ["/tmp/proof/raw/production_path.json"],
    }


def test_closure_report_valid_report_passes() -> None:
    ok, violations = closure_report.validate_report(_valid_closure_report())
    assert violations == []
    assert ok is True


def test_closure_report_rejects_fabricated_proven_proof() -> None:
    doc = _valid_closure_report()
    # A "proven" claim whose numbers contradict its own evidence:
    # two jobs created but none ever started, zero concurrency, no evidence.
    doc["production_path_proof"] = {
        "status": closure_report.PROOF_PROVEN,
        "jobs_created": 2,
        "jobs_started": 0,
        "jobs_completed": 0,
        "max_concurrent_observed": 0,
    }
    ok, violations = closure_report.validate_report(doc)
    assert ok is False
    assert any("jobs_started >=" in v for v in violations)
    assert any("evidence list" in v for v in violations)


def test_closure_report_concurrent_claim_requires_two_live_jobs() -> None:
    doc = _valid_closure_report()
    proof = doc["production_path_proof"]
    proof["jobs_started"] = 1
    proof["jobs_completed"] = 1
    proof["max_concurrent_observed"] = 1
    ok, violations = closure_report.validate_report(doc)
    assert ok is False
    assert any("concurrent" in v for v in violations)


def test_closure_report_blocked_requires_reason() -> None:
    doc = _valid_closure_report()
    doc["production_path_proof"]["status"] = closure_report.PROOF_BLOCKED
    ok, violations = closure_report.validate_report(doc)
    assert ok is False
    assert any("blocking_reason" in v for v in violations)


def test_closure_report_proven_requires_measured_job_counters() -> None:
    """PR #193 finding: a PROOF_PROVEN production proof must carry the
    measured jobs_created/jobs_started/jobs_completed counters — omitting
    them is a fabricated positive proof and must be rejected."""
    for field in ("jobs_created", "jobs_started", "jobs_completed"):
        doc = _valid_closure_report()
        del doc["production_path_proof"][field]
        ok, violations = closure_report.validate_report(doc)
        assert ok is False, field
        assert any(field in v for v in violations), violations


def test_closure_report_proven_conflict_requires_canonical_reason_code() -> None:
    """PR #193 finding: a PROOF_PROVEN conflict proof must cite exactly the
    canonical SAME_WORKTREE_CONFLICT code; unrelated reason codes are
    rejected."""
    for bogus in ("SOME_OTHER_CODE", "WORKTREE", "UNEXPECTED_ERROR", ""):
        doc = _valid_closure_report()
        doc["conflict_proof"]["reason_code"] = bogus
        ok, violations = closure_report.validate_report(doc)
        assert ok is False, bogus
        assert any(model.ERR_SAME_WORKTREE_CONFLICT in v for v in violations), violations


def test_closure_report_blocked_conflict_allows_unrelated_reason_code() -> None:
    """The canonical-code rule applies only to proven claims: a blocked proof
    with a non-canonical reason code is not rejected on that ground."""
    doc = _valid_closure_report()
    conflict = doc["conflict_proof"]
    conflict["status"] = closure_report.PROOF_BLOCKED
    conflict["reason_code"] = "UNEXPECTED_ERROR"
    ok, violations = closure_report.validate_report(doc)
    assert ok is True, violations


def test_closure_report_render_is_deterministic_and_derived() -> None:
    doc = _valid_closure_report()
    first = closure_report.render_markdown(doc)
    second = closure_report.render_markdown(doc)
    assert first == second
    assert "001-D" in first
    assert model.ERR_SAME_WORKTREE_CONFLICT in first
    assert "pending" in first
