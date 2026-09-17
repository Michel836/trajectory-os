"""Mission 007 — structured semantic-result producer/consumer contract tests.

Covers:
* the semantic result contract (validation, sub-run binding, fail-closed
  interpretation, file reading);
* deterministic sub-run classification with semantic evidence
  (exit 0 + SUCCESS -> COMPLETED; everything else fail closed; process
  failure never upgraded by semantic evidence);
* the ProcessPhaseRunner subprocess contract (env vars, stale file
  clearing, real subprocess producers — no LLM involved).
"""

from __future__ import annotations

import json
from pathlib import Path

from trajectory_os.missions import model, runner, semantic

#: A producer (bash) that emits a semantic result with the status given as
#: $1, bound to $TRAJECTORY_SUBRUN_ID — mimics the canonical trajectory-pi
#: wrapper's terminal emission.
_PRODUCER_BODY = (
    'printf '
    '\'{"schema_version":1,"subrun_id":"%s","status":"%s",'
    '"agent_classification":"TEST_PRODUCER","reason":"test"}\' '
    '"$TRAJECTORY_SUBRUN_ID" "$1" > "$TRAJECTORY_SUBRUN_RESULT_FILE"'
)

#: A producer that writes an evidence file bound to the WRONG sub-run.
_WRONG_BIND_PRODUCER = (
    'printf '
    '\'{"schema_version":1,"subrun_id":"other-subrun","status":"SUCCESS",'
    '"agent_classification":"TEST_PRODUCER"}\' '
    '> "$TRAJECTORY_SUBRUN_RESULT_FILE"'
)

#: Mission 008 — a real producer that emits the frozen M007 core PLUS a
#: fully verifiable exact attestation, creating the wrapper run directory
#: and patch artifact the runner independently re-derives.
_V2_PRODUCER = r'''
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

status = sys.argv[1]
repo = Path(os.getcwd()).resolve()
head = subprocess.run(
    ["git", "-C", str(repo), "rev-parse", "HEAD"],
    capture_output=True, text=True, check=True).stdout.strip()
run_id = "run-" + os.environ["TRAJECTORY_SUBRUN_ID"]
run_dir = repo / ".trajectory-pi" / "runs" / run_id
run_dir.mkdir(parents=True, exist_ok=True)
(run_dir / "meta.txt").write_text(
    "run_id=" + run_id + "\n"
    "workspace=" + str(repo) + "\n"
    "head_before=" + head + "\n",
    encoding="utf-8")
patch = b""
(run_dir / "worktree.patch").write_bytes(patch)
patch_sha = hashlib.sha256(patch).hexdigest()
doc = {
    "schema_version": 1,
    "subrun_id": os.environ["TRAJECTORY_SUBRUN_ID"],
    "status": status,
    "agent_classification": "TEST_PRODUCER",
    "reason": "test",
    "attestation": {
        "schema_version": 1,
        "subrun_id": os.environ["TRAJECTORY_SUBRUN_ID"],
        "run_id": run_id,
        "repo_head_before": head,
        "repo_head_after": head,
        "patch_sha256": patch_sha,
    },
}
Path(os.environ["TRAJECTORY_SUBRUN_RESULT_FILE"]).write_text(
    json.dumps(doc), encoding="utf-8")
'''


def _v2_producer(tmp_path: Path, status: str = "SUCCESS") -> tuple[str, ...]:
    script = tmp_path / "v2_producer.py"
    script.write_text(_V2_PRODUCER, encoding="utf-8")
    return ("python3", str(script), status)


def _req(tmp: Path,
         command: tuple[str, ...] | None,
         subrun_id: str = "mr-m007-a1",
         phase_id: str = "plan",
         timeout_s: int = 30,
         cwd: str | None = None,
         ) -> runner.SubrunRequest:
    base = tmp / "ev"
    return runner.SubrunRequest(
        mission_id="m007",
        subrun_id=subrun_id,
        phase_id=phase_id,
        kind="IMPLEMENT",
        mode="feature",
        round=1,
        attempt=1,
        command=command or (),
        cwd=cwd,
        timeout_s=timeout_s,
        stdout_file=f"{base}/subrun.stdout.log",
        stderr_file=f"{base}/subrun.stderr.log",
        resources=None,
        semantic_required=True,
    )


def _sem_file(req: runner.SubrunRequest) -> Path:
    return runner.semantic_evidence_path(req)


def _bash(status: str) -> tuple[str, ...]:
    # bash -c SCRIPT _ ARG -> $1 == ARG ("_" is the positional $0)
    return ("bash", "-c", _PRODUCER_BODY, "_", status)


# -- the semantic contract itself (pure, deterministic) ------------------------


def test_success_bound_semantic_is_acceptable():
    ok = {"schema_version": 1, "subrun_id": "s1",
          "status": "SUCCESS", "reason": "done"}
    assert semantic.validate_semantic(ok) == "SUCCESS"
    semantic.validate_subrun_binding(ok, "s1")
    assert semantic.interpret_semantic(ok, "s1") == ("SUCCESS", None)
    assert semantic.semantic_supports_success(ok, "s1") is True


def test_non_success_statuses_are_rejected():
    for bad in ("INCOMPLETE", "FAILED", "PROVIDER_FAILURE", "UNKNOWN"):
        doc = {"schema_version": 1, "subrun_id": "s1", "status": bad}
        assert semantic.interpret_semantic(doc, "s1") == (bad, None)
        assert semantic.semantic_supports_success(doc, "s1") is False


def test_missing_subrun_id_is_rejected():
    doc = {"schema_version": 1, "status": "SUCCESS"}
    status, error = semantic.interpret_semantic(doc, "s1")
    assert status is None
    assert error == "SUBRUN_ID_MISSING"


def test_empty_or_oversized_fields_are_rejected():
    assert semantic.interpret_semantic(
        {"schema_version": 1, "subrun_id": "", "status": "SUCCESS"}, "s1") == \
        (None, "FIELD_INVALID")
    assert semantic.interpret_semantic(
        {"schema_version": 1, "subrun_id": "s1", "status": "SUCCESS",
         "reason": "x" * 513}, "s1") == \
        (None, "FIELD_INVALID")
    # null fields are contract violations (fail closed)
    assert semantic.interpret_semantic(
        {"schema_version": 1, "subrun_id": "s1", "status": "SUCCESS",
         "reason": None}, "s1") == (None, "FIELD_INVALID")


def test_bad_schema_version_and_status_are_rejected():
    assert semantic.interpret_semantic(
        {"schema_version": 2, "subrun_id": "s1", "status": "SUCCESS"}, "s1") == \
        (None, "SCHEMA_VERSION_INVALID")
    assert semantic.interpret_semantic(
        {"schema_version": 1, "subrun_id": "s1", "status": "DONE"}, "s1") == \
        (None, "STATUS_INVALID")
    assert semantic.interpret_semantic(
        "not a dict", "s1") == (None, "MALFORMED")
    assert semantic.interpret_semantic(None, "s1") == (None, "MISSING")


def test_subrun_binding_mismatch_is_rejected():
    doc = {"schema_version": 1, "subrun_id": "other", "status": "SUCCESS"}
    status, error = semantic.interpret_semantic(doc, "mr-m007-a1")
    assert status is None
    assert error == "SUBRUN_MISMATCH"
    assert semantic.semantic_supports_success(doc, "mr-m007-a1") is False


def test_read_semantic_file_fail_closed(tmp_path: Path):
    p = tmp_path / "absent.json"
    assert semantic.read_semantic_file(str(p)) == (None, "MISSING")

    empty = tmp_path / "empty.json"
    empty.write_text("   \n", encoding="utf-8")
    assert semantic.read_semantic_file(str(empty))[0] is None

    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    doc, err = semantic.read_semantic_file(str(bad))
    assert doc is None and err.startswith("MALFORMED")

    good = tmp_path / "good.json"
    good.write_text(json.dumps(
        {"schema_version": 1, "subrun_id": "s1", "status": "SUCCESS"}),
        encoding="utf-8")
    doc, err = semantic.read_semantic_file(str(good))
    assert err is None and doc["status"] == "SUCCESS"


# -- pure classification (fail closed) ------------------------------------------


def test_classification_semantic_matrix():
    # exit 0: a VERIFIED exact attestation plus SUCCESS is the only route to
    # COMPLETED (Mission 008). An unattested SUCCESS is explicitly UNPROVEN.
    assert runner.classify_subrun(
        0, semantic_status="SUCCESS",
        attestation=semantic.ATTESTATION_VERIFIED).classification \
        == model.CR_COMPLETED
    assert runner.classify_subrun(0, semantic_status="SUCCESS").classification \
        == model.CR_UNPROVEN
    assert runner.classify_subrun(
        0, semantic_status="SUCCESS",
        attestation_error=semantic.ATT_MISSING).classification \
        == model.CR_UNPROVEN
    assert runner.classify_subrun(0, semantic_status="INCOMPLETE").classification \
        == model.CR_UNPROVEN
    assert runner.classify_subrun(0, semantic_status="FAILED").classification \
        == model.CR_FAILED
    assert runner.classify_subrun(0, semantic_status="PROVIDER_FAILURE").classification \
        == model.CR_CRASHED
    assert runner.classify_subrun(0, semantic_status="UNKNOWN").classification \
        == model.CR_UNPROVEN
    # no semantic evidence -> fail closed (never COMPLETED)
    assert runner.classify_subrun(0).classification == model.CR_UNPROVEN
    assert runner.classify_subrun(0, semantic_status=None,
                                  semantic_error="MALFORMED:x").classification \
        == model.CR_UNPROVEN


def test_process_failure_is_never_upgraded():
    r = runner.classify_subrun(
        3, semantic_status="SUCCESS",
        attestation=semantic.ATTESTATION_VERIFIED)
    assert r.classification == model.CR_FAILED
    assert r.semantic_status == "SUCCESS"  # recorded for audit
    assert r.attestation == semantic.ATTESTATION_VERIFIED
    r = runner.classify_subrun(143, semantic_status="PROVIDER_FAILURE")
    assert r.classification == model.CR_CRASHED
    r = runner.classify_subrun(None, timed_out=True, semantic_status="SUCCESS")
    assert r.classification == model.CR_CRASHED
    assert r.timed_out is True


# -- ProcessPhaseRunner subprocess contract (real, deterministic) ---------------


def _mk(tmp: Path, **kw) -> runner.SubrunRequest:
    kw.setdefault("command", None)
    return _req(tmp, **kw)


def _git_repo(tmp_path: Path) -> str:
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t.t",
         "-c", "user.name=t", "commit", "-q", "--allow-empty",
         "-m", "baseline"],
        check=True,
    )
    return str(repo)


def test_e2e_exit0_success_semantic(tmp_path: Path):
    repo = _git_repo(tmp_path)
    req = _req(tmp_path, command=_v2_producer(tmp_path), cwd=repo)
    res = runner.ProcessPhaseRunner().run(req)
    assert res.exit_code == 0
    assert res.classification == model.CR_COMPLETED
    assert res.semantic_status == "SUCCESS"
    assert res.semantic_error is None
    assert res.attestation == semantic.ATTESTATION_VERIFIED
    assert res.attestation_error is None


def test_e2e_unattested_success_is_not_promoted(tmp_path: Path):
    """A legacy M007 v1 SUCCESS (no attestation) is never COMPLETED."""
    repo = _git_repo(tmp_path)
    req = _req(tmp_path, command=_bash("SUCCESS"), cwd=repo)
    res = runner.ProcessPhaseRunner().run(req)
    assert res.exit_code == 0
    assert res.semantic_status == "SUCCESS"
    assert res.classification == model.CR_UNPROVEN
    assert res.attestation is None
    assert res.attestation_error == semantic.ATT_MISSING


def test_e2e_exit0_incomplete_semantic(tmp_path: Path):
    req = _mk(tmp_path, command=_bash("INCOMPLETE"))
    res = runner.ProcessPhaseRunner().run(req)
    assert res.exit_code == 0
    assert res.classification == model.CR_UNPROVEN
    assert res.semantic_status == "INCOMPLETE"


def test_e2e_exit0_no_semantic_file(tmp_path: Path):
    req = _mk(tmp_path, command=("echo", "no evidence"))
    res = runner.ProcessPhaseRunner().run(req)
    assert res.exit_code == 0
    assert res.classification == model.CR_UNPROVEN
    assert res.semantic_status is None
    assert res.semantic_error == "MISSING"


def test_e2e_exit0_malformed_semantic(tmp_path: Path):
    req = _mk(tmp_path,
              command=("bash", "-c",
                       'echo "{ not json" > "$TRAJECTORY_SUBRUN_RESULT_FILE"'))
    res = runner.ProcessPhaseRunner().run(req)
    assert res.exit_code == 0
    assert res.classification == model.CR_UNPROVEN
    assert res.semantic_error.startswith("MALFORMED")


def test_e2e_exit0_wrong_subrun_binding(tmp_path: Path):
    req = _mk(tmp_path, command=("bash", "-c", _WRONG_BIND_PRODUCER))
    res = runner.ProcessPhaseRunner().run(req)
    assert res.exit_code == 0
    assert res.classification == model.CR_UNPROVEN
    assert res.semantic_status is None
    assert res.semantic_error == "SUBRUN_MISMATCH"


def test_e2e_failure_with_success_file_is_not_promoted(tmp_path: Path):
    req = _req(tmp_path,
               command=("bash", "-c",
                        _PRODUCER_BODY + " && exit 3", "_", "SUCCESS"))
    res = runner.ProcessPhaseRunner().run(req)
    assert res.exit_code == 3
    assert res.classification == model.CR_FAILED
    assert res.semantic_status == "SUCCESS"  # recorded, not promoted


def test_e2e_stale_semantic_file_is_cleared(tmp_path: Path):
    req = _mk(tmp_path, command=("echo", "stale"))
    # A PRE-EXISTING success file (e.g., from a previous attempt) must not
    # count as THIS sub-run's evidence — the runner clears it before launch.
    _sem_file(req).parent.mkdir(parents=True, exist_ok=True)
    _sem_file(req).write_text(json.dumps(
        {"schema_version": 1, "subrun_id": req.subrun_id, "status": "SUCCESS"}),
        encoding="utf-8")
    res = runner.ProcessPhaseRunner().run(req)
    assert res.exit_code == 0
    assert res.classification == model.CR_UNPROVEN
    assert res.semantic_status is None
    assert not _sem_file(req).exists()


def test_e2e_timeout_is_crashed(tmp_path: Path):
    req = _req(tmp_path, command=("sleep", "10"), timeout_s=1)
    res = runner.ProcessPhaseRunner().run(req)
    assert res.exit_code is None
    assert res.timed_out is True
    assert res.classification == model.CR_CRASHED


def test_semantic_env_vars_are_contract(tmp_path: Path):
    """The runner must export the result file + exact sub-run id."""
    cmd = ("bash", "-c",
           'echo "F=$TRAJECTORY_SUBRUN_RESULT_FILE"'
           ' echo "I=$TRAJECTORY_SUBRUN_ID"')
    req = _req(tmp_path, command=cmd, subrun_id="mr-m007-a1")
    run = runner.ProcessPhaseRunner().run(req)
    assert run.exit_code == 0
    out = Path(req.stdout_file).read_text(encoding="utf-8")
    assert f"F={runner.semantic_evidence_path(req)}" in out
    assert "I=mr-m007-a1" in out
    # and that file is exactly where the producer wrote:
    assert run.semantic_error == "MISSING" or _sem_file(req).is_file()


def test_empty_command_is_unproven(tmp_path: Path):
    req = _mk(tmp_path)
    res = runner.ProcessPhaseRunner().run(req)
    assert res.exit_code is None
    assert res.classification == model.CR_UNPROVEN


def test_semantic_evidence_path_is_per_subrun(tmp_path: Path):
    a = _mk(tmp_path, subrun_id="plan-a1")
    b = _mk(tmp_path, subrun_id="implement-a1")
    assert _sem_file(a) != _sem_file(b)
    assert a.subrun_id in _sem_file(a).name
    assert b.subrun_id in _sem_file(b).name
    try:
        runner.semantic_evidence_path(
            _req(tmp_path, ("x",), subrun_id=""))
        raise AssertionError("expected ValueError for empty subrun_id")
    except ValueError:
        pass


def test_semantic_success_is_persisted_and_exposed(tmp_path: Path):
    """Mission 007: verified semantic provenance survives durable storage."""
    from trajectory_os.missions import orchestrator, store

    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.email=t@t.t",
            "-c", "user.name=t",
            "commit", "-q", "--allow-empty", "-m", "baseline",
        ],
        check=True,
    )
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    root = tmp_path / "root"

    commands = {
        model.PH_PLAN: _v2_producer(tmp_path, "SUCCESS"),
        model.PH_IMPLEMENT: _v2_producer(tmp_path, "SUCCESS"),
        model.PH_VALIDATE: ("true",),
        model.PH_REVIEW: _v2_producer(tmp_path, "SUCCESS"),
        model.PH_CONSOLIDATE: ("true",),
    }

    cfg = orchestrator.MissionConfig(
        mission_id="m007-persist",
        objective="persist semantic provenance",
        repo_root=str(repo),
        cwd=str(repo),
        baseline_revision=head,
        phase_specs=orchestrator.default_phase_specs(
            commands, repair_budget=1),
        time_budget_s=300,
        repair_budget=1,
        subrun_budget=8,
    )

    orchestrator.create_mission(str(root), cfg)
    report = orchestrator.run_mission(
        str(root),
        "m007-persist",
        runner.ProcessPhaseRunner(),
    )

    assert report.mission_state == model.MS_COMPLETE

    mission, paths = store.load_mission(str(root), "m007-persist")
    implement = mission.phase("implement")
    assert implement.state == model.PS_PASSED
    assert implement.subrun_ids == ["implement-a1"]

    record = store.load_subrun(paths, "implement-a1")
    assert record.semantic_aware is True
    assert record.semantic_status == semantic.STATUS_SUCCESS
    assert record.semantic_error is None
    assert record.semantic_agent_classification == "TEST_PRODUCER"
    assert record.semantic_readiness is None
    assert record.semantic_reason == "test"
    assert record.attestation == semantic.ATTESTATION_VERIFIED
    assert record.attestation_error is None

    persisted = record.to_dict()
    assert persisted["semantic_status"] == "SUCCESS"
    assert persisted["semantic_agent_classification"] == "TEST_PRODUCER"
    assert persisted["semantic_reason"] == "test"
    assert persisted["attestation"] == semantic.ATTESTATION_VERIFIED

    evidence = store.load_phase_evidence(paths, "implement")
    last = evidence["last_subrun"]
    assert last["classification"] == model.CR_COMPLETED
    assert last["semantic_status"] == "SUCCESS"
    assert last["semantic_agent_classification"] == "TEST_PRODUCER"
    assert last["semantic_reason"] == "test"


def test_persisted_completed_without_success_semantic_fails_closed(
        tmp_path: Path):
    """Durable COMPLETED may only coexist with semantic SUCCESS."""
    from trajectory_os.missions import store

    record = store.SubrunDoc(
        subrun_id="implement-a1",
        phase_id="implement",
        kind=model.PH_IMPLEMENT,
        mode="IMPLEMENT",
        round=0,
        attempt=1,
        command=["true"],
        cwd=None,
        started_at="2026-09-15T00:00:00Z",
        finished_at="2026-09-15T00:00:01Z",
        exit_code=0,
        classification=model.CR_COMPLETED,
        stdout_file="stdout.log",
        stderr_file="stderr.log",
        resources=None,
        semantic_status=semantic.STATUS_INCOMPLETE,
        semantic_error=None,
        semantic_agent_classification="INCOMPLETE_AGENT_RUN",
        semantic_readiness="NOT_READY",
        semantic_reason="incomplete",
        semantic_aware=True,
    )

    path = tmp_path / "implement-a1.json"
    path.write_text(
        json.dumps(record.to_dict()),
        encoding="utf-8",
    )

    try:
        store.SubrunDoc.from_dict(
            json.loads(path.read_text(encoding="utf-8")),
            str(path),
        )
        raise AssertionError("expected persisted semantic contradiction")
    except store.MalformedMissionError as exc:
        assert exc.code == model.R_CONTRADICTION
