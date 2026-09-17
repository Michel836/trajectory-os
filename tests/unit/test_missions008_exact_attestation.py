"""Mission 008 — exact execution attestation (contract + independent verify).

Covers:
* the pure, versioned attestation contract (shape, partial, malformed,
  contradictory, binding, aliases);
* the runner's independent verification (run identity / meta cross-check,
  clock-free staleness anchor, re-derived HEAD, recomputed patch digest);
* fail-closed gating: only a verified attestation + SUCCESS -> COMPLETED,
  legacy M007 v1 SUCCESS -> UNPROVEN (never silently promoted), process
  failure precedence preserved, deterministic phases untouched;
* durable persistence + backward readability of legacy records.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from trajectory_os.missions import model, runner, semantic, store

_EMPTY_SHA = hashlib.sha256(b"").hexdigest()
_OTHER_HEAD = "0" * 40

#: A real producer that emits the frozen M007 core plus a configurable
#: attestation and creates the wrapper run directory the runner re-checks.
_PRODUCER = r'''
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

status = sys.argv[1]
cfg = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
repo = Path(os.getcwd()).resolve()
head = subprocess.run(
    ["git", "-C", str(repo), "rev-parse", "HEAD"],
    capture_output=True, text=True, check=True).stdout.strip()
subrun = os.environ["TRAJECTORY_SUBRUN_ID"]
run_id = cfg.get("run_id", "run-" + subrun)
run_dir = repo / ".trajectory-pi" / "runs" / run_id
if cfg.get("write_meta", True):
    run_dir.mkdir(parents=True, exist_ok=True)
    meta_ws = cfg.get("workspace", str(repo))
    meta_head = cfg.get("head_before", head)
    (run_dir / "meta.txt").write_text(
        "run_id=" + run_id + "\n"
        "workspace=" + meta_ws + "\n"
        "head_before=" + meta_head + "\n",
        encoding="utf-8")
    patch_bytes = cfg.get("patch_bytes", "").encode("utf-8")
    (run_dir / "worktree.patch").write_bytes(patch_bytes)
    if cfg.get("stale"):
        os.utime(run_dir / "meta.txt", (1.0, 1.0))
doc = {
    "schema_version": 1,
    "subrun_id": subrun,
    "status": status,
    "agent_classification": "TEST_PRODUCER",
    "reason": "test",
}
if cfg.get("attestation", True):
    patch_sha = cfg.get("patch_sha256")
    if patch_sha is None:
        patch_sha = hashlib.sha256(
            cfg.get("patch_bytes", "").encode("utf-8")).hexdigest()
    att = {
        "schema_version": cfg.get("attestation_schema", 1),
        "subrun_id": subrun,
        "run_id": cfg.get("att_run_id", run_id),
        "repo_head_before": cfg.get("repo_head_before", head),
        "repo_head_after": cfg.get("repo_head_after", head),
        "patch_sha256": patch_sha,
    }
    omit = cfg.get("omit_field")
    if omit:
        att.pop(omit, None)
    doc["attestation"] = att
Path(os.environ["TRAJECTORY_SUBRUN_RESULT_FILE"]).write_text(
    json.dumps(doc), encoding="utf-8")
'''


def _git_repo(tmp_path: Path) -> str:
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


def _producer(tmp_path: Path, status: str = "SUCCESS",
              **cfg: object) -> tuple[str, ...]:
    script = tmp_path / "producer.py"
    script.write_text(_PRODUCER, encoding="utf-8")
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    return ("python3", str(script), status, str(cfg_path))


def _req(tmp: Path, command: tuple[str, ...] | None, *,
         subrun_id: str = "implement-a1", phase_id: str = "implement",
         kind: str = "IMPLEMENT", cwd: str | None = None,
         semantic_required: bool = True,
         timeout_s: int = 30) -> runner.SubrunRequest:
    base = tmp / "ev"
    return runner.SubrunRequest(
        mission_id="m008",
        subrun_id=subrun_id,
        phase_id=phase_id,
        kind=kind,
        mode="IMPLEMENT",
        round=0,
        attempt=1,
        command=command or (),
        cwd=cwd,
        timeout_s=timeout_s,
        stdout_file=f"{base}/{subrun_id}.stdout.log",
        stderr_file=f"{base}/{subrun_id}.stderr.log",
        resources=None,
        semantic_required=semantic_required,
    )


def _valid_att(**over: object) -> dict[str, object]:
    att: dict[str, object] = {
        "schema_version": 1,
        "subrun_id": "s1",
        "run_id": "20260101-000000",
        "repo_head_before": "a" * 40,
        "repo_head_after": "a" * 40,
        "patch_sha256": "b" * 64,
    }
    att.update(over)
    return att


# -- pure contract -------------------------------------------------------------


def test_validate_attestation_accepts_complete_wellformed():
    assert semantic.validate_attestation(_valid_att()) is None


def test_validate_attestation_missing_and_malformed():
    assert semantic.validate_attestation(None) == semantic.ATT_MISSING
    assert semantic.validate_attestation(["not", "a", "dict"]) \
        == semantic.ATT_MALFORMED
    assert semantic.validate_attestation(_valid_att(schema_version=2)) \
        == semantic.ATT_MALFORMED
    assert semantic.validate_attestation(_valid_att(run_id=5)) \
        == semantic.ATT_MALFORMED
    assert semantic.validate_attestation(_valid_att(repo_head_before="zz")) \
        == semantic.ATT_MALFORMED
    assert semantic.validate_attestation(_valid_att(patch_sha256="deadbeef")) \
        == semantic.ATT_MALFORMED
    assert semantic.validate_attestation(_valid_att(run_id="x" * 513)) \
        == semantic.ATT_MALFORMED


def test_validate_attestation_partial_and_contradictory():
    for field in ("run_id", "repo_head_before", "repo_head_after",
                  "patch_sha256"):
        att = _valid_att()
        del att[field]
        assert semantic.validate_attestation(att) == semantic.ATT_PARTIAL
        assert semantic.validate_attestation(
            _valid_att(**{field: "   "})) == semantic.ATT_PARTIAL
    assert semantic.validate_attestation(
        _valid_att(repo_head_after="c" * 40)) == semantic.ATT_CONTRADICTORY


def test_validate_attestation_aliases():
    att = {
        "schema_version": 1,
        "wrapper_run_id": "r1",
        "head_before": "a" * 40,
        "head_after": "a" * 40,
        "evidence_sha256": "b" * 64,
    }
    assert semantic.validate_attestation(att) is None


def test_extract_attestation_nested_flat_and_absent():
    nested = {"schema_version": 1, "subrun_id": "s1",
              "attestation": _valid_att()}
    assert semantic.extract_attestation(nested) == _valid_att()
    assert semantic.extract_attestation({"subrun_id": "s1", "status": "SUCCESS",
                                         "run_id": "r1"}) is not None
    assert semantic.extract_attestation(
        {"schema_version": 1, "subrun_id": "s1", "status": "SUCCESS"}) is None
    assert semantic.extract_attestation("nope") is None


def test_validate_attestation_binding():
    semantic.validate_attestation_binding(_valid_att(), "s1")  # matches
    semantic.validate_attestation_binding({"run_id": "r1"}, "s1")  # optional
    try:
        semantic.validate_attestation_binding(_valid_att(), "other")
        raise AssertionError("expected mismatch")
    except semantic.SemanticError as exc:
        assert exc.code == semantic.ATT_MISMATCH


# -- runner independent verification -------------------------------------------


def test_runner_verifies_exact_attestation(tmp_path: Path):
    repo = _git_repo(tmp_path)
    req = _req(tmp_path, _producer(tmp_path), cwd=repo)
    res = runner.ProcessPhaseRunner().run(req)
    assert res.classification == model.CR_COMPLETED
    assert res.semantic_status == semantic.STATUS_SUCCESS
    assert res.attestation == semantic.ATTESTATION_VERIFIED
    assert res.attestation_error is None
    assert res.attestation_verified is True


def test_runner_rejects_missing_attestation(tmp_path: Path):
    repo = _git_repo(tmp_path)
    req = _req(tmp_path, _producer(tmp_path, attestation=False), cwd=repo)
    res = runner.ProcessPhaseRunner().run(req)
    assert res.semantic_status == semantic.STATUS_SUCCESS
    assert res.classification == model.CR_UNPROVEN
    assert res.attestation is None
    assert res.attestation_error == semantic.ATT_MISSING


def test_runner_rejects_partial_attestation(tmp_path: Path):
    repo = _git_repo(tmp_path)
    req = _req(tmp_path, _producer(tmp_path, omit_field="run_id"), cwd=repo)
    res = runner.ProcessPhaseRunner().run(req)
    assert res.classification == model.CR_UNPROVEN
    assert res.attestation_error == semantic.ATT_PARTIAL


def test_runner_rejects_tampered_patch_digest(tmp_path: Path):
    repo = _git_repo(tmp_path)
    req = _req(tmp_path,
               _producer(tmp_path, patch_bytes="hello", patch_sha256=_EMPTY_SHA),
               cwd=repo)
    res = runner.ProcessPhaseRunner().run(req)
    assert res.classification == model.CR_UNPROVEN
    assert res.attestation_error == semantic.ATT_MISMATCH


def test_runner_rejects_forged_run_identity(tmp_path: Path):
    repo = _git_repo(tmp_path)
    req = _req(tmp_path,
               _producer(tmp_path, att_run_id="run-does-not-exist"),
               cwd=repo)
    res = runner.ProcessPhaseRunner().run(req)
    assert res.classification == model.CR_UNPROVEN
    assert res.attestation_error == semantic.ATT_STALE


def test_runner_rejects_stale_run_directory(tmp_path: Path):
    repo = _git_repo(tmp_path)
    req = _req(tmp_path, _producer(tmp_path, stale=True), cwd=repo)
    res = runner.ProcessPhaseRunner().run(req)
    assert res.classification == model.CR_UNPROVEN
    assert res.attestation_error == semantic.ATT_STALE


def test_runner_rejects_workspace_mismatch(tmp_path: Path):
    repo = _git_repo(tmp_path)
    req = _req(tmp_path,
               _producer(tmp_path, workspace=str(tmp_path / "elsewhere")),
               cwd=repo)
    res = runner.ProcessPhaseRunner().run(req)
    assert res.classification == model.CR_UNPROVEN
    assert res.attestation_error == semantic.ATT_MISMATCH


def test_runner_rejects_meta_head_mismatch(tmp_path: Path):
    repo = _git_repo(tmp_path)
    req = _req(tmp_path, _producer(tmp_path, head_before=_OTHER_HEAD), cwd=repo)
    res = runner.ProcessPhaseRunner().run(req)
    assert res.classification == model.CR_UNPROVEN
    assert res.attestation_error == semantic.ATT_MISMATCH


def test_runner_rejects_head_disagreement(tmp_path: Path):
    repo = _git_repo(tmp_path)
    req = _req(tmp_path,
               _producer(tmp_path, repo_head_before=_OTHER_HEAD,
                         repo_head_after=_OTHER_HEAD),
               cwd=repo)
    res = runner.ProcessPhaseRunner().run(req)
    assert res.classification == model.CR_UNPROVEN
    assert res.attestation_error == semantic.ATT_MISMATCH


def test_runner_rejects_contradictory_heads(tmp_path: Path):
    repo = _git_repo(tmp_path)
    req = _req(tmp_path,
               _producer(tmp_path, repo_head_after=_OTHER_HEAD), cwd=repo)
    res = runner.ProcessPhaseRunner().run(req)
    assert res.classification == model.CR_UNPROVEN
    assert res.attestation_error == semantic.ATT_CONTRADICTORY


def test_runner_without_repo_fails_closed(tmp_path: Path):
    req = _req(tmp_path, _producer(tmp_path), cwd=None)
    res = runner.ProcessPhaseRunner().run(req)
    assert res.classification == model.CR_UNPROVEN
    assert res.attestation_error == semantic.ATT_MISMATCH


def test_process_failure_precedence_with_verified_attestation(tmp_path: Path):
    repo = _git_repo(tmp_path)
    cmd = _producer(tmp_path)
    req = _req(tmp_path, ("bash", "-c",
                          " ".join(cmd) + "; exit 3"), cwd=repo)
    res = runner.ProcessPhaseRunner().run(req)
    assert res.exit_code == 3
    assert res.classification == model.CR_FAILED
    assert res.semantic_status == semantic.STATUS_SUCCESS
    assert res.attestation == semantic.ATTESTATION_VERIFIED


def test_deterministic_phase_semantics_unchanged(tmp_path: Path):
    req = _req(tmp_path, ("true",), kind="VALIDATE",
               phase_id="validate", semantic_required=False)
    res = runner.ProcessPhaseRunner().run(req)
    assert res.exit_code == 0
    assert res.classification == model.CR_COMPLETED
    assert res.semantic_status is None
    assert res.attestation is None
    assert res.attestation_error is None


# -- durable persistence + backward readability --------------------------------


def _attested_record(**over: object) -> store.SubrunDoc:
    base: dict[str, object] = {
        "subrun_id": "implement-a1",
        "phase_id": "implement",
        "kind": model.PH_IMPLEMENT,
        "mode": "IMPLEMENT",
        "round": 0,
        "attempt": 1,
        "command": ["true"],
        "cwd": None,
        "started_at": "2026-09-15T00:00:00Z",
        "finished_at": "2026-09-15T00:00:01Z",
        "exit_code": 0,
        "classification": model.CR_COMPLETED,
        "stdout_file": "stdout.log",
        "stderr_file": "stderr.log",
        "resources": None,
        "semantic_status": semantic.STATUS_SUCCESS,
        "semantic_error": None,
        "semantic_agent_classification": "AGENT_COMPLETED",
        "semantic_readiness": None,
        "semantic_reason": "done",
        "semantic_aware": True,
        "attestation": semantic.ATTESTATION_VERIFIED,
        "attestation_error": None,
    }
    base.update(over)
    return store.SubrunDoc(**base)  # type: ignore[arg-type]


def test_attested_record_round_trips(tmp_path: Path) -> None:
    doc = _attested_record().to_dict()
    assert doc["attestation"] == semantic.ATTESTATION_VERIFIED
    assert doc["attestation_error"] is None
    path = tmp_path / "implement-a1.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    loaded = store.SubrunDoc.from_dict(doc, str(path))
    assert loaded.attestation == semantic.ATTESTATION_VERIFIED
    assert loaded.attestation_error is None
    assert loaded.classification == model.CR_COMPLETED


def test_legacy_m007_record_reads_without_attestation(tmp_path: Path) -> None:
    legacy = {
        "subrun_id": "implement-a1",
        "phase_id": "implement",
        "kind": model.PH_IMPLEMENT,
        "mode": "IMPLEMENT",
        "round": 0,
        "attempt": 1,
        "command": ["true"],
        "cwd": None,
        "started_at": "2026-09-15T00:00:00Z",
        "finished_at": "2026-09-15T00:00:01Z",
        "exit_code": 0,
        "classification": model.CR_COMPLETED,
        "stdout_file": "stdout.log",
        "stderr_file": "stderr.log",
        "resources": None,
        "semantic_status": semantic.STATUS_SUCCESS,
        "semantic_error": None,
        "semantic_agent_classification": "AGENT_COMPLETED",
        "semantic_readiness": None,
        "semantic_reason": "done",
    }
    loaded = store.SubrunDoc.from_dict(legacy, "legacy.json")
    assert loaded.semantic_status == semantic.STATUS_SUCCESS
    assert loaded.attestation is None
    assert loaded.attestation_error is None


def test_persisted_completed_without_verified_attestation_fails_closed(
        tmp_path: Path) -> None:
    doc = _attested_record(
        semantic_status=semantic.STATUS_INCOMPLETE,
        attestation=None,
        attestation_error=semantic.ATT_MISSING,
    ).to_dict()
    path = tmp_path / "implement-a1.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    try:
        store.SubrunDoc.from_dict(doc, str(path))
        raise AssertionError("expected persisted attestation contradiction")
    except store.MalformedMissionError as exc:
        assert exc.code == model.R_CONTRADICTION


def test_persisted_partial_attestation_shape_fails_closed(tmp_path: Path) -> None:
    doc = _attested_record().to_dict()
    del doc["attestation_error"]  # partial pair
    path = tmp_path / "implement-a1.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    try:
        store.SubrunDoc.from_dict(doc, str(path))
        raise AssertionError("expected partial attestation shape rejection")
    except store.MalformedMissionError as exc:
        assert exc.code == model.R_MALFORMED_STATE
