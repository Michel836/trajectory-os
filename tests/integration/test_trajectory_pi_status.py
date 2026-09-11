"""Integration tests for scripts/trajectory-pi-status (issue #176 / V1.67).

Every test drives the real ``scripts/trajectory-pi-status`` reader against a
deterministic synthetic ``.trajectory-pi/runs`` root inside ``tmp_path``.

Covered:
- run selection (latest vs explicit) and text/JSON rendering
- parsed telemetry (model, elapsed, speed, counters, phase, last event)
- absent telemetry (renders unknown/unavailable, never invents)
- malformed artifacts (flagged as warnings, valid parts still parsed)
- stale artifacts (flagged, without mutation)
- no-mutation inspection (byte-identical runs root before/after)
- no forbidden commands (no mutating git, no network, no daemon/loop)
- V1.69 run identity & lifecycle fields
- V1.70+ producer-recorded identity (pi_pid passthrough, never invented)
- V1.72 control-surface readiness view (fail-closed, evidence-gated)
- milestone scope guards (producer contract pinned; co-scripts untouched)

Stdlib + pytest only. No network, no real Pi, no credentials.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
READER = REPO_ROOT / "scripts" / "trajectory-pi-status"

NOW = "2026-01-01T12:00:30+00:00"
NOW_STALE = "2026-01-01T13:00:30+00:00"

RUN_ROOT = Path("runs")

COMPLETED = "20260101-100000"
MALFORMED = "20260101-110000"
LIVE = "20260101-120000"


# ------------------------------------------------------------ fixtures

def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_runs_root(tmp_path: Path) -> Path:
    root = tmp_path / RUN_ROOT

    # --- completed run (READY_FOR_COMMIT) --------------------------
    d = root / COMPLETED
    _write(d / "meta.txt", """\
trajectory_pi_version=0.3.1
started_at=2026-01-01T09:50:00+00:00
run_class=feature
eta=20-45 min
model=fake-model
branch=test/pi-status
head_before=0123456789abcdef
review_enabled=1
ended_at=2026-01-01T09:58:00+00:00
elapsed_seconds=480
pi_exit_code=0
head_after=0123456789abcdef
agent_classification=AGENT_COMPLETED
repository_readiness=READY_FOR_COMMIT
readiness_reason=ALL DETERMINISTIC GATES PASS + INDEPENDENT REVIEW PASS
validation=NOT_CONFIGURED
snapshot_status=COMPLETE
diff_check=PASS
review_status=PASS
final_verify_status=PASS
decision_required=GO COMMIT
""")
    _write(d / "status.log",
           "[09:50:01] elapsed=00:00:01 | within expected startup | "
           "files=0 (+0) | ollama=active | GPU 12% | 100 W | "
           "VRAM 1000/2000 MiB | gen_3s=10.5 tok/s | gen_total=10 tokens\n"
           "[09:51:01] elapsed=00:01:01 | within expected startup | "
           "files=2 (+2) | ollama=active | GPU 55% | 200 W | "
           "VRAM 1500/2000 MiB | gen_3s=12.25 tok/s | gen_total=25 tokens\n")
    _write(d / "pi.log", "fake finished transcript\n")
    _write(d / "worktree-status.txt",
           "# current working-tree status (real index)\n"
           " M src/thing.py\n"
           " M src/other.py\n"
           "?? tests/thing.py\n")
    _write(d / "worktree-files.txt",
           " M src/thing.py\n M src/other.py\n?? tests/thing.py\n")
    _write(d / "worktree-metadata.txt",
           "snapshot_status=COMPLETE\ntotal_files=3\n")
    _write(d / "review.txt",
           "VERDICT: PASS\n\nBLOCKERS:\n- None\n\n"
           "MAJORS:\n- None\n\nFINAL RECOMMENDATION: GO COMMIT\n")
    _write(d / "review-meta.txt", "review_status=PASS\n")

    # --- malformed run (flagged, still partially parsed) -----------
    d = root / MALFORMED
    _write(d / "meta.txt", (
        "trajectory_pi_version=0.3.1\n"
        "started_at=2026-01-01T11:59:50+00:00\n"
        "not a key value line\n"
        "model=fake-model\n"
        "\n"
        "=value-without-key\n"
        "run_class=smoke\n"
    ))
    _write(d / "status.log",
           "this line is not a heartbeat\n"
           "[11:59:51] elapsed=00:00:01 | within expected startup | "
           "files=0 (+0) | ollama=idle | GPU 5% | 50 W | "
           "VRAM 500/2000 MiB | gen_3s=unavailable\n"
    )
    _write(d / "pi.log", "")

    # --- live run (fresh heartbeats relative to NOW) ----------------
    d = root / LIVE
    _write(d / "meta.txt",
           "trajectory_pi_version=0.3.1\n"
           "started_at=2026-01-01T12:00:00+00:00\n"
           "run_class=feature\n"
           "eta=20-45 min\n"
           "model=fake-model\n"
           "branch=test/pi-status\n"
           "head_before=0123456789abcdef\n"
           "review_enabled=0\n")
    _write(d / "status.log",
           "[12:00:01] elapsed=00:00:01 | within expected startup | "
           "files=0 (+0) | ollama=active | GPU 10% | 100 W | "
           "VRAM 1000/2000 MiB | gen_3s=10.5 tok/s\n"
           "[12:00:20] elapsed=00:00:20 | within expected startup | "
           "files=1 (+1) | ollama=active | GPU 20% | 100 W | "
           "VRAM 1100/2000 MiB | gen_3s=9.3 tok/s | "
           "last_gen=10.5 tok/s age=5s\n")
    _write(d / "pi.log", "")
    return root


# ------------------------------------------------------------ helpers

def run_reader(tmp_path: Path, *extra: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    return subprocess.run(
        [sys.executable, str(READER),
         "--runs-root", str(tmp_path / RUN_ROOT),
         "--now", NOW, *extra],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def tree_fingerprint(root: Path) -> dict[str, tuple[str, int]]:
    out: dict[str, tuple[str, int]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                path.stat().st_mtime_ns,
            )
        elif path.is_dir():
            out[str(path.relative_to(root)) + "/"] = ("dir",
                                                      path.stat().st_mtime_ns)
    return out


# ------------------------------------------------------------ selection

def test_latest_run_selected_by_default(tmp_path: Path) -> None:
    build_runs_root(tmp_path)
    proc = run_reader(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[0].startswith(f"RUN: {LIVE}")


def test_explicit_run_selection(tmp_path: Path) -> None:
    build_runs_root(tmp_path)
    proc = run_reader(tmp_path, "--run", str(tmp_path / RUN_ROOT / COMPLETED))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[0].startswith(f"RUN: {COMPLETED}")


def test_missing_runs_root_fails_deterministically(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(READER),
         "--runs-root", str(tmp_path / "does-not-exist"), "--now", NOW],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 2
    assert "no runs found" in proc.stderr


def test_bad_explicit_run_dir_fails_deterministically(tmp_path: Path) -> None:
    proc = run_reader(tmp_path, "--run", str(tmp_path / "no-such-run"))
    assert proc.returncode == 2
    assert "not found" in proc.stderr


# ------------------------------------------------------------ rendering

@pytest.fixture
def runs(tmp_path: Path) -> Path:
    return build_runs_root(tmp_path)


def test_completed_run_parsed_telemetry_and_rendering(runs: Path) -> None:
    proc = run_reader(runs.parent, "--run", str(runs / COMPLETED))
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "pi_state    : ended (pi_exit_code=0)" in out
    assert "model       : fake-model" in out
    assert "elapsed     : 00:08:00" in out          # meta elapsed_seconds=480
    assert "eta         : n/a (run ended)" in out
    assert "12.25 tok/s (gen_3s" in out             # last heartbeat speed
    assert "25 tokens (gen_total @ 09:51:01)" in out
    assert "files=2 (+2) | ollama=active" in out    # counters
    assert "phase       : within expected startup" in out
    assert "repository_readiness=READY_FOR_COMMIT" in out
    assert "src/thing.py" in out
    assert "src/other.py" in out
    assert "tests/thing.py" in out
    assert "3 changed (2 modified, 1 untracked)" in out
    assert "blockers    : none" in out
    assert "readiness   : READY_FOR_COMMIT" in out
    assert "human decision first (decision_required=GO COMMIT)" in out
    assert "warnings    : none" in out


def test_live_run_parsed_telemetry(runs: Path) -> None:
    proc = run_reader(runs.parent)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "pi_state    : running (live" in out
    assert "model       : fake-model" in out
    assert "elapsed     : 00:00:20" in out           # last heartbeat
    assert "unavailable (no measured progress baseline)" in out  # no invented ETA
    assert "9.3 tok/s (gen_3s @ 12:00:20)" in out
    assert "tokens      : unavailable" in out
    assert "files=1 (+1) | ollama=active" in out
    assert "phase       : within expected startup" in out
    assert "[12:00:20] elapsed=00:00:20" in out      # last event line
    assert "python scripts/trajectory_gate.py implement" in out
    out_json = run_reader(runs.parent, "--json")
    data = json.loads(out_json.stdout)
    assert data["pi_state"] == "running"
    assert data["model"] == "fake-model"
    assert data["run"] == LIVE
    assert data["blockers"] == []
    assert data["eta"].startswith("unavailable")
    assert data["tokens"] == "unavailable"


def test_absent_telemetry_never_invented(runs: Path) -> None:
    proc = run_reader(runs.parent, "--run", str(runs / MALFORMED))
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "pi_state    : running" in out
    assert "speed       : unavailable" in out
    assert "unavailable (no measured progress baseline)" in out
    assert "unavailable (no worktree artifact)" in out
    data = json.loads(run_reader(
        runs.parent, "--run", str(runs / MALFORMED), "--json").stdout)
    assert data["speed"] == "unavailable"
    assert data["worktree"]["state"].startswith("unavailable")
    assert data["readiness"]["repository_readiness"] == "unknown"


def test_completed_empty_snapshot_renders_clean_not_unavailable(
    runs: Path,
) -> None:
    run = runs / COMPLETED
    _write(run / "worktree-files.txt", "")
    _write(run / "worktree-status.txt", "# current working-tree status (real index)\n")
    _write(run / "worktree-metadata.txt", "snapshot_status=COMPLETE\ntotal_files=0\n")

    data = json.loads(run_reader(
        runs.parent, "--run", str(run), "--json").stdout)

    assert data["worktree"]["state"] == "clean / 0 changed"
    assert data["worktree"]["source"] == "worktree-files.txt"
    assert data["worktree"]["entries"] == []


def test_incomplete_snapshot_is_explicit_and_never_clean(runs: Path) -> None:
    run = runs / COMPLETED
    _write(run / "worktree-files.txt", "")
    _write(run / "worktree-metadata.txt", "snapshot_status=FAILED\ntotal_files=0\n")

    data = json.loads(run_reader(
        runs.parent, "--run", str(run), "--json").stdout)

    assert data["worktree"]["state"].startswith("unavailable")
    assert data["worktree"]["state"] != "clean / 0 changed"
    assert any("snapshot evidence" in warning for warning in data["warnings"])


def test_decreasing_cumulative_tokens_are_unavailable(runs: Path) -> None:
    run = runs / LIVE
    _write(run / "status.log",
           "[12:00:01] elapsed=00:00:01 | within expected startup | "
           "files=0 (+0) | ollama=active | gen_total=20 tokens\n"
           "[12:00:20] elapsed=00:00:20 | within expected startup | "
           "files=0 (+0) | ollama=active | gen_total=19 tokens\n")

    data = json.loads(run_reader(runs.parent, "--json").stdout)

    assert data["tokens"] == "unavailable"
    assert any("cumulative token telemetry decreased" in warning
               for warning in data["warnings"])


def test_empty_run_dir_no_crash(tmp_path: Path) -> None:
    (tmp_path / RUN_ROOT / "20260102-000000").mkdir(parents=True)
    proc = run_reader(tmp_path, "--run",
                      str(tmp_path / RUN_ROOT / "20260102-000000"))
    assert proc.returncode == 0, proc.stderr
    assert "pi_state    : unknown" in proc.stdout
    assert "WARNING: missing meta.txt (metadata unavailable)" in proc.stdout


def test_malformed_artifacts_flagged_without_hiding_data(runs: Path) -> None:
    proc = run_reader(runs.parent, "--run", str(runs / MALFORMED))
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "WARNING: meta.txt has 2 malformed line(s)" in out
    assert "WARNING: status.log has 1 malformed line(s)" in out
    # valid parts of the same artifacts must still be rendered:
    assert "model       : fake-model" in out
    assert "phase       : within expected startup" in out
    assert "elapsed     : 00:00:01" in out


def test_stale_artifacts_flagged(runs: Path) -> None:
    env = dict(os.environ)
    proc = subprocess.run(
        [sys.executable, str(READER), "--runs-root", str(runs),
         "--now", NOW_STALE],
        cwd=runs.parent, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "pi_state    : stale" in proc.stdout
    assert "WARNING: STALE status.log" in proc.stdout
    assert "stale heartbeat telemetry" in proc.stdout  # blocker raised


def test_review_blockers_rendered(tmp_path: Path) -> None:
    run = tmp_path / RUN_ROOT / "20260103-000000"
    (run / "meta.txt").parent.mkdir(parents=True, exist_ok=True)
    _write(run / "meta.txt",
           "started_at=2026-01-02T23:50:00+00:00\nmodel=fake-model\n"
           "ended_at=2026-01-02T23:59:00+00:00\npi_exit_code=0\n"
           "elapsed_seconds=540\n"
           "repository_readiness=NEEDS_REVIEW\n"
           "readiness_reason=INDEPENDENT REVIEW FAIL (NEVER TREATED AS PASS)\n"
           "review_status=FAIL\nfinal_verify_status=PASS\n"
           "diff_check=PASS\nsnapshot_status=COMPLETE\n"
           "decision_required=HUMAN REVIEW REQUIRED\n")
    _write(run / "review.txt",
           "VERDICT: FAIL\n\nBLOCKERS:\n- patch alters out-of-scope module\n"
           "- missing tests\n\nFINAL RECOMMENDATION: NO COMMIT\n")
    proc = run_reader(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "- independent review: FAIL" in proc.stdout
    assert "- review blocker: patch alters out-of-scope module" in proc.stdout
    assert "- review blocker: missing tests" in proc.stdout
    assert "human decision first" in proc.stdout


def test_review_none_is_not_rendered_as_a_blocker(runs: Path) -> None:
    proc = run_reader(runs.parent, "--run", str(runs / COMPLETED))
    assert proc.returncode == 0, proc.stderr
    assert "review blocker: None" not in proc.stdout


def test_max_files_truncation(runs: Path) -> None:
    data = json.loads(run_reader(
        runs.parent, "--run", str(runs / COMPLETED), "--max-files", "2",
        "--json").stdout)
    assert len(data["worktree"]["entries"]) == 2
    assert data["worktree"]["truncated"] == 1
    assert "(truncated)" in run_reader(
        runs.parent, "--run", str(runs / COMPLETED), "--max-files", "2"
    ).stdout


# ------------------------------------------------------------ no mutation

def test_inspection_is_byte_for_byte_non_mutating(runs: Path) -> None:
    before = tree_fingerprint(runs)
    for extra in ([], ["--json"], ["--max-files", "1"]):
        proc = run_reader(runs.parent, *extra)
        assert proc.returncode == 0, proc.stderr
    after = tree_fingerprint(runs)
    assert before == after, "reader mutated or created run artifacts"


# ------------------------------------------------------------ forbidden ops

def test_no_mutating_git_or_network_in_source() -> None:
    src = READER.read_text(encoding="utf-8")
    for verb in ("add", "commit", "push", "merge", "amend", "reset",
                 "restore", "clean", "stash", "rebase", "cherry-pick",
                 "tag", "clone", "fetch", "pull", "reflog", "gc", "repack"):
        assert not re.search(rf"git\s+{re.escape(verb)}\b", src), \
            f"forbidden mutating git verb in reader source: {verb}"
    for token in ("\bcurl\b", "\bwget\b", "\bncat\b", "\bsocket\b",
                  "\burllib\b", "http.client", "http://", "https://",
                  "\brequests\b"):
        assert not re.search(token, src), \
            f"network/tooling token in reader source: {token}"
    # exactly one external invocation, and it is the read-only status call
    assert src.count("subprocess.run(") == 1
    assert re.search(r'"git", "status", "--porcelain"', src)


def test_git_observation_is_lock_free_and_read_only() -> None:
    src = READER.read_text(encoding="utf-8")
    assert 'env["GIT_OPTIONAL_LOCKS"] = "0"' in src
    assert "stdin=subprocess.DEVNULL" in src


def test_no_daemon_or_polling_loop_in_source() -> None:
    src = READER.read_text(encoding="utf-8")
    assert "while True" not in src
    assert "sleep(" not in src
    assert "fork(" not in src
    assert "Popen" not in src


def test_worktree_live_fallback_degrades_outside_a_work_tree(runs: Path) -> None:
    # The live fallback may only run inside a real work tree; the tmp_path
    # cwd is NOT one, so the reader must degrade to 'unavailable' without
    # spawning any git state change.
    proc = run_reader(runs.parent, "--worktree-live", "--json")
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["worktree"]["state"].startswith("unavailable")
    assert "live" not in data["worktree"]["source"]


# ------------------------------------------------------------ V1.69: run identity & lifecycle
#
# V1.69 contract (deterministic Pi run identity and lifecycle metadata):
# - a live Pi execution is unambiguously associated with a run identity;
# - consumers observing the same live run see the same identity, regardless
#   of selection mode (latest vs explicit path/name);
# - a stale/previous run never shares its identity with a new run;
# - lifecycle state + started/updated timestamps are exposed without
#   mutation; the updated timestamp is only ever a measurement of recorded
#   artifacts, never invented.


def _json(runs: Path, *extra: str) -> dict:
    proc = run_reader(runs.parent, *extra, "--json")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_run_identity_is_stable_across_selection_modes(runs: Path) -> None:
    latest = _json(runs)                                  # latest = LIVE
    explicit_path = _json(runs, "--run", str(runs / LIVE))
    explicit_name = _json(runs, "--run", LIVE)
    for data in (latest, explicit_path, explicit_name):
        assert data["run_id"] == LIVE
        assert data["identity"]["run_id"] == LIVE
        assert data["identity"]["run_dir"] == str((runs / LIVE).resolve())
    assert latest["identity"]["run_id"] == explicit_path["identity"]["run_id"]


def test_different_runs_never_share_identity(runs: Path) -> None:
    completed = _json(runs, "--run", str(runs / COMPLETED))
    live = _json(runs, "--run", str(runs / LIVE))
    assert completed["identity"]["run_id"] != live["identity"]["run_id"]
    assert completed["identity"]["run_dir"] != live["identity"]["run_dir"]
    # The completed run is the earlier/previous run; its identity must not
    # bleed into the newer live run.
    assert completed["run_id"] == COMPLETED
    assert live["run_id"] == LIVE


def test_identity_fields_follow_meta_or_unknown(runs: Path) -> None:
    data = _json(runs)
    idt = data["identity"]
    assert idt["branch"] == "test/pi-status"
    assert idt["head_before"] == "0123456789abcdef"
    # Legacy runs (producer did not record these) must render unknown,
    # never an invented value.
    assert idt["workspace"] == "unknown"
    assert idt["pid"] == "unknown"


def test_identity_passthrough_when_recorded(tmp_path: Path) -> None:
    run = tmp_path / RUN_ROOT / "20260104-000000"
    (run / "meta.txt").parent.mkdir(parents=True, exist_ok=True)
    _write(run / "meta.txt",
           "started_at=2026-01-04T00:00:00+00:00\n"
           "branch=test/pi-status\n"
           "head_before=0123456789abcdef\n"
           "workspace=/srv/trajectory-os\n"
           "pid=4242\n")
    data = _json(tmp_path / RUN_ROOT)
    idt = data["identity"]
    assert idt["run_id"] == "20260104-000000"
    assert idt["workspace"] == "/srv/trajectory-os"
    assert idt["pid"] == "4242"
    assert idt["branch"] == "test/pi-status"


def test_identity_rejects_malformed_pid(tmp_path: Path) -> None:
    run = tmp_path / RUN_ROOT / "20260105-000000"
    (run / "meta.txt").parent.mkdir(parents=True, exist_ok=True)
    _write(run / "meta.txt",
           "started_at=2026-01-05T00:00:00+00:00\npid=not-a-pid\n")
    data = _json(tmp_path / RUN_ROOT)
    assert data["identity"]["pid"] == "unknown"


def test_lifecycle_state_tracks_pi_state(runs: Path) -> None:
    completed = _json(runs, "--run", str(runs / COMPLETED))
    assert completed["lifecycle"]["state"] == "ended"
    assert completed["pi_state"] == "ended"         # backward compatible
    assert completed["lifecycle"]["started_at"] == "2026-01-01T09:50:00+00:00"
    assert completed["lifecycle"]["ended_at"] == "2026-01-01T09:58:00+00:00"

    live = _json(runs)
    assert live["lifecycle"]["state"] == "running"
    assert live["lifecycle"]["ended_at"] == "unknown"

    stale = subprocess.run(
        [sys.executable, str(READER), "--runs-root", str(runs),
         "--now", NOW_STALE, "--json"],
        cwd=runs.parent, capture_output=True, text=True, timeout=60,
    )
    assert stale.returncode == 0, stale.stderr
    stale_data = json.loads(stale.stdout)
    assert stale_data["lifecycle"]["state"] == "stale"


def test_lifecycle_updated_at_is_measured_not_invented(runs: Path) -> None:
    live = _json(runs)
    lc = live["lifecycle"]
    # last heartbeat elapsed=00:00:20 + started_at 12:00:00Z
    assert lc["updated_at"] == "2026-01-01T12:00:20+00:00"
    assert "heartbeat" in lc["updated_source"]

    completed = _json(runs, "--run", str(runs / COMPLETED))
    assert completed["lifecycle"]["updated_at"] == "2026-01-01T09:58:00+00:00"
    assert "ended_at" in completed["lifecycle"]["updated_source"]

    # malformed run: started_at + last valid heartbeat elapsed
    malformed = _json(runs, "--run", str(runs / MALFORMED))
    assert malformed["lifecycle"]["updated_at"] == "2026-01-01T11:59:51+00:00"


def _require_iso(value: str) -> None:
    from datetime import datetime as _dt
    assert value not in ("", "unknown")
    _dt.fromisoformat(value)  # raises if not valid ISO-8601


def test_lifecycle_updated_at_mtime_fallback(tmp_path: Path) -> None:
    run = tmp_path / RUN_ROOT / "20260106-000000"
    (run / "status.log").parent.mkdir(parents=True, exist_ok=True)
    _write(run / "status.log", "no parseable heartbeat here\n")
    # no meta, no started_at, no valid heartbeat: only artifact mtime knows
    fixed = time.mktime(time.strptime("2026-01-06 00:00:00", "%Y-%m-%d %H:%M:%S"))
    os.utime(run / "status.log", (fixed, fixed))
    data = _json(tmp_path / RUN_ROOT)
    lc = data["lifecycle"]
    assert lc["state"] == "unknown"
    assert lc["updated_at"] != "unknown"
    assert "status.log" in lc["updated_source"]
    # deterministic under pinned clock
    again = _json(tmp_path / RUN_ROOT)
    assert again["lifecycle"]["updated_at"] == lc["updated_at"]
    # round-trips as parseable ISO-8601
    _require_iso(again["lifecycle"]["updated_at"])


def test_lifecycle_empty_run_is_unknown_without_crash(tmp_path: Path) -> None:
    (tmp_path / RUN_ROOT / "20260107-000000").mkdir(parents=True)
    data = _json(tmp_path / RUN_ROOT)
    lc = data["lifecycle"]
    assert lc["state"] == "unknown"
    assert lc["started_at"] == "unknown"
    assert lc["updated_at"] == "unknown"
    assert data["identity"]["run_id"] == "20260107-000000"


def test_end_of_run_requires_a_parseable_ended_at(runs: Path) -> None:
    run = runs / COMPLETED
    text = (run / "meta.txt").read_text(encoding="utf-8")
    (run / "meta.txt").write_text(
        text.replace("ended_at=2026-01-01T09:58:00+00:00",
                     "ended_at=not-a-timestamp"),
        encoding="utf-8")
    data = _json(runs, "--run", str(run))
    lc = data["lifecycle"]
    # Without a parseable ended_at the run must NOT be claimed as ended,
    # and the malformed value must never leak into the updated timestamp.
    assert lc["state"] != "ended"
    assert lc["updated_at"] != "not-a-timestamp"
    # The heartbeat-derived measurement remains the only update evidence.
    assert lc["updated_at"] == "2026-01-01T09:51:01+00:00"


def test_v169_text_rendering_includes_identity_and_lifecycle(runs: Path) -> None:
    proc = run_reader(runs.parent, "--run", str(runs / LIVE))
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert f"RUN: {LIVE}" in out
    assert "run_id      : " + LIVE in out
    assert 'updated_at  : 2026-01-01T12:00:20+00:00' in out
    assert "workspace   : unknown" in out


def test_v169_json_output_is_deterministic(runs: Path) -> None:
    assert _json(runs) == _json(runs)          # byte-stable state object
    p1 = run_reader(runs.parent)
    p2 = run_reader(runs.parent)
    assert p1.stdout == p2.stdout


# ------------------------------------------------------------ milestone guards


def test_milestone_scope_guard() -> None:
    """Scope guard for the V1.70–V1.72 increment.

    V1.70 *intentionally* hardens ``scripts/trajectory-pi`` (producer-native
    run identity + fail-closed collision guard), so the old "byte-identical
    vs HEAD" assertion for that file no longer expresses a valid invariant
    (after merge, HEAD == disk would fail it). The invariant is now positive:
    the producer's identity contract must be pinned in source, and the
    milestone's non-goals (co-scripts untouched) stay byte-guarded.
    """
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    # non-goals: these scripts must remain byte-identical to HEAD
    for rel in ("scripts/trajectory-codex-pi", "scripts/trajectory_gate.py"):
        disk = (REPO_ROOT / rel).read_bytes()
        proc = subprocess.run(
            ["git", "show", f"HEAD:{rel}"],
            cwd=REPO_ROOT, env=env, capture_output=True, timeout=60,
        )
        assert proc.returncode == 0, rel
        assert disk == proc.stdout, f"out-of-scope script modified: {rel}"
    # in-scope: the producer contract is positive, structural, merge-stable
    producer = (REPO_ROOT / "scripts" / "trajectory-pi").read_text()
    assert "run_id=$STAMP" in producer
    assert "pid=$$" in producer
    assert "workspace=$WORKSPACE" in producer
    assert "pi_pid=$PI_PID" in producer
    assert "Run identity must be unique" in producer


def test_reader_is_new_and_executable() -> None:
    assert READER.is_file()
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    proc = subprocess.run(
        ["git", "ls-files", "--stage", "--", "scripts/trajectory-pi-status",
         "tests/integration/test_trajectory_pi_status.py"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0
    staged = {
        line.split("\t", maxsplit=1)[1]: line.split()[0]
        for line in proc.stdout.splitlines()
    }
    assert staged["scripts/trajectory-pi-status"] == "100755"
    assert staged["tests/integration/test_trajectory_pi_status.py"] == "100644"


# ------------------------------------------------------------ V1.72: control-surface foundation

# A run whose lifecycle evidence is complete: started_at recorded, a recent
# heartbeat, and producer-recorded identity — the minimal state in which a
# reader may say the run is ready for the control plane.
FULL_EVIDENCE = "20260102-090000"


def _full_evidence_meta(run_name: str, **overrides: str) -> str:
    base = {
        "trajectory_pi_version": "0.3.1",
        "run_id": run_name,
        "started_at": "2026-01-01T12:00:10+00:00",
        "pid": "4242",
        "pi_pid": "4243",
        "workspace": "/home/op/projects/demo",
        "run_class": "feature",
        "model": "fake-model",
        "branch": "test/pi-status",
        "head_before": "0123456789abcdef",
    }
    base.update(overrides)
    return "".join(f"{key}={value}\n" for key, value in base.items())


def _full_evidence_heartbeat() -> str:
    return (
        "[12:00:11] elapsed=00:00:01 | within expected startup | "
        "files=1 (+1) | ollama=active | GPU 10% | 100 W | "
        "VRAM 1000/2000 MiB | gen_3s=10.5 tok/s\n"
        "[12:00:20] elapsed=00:00:10 | within expected startup | "
        "files=2 (+2) | ollama=active | GPU 20% | 100 W | "
        "VRAM 1100/2000 MiB | gen_3s=11.1 tok/s\n"
    )


def test_control_ready_when_lifecycle_evidence_complete(tmp_path: Path) -> None:
    d = tmp_path / RUN_ROOT / FULL_EVIDENCE
    _write(d / "meta.txt", _full_evidence_meta(FULL_EVIDENCE))
    _write(d / "status.log", _full_evidence_heartbeat())

    data = _json(tmp_path / RUN_ROOT)
    assert data["run_id"] == FULL_EVIDENCE
    assert data["identity"]["pid"] == "4242"
    assert data["identity"]["pi_pid"] == "4243"
    assert data["lifecycle"]["state"] == "running"
    assert data["control"] == {
        "state": "running",
        "missing_evidence": [],
        "ready_for_control": True,
    }


def test_control_never_ready_for_weak_or_unknown_runs(tmp_path: Path) -> None:
    # legacy live run (pre-V1.70: no pid/pi_pid/workspace recorded):
    build_runs_root(tmp_path)
    data = _json(tmp_path / RUN_ROOT, "--run", str(tmp_path / RUN_ROOT / LIVE))
    assert data["lifecycle"]["state"] in ("running", "stale")
    assert data["control"]["ready_for_control"] is False
    for field in ("pid", "pi_pid", "workspace"):
        assert field in data["control"]["missing_evidence"]

    # evidence-less run: state unknown -> never ready
    (tmp_path / RUN_ROOT / "20260102-095000").mkdir(parents=True)
    data = _json(tmp_path / RUN_ROOT, "--run", "20260102-095000")
    assert data["lifecycle"]["state"] == "unknown"
    assert data["control"]["ready_for_control"] is False
    assert "started_at" in data["control"]["missing_evidence"]


def test_pi_pid_passthrough_is_recorded_or_unknown_never_invented(
    tmp_path: Path,
) -> None:
    # unrecorded (legacy) run -> unknown, not an invented value
    build_runs_root(tmp_path)
    data = _json(tmp_path / RUN_ROOT, "--run", str(tmp_path / RUN_ROOT / LIVE))
    assert data["identity"]["pi_pid"] == "unknown"
    assert data["identity"]["pid"] == "unknown"

    # malformed (non-numeric) values -> unknown, never "fixed"/derived
    d = tmp_path / RUN_ROOT / FULL_EVIDENCE
    _write(
        d / "meta.txt",
        "started_at=2026-01-01T12:00:10+00:00\n"
        "pid=not-a-number\n"
        "pi_pid=garbage-value\n",
    )
    _write(d / "status.log", _full_evidence_heartbeat())
    data = _json(tmp_path / RUN_ROOT, "--run", FULL_EVIDENCE)
    assert data["identity"]["pid"] == "unknown"
    assert data["identity"]["pi_pid"] == "unknown"


def test_run_id_crosscheck_surfaces_disagreement(tmp_path: Path) -> None:
    d = tmp_path / RUN_ROOT / FULL_EVIDENCE
    # producer-recorded run_id disagrees with the authoritative dir name:
    # the dir stays authoritative, and the disagreement is surfaced
    _write(d / "meta.txt", _full_evidence_meta("20990101-000000"))
    _write(d / "status.log", _full_evidence_heartbeat())
    data = _json(tmp_path / RUN_ROOT)
    assert data["run_id"] == FULL_EVIDENCE
    assert sum("disagrees" in w for w in data["warnings"]) >= 1

    # when the producer records the correct identity, no disagreement warning
    _write(d / "meta.txt", _full_evidence_meta(FULL_EVIDENCE))
    data = _json(tmp_path / RUN_ROOT)
    assert sum("disagrees" in w for w in data["warnings"]) == 0


def test_control_surface_appears_in_text_output(tmp_path: Path) -> None:
    d = tmp_path / RUN_ROOT / FULL_EVIDENCE
    _write(d / "meta.txt", _full_evidence_meta(FULL_EVIDENCE))
    _write(d / "status.log", _full_evidence_heartbeat())

    proc = run_reader(tmp_path)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "pi_pid      : 4243" in out
    assert "control     : ready (state=running; evidence complete)" in out


def test_v169_contract_fields_stable_under_v172(runs: Path) -> None:
    # backward compatibility: every V1.69 reader field the existing consumer
    # surface relies on is still present and unchanged in shape under V1.72
    data = _json(runs)
    assert set(data) >= {
        "run_id", "run_dir", "identity", "lifecycle", "started_at", "ended_at",
        "run_class", "branch", "model", "elapsed", "warnings",
    }
    assert set(data["identity"]) >= {
        "run_id", "run_dir", "branch", "head_before", "workspace",
        "pid", "pi_pid",
    }
    assert set(data["lifecycle"]) >= {
        "state", "detail", "started_at", "ended_at", "updated_at",
        "updated_source", "elapsed",
    }
    assert "control" in data
