"""Integration tests for the explicit ``--require-changes`` operator contract.

Proves, against the REAL ``scripts/trajectory-pi`` wrapper inside an
isolated temporary Git repository with a deterministic fake ``pi`` on
PATH, that a run that explicitly requires repository changes can never
reach READY_FOR_COMMIT merely because of:

    Pi exit = 0 + completion marker + empty new patch + review PASS,

and that the contract:

  * leaves default (read-only / smoke, zero-diff) runs completely
    unchanged;
  * fails closed (NEEDS_REVIEW, deterministic reason) when >=1 change
    NEW RELATIVE TO THE BASELINE is missing;
  * never counts pre-existing dirty-baseline files (with --dirty-ok);
  * is satisfied by a genuine new worktree change;
  * cannot be faked into success by repair passes or an independent
    review PASS; can be genuinely satisfied only when a repair pass
    lands a NEW change;
  * reports the requirement and result in deterministic metadata;
  * introduces no live Git index mutation.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "scripts" / "trajectory-pi"

# Deterministic fake agent (writable). Supports:
#   FAKE_PI_CREATE_FILE        create a file on invocation
#   FAKE_PI_CREATE_ON_REPAIR   create the file ONLY on the Nth>=2
#                              invocation (repair pass), never on pass 1
#   FAKE_PI_INVOCATION_LOG     log every agent invocation (one line each)
FAKE_PI = r'''#!/usr/bin/env bash
if [[ -n "${FAKE_PI_INVOCATION_LOG:-}" ]]; then
  printf 'AGENT\n' >> "$FAKE_PI_INVOCATION_LOG"
fi

if [[ -n "${FAKE_PI_CREATE_FILE:-}" ]]; then
  mkdir -p "$(dirname "${FAKE_PI_CREATE_FILE}")"
  printf '%s\n' "${FAKE_PI_CREATE_CONTENT:-hello}" > "${FAKE_PI_CREATE_FILE}"
fi

if [[ -n "${FAKE_PI_CREATE_ON_REPAIR:-}" ]]; then
  # invocation numbering: first pass = 1; repair pass = >= 2. Only the
  # repair pass (or later) lands the new file — pass 1 stays zero-diff.
  count=0
  if [[ -n "${FAKE_PI_INVOCATION_LOG:-}" && -f "$FAKE_PI_INVOCATION_LOG" ]]; then
    count="$(wc -l < "$FAKE_PI_INVOCATION_LOG")"
  fi
  if (( count >= 2 )); then
    mkdir -p "$(dirname "${FAKE_PI_CREATE_ON_REPAIR}")"
    printf '%s\n' "${FAKE_PI_CREATE_CONTENT:-repaired}" > "${FAKE_PI_CREATE_ON_REPAIR}"
  fi
fi

printf 'HANDOFF\n'
printf 'TASK: fake agent task\n'
printf 'RESULT: complete\n'
printf '%s\n' "${FAKE_PI_MARKER-FEATURE_IMPLEMENTED_COMPLETE}"
exit 0
'''

REVIEW_PASS = (
    "VERDICT: PASS\n"
    "\n"
    "BLOCKERS:\n"
    "- none\n"
    "\n"
    "MAJORS:\n"
    "- none\n"
    "\n"
    "MINORS:\n"
    "- none\n"
    "\n"
    "FINAL RECOMMENDATION: GO COMMIT\n"
)


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    fake_pi = fake_bin / "pi"
    fake_pi.write_text(FAKE_PI)
    fake_pi.chmod(0o755)

    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-b", "main")
    git(r, "config", "user.email", "t@example.invalid")
    git(r, "config", "user.name", "TrajectoryOS Test")
    (r / "tracked.py").write_text("v = 1\n")
    (r / ".gitignore").write_text(".trajectory-pi/\n")
    git(r, "add", "-A")
    git(r, "commit", "-m", "init", "--quiet")
    git(r, "checkout", "-b", "feature/test")

    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    return r


def wrapper_env(**extra: str) -> dict[str, str]:
    """Caller environment for launching a fresh wrapper (never inherit
    wrapper-internal bootstrap state from the ambient environment)."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("TRAJECTORY_PI_BOOTSTRAP", "TRAJECTORY_PI_BOOTSTRAP_COPY")
    }
    env.update(extra)
    return env


def run(
    repo: Path,
    env: dict[str, str] | None = None,
    *args: str,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    full_env = wrapper_env()
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", str(WRAPPER), *args],
        cwd=repo,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def run_dir(repo: Path, proc: subprocess.CompletedProcess) -> Path:
    m = re.search(r"ARTIFACTS: (.+)", proc.stdout)
    assert m, f"no ARTIFACTS line in output:\n{proc.stdout}"
    return Path(m.group(1))


def line(proc: subprocess.CompletedProcess, key: str) -> str:
    m = re.search(
        r"^\s*" + re.escape(key) + r": (.*)$",
        proc.stdout,
        re.IGNORECASE | re.MULTILINE,
    )
    assert m, f"missing '{key}: ' line in:\n{proc.stdout}"
    return m.group(1).strip()


def meta_value(repo: Path, proc: subprocess.CompletedProcess, key: str) -> str:
    """Last occurrence of key= value in meta.txt (repair passes append
    again; the final pass is authoritative)."""
    meta = (run_dir(repo, proc) / "meta.txt").read_text()
    matches = [m.group(1) for m in re.finditer(r"^" + re.escape(key) + r"=(.*)$", meta, re.M)]
    assert matches, f"missing '{key}=...' in meta.txt:\n{meta}"
    return matches[-1].strip()


def index_identity(repo: Path) -> tuple[bytes, int]:
    p = repo / ".git" / "index"
    st = p.stat()
    return p.read_bytes(), st.st_mtime_ns


class FakeOllama:
    """Isolated local Ollama /api/chat stand-in (PASS verdicts only); the
    only reviewer the wrapper can reach — proves the review path does NOT
    launch a second Pi process."""

    def __init__(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(n)
                response = {
                    "model": "fake",
                    "message": {"role": "assistant", "content": REVIEW_PASS},
                    "done": True,
                }
                data = json.dumps(response).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# ---------------------------------------------------------------------------
# Default behavior unchanged (no --require-changes)
# ---------------------------------------------------------------------------


def test_default_read_only_zero_diff_run_unchanged(repo: Path) -> None:
    """A legitimate read-only/smoke zero-diff run keeps its established
    behavior: no gate line, NOT_REQUIRED state, READY_FOR_REVIEW
    (no-review run), and the pre-existing readiness reason unchanged."""
    proc = run(
        repo,
        {},  # fake pi is a no-op agent, zero new files
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review", "--", "inspect the repository",
    )
    assert proc.returncode == 0, proc.stdout

    assert "Require changes not required" in proc.stdout
    assert "REQUIRE-CHANGES GATE:" not in proc.stdout
    assert "REQUIRE-CHANGES UNSATISFIED" not in proc.stdout

    assert line(proc, "CHANGED FILES") == "0"
    assert line(proc, "REPOSITORY STATE") == "READY_FOR_REVIEW"
    assert line(proc, "DECISION REQUIRED") == "HUMAN REVIEW REQUIRED"
    assert (
        line(proc, "READINESS REASON")
        == "ALL DETERMINISTIC GATES PASS; NO INDEPENDENT REVIEW RUN"
    )

    assert meta_value(repo, proc, "require_changes_gate") == "0"
    assert meta_value(repo, proc, "require_changes_state") == "NOT_REQUIRED"
    assert (
        meta_value(repo, proc, "repository_readiness") == "READY_FOR_REVIEW"
    )


# ---------------------------------------------------------------------------
# Fail-closed: --require-changes with zero NEW changes
# ---------------------------------------------------------------------------


def test_require_changes_clean_baseline_zero_delta_fails_closed(
    repo: Path,
) -> None:
    """Pi exit 0 + completion marker + empty patch must NOT reach
    READY_FOR_COMMIT / READY_FOR_REVIEW when the operator explicitly
    required changes: the gate fails closed with the deterministic
    reason and human review."""
    proc = run(
        repo,
        {},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review", "--require-changes", "--", "implement the feature",
    )
    assert proc.returncode == 0, proc.stdout
    assert "Require changes required" in proc.stdout
    assert line(proc, "CHANGED FILES") == "0"
    assert line(proc, "INDEPENDENT REVIEW") in ("NOT_RUN", "DISABLED")

    state = line(proc, "REPOSITORY STATE")
    assert state == "NEEDS_REVIEW"
    assert line(proc, "DECISION REQUIRED") == "HUMAN REVIEW REQUIRED"
    assert "GO COMMIT" not in line(proc, "DECISION REQUIRED")

    reason = line(proc, "READINESS REASON")
    assert reason == (
        "REQUIRE-CHANGES UNSATISFIED: operator contract "
        "--require-changes requires >=1 repository change NEW RELATIVE "
        "TO BASELINE; new-delta files=0 (pre-existing baseline changes "
        "never count). READY_FOR_COMMIT forbidden; HUMAN REVIEW REQUIRED."
    )

    assert "REQUIRE-CHANGES GATE: UNSATISFIED (new repository changes " \
        "relative to baseline: 0)" in proc.stdout
    assert meta_value(repo, proc, "require_changes_gate") == "1"
    assert meta_value(repo, proc, "require_changes_state") == "UNSATISFIED"
    assert (
        meta_value(repo, proc, "require_changes_delta_files") == "0"
    )
    assert (
        meta_value(repo, proc, "repository_readiness") == "NEEDS_REVIEW"
    )


def test_require_changes_zero_delta_does_not_mutate_live_index(
    repo: Path,
) -> None:
    # The new gate code path must not introduce any live-index mutation:
    # the real .git/index (bytes and mtime) is untouched across the run.
    before = index_identity(repo)

    proc = run(
        repo,
        {},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review", "--require-changes", "--", "implement the feature",
    )
    assert proc.returncode == 0, proc.stdout

    after = index_identity(repo)
    assert before == after, "real git index must never be mutated"
    assert (
        subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=repo, check=False
        ).returncode
        == 0
    ), "real index must contain no staged entries"


# ---------------------------------------------------------------------------
# Satisfied gate: --require-changes with a genuine new change
# ---------------------------------------------------------------------------


def test_require_changes_clean_baseline_new_file_passes_gates(
    repo: Path,
) -> None:
    """A real new file (new relative to a CLEAN baseline) satisfies the
    gate and the run reaches the SAME readiness it would without the
    contract: READY_FOR_COMMIT with review PASS (normal gates intact)."""
    server = FakeOllama()
    try:
        proc = run(
            repo,
            {"FAKE_PI_CREATE_FILE": "src/implemented.py",
             "TP_REVIEWER_URL": server.url},
            "--class", "feature", "--interval", "1", "--no-notify",
            "--review", "--require-changes", "--", "implement the feature",
        )
    finally:
        server.close()

    assert proc.returncode == 0, proc.stdout

    assert "Require changes required" in proc.stdout
    assert line(proc, "UNTRACKED") == "1"
    assert line(proc, "INDEPENDENT REVIEW") == "PASS"
    assert line(proc, "REPOSITORY STATE") == "READY_FOR_COMMIT"
    assert line(proc, "DECISION REQUIRED") == "GO COMMIT"
    assert "REQUIRE-CHANGES GATE: SATISFIED (new repository changes " \
        "relative to baseline: 1)" in proc.stdout
    assert "REQUIRE-CHANGES UNSATISFIED" not in proc.stdout

    assert meta_value(repo, proc, "require_changes_gate") == "1"
    assert meta_value(repo, proc, "require_changes_state") == "SATISFIED"
    assert (
        meta_value(repo, proc, "require_changes_delta_files") == "1"
    )
    assert (
        meta_value(repo, proc, "repository_readiness") == "READY_FOR_COMMIT"
    )


# ---------------------------------------------------------------------------
# Dirty baseline: pre-existing changes never satisfy the gate
# ---------------------------------------------------------------------------


def test_require_changes_dirty_baseline_does_not_count(repo: Path) -> None:
    """Pre-existing dirty-baseline files (permitted via --dirty-ok) must
    NOT satisfy --require-changes: the agent still produced zero NEW
    changes relative to the baseline, so the run fails closed."""
    (repo / "tracked.py").write_text("v = 1\npre-existing work\n")

    proc = run(
        repo,
        {},
        "--class", "repair", "--interval", "1", "--no-notify",
        "--no-review", "--dirty-ok", "--require-changes",
        "--", "implement the remaining feature",
    )
    assert proc.returncode == 0, proc.stdout

    assert "Baseline files 1" in proc.stdout
    # the pre-existing dirty file is in the snapshot...
    assert line(proc, "CHANGED FILES") == "1"
    # ...but it is BASELINE, not a new delta
    assert "REQUIRE-CHANGES GATE: UNSATISFIED (new repository changes " \
        "relative to baseline: 0)" in proc.stdout
    assert line(proc, "REPOSITORY STATE") == "NEEDS_REVIEW"
    assert line(proc, "DECISION REQUIRED") == "HUMAN REVIEW REQUIRED"

    assert meta_value(repo, proc, "require_changes_state") == "UNSATISFIED"
    assert (
        meta_value(repo, proc, "require_changes_delta_files") == "0"
    )
    # pre-existing work preserved as-is
    assert "pre-existing work" in (repo / "tracked.py").read_text()


def test_require_changes_dirty_baseline_plus_new_change_satisfies(
    repo: Path,
) -> None:
    """With an intentional dirty baseline, an ADDITIONAL new change by the
    agent satisfies the gate (delta = 1 new file; baseline change
    excluded), and the pre-existing baseline change is preserved."""
    (repo / "tracked.py").write_text("v = 1\npre-existing work\n")

    proc = run(
        repo,
        {"FAKE_PI_CREATE_FILE": "src/more.py"},
        "--class", "repair", "--interval", "1", "--no-notify",
        "--no-review", "--dirty-ok", "--require-changes",
        "--", "implement the remaining feature",
    )
    assert proc.returncode == 0, proc.stdout

    assert "Baseline files 1" in proc.stdout
    assert line(proc, "CHANGED FILES") == "2"
    assert (
        line(proc, "REPOSITORY STATE") == "READY_FOR_REVIEW"
    )  # no-review run: the contract adds nothing extra when satisfied
    assert "REQUIRE-CHANGES GATE: SATISFIED (new repository changes " \
        "relative to baseline: 1)" in proc.stdout
    assert "REQUIRE-CHANGES UNSATISFIED" not in proc.stdout

    assert meta_value(repo, proc, "require_changes_state") == "SATISFIED"
    assert (
        meta_value(repo, proc, "require_changes_delta_files") == "1"
    )
    assert "pre-existing work" in (repo / "tracked.py").read_text()
    assert (repo / "src" / "more.py").exists()


# ---------------------------------------------------------------------------
# Repair and review can neither fake nor block the gate
# ---------------------------------------------------------------------------


def test_repair_and_review_pass_cannot_override_unsatisfied_gate(
    repo: Path,
) -> None:
    """The full adversarial combination from the mission: Pi exit 0,
    completion marker present, empty new patch, review PASS, AND a
    repair pass — none of which may turn an empty mission delta into
    false success."""
    server = FakeOllama()
    invocation_log = repo.parent / "pi_invocations.log"
    invocation_log.unlink(missing_ok=True)
    try:
        proc = run(
            repo,
            {
                "FAKE_PI_INVOCATION_LOG": str(invocation_log),
                "TP_REVIEWER_URL": server.url,
            },
            "--class", "feature", "--interval", "1", "--no-notify",
            "--review", "--require-changes", "--repair-attempts", "1",
            "--", "implement the feature",
        )
    finally:
        server.close()

    assert proc.returncode == 0, proc.stdout

    # the repair pass really ran (two agent invocations: first pass +
    # one repair) — and it landed nothing new
    invocations = [
        ln for ln in invocation_log.read_text().splitlines() if ln.strip()
    ]
    assert len(invocations) == 2, invocations
    assert line(proc, "CHANGED FILES") == "0"

    # review + repair were all "green" but the gate still fails closed
    assert line(proc, "INDEPENDENT REVIEW") == "PASS"
    assert line(proc, "REPAIR ATTEMPTS") == "1 used / 1 requested"
    assert "EXHAUSTED" in line(proc, "CONVERGENCE")

    state = line(proc, "REPOSITORY STATE")
    assert state == "NEEDS_REVIEW"
    assert "READY_FOR_COMMIT" not in state
    assert line(proc, "DECISION REQUIRED") == "HUMAN REVIEW REQUIRED"
    assert line(proc, "READINESS REASON").startswith(
        "REQUIRE-CHANGES UNSATISFIED"
    )
    assert meta_value(repo, proc, "require_changes_state") == "UNSATISFIED"
    assert (
        meta_value(repo, proc, "repository_readiness") == "NEEDS_REVIEW"
    )


def test_repair_landing_a_new_change_legitimately_satisfies_gate(
    repo: Path,
) -> None:
    """A repair pass only satisfies --require-changes when it actually
    lands a change NEW RELATIVE TO THE BASELINE: first pass zero-diff,
    repair pass creates a real file -> SATISFIED on the final pass."""
    invocation_log = repo.parent / "pi_invocations.log"
    invocation_log.unlink(missing_ok=True)

    proc = run(
        repo,
        {
            "FAKE_PI_INVOCATION_LOG": str(invocation_log),
            "FAKE_PI_CREATE_ON_REPAIR": "src/late.py",
        },
        "--class", "feature", "--interval", "1", "--no-notify",
        "--no-review", "--require-changes", "--repair-attempts", "1",
        "--", "implement the feature",
    )
    assert proc.returncode == 0, proc.stdout

    invocations = [
        ln for ln in invocation_log.read_text().splitlines() if ln.strip()
    ]
    assert len(invocations) == 2, invocations

    assert (repo / "src" / "late.py").exists()
    assert line(proc, "CHANGED FILES") == "1"
    assert line(proc, "INDEPENDENT REVIEW") in ("NOT_RUN", "DISABLED")
    assert "REQUIRE-CHANGES GATE: SATISFIED (new repository changes " \
        "relative to baseline: 1)" in proc.stdout
    assert line(proc, "REPOSITORY STATE") == "READY_FOR_REVIEW"

    meta = (run_dir(repo, proc) / "meta.txt").read_text()
    # the final (repair) pass satisfied the gate on the canonical meta
    assert "require_changes_state=SATISFIED" in meta
    assert line(proc, "CONVERGENCE").startswith("CONVERGED")


# ---------------------------------------------------------------------------
# Deterministic metadata / readiness reason
# ---------------------------------------------------------------------------


def test_require_changes_reason_is_deterministic_across_runs(
    repo: Path,
) -> None:
    def one(r: Path) -> tuple[str, str, str]:
        p = run(
            r,
            {},
            "--class", "smoke", "--interval", "1", "--no-notify",
            "--no-review", "--require-changes", "--", "implement it",
        )
        assert p.returncode == 0, p.stdout
        rd = run_dir(r, p)
        meta = (rd / "meta.txt").read_text()
        reason = re.search(r"^readiness_reason=(.*)$", meta, re.M)
        assert reason, meta
        return (
            line(p, "REPOSITORY STATE"),
            reason.group(1),
            meta_value(r, p, "require_changes_state"),
        )

    first = one(repo)
    # same scenario in a fresh clean repository: identical deterministic
    # classification / reason / gate state
    second = one(repo)
    assert first == second, (first, second)
    assert first == (
        "NEEDS_REVIEW",
        "REQUIRE-CHANGES UNSATISFIED: operator contract "
        "--require-changes requires >=1 repository change NEW RELATIVE "
        "TO BASELINE; new-delta files=0 (pre-existing baseline changes "
        "never count). READY_FOR_COMMIT forbidden; HUMAN REVIEW REQUIRED.",
        "UNSATISFIED",
    )


# ---------------------------------------------------------------------------
# Mission 010 — semantic result emission (status + require_changes)
# ---------------------------------------------------------------------------


def _run_with_semantic(repo: Path,
                       env: dict[str, str] | None = None,
                       *args: str) -> tuple[
        subprocess.CompletedProcess, dict[str, object]]:
    result_file = repo.parent / "m010.semantic.json"
    result_file.unlink(missing_ok=True)
    full_env = {
        "TRAJECTORY_SUBRUN_ID": "implement-a1",
        "TRAJECTORY_SUBRUN_RESULT_FILE": str(result_file),
    }
    if env:
        full_env.update(env)
    proc = run(repo, full_env, *args)
    assert proc.returncode == 0, proc.stdout
    return proc, json.loads(result_file.read_text(encoding="utf-8"))


def test_semantic_emission_require_changes_unsatisfied_fails_closed(
    repo: Path,
) -> None:
    """--require-changes with zero new delta emits a non-SUCCESS status and
    UNSATISFIED provenance (writable promotion contract)."""
    _, doc = _run_with_semantic(
        repo, None,
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review", "--require-changes", "--", "implement the feature",
    )
    assert doc["status"] == "FAILED"
    assert doc["status"] != "SUCCESS"
    assert doc["require_changes"] == "UNSATISFIED"
    assert doc["readiness"] == "NEEDS_REVIEW"


def test_semantic_emission_require_changes_satisfied_promotes(
    repo: Path,
) -> None:
    """--require-changes satisfied by a genuine new file emits SUCCESS and
    SATISFIED provenance."""
    _, doc = _run_with_semantic(
        repo,
        {"FAKE_PI_CREATE_FILE": "src/m010.py"},
        "--class", "feature", "--interval", "1", "--no-notify",
        "--no-review", "--require-changes", "--", "implement the feature",
    )
    assert doc["status"] == "SUCCESS"
    assert doc["require_changes"] == "SATISFIED"
    assert doc["readiness"] == "READY_FOR_REVIEW"


def test_semantic_emission_without_flag_is_not_required(repo: Path) -> None:
    """A default zero-diff run emits SUCCESS and NOT_REQUIRED provenance."""
    _, doc = _run_with_semantic(
        repo, None,
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review", "--", "inspect the repository",
    )
    assert doc["status"] == "SUCCESS"
    assert doc["require_changes"] == "NOT_REQUIRED"
    assert doc["readiness"] == "READY_FOR_REVIEW"


# ---------------------------------------------------------------------------
# Wrapper source invariants
# ---------------------------------------------------------------------------


def test_source_contract_invariants() -> None:
    text = WRAPPER.read_text(encoding="utf-8")
    assert "--require-changes)" in text
    assert "REQUIRE_CHANGES=0" in text
    assert "REQUIRE_CHANGES_STATE=" in text
    assert "REQUIRE-CHANGES UNSATISFIED" in text
    # the gate must be a plain deterministic flag, never prompt inference
    assert "REQUIRE_CHANGES=1" in text
    assert "require_changes_state=" in text
    assert "require_changes_delta_files=" in text
    # Mission 010: the semantic result carries bounded require-changes
    # provenance and applies the writable promotion contract.
    assert '"require_changes":"%s"' in text
    assert "IMPLEMENT|REPAIR|RECOVERY|SMOKE)" in text
