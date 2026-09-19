"""M035 integration — deterministic production acceptance matrix + CLI."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from trajectory_os.assembly import acceptance, model, store
from trajectory_os.assembly import cli as assembly_cli
from trajectory_os.assembly import orchestrator as assembly_run
from trajectory_os.benchmark.executor import FixtureExecutor
from trajectory_os.observability import model as obs_model
from trajectory_os.observability import run as obs_run
from trajectory_os.observability import store as obs_store

PASS_REVIEW = (
    "VERDICT: PASS\nBLOCKERS:\n- none\nMAJORS:\n- none\nMINORS:\n- none\n"
    "FINAL RECOMMENDATION: GO COMMIT\n")


def test_acceptance_matrix_passes_end_to_end(tmp_path: Path) -> None:
    report = acceptance.run_acceptance(tmp_path / "acceptance")
    assert report.status == "PASS"
    assert len(report.cases) == 14
    assert all(case.ok for case in report.cases)
    assert all(check.ok for check in report.checks)

    document = report.to_dict()
    assert document["schema"] == f"trajectory-acceptance/{report.version}"
    assert document["status"] == "PASS"
    assert document["summary"]["passed_cases"] == 14
    assert len(document["cases"]) == 14

    human = report.render()
    assert "PRODUCTION ACCEPTANCE" in human
    assert "14/14 cases" in human


def test_acceptance_reruns_deterministically(tmp_path: Path) -> None:
    first = acceptance.run_acceptance(tmp_path / "first").to_dict()
    second = acceptance.run_acceptance(tmp_path / "second").to_dict()
    for document in (first, second):
        document.pop("generated_at")
        document.pop("root")
    assert first == second


def _capture(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = assembly_cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def _seed_mission(tmp_path: Path) -> str:
    root = str(tmp_path / "state")
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    orchestrator = assembly_run.MissionOrchestrator(
        root, executor=FixtureExecutor(interrupt_once=False),
        reviewer_factory=obs_run.scripted_reviewer_factory([PASS_REVIEW]))
    orchestrator.start(assembly_run.MissionRequest(
        objective="fix add(a, b) so it adds",
        workspace=str(tmp_path / "ws"),
        mission_id="m035-cli",
        workload_id="small-targeted-repair"))
    return root


def test_cli_status_fails_closed_on_identity_mismatch(tmp_path: Path) -> None:
    root = _seed_mission(tmp_path)
    mission_root = store.mission_root(root, "m035-cli")
    status = obs_store.load_status(mission_root)
    status["run_id"] = "some-other-mission"
    obs_store.write_json(mission_root / obs_store.STATUS_NAME, status)
    code, _, err = _capture(
        ["status", "--root", root, "--mission-id", "m035-cli", "--json"])
    assert code == assembly_cli.EXIT_USAGE
    assert "MISSION" in err or "IDENTITY" in err
    # An unknown mission is also a hard failure, never a most-recent fallback.
    code, _, err = _capture(
        ["status", "--root", root, "--mission-id", "ghost", "--json"])
    assert code == assembly_cli.EXIT_USAGE
    assert "MISSION" in err


def test_cli_control_cancel_and_recovery(tmp_path: Path) -> None:
    root = _seed_mission(tmp_path)
    # Interrupt a second mission and cancel it through the CLI.
    (tmp_path / "ws2").mkdir(parents=True, exist_ok=True)
    orchestrator = assembly_run.MissionOrchestrator(
        root, executor=FixtureExecutor(interrupt_once=False),
        reviewer_factory=obs_run.scripted_reviewer_factory([PASS_REVIEW]))
    orchestrator.start(assembly_run.MissionRequest(
        objective="fix add(a, b) so it adds",
        workspace=str(tmp_path / "ws2"),
        mission_id="m035-cancel-cli",
        workload_id="small-targeted-repair"),
        interrupt_after_phase=model.MP_PLAN)
    code, output, _ = _capture([
        "cancel", "--root", root, "--mission-id", "m035-cancel-cli", "--json"])
    assert code == assembly_cli.EXIT_OK
    assert json.loads(output)["readiness"] == obs_model.RD_CANCELLED
    code, output, _ = _capture([
        "recovery", "--root", root, "--mission-id", "m035-cancel-cli"])
    assert code == assembly_cli.EXIT_OK
    assert json.loads(output)["kind"] == "TERMINAL_COMPLETE"
    code, output, _ = _capture([
        "control-log", "--root", root, "--mission-id", "m035-cancel-cli"])
    assert code == assembly_cli.EXIT_OK
    log = json.loads(output)
    assert log[-1]["action"] == "CANCEL"
    code, output, _ = _capture([
        "request-stop", "--root", root, "--mission-id", "m035-cli",
        "--json"])
    assert code == assembly_cli.EXIT_REJECTED
    assert json.loads(output)["result"] == "ALREADY_TERMINAL"


def test_cli_acceptance_command(tmp_path: Path) -> None:
    root = str(tmp_path / "acc")
    out = str(tmp_path / "acceptance.json")
    code, output, _ = _capture([
        "acceptance", "--root", root, "--out", out])
    assert code == assembly_cli.EXIT_OK
    assert "14/14 cases" in output
    document = json.loads(Path(out).read_text(encoding="utf-8"))
    assert document["status"] == "PASS"
