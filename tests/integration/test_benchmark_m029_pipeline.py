"""M029 integration — full benchmark artifact pipeline (deterministic).

Exercises the production CLI and engine with a deterministic fixture executor
(no network, no Git trust-boundary write) and verifies the complete artifact
suite, the interruption/resume lifecycle and the decision report.
"""

from __future__ import annotations

import json
from pathlib import Path

from trajectory_os.agents import model as agent_model
from trajectory_os.benchmark import cli, model, store


def _run(root: str, run_id: str) -> int:
    return cli.main([
        "run", "--root", root, "--run-id", run_id, "--mode", "fixture",
        "--repetitions", "1", "--json"])


def test_full_benchmark_artifact_pipeline(tmp_path: Path) -> None:
    root = str(tmp_path / "state")
    first = _run(root, "integration-run")
    # The first pass is interrupted by the interruption/resume workload.
    assert first == cli.EXIT_REJECTED
    state = json.loads(
        (store.run_root(root, "integration-run")
         / store.STATE_NAME).read_text(encoding="utf-8"))
    assert state["state"] == model.RUN_CANCELLED

    resumed = cli.main([
        "run", "--root", root, "--run-id", "integration-run", "--mode",
        "fixture", "--repetitions", "1", "--resume", "--json"])
    assert resumed == cli.EXIT_OK

    run_root = store.run_root(root, "integration-run")
    for name in (store.MANIFEST_NAME, store.EVENTS_NAME, store.SUMMARY_NAME,
                 store.REPORT_NAME, store.STATE_NAME):
        assert (run_root / name).is_file(), name
    trials = sorted((run_root / store.TRIALS_DIR).glob("*.json"))
    assert len(trials) == 10

    # Both backend identities traversed the same resumable lifecycle, and the
    # fixture evidence stays explicitly non-authoritative.
    records = store.load_trials(run_root)
    resumed = {r.backend for r in records
               if r.workload_id == "interruption-resume" and r.resumed}
    assert resumed == {agent_model.BACKEND_PI,
                       agent_model.BACKEND_DEEPSEEK_HARNESS}
    assert all(r.agent.get("evidence") == "PIPELINE_FIXTURE"
               for r in records)

    summary = json.loads(
        (run_root / store.SUMMARY_NAME).read_text(encoding="utf-8"))
    assert summary["state"] == model.RUN_READY_FOR_COMMIT
    assert summary["mode"] == model.MODE_FIXTURE
    assert summary["final_reviewer_model"] == model.FINAL_REVIEWER_MODEL
    for backend in ("pi", "deepseek-harness"):
        assert backend in summary["by_backend"]

    report = (run_root / store.REPORT_NAME).read_text(encoding="utf-8")
    assert "Runtime decision (operator-owned, no auto-promotion)" in report
    assert "Harness primary / Pi fallback" in report
    assert "qwen3.6" not in report

    reconstructed = json.loads(cli_reconstruct(root, "integration-run"))
    assert reconstructed["counts"]["trials"] == 10
    assert reconstructed["manifest"]["manifest_id"]


def cli_reconstruct(root: str, run_id: str) -> str:
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = cli.main([
            "reconstruct", "--root", root, "--run-id", run_id])
    assert code == cli.EXIT_OK
    return buffer.getvalue()


def test_status_document_distinguishes_actors(tmp_path: Path) -> None:
    root = str(tmp_path / "state")
    _run(root, "actors-run")
    cli.main([
        "run", "--root", root, "--run-id", "actors-run", "--mode", "fixture",
        "--repetitions", "1", "--resume", "--json"])
    document = json.loads(cli_reconstruct_status(root, "actors-run"))
    actors = document["actors"]
    assert actors["final_independent_reviewer"]["model"] == (
        model.FINAL_REVIEWER_MODEL)
    assert actors["final_independent_reviewer"]["active"] is True
    assert actors["inline_reviewer"]["active"] is False
    assert actors["implementation_agent"]["active"] is True


def cli_reconstruct_status(root: str, run_id: str) -> str:
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = cli.main([
            "status", "--root", root, "--run-id", run_id, "--json"])
    assert code == cli.EXIT_OK
    return buffer.getvalue()
