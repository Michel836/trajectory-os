"""Integration tests for the ``trajectory-pi-control`` CLI (V1.73-V1.76).

Every test drives the real CLI as a subprocess against a synthetic runs
root.  Live "pi" stand-ins are short-lived local Python processes that are
always reaped; deterministic dead PIDs use a value far above any Linux
pid_max, so they are dead by definition.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONTROL = REPO / "scripts" / "trajectory-pi-control"
READER = REPO / "scripts" / "trajectory-pi-status"
DEAD_PID = 4_294_967_295
RUN_NAME = "20260101-093000"


# ----------------------------------------------------------------- helpers

def cli(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CONTROL), *args],
        capture_output=True, text=True, timeout=timeout,
    )


def start_sitter(workspace: Path, *, ignore_term: bool = False) -> subprocess.Popen:
    """A long-lived stand-in for the ``pi`` process, rooted in workspace."""
    if ignore_term:
        program = (
            "import signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(3000)"
        )
    else:
        program = "import time; time.sleep(600)"
    return subprocess.Popen(
        [sys.executable, "-c", program],
        cwd=str(workspace),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def make_run(
    runs_root: Path,
    name: str,
    *,
    started_offset_seconds: float,
    heartbeat_elapsed: int = 2,
    pid: int | None = None,
    extra_meta: tuple[str, ...] = (),
    ended: bool = False,
    pi_exit_code: str | None = None,
) -> Path:
    """A run directory shaped exactly like the trajectory-pi producer's."""
    runs_root.mkdir(parents=True, exist_ok=True)
    d = runs_root / name
    d.mkdir()
    now = time.time()
    started_local = time.strftime(
        "%Y-%m-%d %H:%M:%S", time.localtime(now - started_offset_seconds))
    meta_lines = [
        "run_id=" + name,
        "started_at=" + started_local,
        "workspace=" + str(runs_root.parents[1].resolve()),
    ]
    if pid is not None:
        meta_lines.append(f"pid={pid}")
        meta_lines.append(f"pi_pid={pid}")
    if ended:
        ended_local = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(now - started_offset_seconds + 60))
        meta_lines.append("ended_at=" + ended_local)
        if pi_exit_code is not None:
            meta_lines.append(f"pi_exit_code={pi_exit_code}")
    for line in extra_meta:
        meta_lines.append(line)
    (d / "meta.txt").write_text("\n".join(meta_lines) + "\n", encoding="utf-8")
    hh, mm, ss = 0, 0, heartbeat_elapsed
    (d / "status.log").write_text(
        f"[{hh:02d}:{mm:02d}:{ss:02d}] elapsed={hh:02d}:{mm:02d}:{ss:02d} | "
        "smoke | files=0 | ollama=none | gen_3s=unavailable tok/s |\n",
        encoding="utf-8",
    )
    if ended and pi_exit_code is not None:
        (d / "status.log").write_text(
            (d / "status.log").read_text(encoding="utf-8")
            + f"[01:00:00] rc={pi_exit_code}\n",
            encoding="utf-8",
        )
    return d


def text_of(result: subprocess.CompletedProcess) -> str:
    """stderr and stdout of a CLI invocation (assert against both)."""
    return result.stdout + result.stderr


def alive(pid: int) -> bool:
    """True when ``pid`` is live and not yet a zombie."""
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


def events(d: Path) -> list[dict[str, object]]:
    path = d / "control-events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def fingerprint(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()).hexdigest()
    return out


# -------------------------------------------------------------------- stop


def test_stop_success_produces_full_evidence(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    runs = ws / ".trajectory-pi" / "runs"
    proc = start_sitter(ws)
    try:
        d = make_run(runs, RUN_NAME, started_offset_seconds=5, pid=proc.pid)
        result = cli("--runs-root", str(runs), "stop", "--run", RUN_NAME,
                     "--grace", "5")
        assert result.returncode == 0, result.stderr
        assert "STOPPED" in result.stdout
        assert not alive(proc.pid), "target must actually be terminated"
    finally:
        if alive(proc.pid):
            proc.kill()
        proc.wait(timeout=10)
    trail = events(d)
    kinds = [str(e) for e in trail]
    assert any("stop-intent" in k for k in kinds)
    assert any("stop-signal-sent" in k for k in kinds)
    assert any('"STOPPED"' in k or "STOPPED" in k for k in kinds)
    assert not (d / "control.lock").exists(), "lock must be released"


def test_stop_rejects_dead_target_and_leaves_no_trace(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    runs = ws / ".trajectory-pi" / "runs"
    d = make_run(runs, RUN_NAME, started_offset_seconds=5, pid=DEAD_PID)
    before = fingerprint(runs)
    result = cli("--runs-root", str(runs), "stop", "--run", RUN_NAME)
    assert result.returncode == 2, text_of(result)
    assert "process-dead" in text_of(result)
    assert fingerprint(runs) == before, "a rejected stop must not mutate the run"
    assert not (d / "control-events.jsonl").exists()
    assert not (d / "control.lock").exists()


def test_stop_without_candidates_fails_closed(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    make_run(runs, RUN_NAME, started_offset_seconds=3600, pid=DEAD_PID)
    result = cli("--runs-root", str(runs), "stop")
    assert result.returncode == 2, text_of(result)
    assert "no stoppable" in text_of(result)


def test_stop_ambiguous_targets_fails_closed_and_signals_nobody(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    runs = ws / ".trajectory-pi" / "runs"
    a = start_sitter(ws)
    b = start_sitter(ws)
    try:
        make_run(runs, "20260101-093000", started_offset_seconds=5, pid=a.pid)
        make_run(runs, "20260102-093000", started_offset_seconds=5, pid=b.pid)
        result = cli("--runs-root", str(runs), "stop")
        assert result.returncode == 2
        assert "ambiguous" in text_of(result)
        assert "20260101-093000" in text_of(result)
        assert "20260102-093000" in text_of(result)
        assert alive(a.pid), "must not signal a candidate when ambiguous"
        assert alive(b.pid)
    finally:
        for p in (a, b):
            if alive(p.pid):
                p.kill()
            p.wait(timeout=10)


def test_stop_stale_but_verified_run_is_stoppable(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    runs = ws / ".trajectory-pi" / "runs"
    proc = start_sitter(ws)
    try:
        # started 200s ago, last heartbeat 1s into the run: stale (>120s)
        # but inside the identity start window (<=300s): stoppable.
        d = make_run(runs, RUN_NAME, started_offset_seconds=200,
                     heartbeat_elapsed=1, pid=proc.pid)
        assert _state_of(d, runs) == "stale"
        result = cli("--runs-root", str(runs), "stop", "--run", RUN_NAME,
                     "--grace", "5")
        assert result.returncode == 0, result.stderr
        assert not alive(proc.pid)
        assert any("stop-signal-sent" in str(e) for e in events(d))
    finally:
        if alive(proc.pid):
            proc.kill()
        proc.wait(timeout=10)


def test_stop_rejects_reused_pid_signature(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    runs = ws / ".trajectory-pi" / "runs"
    proc = start_sitter(ws)
    try:
        # Live pid, but the run's recorded start is an hour ago: a classic
        # pid-reuse signature.  Must be rejected and the process untouched.
        d = make_run(runs, RUN_NAME, started_offset_seconds=3600, pid=proc.pid)
        result = cli("--runs-root", str(runs), "stop", "--run", RUN_NAME)
        assert result.returncode == 2, text_of(result)
        assert "start-time-window-exceeded" in text_of(result)
        assert alive(proc.pid), "a reuse-signature pid must never be signalled"
        assert not (d / "control-events.jsonl").exists()
    finally:
        if alive(proc.pid):
            proc.kill()
        proc.wait(timeout=10)


def test_grace_timeout_reports_timeout_and_keeps_process(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    runs = ws / ".trajectory-pi" / "runs"
    proc = start_sitter(ws, ignore_term=True)
    try:
        d = make_run(runs, RUN_NAME, started_offset_seconds=5, pid=proc.pid)
        result = cli("--runs-root", str(runs), "stop", "--run", RUN_NAME,
                     "--grace", "1", timeout=30)
        assert result.returncode == 3, result.stdout + result.stderr
        assert "GRACE_TIMEOUT" in result.stdout
        assert alive(proc.pid), "a grace timeout must NEVER kill the process"
        trail = [str(e) for e in events(d)]
        assert any("stop-signal-sent" in k for k in trail)
        assert any("GRACE_TIMEOUT" in k for k in trail)
    finally:
        if alive(proc.pid):
            proc.kill()
        proc.wait(timeout=10)


def _state_of(d: Path, runs: Path) -> str:
    result = cli("--runs-root", str(runs), "status", "--run", d.name, "--json")
    assert result.returncode == 0, text_of(result)
    return json.loads(result.stdout)["control"]["state"]


# ------------------------------------------------------------------- locks


def test_lock_live_holder_conflicts_and_signals_nothing(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    runs = ws / ".trajectory-pi" / "runs"
    target = start_sitter(ws)
    holder = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        d = make_run(runs, RUN_NAME, started_offset_seconds=5, pid=target.pid)
        (d / "control.lock").write_text(
            json.dumps({"schema": "control-lock/1", "action": "stop",
                        "holder": str(holder.pid)}),
            encoding="utf-8",
        )
        result = cli("--runs-root", str(runs), "stop", "--run", RUN_NAME)
        assert result.returncode == 5, result.stdout + result.stderr
        assert "lock" in text_of(result).lower()
        assert alive(target.pid), "a lock conflict must never signal the target"
    finally:
        for p in (target, holder):
            if alive(p.pid):
                p.kill()
            p.wait(timeout=10)


def test_lock_dead_holder_is_reclaimed_and_stop_proceeds(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    runs = ws / ".trajectory-pi" / "runs"
    target = start_sitter(ws)
    try:
        d = make_run(runs, RUN_NAME, started_offset_seconds=5, pid=target.pid)
        (d / "control.lock").write_text(
            json.dumps({"schema": "control-lock/1", "action": "stop",
                        "holder": str(DEAD_PID)}),
            encoding="utf-8",
        )
        result = cli("--runs-root", str(runs), "stop", "--run", RUN_NAME,
                     "--grace", "5")
        assert result.returncode == 0, result.stdout + result.stderr
        assert not alive(target.pid)
    finally:
        if alive(target.pid):
            target.kill()
        target.wait(timeout=10)


def test_lock_corrupt_content_fails_closed_and_signals_nothing(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    runs = ws / ".trajectory-pi" / "runs"
    target = start_sitter(ws)
    try:
        d = make_run(runs, RUN_NAME, started_offset_seconds=5, pid=target.pid)
        (d / "control.lock").write_text("{not-json", encoding="utf-8")
        result = cli("--runs-root", str(runs), "stop", "--run", RUN_NAME)
        assert result.returncode == 6, result.stdout + result.stderr
        assert alive(target.pid), "a corrupt lock must fail closed, never guess"
    finally:
        if alive(target.pid):
            target.kill()
        target.wait(timeout=10)


# ------------------------------------------------------------- list/status


def test_list_is_read_only_deterministic_and_complete(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    runs = ws / ".trajectory-pi" / "runs"
    make_run(runs, "20260101-093000", started_offset_seconds=5,
             pid=DEAD_PID, ended=True, pi_exit_code="0")
    make_run(runs, "20260102-093000", started_offset_seconds=5,
             pid=DEAD_PID, ended=True, pi_exit_code="1")
    before = fingerprint(runs)
    first = cli("--runs-root", str(runs), "list", "--json")
    second = cli("--runs-root", str(runs), "list", "--json")
    assert first.returncode == 0
    a, b = json.loads(first.stdout), json.loads(second.stdout)
    for view in (a, b):
        view.pop("generated_at", None)
    assert a == b, "list must be deterministic for unchanged run evidence"
    assert fingerprint(runs) == before, "list must not mutate anything"
    assert a["schema"] == "trajectory-pi-control/1.76"
    by_name = {r["name"]: r["control"] for r in a["runs"]}
    clean = by_name["20260101-093000"]
    assert clean["stoppable"] is False
    assert clean["recovery"]["ready"] is True
    assert "resume_hint" in clean
    crashed = by_name["20260102-093000"]
    assert crashed["recovery"]["ready"] is False
    assert "abnormal-exit:1" in crashed["recovery"]["reasons"]


def test_status_exposes_view_with_reasons(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    runs = ws / ".trajectory-pi" / "runs"
    make_run(runs, "20260101-093000", started_offset_seconds=5,
             pid=DEAD_PID, ended=True, pi_exit_code="0")
    result = cli("--runs-root", str(runs), "status", "--run", "20260101-093000", "--json")
    assert result.returncode == 0
    view = json.loads(result.stdout)["control"]
    assert view["state"] == "ended"
    assert view["recovery"]["ready"] is True
    assert view["stoppable"] is False
    assert "targetable_safe" in view and "stoppable_reasons" in view


def test_status_rejects_unknown_run(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    result = cli("--runs-root", str(runs), "status", "--run", "no-such-run")
    # An unknown run is a usage error (64), not a run-level rejection (2).
    assert result.returncode == 64
    assert "unknown run" in result.stderr


def test_usage_errors_exit_64(tmp_path: Path) -> None:
    for args in ((),):
        result = cli("--runs-root", str(tmp_path), *args)
        assert result.returncode == 64
    result = cli("--runs-root", str(tmp_path), "bogus-command")
    assert result.returncode == 64
    result = cli("--runs-root", str(tmp_path), "stop", "--run", "x",
                 "--grace", "not-a-number")
    assert result.returncode == 64


# --------------------------------------------------------------- the reader


def test_reader_exposes_runtime_control_surface(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / ".trajectory-pi" / "runs").mkdir(parents=True)
    make_run(ws / ".trajectory-pi" / "runs", "20260101-093000",
             started_offset_seconds=3600, pid=DEAD_PID,
             ended=True, pi_exit_code="0")
    result = subprocess.run(
        [sys.executable, str(READER),
         "--runs-root", str(ws / ".trajectory-pi" / "runs"),
         "--now", "2026-01-01T23:59:59+00:00", "--json"],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, text_of(result)
    data = json.loads(result.stdout)
    run = (
        next(r for r in data["runs"] if r.get("name") == "20260101-093000")
        if "runs" in data
        else data
    )
    rc = run["runtime_control"]
    assert rc is not None
    assert rc["recovery"]["ready"] is True
    assert rc["stoppable"] is False
    assert rc["targetable_safe"] is False
    # The historical control surface must remain present and intact.
    assert "control" in run and isinstance(run["control"], dict)


# ------------------------------------------------------------ source guards


def test_cli_source_has_no_daemon_or_kill_path() -> None:
    src = CONTROL.read_text(encoding="utf-8")
    for token in ("signal.SIGKILL", "os.kill(pid, 9)", "subprocess",
                  "import socket", "urllib", "requests", "http://",
                  "https://"):
        assert token not in src, token


def test_stop_never_escalates_beyond_sigterm_in_core() -> None:
    src = (REPO / "src/trajectory_os/runtime_control.py").read_text(
        encoding="utf-8")
    assert "signal.SIGKILL" not in src
