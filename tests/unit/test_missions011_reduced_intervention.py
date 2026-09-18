"""Mission 011 — reduced-intervention execution & explicit human gates.

Required coverage (GitHub Issue #213):

1.  green launch progresses autonomously to GO COMMIT;
2.  no intermediate human micro-gates on the green flow;
3.  bounded repair proceeds automatically;
4.  exhausted repair budget produces one consolidated human gate;
5.  validation failure cannot be skipped;
6.  review failure cannot be skipped;
7.  semantic/attestation failure cannot be promoted;
8.  resume reconstructs the current human gate;
9.  resume does not replay proven work;
10. M008/M009/M010 contracts remain intact;
11. summary exposes gate/reason/next action/budget;
12. no autonomous Git trust-boundary write is introduced.

The in-process ``ScriptedRunner`` keeps cases 1-11 deterministic and fast;
the CLI dogfood cases drive the real ``start`` / ``status`` /
``approve-commit`` / ``approve-merge`` operator path end to end.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from trajectory_os.missions import (
    adapter,
    cli,
    gate,
    identity,
    model,
    orchestrator,
    runner,
    semantic,
    store,
    summary,
)
from trajectory_os.missions.orchestrator import (
    MissionConfig,
    create_mission,
    default_phase_specs,
    reconstruct,
    run_mission,
)
from trajectory_os.missions.runner import SubrunRequest, SubrunResult

MID = "m011"


# ---------------------------------------------------------------------------
# deterministic in-process runner + mission helpers
# ---------------------------------------------------------------------------


class ScriptedRunner:
    """Per-phase ordered exit codes (0 -> COMPLETED, 1..120 -> FAILED)."""

    def __init__(
        self,
        script: dict[str, list[int]] | None = None,
        default: int = 0,
        override: Any | None = None,
    ) -> None:
        self.script = {p: list(x) for p, x in (script or {}).items()}
        self.default = default
        self.override = override
        self.calls: list[str] = []

    def run(self, request: SubrunRequest) -> SubrunResult:
        self.calls.append(f"{request.phase_id}:a{request.attempt}")
        if self.override is not None:
            forced = self.override(request.phase_id, request.attempt)
            if forced is not None:
                return SubrunResult(0, forced)
        seq = self.script.get(request.phase_id)
        code = seq.pop(0) if seq else self.default
        if code == 0:
            return SubrunResult(0, model.CR_COMPLETED)
        if code <= model.MAX_DETERMINISTIC_FAILURE_EXIT:
            return SubrunResult(code, model.CR_FAILED)
        return SubrunResult(code, model.CR_CRASHED, timed_out=(code == 124))


def _commands() -> dict[str, tuple[str, ...]]:
    return {kind: ("true",) for kind in model.CANONICAL_SEQUENCE}


def _build(root: str, *,
           repair_budget: int = model.MAX_REPAIR_ROUNDS,
           commands: dict[str, tuple[str, ...]] | None = None) -> None:
    commands = commands or _commands()
    create_mission(root, MissionConfig(
        mission_id=MID,
        objective="M011 reduced-intervention test",
        phase_specs=default_phase_specs(commands, repair_budget=repair_budget),
        repair_budget=repair_budget,
    ))


def _load(root: str) -> store.MissionDoc:
    return store.load_mission(root, MID)[0]


def _mission_summary(root: str) -> dict[str, Any]:
    mission, paths = store.load_mission(root, MID)
    return summary.mission_summary(mission, paths)


@pytest.fixture()
def root(tmp_path: Path) -> str:
    return str(tmp_path / "root")


# ---------------------------------------------------------------------------
# 1 + 11: green launch -> GO COMMIT; summary exposes the gate contract
# ---------------------------------------------------------------------------


def test_green_launch_reaches_go_commit_autonomously(root: str) -> None:
    _build(root)
    report = run_mission(root, MID, ScriptedRunner())
    doc = _load(root)
    assert report.stop == "terminal_after_subrun"
    assert report.mission_state == model.MS_COMPLETE
    assert report.mission_reason == model.R_COMPLETE
    assert all(p.state == model.PS_PASSED for p in doc.phases)
    assert doc.repairs_used == 0

    operator_gate = report.summary["operator_gate"]
    assert operator_gate["gate"] == gate.GATE_GO_COMMIT
    assert operator_gate["reason"] == gate.REASON_READY_FOR_COMMIT
    assert operator_gate["human_action_required"] is True
    assert operator_gate["next_human_action"]
    # The green terminal reconstructs to exactly one GO COMMIT gate; the
    # next human action is a commit decision, not an internal micro-gate.
    recon = reconstruct(root, MID)
    assert recon.operator_gate["gate"] == gate.GATE_GO_COMMIT
    assert recon.resumable is False


def test_summary_exposes_gate_reason_next_action_budget(root: str) -> None:
    _build(root)
    run_mission(root, MID, ScriptedRunner())
    doc = _mission_summary(root)
    operator_gate = doc["operator_gate"]
    for key in ("gate", "reason", "human_action_required", "mission_reason",
                "next_human_action", "autonomous_budget", "evidence",
                "repository"):
        assert key in operator_gate, key
    budget = operator_gate["autonomous_budget"]
    for key in ("repairs_used", "repair_budget", "repairs_remaining",
                "subruns_started", "subrun_budget", "subruns_remaining",
                "time_budget_s", "waiting_for_human"):
        assert key in budget, key
    assert "patch_identity" in operator_gate["evidence"]
    assert operator_gate["repository"]["baseline_revision"] is None

    rendered = summary.render_summary(doc)
    assert f"gate      : {gate.GATE_GO_COMMIT}" in rendered
    assert "next      :" in rendered
    assert "budget    :" in rendered

    status = summary.mission_status(root, MID)
    assert status["summary"]["operator_gate"]["gate"] == gate.GATE_GO_COMMIT
    assert "gate      :" in status["rendered"]


# ---------------------------------------------------------------------------
# 2: no intermediate human micro-gates on the green flow
# ---------------------------------------------------------------------------


def test_no_intermediate_human_micro_gates_on_green_flow(root: str) -> None:
    _build(root)
    # One bounded launch performs every internal transition.
    report = run_mission(root, MID, ScriptedRunner())
    doc = _load(root)
    assert report.stop == "terminal_after_subrun"
    assert len(doc.subruns) == len(model.CANONICAL_SEQUENCE)
    assert doc.mission_state == model.MS_COMPLETE
    # No human note or approval was required to reach GO COMMIT.
    assert doc.human_notes == []
    assert doc.commit_approved_at is None
    assert doc.merge_approved_at is None
    # Exactly one human gate (GO COMMIT) exists on the green path.
    assert reconstruct(root, MID).operator_gate["gate"] == gate.GATE_GO_COMMIT


# ---------------------------------------------------------------------------
# 3: bounded repair proceeds automatically
# ---------------------------------------------------------------------------


def test_bounded_repair_proceeds_automatically(root: str) -> None:
    _build(root, repair_budget=3)
    report = run_mission(root, MID, ScriptedRunner({"validate": [1, 0]}))
    doc = _load(root)
    assert report.mission_state == model.MS_COMPLETE
    assert doc.repairs_used == 1
    assert doc.phase("repair.1").state == model.PS_PASSED
    assert doc.phase("validate").state == model.PS_PASSED
    assert doc.phase("validate").attempt == 2
    # The repair and re-validation happened without any human action.
    assert doc.human_notes == []
    operator_gate = report.summary["operator_gate"]
    assert operator_gate["gate"] == gate.GATE_GO_COMMIT
    assert operator_gate["autonomous_budget"]["repairs_used"] == 1
    assert operator_gate["autonomous_budget"]["repairs_remaining"] == 2


# ---------------------------------------------------------------------------
# 4: exhausted repair budget -> one consolidated human gate
# ---------------------------------------------------------------------------


def test_exhausted_repair_budget_is_one_consolidated_gate(root: str) -> None:
    # repair_budget=1 and a validation that keeps failing after the one
    # allowed repair: the bounded loop stops fail-closed.
    _build(root, repair_budget=1)
    report = run_mission(root, MID, ScriptedRunner({"validate": [1, 1]}))
    doc = _load(root)
    assert report.mission_state == model.MS_BLOCKED
    assert report.mission_reason == model.R_REPAIR_BUDGET_EXHAUSTED
    assert doc.repairs_used == 1
    operator_gate = report.summary["operator_gate"]
    assert operator_gate["gate"] == gate.GATE_STOP
    assert operator_gate["reason"] == gate.REASON_CONSOLIDATED
    assert operator_gate["human_action_required"] is True
    assert operator_gate["autonomous_budget"]["repairs_remaining"] == 0
    # Exactly one consolidated gate: no chain of micro-prompts.
    recon = reconstruct(root, MID)
    assert recon.operator_gate["gate"] == gate.GATE_STOP
    assert recon.operator_gate["reason"] == gate.REASON_CONSOLIDATED
    assert recon.resumable is False


# ---------------------------------------------------------------------------
# 5 + 6: validation / review failure cannot be skipped
# ---------------------------------------------------------------------------


def test_validation_failure_cannot_be_skipped(root: str) -> None:
    _build(root, repair_budget=0)
    report = run_mission(root, MID, ScriptedRunner({"validate": [1]}))
    doc = _load(root)
    assert doc.phase("validate").state == model.PS_FAILED
    assert doc.phase("review").state == model.PS_PENDING
    assert doc.phase("consolidate").state == model.PS_PENDING
    assert report.mission_state == model.MS_BLOCKED
    assert report.summary["operator_gate"]["gate"] == gate.GATE_STOP


def test_review_failure_cannot_be_skipped(root: str) -> None:
    _build(root, repair_budget=0)
    report = run_mission(root, MID, ScriptedRunner({"review": [1]}))
    doc = _load(root)
    assert doc.phase("review").state == model.PS_FAILED
    assert doc.phase("consolidate").state == model.PS_PENDING
    assert report.mission_state == model.MS_BLOCKED
    assert report.mission_state != model.MS_COMPLETE
    assert report.summary["operator_gate"]["gate"] == gate.GATE_STOP


# ---------------------------------------------------------------------------
# 7: semantic / attestation failure cannot be promoted
# ---------------------------------------------------------------------------


def test_semantic_attestation_failure_cannot_be_promoted(root: str) -> None:
    # An unattested model-heavy outcome is UNPROVEN, never PASSED.
    _build(root, repair_budget=0)
    report = run_mission(root, MID, ScriptedRunner(
        override=lambda phase, attempt: (
            model.CR_UNPROVEN if phase == "implement" else None)))
    doc = _load(root)
    assert doc.phase("implement").state == model.PS_UNPROVEN
    assert doc.phase("implement").state != model.PS_PASSED
    assert report.mission_state != model.MS_COMPLETE
    assert report.mission_state == model.MS_BLOCKED
    assert report.summary["operator_gate"]["gate"] == gate.GATE_STOP

    # Pure M008 contract: SUCCESS without a verified attestation is UNPROVEN.
    result = runner.classify_subrun(
        0, semantic_status=semantic.STATUS_SUCCESS, attestation=None)
    assert result.classification == model.CR_UNPROVEN
    # And a persisted unattested COMPLETED record still fails closed on read.
    record = {
        "subrun_id": "implement-a1", "phase_id": "implement",
        "kind": model.PH_IMPLEMENT, "mode": "IMPLEMENT", "round": 0,
        "attempt": 1, "command": ["true"], "cwd": None,
        "started_at": "2026-09-18T00:00:00Z",
        "finished_at": "2026-09-18T00:00:01Z", "exit_code": 0,
        "classification": model.CR_COMPLETED,
        "stdout_file": "s.log", "stderr_file": "e.log", "resources": None,
        "semantic_status": semantic.STATUS_SUCCESS, "semantic_error": None,
        "semantic_agent_classification": "X", "semantic_readiness": None,
        "semantic_reason": "r", "semantic_aware": True,
        "attestation": None, "attestation_error": semantic.ATT_MISSING,
    }
    with pytest.raises(store.MalformedMissionError):
        store.SubrunDoc.from_dict(record, "bad.json")


# ---------------------------------------------------------------------------
# 8: resume reconstructs the current human gate (and approval ordering)
# ---------------------------------------------------------------------------


def test_resume_reconstructs_current_human_gate(root: str) -> None:
    _build(root)
    recon = reconstruct(root, MID)
    assert recon.operator_gate["gate"] == gate.GATE_LAUNCH
    assert recon.operator_gate["human_action_required"] is True
    assert recon.resumable is True

    run_mission(root, MID, ScriptedRunner())
    recon = reconstruct(root, MID)
    assert recon.operator_gate["gate"] == gate.GATE_GO_COMMIT
    assert recon.resumable is False

    orchestrator.approve_commit(root, MID, "deadbeef")
    recon = reconstruct(root, MID)
    assert recon.operator_gate["gate"] == gate.GATE_GO_MERGE
    assert recon.operator_gate["reason"] == gate.REASON_COMMIT_APPROVED
    assert recon.resumable is False

    orchestrator.approve_merge(root, MID)
    recon = reconstruct(root, MID)
    assert recon.operator_gate["gate"] == gate.GATE_DONE
    assert recon.operator_gate["human_action_required"] is False
    # Reconstruction is deterministic and does not replay proven work.
    assert [p.phase_id for p in _load(root).phases
            if p.state == model.PS_PASSED] == [
        "plan", "implement", "validate", "review", "consolidate"]


def test_approval_ordering_is_fail_closed(root: str) -> None:
    _build(root)
    # Not yet green: GO COMMIT approval is rejected.
    with pytest.raises(orchestrator.MissionGuardBlocked) as exc:
        orchestrator.approve_commit(root, MID)
    assert exc.value.code == model.R_ILLEGAL_TRANSITION

    run_mission(root, MID, ScriptedRunner())
    # Merge before commit is rejected.
    with pytest.raises(orchestrator.MissionGuardBlocked):
        orchestrator.approve_merge(root, MID)

    mission = orchestrator.approve_commit(root, MID, "abc123")
    assert mission.commit_revision == "abc123"
    # Replaying the commit approval is rejected (gate advanced).
    with pytest.raises(orchestrator.MissionGuardBlocked):
        orchestrator.approve_commit(root, MID)
    # An oversized revision is a bounded config rejection, not a Git write.
    with pytest.raises(orchestrator.ConfigError):
        orchestrator.approve_commit(root, MID, "x" * 300)


# ---------------------------------------------------------------------------
# 9: resume does not replay proven work
# ---------------------------------------------------------------------------


def test_resume_does_not_replay_proven_work(root: str) -> None:
    _build(root)
    report = run_mission(root, MID, ScriptedRunner(), max_session_subruns=2)
    doc = _load(root)
    assert report.stop == "session_bound"
    assert [p.state for p in doc.phases[:2]] == [
        model.PS_PASSED, model.PS_PASSED]
    before = {p.phase_id: len(p.subrun_ids) for p in doc.phases}

    recon = reconstruct(root, MID)
    assert recon.proven_passed == ["plan", "implement"]
    assert recon.resumable is True
    assert recon.operator_gate["gate"] == gate.GATE_AUTONOMOUS

    report2 = run_mission(root, MID, ScriptedRunner())
    doc2 = _load(root)
    assert report2.mission_state == model.MS_COMPLETE
    # Already-proven phases are never replayed; the remaining phases ran
    # exactly once (resume completed only eligible work).
    for phase_id in recon.proven_passed:
        assert len(doc2.phase(phase_id).subrun_ids) \
            == before[phase_id], phase_id
    for phase in doc2.phases:
        assert len(phase.subrun_ids) == 1, phase.phase_id
    assert doc2.mission_state == model.MS_COMPLETE
    assert report2.summary["operator_gate"]["gate"] == gate.GATE_GO_COMMIT


# ---------------------------------------------------------------------------
# 10: M008 / M009 / M010 contracts remain intact
# ---------------------------------------------------------------------------


def test_m008_m009_m010_contracts_remain_intact(root: str) -> None:
    # M008: only a verified attestation promotes SUCCESS to COMPLETED.
    assert runner.classify_subrun(
        0, semantic_status=semantic.STATUS_SUCCESS,
        attestation=semantic.ATTESTATION_VERIFIED).classification \
        == model.CR_COMPLETED
    assert runner.classify_subrun(
        0, semantic_status=semantic.STATUS_SUCCESS,
        attestation=None).classification == model.CR_UNPROVEN
    # M010: a known non-green readiness fails closed for writable modes.
    assert runner.classify_subrun(
        0, semantic_status=semantic.STATUS_SUCCESS,
        semantic_readiness="NEEDS_REVIEW", semantic_mode="IMPLEMENT",
        attestation=semantic.ATTESTATION_VERIFIED).classification \
        == model.CR_FAILED
    # M010: the two patch identity domains stay distinct.
    assert identity.WRAPPER_SNAPSHOT_DOMAIN != identity.MISSION_WORKTREE_DOMAIN
    assert identity.WRAPPER_SNAPSHOT_FIELD != identity.MISSION_WORKTREE_FIELD
    # M009: the attestation projection is still exposed (legacy record).
    _build(root)
    run_mission(root, MID, ScriptedRunner())
    doc = _mission_summary(root)
    assert set(doc["attestation"]) == {
        "model_heavy_subruns", "verified", "unproven", "legacy"}
    assert "patch_identity" in doc


# ---------------------------------------------------------------------------
# 12: no autonomous Git trust-boundary write is introduced
# ---------------------------------------------------------------------------


_READ_ONLY_GIT_VERBS = frozenset({
    "rev-parse", "diff", "status", "log", "show", "cat-file", "ls-files",
    "symbolic-ref", "rev-list",
})
_GIT_WRITE_VERBS = frozenset({
    "commit", "push", "merge", "reset", "restore", "clean", "stash",
    "rebase", "switch", "checkout", "add", "am", "pull", "fetch", "tag",
    "remote", "cherry-pick", "revert",
})


def test_no_autonomous_git_trust_boundary_write() -> None:
    for module in (orchestrator, runner, adapter):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for match in re.finditer(r'"git"\s*,\s*"([a-z0-9-]+)"', source):
            assert match.group(1) in _READ_ONLY_GIT_VERBS, (
                module.__name__, match.group(1))
        for match in re.finditer(r'_git_read\([^,]+,\s*"([a-z0-9-]+)"', source):
            assert match.group(1) in _READ_ONLY_GIT_VERBS, (
                module.__name__, match.group(1))
    # The gate/approval layer is pure: it contains no process or Git I/O.
    gate_source = Path(gate.__file__).read_text(encoding="utf-8")
    assert "subprocess" not in gate_source
    assert "os.system" not in gate_source
    for verb in _GIT_WRITE_VERBS:
        assert f'"{verb}"' not in gate_source, verb


# ---------------------------------------------------------------------------
# Production-path CLI dogfood: green + bounded repair + trust boundaries
# ---------------------------------------------------------------------------


def _git_setup(tmp_path: Path) -> str:
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


def _fake_provider(tmp_path: Path) -> str:
    """Deterministic pi-wrapper stand-in emitting attested M008 success."""
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
        "\"reason\":\"fixture\","
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


def _flaky_validate(tmp_path: Path) -> str:
    """Validation that fails exactly once (drives bounded automatic repair)."""
    counter = tmp_path / "validate-count"
    script = tmp_path / "validate.sh"
    script.write_text(
        "#!/bin/bash\n"
        f"n=0\nif [[ -f '{counter}' ]]; then n=\"$(cat '{counter}')\"; fi\n"
        "n=$((n+1))\necho \"$n\" > '" + str(counter) + "'\n"
        "if [[ \"$n\" -eq 1 ]]; then exit 1; fi\nexit 0\n",
        encoding="utf-8")
    script.chmod(0o755)
    return f"bash {script}"


def _last_json(text: str) -> dict[str, Any]:
    return json.loads(text)


def test_cli_green_flow_reaches_go_commit_without_micro_gates(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _git_setup(tmp_path)
    root = str(tmp_path / "root")
    code = cli.main([
        "--root", root, "start", "m011green",
        "--objective", "green dogfood",
        "--repo", repo, "--head", _head_of(repo),
        "--pi-wrapper", _fake_provider(tmp_path), "--model", "fake",
        "--validate", "true",
    ])
    assert code == cli.EXIT_OK
    capsys.readouterr()

    assert cli.main(["--root", root, "--json", "status", "m011green"]) \
        == cli.EXIT_OK
    payload = _last_json(capsys.readouterr().out)
    operator_gate = payload["summary"]["operator_gate"]
    assert operator_gate["gate"] == gate.GATE_GO_COMMIT
    assert operator_gate["autonomous_budget"]["repairs_used"] == 0
    # Exact verified run/repository/patch identity is part of the gate
    # evidence (never inferred from a bare exit code).
    identity_block = operator_gate["evidence"]["latest_attested_identity"]
    assert identity_block is not None
    assert set(identity_block) == {"run_id", "repo_head", "patch_sha256"}


def test_cli_bounded_repair_flow_and_trust_boundaries(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _git_setup(tmp_path)
    root = str(tmp_path / "root")
    # One LAUNCH: the runner autonomously repairs the first validation
    # failure, re-validates, reviews and consolidates -> GO COMMIT.
    code = cli.main([
        "--root", root, "start", "m011dog",
        "--objective", "repair dogfood",
        "--repo", repo, "--head", _head_of(repo),
        "--pi-wrapper", _fake_provider(tmp_path), "--model", "fake",
        "--validate", _flaky_validate(tmp_path),
    ])
    assert code == cli.EXIT_OK
    capsys.readouterr()

    assert cli.main(["--root", root, "--json", "status", "m011dog"]) \
        == cli.EXIT_OK
    payload = _last_json(capsys.readouterr().out)
    operator_gate = payload["summary"]["operator_gate"]
    assert operator_gate["gate"] == gate.GATE_GO_COMMIT
    assert operator_gate["autonomous_budget"]["repairs_used"] == 1

    # Human GO COMMIT boundary (recorded, no Git write).
    code = cli.main(["--root", root, "approve-commit", "m011dog",
                     "--revision", "abc123"])
    assert code == cli.EXIT_OK
    assert "GO_MERGE" in capsys.readouterr().out
    assert cli.main(["--root", root, "--json", "status", "m011dog"]) \
        == cli.EXIT_OK
    payload = _last_json(capsys.readouterr().out)
    assert payload["summary"]["operator_gate"]["gate"] == gate.GATE_GO_MERGE

    # Human GO MERGE boundary (recorded, no Git write).
    code = cli.main(["--root", root, "approve-merge", "m011dog"])
    assert code == cli.EXIT_OK
    capsys.readouterr()
    assert cli.main(["--root", root, "--json", "status", "m011dog"]) \
        == cli.EXIT_OK
    payload = _last_json(capsys.readouterr().out)
    assert payload["summary"]["operator_gate"]["gate"] == gate.GATE_DONE
    assert payload["summary"]["operator_gate"]["human_action_required"] is False


def test_cli_approval_and_reconstruct_report_the_gate(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _git_setup(tmp_path)
    root = str(tmp_path / "root")
    assert cli.main([
        "--root", root, "start", "m011rec",
        "--objective", "reconstruct dogfood",
        "--repo", repo, "--head", _head_of(repo),
        "--pi-wrapper", _fake_provider(tmp_path), "--model", "fake",
        "--validate", "true",
    ]) == cli.EXIT_OK
    capsys.readouterr()

    assert cli.main(["--root", root, "reconstruct", "m011rec"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert f"gate          : {gate.GATE_GO_COMMIT}" in out
    assert "next action   :" in out

    # Out-of-order merge approval is rejected before any state change.
    assert cli.main(["--root", root, "approve-merge", "m011rec"]) \
        == cli.EXIT_REJECTED
    capsys.readouterr()
    assert cli.main(["--root", root, "--json", "status", "m011rec"]) \
        == cli.EXIT_OK
    payload = _last_json(capsys.readouterr().out)
    assert payload["summary"]["operator_gate"]["gate"] == gate.GATE_GO_COMMIT
    assert cli.main(["--root", root, "approve-commit", "m011rec"]) \
        == cli.EXIT_OK
    capsys.readouterr()
    assert cli.main(["--root", root, "reconstruct", "m011rec"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert f"gate          : {gate.GATE_GO_MERGE}" in out


# ---------------------------------------------------------------------------
# legacy readability: missions without the new approval fields still load
# ---------------------------------------------------------------------------


def test_legacy_mission_document_without_approval_fields(
        tmp_path: Path) -> None:
    root = str(tmp_path / "root")
    _build(root)
    mission_path = store.mission_paths(root, MID)["mission"]
    raw = json.loads(mission_path.read_text(encoding="utf-8"))
    for field in ("commit_approved_at", "commit_revision", "merge_approved_at"):
        raw.pop(field)
    mission_path.write_text(json.dumps(raw), encoding="utf-8")

    mission, _ = store.load_mission(root, MID)
    assert mission.commit_approved_at is None
    assert mission.commit_revision is None
    assert mission.merge_approved_at is None
    assert gate.derive_gate(mission) == gate.GATE_LAUNCH
