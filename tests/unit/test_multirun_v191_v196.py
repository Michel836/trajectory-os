"""Tests for V1.91-V1.96 multi-run additions (spec, workspace, lock, retry, completion)."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from trajectory_os.runs import completion, locking, model, retry, spec, store, workspace

# ---------------------------------------------------------------------------
# V1.91 — canonical job spec
# ---------------------------------------------------------------------------

def test_spec_build_and_roundtrip() -> None:
    s = spec.build_spec(
        "job-1",
        ["echo", "hi"],
        spec.EXEC_READ_ONLY,
        workspace_policy=spec.WORKSPACE_SHARED_READ_ONLY,
        source_revision="0123456789abcdef",
        source_checkout=Path("/tmp/src"),
        query="final user query",
    )
    assert s.query_source == "inline"
    data = s.to_dict()
    back = spec.JobSpec.from_dict(data)
    assert back == s


def test_spec_strict_parse_rejects_deviations() -> None:
    with pytest.raises(spec.SpecValidationError) as exc:
        spec.JobSpec.from_dict(5)
    assert exc.value.code == "SPEC_NOT_OBJECT"
    data = spec.build_spec("ok1", ["true"], spec.EXEC_AD_HOC).to_dict()
    data["bogus"] = 1
    with pytest.raises(spec.SpecValidationError) as exc2:
        spec.JobSpec.from_dict(data)
    assert exc2.value.code == "SPEC_UNKNOWN_FIELDS"


def test_spec_requires_explicit_revision_with_source() -> None:
    with pytest.raises(spec.SpecValidationError) as exc:
        spec.build_spec(
            "job-2",
            ["true"],
            spec.EXEC_READ_ONLY,
            source_checkout=Path("/tmp/src"),
        )
    assert exc.value.code == "SPEC_REVISION_REQUIRED"


def test_spec_mutating_requires_isolated() -> None:
    with pytest.raises(spec.SpecValidationError) as exc:
        spec.build_spec(
            "job-3",
            ["true"],
            spec.EXEC_MUTATING,
            workspace_policy=spec.WORKSPACE_SHARED_READ_ONLY,
            source_revision="0123456789abcdef",
            source_checkout=Path("/tmp/src"),
        )
    assert exc.value.code == "SPEC_MUTATING_NEEDS_ISOLATED"


# ---------------------------------------------------------------------------
# V1.94 — evidence classification and supervisor observations
# ---------------------------------------------------------------------------

def test_classify_with_exit_evidence() -> None:
    for code, expected in [(0, model.TERMINAL_DONE), (3, model.TERMINAL_FAILED)]:
        c = completion.classify(
            job_id="j", seq=1, attempts=1,
            owner_kill_reasons=("NOT_LIVE",),
            exit_code=code,
        )
        assert c.terminal == expected
        assert c.evidence == f"exit:{code}"


def test_classify_unproven_is_explicit_unknown() -> None:
    c = completion.classify(job_id="j", seq=1, attempts=1, owner_kill_reasons=())
    assert c.terminal == model.TERMINAL_UNKNOWN
    assert c.exit_code is None and c.signal is None


def test_parse_evidence_signals_and_malformed_fail_closed() -> None:
    assert completion._parse_evidence("exit:0") == (0, None, "exit:0")
    sig = completion._parse_evidence("killed:SIGKILL")
    assert sig[1] == 9
    assert completion._parse_evidence("garbage") == (None, None, "evidence:unrecognized")
    with pytest.raises(completion.EvidenceError):
        completion._parse_evidence([1, 2])


def test_terminal_conflict_rejects_divergent_record() -> None:
    record = types.SimpleNamespace(job_id="j", seq=1, attempts=1,
                                   terminal=model.TERMINAL_DONE)
    with pytest.raises(completion.TerminalConflictError):
        completion.check_terminal_conflict(
            job_id="j", seq=1, attempts=1,
            proposed_terminal=model.TERMINAL_FAILED,
            existing_records=[record],
        )
    # idempotent re-observe (same terminal) does not conflict
    completion.check_terminal_conflict(
        job_id="j", seq=1, attempts=1,
        proposed_terminal=model.TERMINAL_DONE,
        existing_records=[record],
    )


def test_supervisor_observe_returns_each_finished_job() -> None:
    completion.supervisor_clear_all()
    try:
        finished_a = subprocess.run([sys.executable, "-c", "pass"])
        finished_b = subprocess.run([sys.executable, "-c", "pass"])
        del finished_a, finished_b
        proc_a = subprocess.Popen([sys.executable, "-c", "pass"])
        proc_b = subprocess.Popen([sys.executable, "-c", "pass"])
        proc_a.wait()
        proc_b.wait()
        completion.supervisor_register_launch("job-a", proc_a)
        completion.supervisor_register_launch("job-b", proc_b)
        result = completion.supervisor_observe(["job-a", "job-b"])
        assert result == {"job-a": "exit:0", "job-b": "exit:0"}
        # released after the authoritative reading: a second pass observes nothing
        assert completion.supervisor_observe(["job-a", "job-b"]) == {}
    finally:
        completion.supervisor_clear_all()


def test_supervisor_observe_omits_running_processes() -> None:
    completion.supervisor_clear_all()
    running = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(2)"]
    )
    try:
        completion.supervisor_register_launch("job-r", running)
        result = completion.supervisor_observe(["job-r"])
        assert "job-r" not in result
    finally:
        running.kill()
        running.wait()
        completion.supervisor_clear_all()


# ---------------------------------------------------------------------------
# V1.93 — state lock exclusivity
# ---------------------------------------------------------------------------

def test_state_lock_excludes_second_holder(tmp_path: Path) -> None:
    holder = locking.StateLock(tmp_path, timeout_seconds=0.2)
    with holder:
        assert locking.try_lock(tmp_path) is False
    assert locking.try_lock(tmp_path) is True


def test_state_lock_release_is_idempotent(tmp_path: Path) -> None:
    holder = locking.StateLock(tmp_path)
    holder.acquire()
    holder.release()
    holder.release()
    assert locking.try_lock(tmp_path) is True


# ---------------------------------------------------------------------------
# V1.95 — bounded fair retry selection
# ---------------------------------------------------------------------------

class _Entry:
    def __init__(self, seq: int, job_id: str, retry_wait: int = 0) -> None:
        self.seq = seq
        self.job_id = job_id
        self.retry_wait = retry_wait


def test_select_auto_blocks_waiting_and_falls_back_to_lone_entry() -> None:
    a = _Entry(1, "a", retry_wait=1)
    b = _Entry(2, "b", retry_wait=0)
    entry, facts = retry.select_auto([a, b])
    assert entry is b and facts.eligible

    # every entry waiting -> fail closed
    entry, facts = retry.select_auto([a, _Entry(2, "c", retry_wait=1)])
    assert entry is None and facts.reason == model.ERR_RETRY_WAIT

    # a lone waiting entry must never self-block (no queue deadlock)
    entry, facts = retry.select_auto([a])
    assert entry is a and facts.eligible


def test_discharge_counts_and_backoff_bounds() -> None:
    assert retry.discharge_counts([_Entry(1, "a", 2), _Entry(2, "b", 0)]) == 1
    # backoff grows geometrically and is bounded by MAX_RETRY_WAIT
    assert retry.backoff_for_attempt(1) == 1
    assert retry.backoff_for_attempt(3) == 4
    assert retry.backoff_for_attempt(99) == retry.MAX_RETRY_WAIT
    assert retry.backoff_for_attempt(0) == 0


# ---------------------------------------------------------------------------
# V1.92 — workspace materialization and provenance
# ---------------------------------------------------------------------------

def test_materialize_ad_hoc_slot_is_self_contained(tmp_path: Path) -> None:
    s = spec.build_spec("adhoc", ["true"], spec.EXEC_AD_HOC)
    slot = tmp_path / "s1"
    ws = workspace.materialize_workspace(s, slot)
    assert ws.isolated_copy is False
    assert ws.launch_cwd == slot
    ok, problems = workspace.verify_workspace(slot, expected_isolated=False,
                                              expected_class=spec.EXEC_AD_HOC)
    assert ok, problems


def test_materialize_isolated_copy_and_shared_rejects_collision(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "f.txt").write_text("data", encoding="utf-8")
    slot = tmp_path / "s1"
    smut = spec.build_spec(
        "mut", ["true"], spec.EXEC_MUTATING,
        source_revision="0123456789abcdef", source_checkout=source,
    )
    ws = workspace.materialize_workspace(smut, slot)
    assert ws.isolated_copy is True
    assert (ws.tree / "f.txt").read_text(encoding="utf-8") == "data"
    assert (ws.tree / workspace.MANIFEST_REL).is_file()
    # the isolated copy never aliases the source checkout
    assert str(ws.tree.resolve()) != str(source.resolve())
    # concurrent mutating use of the shared tree is rejected (fail closed)
    reader = spec.build_spec(
        "rd", ["true"], spec.EXEC_READ_ONLY,
        workspace_policy=spec.WORKSPACE_SHARED_READ_ONLY,
        source_revision="0123456789abcdef", source_checkout=source,
    )
    contender = types.SimpleNamespace(
        job_id="mut", execution_class=spec.EXEC_MUTATING, workspace=str(source))
    with pytest.raises(workspace.WorkspaceConflictError):
        workspace.materialize_workspace(reader, tmp_path / "s2",
                                        active_records=[contender])
    # a concurrent read-only record does not conflict
    quiet = types.SimpleNamespace(
        job_id="rd2", execution_class=spec.EXEC_READ_ONLY, workspace=str(source))
    ws2 = workspace.materialize_workspace(reader, tmp_path / "s3",
                                          active_records=[quiet])
    assert ws2.launch_cwd == source and ws2.isolated_copy is False


def test_read_source_revision_snapshot_for_non_git(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    with contextlib.suppress(OSError, FileNotFoundError):
        for marker in (".git",):
            with contextlib.suppress(Exception):
                (source / marker).unlink()
    rev = workspace.read_source_revision(source)
    assert rev.startswith("snapshot:")


def test_read_source_revision_git(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    source = tmp_path / "src"
    source.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", "init", "-q"], cwd=source, check=True, env=env)
    (source / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=source, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=source, check=True, env=env)
    rev = workspace.read_source_revision(source)
    assert not rev.startswith("snapshot:")
    assert len(rev) >= 7


# ---------------------------------------------------------------------------
# V1.95/V1.91 — durable queue spec + backoff fields
# ---------------------------------------------------------------------------

def test_queue_entry_roundtrips_retry_wait_and_spec(tmp_path: Path) -> None:
    path = tmp_path / "queue.json"
    s = spec.build_spec("job-x", ["true"], spec.EXEC_AD_HOC)
    entry = store.QueueEntry(
        seq=1, job_id="job-x", enqueued_at="t", command=["true"],
        max_attempts=2, retry_wait=3, spec=s,
    )
    path.write_text(json.dumps(entry.to_dict(), sort_keys=True), encoding="utf-8")
    back = store.QueueEntry.from_dict(
        json.loads(path.read_text(encoding="utf-8")), path)
    assert back.retry_wait == 3
    assert back.spec == s
