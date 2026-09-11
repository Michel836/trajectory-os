"""V1.77-V1.79 milestone integration tests for ``trajectory-pi-control``.

Covers the operator surface that is new in this milestone:

* V1.77 — hardened run selection and validated grace windows (fail-closed,
  no accidental targets, usage errors exit 64);
* V1.78 — the structured ``result`` envelope in every command's JSON
  (fixed ordered field set; unknown facts are JSON null, never invented);
* V1.79 — the bounded audit trail: control actions (including refusals)
  are audited, passive reads never are, repeated refusals are idempotent
  and deterministic.

All tests run the real CLI as a subprocess against a synthetic runs root;
no network, no daemon, no SIGKILL, no state outside the tmp tree.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONTROL = REPO / "scripts" / "trajectory-pi-control"
DEAD_PID = 4_294_967_295
RUN_NAME = "20260101-093000"

RESULT_FIELDS = (
    "schema",
    "command",
    "run_id",
    "outcome",
    "action_requested",
    "action_performed",
    "reasons",
    "target_state",
    "targetable",
    "stoppable",
    "target_pid",
    "signal_requested",
    "signal_sent",
    "lock",
    "grace_seconds",
    "identity",
    "lifecycle",
    "audit",
    "generated_at",
    "evidence_source",
)


def cli(*args: str, cwd: Path | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CONTROL), *args],
        capture_output=True, text=True, timeout=timeout, cwd=cwd,
    )


def text_of(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout + proc.stderr


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        return False
    except OSError:
        return True
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(errors="replace")
    except OSError:
        return True
    fields = raw.rsplit(")", 1)[-1].split()
    return not (fields and fields[0] == "Z")


def start_sitter(workspace: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        cwd=str(workspace),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def make_run(runs_root: Path, name: str, *, pid: int | None, ended: bool = False,
             pi_exit_code: str | None = None) -> Path:
    runs_root.mkdir(parents=True, exist_ok=True)
    d = runs_root / name
    d.mkdir()
    now = time.time()
    lines = [
        "run_id=" + name,
        "started_at=" + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 5)),
        "workspace=" + str(runs_root.parents[1].resolve()),
    ]
    if pid is not None:
        lines += [f"pid={pid}", f"pi_pid={pid}"]
    if ended:
        lines.append(
            "ended_at=" + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now + 55)))
        if pi_exit_code is not None:
            lines.append(f"pi_exit_code={pi_exit_code}")
    (d / "meta.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (d / "status.log").write_text(
        "[00:00:02] elapsed=00:00:02 | smoke | files=0 | ollama=none | "
        "gen_3s=unavailable tok/s |\n",
        encoding="utf-8",
    )
    return d


def audit_lines(runs_root: Path) -> list[dict[str, object]]:
    path = runs_root / "control-audit.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ------------------------------------------------------------------ V1.77


def test_run_selection_rejects_paths_outside_runs_root(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    runs = ws / ".trajectory-pi" / "runs"
    make_run(runs, RUN_NAME, pid=DEAD_PID)
    other = tmp_path / "other"
    other.mkdir()
    outside = other / "20260102-093000"
    outside.mkdir()
    (outside / "meta.txt").write_text("meta\n", encoding="utf-8")
    # An explicit path that exists but escapes the runs root is a usage
    # error (64), never a target.
    result = cli("--runs-root", str(runs), "status", "--run", str(outside))
    assert result.returncode == 64, text_of(result)
    assert "unknown run" in result.stderr


def test_run_selection_preferes_runs_root_over_same_named_cwd_dir(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    runs = ws / ".trajectory-pi" / "runs"
    make_run(runs, RUN_NAME, pid=DEAD_PID, ended=True, pi_exit_code="0")
    # A same-named directory exists in the CWD the CLI runs from...
    decoy = tmp_path / RUN_NAME
    decoy.mkdir()
    (decoy / "meta.txt").write_text("run_id=decoy\n", encoding="utf-8")
    # ...but a bare --run NAME must still resolve under the runs root.
    result = cli("--runs-root", str(runs), "status", "--run", RUN_NAME,
                 "--json", cwd=tmp_path)
    assert result.returncode == 0, text_of(result)
    data = json.loads(result.stdout)
    assert data["run"] == RUN_NAME
    assert data["result"]["run_id"] == RUN_NAME
    lifecycle = data["result"]["lifecycle"]
    assert lifecycle["ended_at"] is not None


def test_invalid_grace_is_a_usage_error(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    runs = ws / ".trajectory-pi" / "runs"
    make_run(runs, RUN_NAME, pid=DEAD_PID)
    for bad in ("-5", "nan"):
        result = cli("--runs-root", str(runs), "stop", "--run", RUN_NAME,
                     "--grace", bad)
        assert result.returncode == 64, (bad, text_of(result))


# ------------------------------------------------------------------ V1.78


def test_result_envelope_present_with_fixed_field_set(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    runs = ws / ".trajectory-pi" / "runs"
    make_run(runs, RUN_NAME, pid=DEAD_PID, ended=True, pi_exit_code="0")

    for command in (["list", "--json"], ["status", "--run", RUN_NAME, "--json"]):
        proc = cli("--runs-root", str(runs), *command)
        assert proc.returncode == 0, text_of(proc)
        data = json.loads(proc.stdout)
        result = data["result"]
        assert set(result.keys()) == set(RESULT_FIELDS)
        assert result["schema"] == "trajectory-pi-control-result/1"
        assert result["outcome"] == "OK"
        assert result["action_performed"] is None

    stop = cli("--runs-root", str(runs), "stop", "--run", RUN_NAME, "--json")
    assert stop.returncode == 2, text_of(stop)
    sdata = json.loads(stop.stdout)
    assert sdata["result"]["outcome"] == "REJECTED"
    assert sdata["result"]["action_requested"] == "stop"
    assert sdata["result"]["action_performed"] is None


def test_stop_success_result_carries_full_evidence(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    runs = ws / ".trajectory-pi" / "runs"
    proc = start_sitter(ws)
    try:
        make_run(runs, RUN_NAME, pid=proc.pid)
        result = cli("--runs-root", str(runs), "stop", "--run", RUN_NAME,
                     "--grace", "5", "--json")
        assert result.returncode == 0, result.stdout + result.stderr
        data = json.loads(result.stdout)
        assert data["outcome"] == "STOPPED"
        r = data["result"]
        assert set(r.keys()) == set(RESULT_FIELDS)
        assert r["run_id"] == RUN_NAME
        assert r["outcome"] == "STOPPED"
        assert r["action_requested"] == "stop"
        assert r["action_performed"] == "sigterm-sent"
        assert r["stoppable"] is True
        assert r["target_pid"] == proc.pid
        assert r["signal_sent"] is True
        assert r["identity"] is not None
        assert r["lifecycle"] is not None
        assert r["lifecycle"]["reasons"]  # evidence, not empty guess
        assert r["audit"] == "ok"
        assert r["generated_at"] != "unknown"
    finally:
        if alive(proc.pid):
            proc.kill()
        proc.wait(timeout=10)


# ------------------------------------------------------------------ V1.79


def test_passive_reads_never_write_the_audit_trail(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    runs = ws / ".trajectory-pi" / "runs"
    make_run(runs, RUN_NAME, pid=DEAD_PID, ended=True, pi_exit_code="0")
    assert cli("--runs-root", str(runs), "list", "--json").returncode == 0
    assert cli("--runs-root", str(runs), "status", "--run", RUN_NAME,
               "--json").returncode == 0
    assert not (runs / "control-audit.jsonl").exists()


def test_passive_reads_are_byte_identical_with_fixed_clock(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    runs = ws / ".trajectory-pi" / "runs"
    make_run(runs, RUN_NAME, pid=DEAD_PID, ended=True, pi_exit_code="0")
    common = ("--runs-root", str(runs), "status", "--run", RUN_NAME,
              "--json", "--now-tag", "2026-01-01T00:00:00")
    first = cli(*common)
    second = cli(*common)
    assert first.returncode == 0
    assert first.stdout == second.stdout, "passive reads must be byte-identical"


def test_repeated_refusals_are_idempotent_and_audited(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    runs = ws / ".trajectory-pi" / "runs"
    d = make_run(runs, RUN_NAME, pid=DEAD_PID)
    run_before = {
        p.name: p.read_bytes() for p in d.iterdir()
    }
    first = cli("--runs-root", str(runs), "stop", "--run", RUN_NAME)
    second = cli("--runs-root", str(runs), "stop", "--run", RUN_NAME)
    assert first.returncode == second.returncode == 2
    lines = audit_lines(runs)
    assert len(lines) == 2, "one sanitized record per control action"
    for record in lines:
        assert record["schema"] == "control-audit/1"
        assert record["command"] == "stop"
        assert record["run_id"] == RUN_NAME
        assert record["identity_state"] == "dead"
        assert "process-dead" in list(record["reasons"])
    # The run directory itself is untouched by refusals.
    assert {p.name: p.read_bytes() for p in d.iterdir()} == run_before
    assert not (d / "control-events.jsonl").exists()


def test_audit_trail_is_sanitized_and_bounded_over_the_cli(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    runs = ws / ".trajectory-pi" / "runs"
    for i in range(6):
        name = f"20260101-0930{i:02d}"
        make_run(runs, name, pid=DEAD_PID)
        result = cli("--runs-root", str(runs), "stop", "--run", name)
        assert result.returncode == 2
    lines = audit_lines(runs)
    assert len(lines) == 6
    dumped = json.dumps(lines)
    # No free-form caller data may ever reach the trail.
    assert "sk-" not in dumped and "Bearer" not in dumped
    # Every record is a single-line, schema-tagged, bounded object.
    for record in lines:
        assert set(record) <= set(
            ("schema", "ts", "component", "command", "runs_root", "action",
             "decision", "outcome", "reasons", "refusal_reason",
             "run_id", "target_pid", "identity_state",
             "identity_reasons", "signal_requested", "signal_sent", "lock",
             "grace_seconds")), set(record)
