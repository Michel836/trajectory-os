"""Mission 003 — bounded deterministic multi-phase mission orchestrator.

Required-behavior coverage (from the mission):

- full canonical run reaches COMPLETE with per-phase evidence;
- real process runner sub-runs (exit 0 + exact semantic result) succeed with stdout evidence;
- strict durable state: unknown fields / malformed docs are rejected;
- legal vs illegal transitions; absorbing terminal states;
- repair loop converges (validate fails -> repair -> validate passes);
- repair failure hard-stops; sub-run budget blocks before launch;
- human intervention budget is enforced;
- session bound stops deterministically and is later resumable, with no
  re-running of proven phases;
- crash -> ambiguous evidence never guessed; workspace contention blocks a
  new launch; reconstruction classifies UNPROVEN; resume completes;
- stale/ambiguous state fail-closes (never auto-passed);
- HEAD drift and unproven resource capacity block fail-closed; an admitted
  proven capacity allows the launch;
- deterministic evidence: non-zero exit -> FAILED sub-run;
- dependency decisions and config validation are deterministic;
- event log is bounded;
- human-readable and machine-readable outputs agree on state.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import trajectory_os.missions.orchestrator as orchestrator
from trajectory_os.missions import flow, model, store, summary
from trajectory_os.missions.orchestrator import (
    ConfigError,
    HumanBudgetError,
    MissionConfig,
    MissionExists,
    PhaseSpec,
    create_mission,
    default_phase_specs,
    reconstruct,
    record_human_note,
    run_mission,
    run_phase,
    utc_now_iso,
)
from trajectory_os.missions.runner import (
    ProcessPhaseRunner,
    SubrunRequest,
    SubrunResult,
)

MID = "m003"


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def repo(tmp_path: Path) -> tuple[str, str]:
    """A real git repository with one commit; returns (path, head revision)."""
    d = tmp_path / "source"
    d.mkdir()
    env = {**subprocess.os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
           "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=d, env=env, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    git("init", "-q")
    (d / "README.md").write_text("hello\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-q", "-m", "baseline")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=d, env=env, check=True,
        capture_output=True, text=True).stdout.strip()
    return str(d), head


def _semantic_success_cmd(label: str) -> tuple[str, ...]:
    """Deterministic producer satisfying the Mission 007 semantic contract:

    exits 0 AND emits the EXACT structured result bound to the exact
    sub-run (via the runner-set contract env vars), status SUCCESS —
    the same shape the canonical trajectory-pi wrapper emits.
    """
    script = (
        'echo "$1"; '
        'printf '
        '\'{"schema_version":1,"subrun_id":"%s","status":"SUCCESS",'
        '"agent_classification":"TEST_PRODUCER","reason":"test"}\' '
        '"$TRAJECTORY_SUBRUN_ID" > "$TRAJECTORY_SUBRUN_RESULT_FILE"'
    )
    return ("bash", "-c", script, "_", label)


def make_commands() -> dict[str, tuple[str, ...]]:
    return {
        model.PH_PLAN: _semantic_success_cmd("plan"),
        model.PH_IMPLEMENT: _semantic_success_cmd("implement"),
        model.PH_VALIDATE: ("bash", "-c", 'echo "$1"', "_", "validate"),
        model.PH_REVIEW: _semantic_success_cmd("review"),
        model.PH_CONSOLIDATE: ("bash", "-c", 'echo "$1"', "_", "consolidate"),
    }


def build_config(root: Path, repo_path: str, head: str,
                 **kw: Any) -> MissionConfig:
    defaults: dict[str, Any] = {
        "mission_id": MID,
        "objective": "deterministic multi-phase mission test",
        "phase_specs": default_phase_specs(make_commands(), repair_budget=3),
        "repo_root": repo_path,  # the git repo root (HEAD is read from here)
        "cwd": repo_path,
        "baseline_revision": head,
    }
    defaults.update(kw)
    return MissionConfig(**defaults)


class ScriptedRunner:
    """Deterministic fake: per-attempt exit codes per phase.

    0 -> COMPLETED; 1..120 -> deterministic FAILED evidence; >120 ->
    CRASHED (provider failure), with 124 carrying the timed-out flag.
    """

    def __init__(self, script: dict[str, list[int]] | None = None,
                 default: int = 0) -> None:
        self.script = {p: list(x) for p, x in (script or {}).items()}
        self.default = default
        self.calls: list[str] = []

    def run(self, request: SubrunRequest) -> SubrunResult:
        self.calls.append(f"{request.phase_id}:a{request.attempt}")
        seq = self.script.get(request.phase_id)
        code = seq.pop(0) if seq else self.default
        if code == 0:
            return SubrunResult(0, model.CR_COMPLETED)
        if code <= model.MAX_DETERMINISTIC_FAILURE_EXIT:
            return SubrunResult(code, model.CR_FAILED)
        return SubrunResult(code, model.CR_CRASHED, timed_out=(code == 124))


class SecondCallCrashRunner:
    """First sub-run succeeds; the second raises (mid-flight crash)."""

    def __init__(self) -> None:
        self.n = 0

    def run(self, request: SubrunRequest) -> SubrunResult:
        self.n += 1
        if self.n == 2:
            raise RuntimeError("simulated loss of provider connectivity")
        return SubrunResult(0, model.CR_COMPLETED)


def load(root: Path) -> store.MissionDoc:
    return store.load_mission(str(root), MID)[0]


def subrun(root: Path, sid: str) -> store.SubrunDoc:
    return store.load_subrun(store.mission_paths(str(root), MID), sid)


def _crash_implement(root: Path) -> None:
    """Plan passes, then the implement launch crashes mid-flight."""
    with pytest.raises(RuntimeError, match="provider connectivity"):
        run_mission(str(root), MID, SecondCallCrashRunner())


# ---------------------------------------------------------------------------
# creation + strict durable state
# ---------------------------------------------------------------------------


def test_create_mission_idempotent_and_canonical(
        tmp_path: Path, repo: tuple[str, str]) -> None:
    repo_path, head = repo
    create_mission(str(tmp_path), build_config(tmp_path, repo_path, head))
    with pytest.raises(MissionExists):
        create_mission(str(tmp_path), build_config(tmp_path, repo_path, head))

    doc = load(tmp_path)
    assert doc.mission_state == model.MS_PLANNING
    assert [p.phase_id for p in doc.phases] == [
        "plan", "implement", "validate", "review", "consolidate"]
    assert doc.subrun_budget >= 1
    assert doc.repairs_used == 0
    assert doc.baseline_revision == head


def test_store_rejects_unknown_fields_and_malformed_docs(
        tmp_path: Path, repo: tuple[str, str]) -> None:
    repo_path, head = repo
    create_mission(str(tmp_path), build_config(tmp_path, repo_path, head))
    m = store.mission_paths(str(tmp_path), MID)["mission"]
    raw = json.loads(m.read_text(encoding="utf-8"))
    raw["not_a_field"] = True
    m.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(store.MalformedMissionError):
        store.load_mission(str(tmp_path), MID)

    m.write_text("{ definitely not json", encoding="utf-8")
    with pytest.raises(store.MalformedMissionError):
        store.load_mission(str(tmp_path), MID)


def test_subrun_exit_code_range_contract(
        tmp_path: Path, repo: tuple[str, str]) -> None:
    """exit_code range contract (0..255, optional) lives in the store helper.

    The range is validated through the same strict integer helper as every
    other persisted counter (lower bound) plus its inclusive maximum — it
    must accept the full legal range including bounds, and reject
    out-of-range, negative, bool and non-int values (fail closed).
    """
    repo_path, head = repo
    create_mission(str(tmp_path), build_config(tmp_path, repo_path, head))
    run_mission(str(tmp_path), MID, ScriptedRunner())
    doc = subrun(tmp_path, "plan-a1").to_dict()

    for ok in (0, 255, None):  # bounds inclusive; absent allowed (CRASHED)
        got = store.SubrunDoc.from_dict(dict(doc, exit_code=ok), "test")
        assert got.exit_code == ok

    for bad in (256, 300, -1, True, "1", "255"):  # malformed: rejected
        with pytest.raises(store.MalformedMissionError):
            store.SubrunDoc.from_dict(dict(doc, exit_code=bad), "test")


def test_head_probe_timeout_fails_closed(
        tmp_path: Path, repo: tuple[str, str],
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A HEAD probe that cannot complete (slow storage / timeout) blocks.

    The probe bound is a single named constant (auditably, one limit);
    on timeout the guard chain must fail closed with HEAD_DRIFT rather
    than guessing an unstable repository state.
    """
    repo_path, head = repo
    create_mission(str(tmp_path), build_config(tmp_path, repo_path, head))
    mission = load(tmp_path)
    assert mission.baseline_revision == head

    def _timeout(*args: Any, **kwargs: Any) -> None:  # pragma: no cover
        raise subprocess.TimeoutExpired(
            cmd="git rev-parse HEAD",
            timeout=orchestrator.GIT_HEAD_TIMEOUT_S)

    monkeypatch.setattr(orchestrator.subprocess, "run", _timeout)
    with pytest.raises(orchestrator.MissionGuardBlocked) as excinfo:
        orchestrator.assert_head_stable(mission)
    assert excinfo.value.code == model.R_HEAD_DRIFT


def test_finalize_pins_repairs_since_attempt_invariant(
        tmp_path: Path, repo: tuple[str, str]) -> None:
    """flow._failure_decision's repair-since-attempt comparison is sound.

    After the bounded repair loop converges, the failed phase carries the
    mission's repairs_used AT ITS LAST FINALIZE (refreshed unconditionally
    in the same atomic persist as the terminal phase state), so the
    comparison `repairs_used > repairs_at_attempt` is a well-defined
    "a repair ran after this attempt" test, not a stale-state coin flip.
    """
    repo_path, head = repo
    create_mission(str(tmp_path), build_config(tmp_path, repo_path, head))
    report = run_mission(str(tmp_path), MID,
                         ScriptedRunner({"validate": [1, 0]}))
    doc = load(tmp_path)
    assert report.mission_state == model.MS_COMPLETE
    assert doc.repairs_used == 1
    # re-evidencing happened AFTER the repair round: the phase's snapshot
    # reflects the mission's repair usage at finalize time.
    assert doc.phase("validate").repairs_at_attempt == doc.repairs_used
    # the state machine's own decision agrees with persisted terminal state
    # (deterministic, pure, no guessed branch) — a COMPLETE mission can
    # only ever decide "noop" with the recorded completion reason.
    for _ in range(2):
        decision = flow.decide(doc)
        assert decision.action == "noop"
        assert decision.reason == model.R_COMPLETE
        assert decision.mission_state is None  # never mutates the record path


def test_flow_transitions_and_absorbing_terminals() -> None:
    flow.assert_transition(model.MS_PLANNING, model.MS_RUNNING)
    with pytest.raises(flow.IllegalTransitionError):
        flow.assert_transition(model.MS_COMPLETE, model.MS_RUNNING)
    with pytest.raises(flow.IllegalTransitionError):
        flow.assert_transition(model.MS_BLOCKED, model.MS_RUNNING)


def test_invalid_configs_rejected(
        tmp_path: Path, repo: tuple[str, str]) -> None:
    repo_path, head = repo
    with pytest.raises(ConfigError) as excinfo:
        create_mission(str(tmp_path), build_config(
            tmp_path, repo_path, head, mission_id="X BAD ID"))
    assert excinfo.value.code == "MISSION_ID_INVALID"
    with pytest.raises(ConfigError) as excinfo2:
        create_mission(str(tmp_path), build_config(
            tmp_path, repo_path, head, time_budget_s=5))
    assert excinfo2.value.code == "TIME_BUDGET_INVALID"


# ---------------------------------------------------------------------------
# full runs
# ---------------------------------------------------------------------------


def test_full_canonical_run_reaches_complete(
        tmp_path: Path, repo: tuple[str, str]) -> None:
    repo_path, head = repo
    create_mission(str(tmp_path), build_config(tmp_path, repo_path, head))
    report = run_mission(str(tmp_path), MID, ScriptedRunner())

    assert report.stop == "terminal_after_subrun"
    assert report.mission_state == model.MS_COMPLETE
    assert report.mission_reason == model.R_COMPLETE
    doc = load(tmp_path)
    assert all(p.state == model.PS_PASSED for p in doc.phases)
    assert doc.subrun_completed == len(doc.subruns) == 5
    assert doc.repairs_used == 0
    for p in doc.phases:  # deterministic per-phase evidence
        assert len(p.subrun_ids) == 1
        rec = subrun(tmp_path, p.subrun_ids[0])
        assert rec.classification == model.CR_COMPLETED
        assert rec.exit_code == 0
        sp = store.subrun_paths(store.mission_paths(str(tmp_path), MID),
                                p.subrun_ids[0])
        assert sp["stdout"].exists()
    s = report.summary
    assert s is not None
    assert s["mission_state"] == model.MS_COMPLETE
    assert s["jobs"] == {"created": 5, "started": 5,
                         "completed": 5, "budget": doc.subrun_budget}


def test_real_process_runner_e2e(
        tmp_path: Path, repo: tuple[str, str]) -> None:
    repo_path, head = repo
    create_mission(str(tmp_path), build_config(tmp_path, repo_path, head))
    report = run_mission(str(tmp_path), MID, ProcessPhaseRunner())
    assert report.mission_state == model.MS_COMPLETE
    doc = load(tmp_path)
    rec = subrun(tmp_path, doc.phase("plan").subrun_ids[0])
    assert rec.exit_code == 0
    out = Path(rec.stdout_file).read_text(encoding="utf-8", errors="replace")
    assert "plan" in out


def test_deterministic_failure_evidence_marks_subrun_failed(
        tmp_path: Path, repo: tuple[str, str]) -> None:
    repo_path, head = repo
    create_mission(str(tmp_path), build_config(tmp_path, repo_path, head))
    # Deterministic failure evidence: exit 1 (<=120 family) -> FAILED, then
    # the bounded repair loop converges and the phase passes on re-run.
    report = run_mission(str(tmp_path), MID,
                         ScriptedRunner({"validate": [1, 0]}))
    doc = load(tmp_path)
    first = subrun(tmp_path, doc.phase("validate").subrun_ids[0])
    assert first.exit_code == 1
    assert first.classification == model.CR_FAILED
    assert doc.phase("validate").attempt == 2
    assert report.mission_state == model.MS_COMPLETE


# ---------------------------------------------------------------------------
# repair loop (bounded)
# ---------------------------------------------------------------------------


def test_repair_round_converges_and_completes(
        tmp_path: Path, repo: tuple[str, str]) -> None:
    repo_path, head = repo
    create_mission(str(tmp_path), build_config(tmp_path, repo_path, head))
    report = run_mission(str(tmp_path), MID,
                         ScriptedRunner({"validate": [1, 0]}))
    doc = load(tmp_path)
    assert report.mission_state == model.MS_COMPLETE
    assert doc.repairs_used == 1
    assert doc.phase("repair.1").state == model.PS_PASSED
    # Accounting invariant incl. the repair phase: every persisted record
    # counted exactly once as started and once as completed (no
    # double-count on the repair branch of finalize).
    assert doc.subrun_started == doc.subrun_completed == len(doc.subruns)
    assert len(doc.subruns) == 7  # 5 core + validate re-attempt + repair.1
    rec = subrun(tmp_path, doc.phase("repair.1").subrun_ids[0])
    assert rec.classification == model.CR_COMPLETED
    s = report.summary
    assert s is not None
    assert s["automatic"]["repairs_used"] == 1
    assert s["automatic"]["repair_budget"] == 3


def test_repair_failure_hard_stops_bounded(
        tmp_path: Path, repo: tuple[str, str]) -> None:
    repo_path, head = repo
    create_mission(str(tmp_path), build_config(tmp_path, repo_path, head))
    runner = ScriptedRunner(default=1)  # everything deterministically fails
    report = run_mission(str(tmp_path), MID, runner)
    doc = load(tmp_path)
    assert report.mission_state == model.MS_BLOCKED
    assert report.mission_reason == model.R_REPAIR_FAILED
    assert doc.repairs_used == 1
    assert doc.subrun_completed <= 2  # bounded: no runaway loop


def test_subrun_budget_blocks_before_launch(
        tmp_path: Path, repo: tuple[str, str]) -> None:
    repo_path, head = repo
    create_mission(str(tmp_path), build_config(tmp_path, repo_path, head,
                                               subrun_budget=1))
    report = run_mission(str(tmp_path), MID,
                         ScriptedRunner({"validate": [1]}))
    doc = load(tmp_path)
    assert report.mission_state == model.MS_BLOCKED
    assert report.mission_reason == model.R_SUBRUN_BUDGET_EXHAUSTED
    assert doc.subrun_started <= 1
    assert doc.phase("repair.1").state == model.PS_PENDING  # never launched


def test_human_budget_caps_interventions(
        tmp_path: Path, repo: tuple[str, str]) -> None:
    repo_path, head = repo
    create_mission(str(tmp_path), build_config(tmp_path, repo_path, head))
    for i in range(model.MAX_HUMAN_INTERVENTIONS):
        record_human_note(str(tmp_path), MID, f"note {i}")
    with pytest.raises(HumanBudgetError):
        record_human_note(str(tmp_path), MID, "one too many")
    assert len(load(tmp_path).human_notes) == model.MAX_HUMAN_INTERVENTIONS


# ---------------------------------------------------------------------------
# bounded sessions (determinate, bounded wait)
# ---------------------------------------------------------------------------


def test_session_bound_stops_and_resumes_later(
        tmp_path: Path, repo: tuple[str, str]) -> None:
    repo_path, head = repo
    create_mission(str(tmp_path), build_config(tmp_path, repo_path, head))
    report = run_mission(str(tmp_path), MID, ScriptedRunner(),
                         max_session_subruns=2)
    doc = load(tmp_path)
    assert report.stop == "session_bound"
    assert not doc.terminal()
    assert len(doc.subruns) == 2
    states = [p.state for p in doc.phases]
    assert states[:2] == [model.PS_PASSED, model.PS_PASSED]
    assert states[2:] == [model.PS_PENDING] * 3

    # resumption continues exactly where it stopped; proven phases NOT re-run
    report2 = run_mission(str(tmp_path), MID, ScriptedRunner())
    doc2 = load(tmp_path)
    assert report2.mission_state == model.MS_COMPLETE
    for p in doc2.phases:
        assert len(p.subrun_ids) == 1, f"phase {p.phase_id} re-ran"


# ---------------------------------------------------------------------------
# failure + recovery: crash -> UNPROVEN -> reconstruct -> resume
# ---------------------------------------------------------------------------


def test_crash_workspace_conflict_and_resume_after_reconstruct(
        tmp_path: Path, repo: tuple[str, str]) -> None:
    repo_path, head = repo
    create_mission(str(tmp_path), build_config(tmp_path, repo_path, head))
    _crash_implement(tmp_path)

    doc = load(tmp_path)
    implement = doc.phase("implement")
    assert implement.state == model.PS_RUNNING  # ambiguous in-flight evidence
    assert subrun(tmp_path, implement.subrun_ids[-1]).classification \
        == model.CR_RUNNING
    assert doc.phase("plan").state == model.PS_PASSED

    # workspace contention: no new launch while a RUNNING record exists
    with pytest.raises(Exception) as excinfo:
        run_phase(str(tmp_path), MID, ScriptedRunner(), phase_id="implement")
    assert model.R_WORKSPACE_CONFLICT in str(excinfo.value)
    # Accounting invariant: both records (plan + crashed implement) are
    # persisted evidence, so started reflects them; only plan finalized.
    post = load(tmp_path)
    assert post.subrun_started == 2 == len(post.subruns)
    assert post.subrun_completed == 1
    assert post.subrun_started - post.subrun_completed == 1  # in-flight

    # reconstruction: explicit UNPROVEN (never guessed), proven kept
    report = reconstruct(str(tmp_path), MID)
    doc = load(tmp_path)
    assert doc.phase("implement").state == model.PS_UNPROVEN
    assert doc.phase("implement").state != model.PS_PASSED
    assert doc.phase("plan").state == model.PS_PASSED
    assert report.proven_passed == ["plan"]
    assert report.explicit_unproven == ["implement"]
    assert report.resumable

    # resume: only the unproven phase re-runs; plan (proven) is untouched
    report = run_mission(str(tmp_path), MID, ScriptedRunner())
    doc = load(tmp_path)
    assert report.mission_state == model.MS_COMPLETE
    assert len(doc.phase("plan").subrun_ids) == 1
    assert len(doc.phase("implement").subrun_ids) == 2
    s = report.summary
    assert s is not None
    assert s["provider_failures"]["recovered"] >= 1


def test_stale_ambiguous_state_fail_closes(
        tmp_path: Path, repo: tuple[str, str]) -> None:
    repo_path, head = repo
    create_mission(str(tmp_path), build_config(tmp_path, repo_path, head))
    _crash_implement(tmp_path)

    # continuing without reconstruction fail-closes on the stale record:
    # no guess, no new launch
    report = run_mission(str(tmp_path), MID, ScriptedRunner())
    doc = load(tmp_path)
    assert report.mission_state == model.MS_BLOCKED
    assert report.mission_reason == model.R_STALE_EVIDENCE
    # Counters reflect persisted evidence (both records), not only finished
    # sub-runs: the in-flight one stays explicit via started - completed.
    assert doc.subrun_started == 2
    assert doc.subrun_completed == 1
    assert doc.phase("implement").state == model.PS_RUNNING
    # deterministic: the same block re-derives on the next invocation
    report2 = run_mission(str(tmp_path), MID, ScriptedRunner())
    assert report2.mission_state == model.MS_BLOCKED
    assert report2.mission_reason == model.R_STALE_EVIDENCE


# ---------------------------------------------------------------------------
# fail-closed guards: HEAD drift + resources
# ---------------------------------------------------------------------------


def test_head_drift_blocks_fail_closed(
        tmp_path: Path, repo: tuple[str, str]) -> None:
    repo_path, _head = repo
    create_mission(str(tmp_path), build_config(
        tmp_path, repo_path, "0" * 40))  # baseline does not match HEAD
    report = run_mission(str(tmp_path), MID, ScriptedRunner())
    doc = load(tmp_path)
    assert report.mission_state == model.MS_BLOCKED
    assert report.mission_reason == model.R_HEAD_DRIFT
    assert doc.subrun_started == 0  # no new work launched


def _gpu_mission(root: Path, repo_path: str, head: str) -> None:
    specs = default_phase_specs(
        make_commands(), repair_budget=3,
        validate_resources={"gpu": True, "gpu_mem_bytes": 2**30})
    create_mission(str(root), MissionConfig(
        mission_id=MID, objective="gpu requirement test", phase_specs=specs,
        cwd=repo_path, repo_root=repo_path, baseline_revision=head))


def test_resource_admission_defers_without_proven_capacity(
        tmp_path: Path, repo: tuple[str, str]) -> None:
    repo_path, head = repo
    _gpu_mission(tmp_path, repo_path, head)
    # insufficient capacity (0 GPU slots): deferred -> blocked; no launch
    report = run_mission(str(tmp_path), MID, ScriptedRunner(),
                         resource_policy={"gpu_slots": 0})
    doc = load(tmp_path)
    assert report.mission_state == model.MS_BLOCKED
    assert report.mission_reason == model.R_RESOURCE_UNAVAILABLE
    assert doc.phase("validate").state == model.PS_PENDING
    assert len(doc.phase("validate").subrun_ids) == 0


def test_resource_unknown_capacity_defers(
        tmp_path: Path, repo: tuple[str, str]) -> None:
    repo_path, head = repo
    _gpu_mission(tmp_path, repo_path, head)
    # explicit unknown capacity evidence: never guessed allowed
    report = run_mission(str(tmp_path), MID, ScriptedRunner(),
                         resource_policy={"gpu_slots": "unknown"})
    assert report.mission_state == model.MS_BLOCKED
    assert report.mission_reason == model.R_RESOURCE_UNAVAILABLE


def test_resource_admission_allows_with_proven_capacity(
        tmp_path: Path, repo: tuple[str, str]) -> None:
    repo_path, head = repo
    _gpu_mission(tmp_path, repo_path, head)
    report = run_mission(
        str(tmp_path), MID, ScriptedRunner(),
        resource_policy={"gpu_slots": 1, "gpu_mem_bytes": 2**32})
    doc = load(tmp_path)
    assert report.mission_state == model.MS_COMPLETE
    rec = subrun(tmp_path, doc.phase("validate").subrun_ids[0])
    assert rec.classification == model.CR_COMPLETED


# ---------------------------------------------------------------------------
# dependency decisions (pure flow) + config validation
# ---------------------------------------------------------------------------


def test_dependency_missing_blocks(
        tmp_path: Path, repo: tuple[str, str]) -> None:
    repo_path, head = repo
    specs = (
        PhaseSpec(kind=model.PH_PLAN, phase_id="b",
                  command=("true",), depends_on=("a",)),
        PhaseSpec(kind=model.PH_IMPLEMENT, phase_id="a", command=("true",)),
    )
    create_mission(str(tmp_path), build_config(
        tmp_path, repo_path, head, phase_specs=specs))
    decision = flow.decide(load(tmp_path))
    assert decision.action == "blocked"
    assert decision.reason == model.R_DEPENDENCY_MISSING
    assert decision.phase_id == "b"


def test_missing_dependency_id_rejected(
        tmp_path: Path, repo: tuple[str, str]) -> None:
    repo_path, head = repo
    specs = (
        PhaseSpec(kind=model.PH_PLAN, phase_id="b",
                  command=("true",), depends_on=("zzz",)),
        PhaseSpec(kind=model.PH_IMPLEMENT, phase_id="a", command=("true",)),
    )
    with pytest.raises(ConfigError) as excinfo:
        create_mission(str(tmp_path), build_config(
            tmp_path, repo_path, head, phase_specs=specs))
    assert excinfo.value.code == "PHASE_DEPENDENCY_UNKNOWN"


# ---------------------------------------------------------------------------
# bounds
# ---------------------------------------------------------------------------


def test_event_log_is_bounded(
        tmp_path: Path, repo: tuple[str, str]) -> None:
    repo_path, head = repo
    create_mission(str(tmp_path), build_config(tmp_path, repo_path, head))
    p = store.mission_paths(str(tmp_path), MID)
    for i in range(model.MAX_EVENT_LOG + 50):
        store.append_event(p, {"ts": utc_now_iso(),
                              "event": f"filler-{i}"})
    events = store.load_events(p)
    assert len(events) == model.MAX_EVENT_LOG
    assert events[-1]["event"] == f"filler-{model.MAX_EVENT_LOG + 49}"


# ---------------------------------------------------------------------------
# human + machine outputs agree (single source of truth)
# ---------------------------------------------------------------------------


def test_human_and_machine_output_consistency(
        tmp_path: Path, repo: tuple[str, str]) -> None:
    repo_path, head = repo
    create_mission(str(tmp_path), build_config(tmp_path, repo_path, head))
    run_mission(str(tmp_path), MID, ScriptedRunner())

    machine = load(tmp_path)
    p = store.mission_paths(str(tmp_path), MID)
    s = summary.mission_summary(machine, p)
    assert s["mission_state"] == machine.mission_state
    assert s["phases_passed"] == sum(
        1 for ph in machine.phases if ph.state == model.PS_PASSED)

    rendered = summary.render_summary(s)
    assert isinstance(rendered, str) and rendered
    assert f"state     : {s['mission_state']}" in rendered
    for ph in machine.phases:  # same phases, same states, human-readable
        assert f"{ph.phase_id}: {ph.state}" in rendered

    status = summary.mission_status(str(tmp_path), MID)
    assert status["status"] == "OK"
    assert status["summary"]["mission_state"] == s["mission_state"]
    assert status["rendered"] == rendered


def test_render_is_strict_about_schema() -> None:
    with pytest.raises(store.MalformedMissionError):
        summary.render_summary({"mission_id": MID})


def test_unknown_mission_id_is_a_lookup_error(
        tmp_path: Path, repo: tuple[str, str]) -> None:
    repo_path, head = repo
    create_mission(str(tmp_path), build_config(tmp_path, repo_path, head))
    with pytest.raises(store.MissionNotFound):
        store.load_mission(str(tmp_path), "nope")
