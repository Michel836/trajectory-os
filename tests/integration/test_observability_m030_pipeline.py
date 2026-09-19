"""M030 integration — canonical observability CLI + benchmark pipeline.

Drives the production ``trajectory_os.observability`` and
``trajectory_os.benchmark`` CLIs with deterministic fixtures (no network, no
Git trust-boundary write) and verifies the durable event/status/telemetry
artifacts, the shared projection contract, follow auto-exit and preflight
fail-fast.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

from trajectory_os.agents import model as agent_model
from trajectory_os.benchmark import cli as bench_cli
from trajectory_os.benchmark import store as bench_store
from trajectory_os.observability import cli as obs_cli
from trajectory_os.observability import model, projection, store

REPO_ROOT = Path(__file__).resolve().parents[2]
READER = REPO_ROOT / "scripts" / "trajectory-pi-status"

PASS_REVIEW = (
    "VERDICT: PASS\nBLOCKERS:\n- none\nMAJORS:\n- none\nMINORS:\n- none\n"
    "FINAL RECOMMENDATION: GO COMMIT\n")


def _capture(argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = obs_cli.main(argv)
    return code, buffer.getvalue()


def test_cli_run_persists_canonical_artifacts(tmp_path: Path) -> None:
    root = str(tmp_path / "state")
    code, _ = _capture([
        "run", "--root", root, "--run-id", "it-run", "--reviewer",
        "reject-then-pass", "--telemetry", "benchmark", "--json"])
    assert code == obs_cli.EXIT_OK
    run_root = store.run_root(root, "it-run")
    for name in (store.EVENTS_NAME, store.STATUS_NAME, store.TELEMETRY_NAME,
                 store.SUMMARY_NAME):
        assert (run_root / name).is_file(), name
    status = json.loads((run_root / store.STATUS_NAME).read_text())
    assert status["readiness"] == model.RD_READY_FOR_COMMIT
    assert status["state"] == model.LC_COMPLETE
    telemetry_document = json.loads(
        (run_root / store.TELEMETRY_NAME).read_text())
    assert telemetry_document["mode"] == model.TELEMETRY_BENCHMARK
    assert telemetry_document["derived"]["review_reject_rate"]["value"] == 0.5


def test_cli_status_and_project_targets(tmp_path: Path) -> None:
    root = str(tmp_path / "state")
    _capture(["run", "--root", root, "--run-id", "proj-run",
              "--reviewer", "pass", "--json"])
    for target in sorted(projection.PROJECTIONS):
        code, output = _capture([
            "project", "--root", root, "--run-id", "proj-run",
            "--target", target])
        assert code == obs_cli.EXIT_OK
        assert "proj-run" in output
    code, output = _capture([
        "status", "--root", root, "--run-id", "proj-run", "--json"])
    assert code == obs_cli.EXIT_OK
    document = json.loads(output)
    assert document["ready_for_commit"] is True


def test_cli_follow_exits_on_terminal_run(tmp_path: Path) -> None:
    root = str(tmp_path / "state")
    _capture(["run", "--root", root, "--run-id", "follow-run",
              "--reviewer", "pass", "--json"])
    code, output = _capture([
        "follow", "--root", root, "--run-id", "follow-run",
        "--interval", "0.01", "--max-polls", "2"])
    assert code == obs_cli.EXIT_OK
    assert "RUN TERMINÉ" in output


def test_cli_preflight_rejection_is_fail_fast(tmp_path: Path) -> None:
    code, output = _capture([
        "preflight", "--run-id", "pf", "--provider", "ollama",
        "--model", "deepseek-flash", "--workspace", str(tmp_path), "--json"])
    assert code == obs_cli.EXIT_REJECTED
    document = json.loads(output)
    assert document["ok"] is False
    assert document["reason"] == model.R_INVALID_MODEL_PROVIDER
    assert document["readiness"] == model.RD_BLOCKED


def test_benchmark_preflight_stops_before_trials(tmp_path: Path) -> None:
    root = str(tmp_path / "bench")
    code = bench_cli.main([
        "run", "--root", root, "--run-id", "pf-run", "--mode", "fixture",
        "--repetitions", "1", "--provider", "ollama", "--model",
        "deepseek-flash", "--json"])
    assert code == bench_cli.EXIT_REJECTED
    run_root = bench_store.run_root(root, "pf-run")
    status = json.loads((run_root / store.STATUS_NAME).read_text())
    assert status["readiness"] == model.RD_BLOCKED
    assert status["success"] is False
    # No trial could have run: there are no persisted trial records.
    assert not list((run_root / bench_store.TRIALS_DIR).glob("*.json"))
    telemetry_mode = status["telemetry_mode"]
    assert telemetry_mode == model.TELEMETRY_STANDARD


def test_benchmark_run_writes_canonical_artifacts(tmp_path: Path) -> None:
    root = str(tmp_path / "bench")
    bench_cli.main([
        "run", "--root", root, "--run-id", "full-run", "--mode", "fixture",
        "--repetitions", "1", "--json"])
    bench_cli.main([
        "run", "--root", root, "--run-id", "full-run", "--mode", "fixture",
        "--repetitions", "1", "--resume", "--json"])
    run_root = bench_store.run_root(root, "full-run")
    assert (run_root / store.STATUS_NAME).is_file()
    assert (run_root / store.TELEMETRY_NAME).is_file()
    document = json.loads((run_root / store.STATUS_NAME).read_text())
    # Lifecycle completion is explicit and readiness stays independent.
    assert document["state"] == model.LC_COMPLETE
    assert document["readiness"] in model.READINESS_STATES
    final = document["final_reviewer"]
    assert final["display_model"] == model.FINAL_REVIEWER_MODEL
    assert final["active"] is True


def _terminal_status_document(run_id: str) -> dict[str, object]:
    status = model.CanonicalStatus.build(
        run_id=run_id, state=model.LC_COMPLETE, stage=model.STAGE_DONE,
        phase="DONE", attempt=0, current_backend=agent_model.BACKEND_PI,
        current_provider="deepseek", current_model="deepseek-flash",
        inline_review_enabled=False,
        inline_reviewer=model.ReviewerStatus.disabled(
            model.ROLE_INLINE_REVIEWER),
        final_review_enabled=True,
        final_reviewer=model.ReviewerStatus(
            role=model.ROLE_FINAL_INDEPENDENT_REVIEWER, enabled=True,
            active=True, backend="ollama", provider="ollama",
            model=model.FINAL_REVIEWER_MODEL, reason=model.R_OK),
        previous_gate=model.GATE_REVIEW, previous_result=model.RESULT_PASS,
        reviewed_patch="b" * 64, current_patch="b" * 64,
        next_action="operator: commit the reviewed patch",
        last_meaningful_event_at="2026-01-01T00:00:00Z",
        heartbeat_at="2026-01-01T00:00:00Z",
        terminal_reason="all gates passed",
        readiness=model.RD_READY_FOR_COMMIT)
    return status.to_dict()


def test_trajectory_pi_status_follow_auto_exits(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "20260101-000000"
    run_dir.mkdir(parents=True)
    (run_dir / store.STATUS_NAME).write_text(
        json.dumps(_terminal_status_document("20260101-000000")),
        encoding="utf-8")
    proc = subprocess.run(  # noqa: S603 - test invocation of a repo script
        [sys.executable, str(READER), "--run", str(run_dir), "--follow",
         "--follow-interval", "0.01", "--follow-max-polls", "2", "--json"],
        capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    assert "RUN TERMINÉ" in proc.stdout
    outcome = json.loads(proc.stdout.split("=" * 68)[0])
    assert outcome["exited"] is True
    assert outcome["polls"] == 1
