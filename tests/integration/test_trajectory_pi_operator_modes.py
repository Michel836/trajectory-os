"""Focused integration tests for Mission 002 — explicit operator modes.

Proves, against the REAL ``scripts/trajectory-pi`` wrapper inside an
isolated temporary Git repository with a deterministic fake ``pi`` on
PATH, that:

  * an explicit ``--mode`` is respected and recorded (canonical mode),
  * existing ``--class``-only invocations stay backward compatible
    (mode derived from class; writable modes still invoke the agent),
  * read-only VERIFY / REVIEW modes NEVER invoke the Pi implementation
    agent and succeed (COMPLETE) without any implementation completion
    marker — they are never misclassified ``INCOMPLETE_AGENT_RUN``,
  * read-only verification is permitted on a protected branch and on a
    DIRTY worktree (it only inspects, never mutates),
  * unsafe mode/flag combinations fail closed (exit 2) at parse time,
  * deterministic lifecycle evidence (``lifecycle.jsonl`` +
    ``lifecycle.json``) is persisted and is mode/final-state aware,
  * the adaptive startup display exposes the mode, mutation policy,
    changes policy, validation, review and repair-attempt state,
  * the real Git index is never mutated by a read-only run.

These tests never invoke the real ``pi`` binary (a deterministic stub is
substituted on PATH) nor a live model reviewer (a local fake Ollama
endpoint is used).
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

# Deterministic fake agent. It records EVERY invocation and then exits 0
# with a completion marker. That is deliberately the OPPOSITE of what a
# read-only mode should do: if a VERIFY/REVIEW run ever (wrongly) invokes
# the agent, the invocation log will be non-empty and the test fails.
FAKE_PI = r"""#!/usr/bin/env bash
if [[ -n "${FAKE_PI_INVOCATION_LOG:-}" ]]; then
  printf 'AGENT\n' >> "$FAKE_PI_INVOCATION_LOG"
fi
printf 'HANDOFF\n'
printf 'RESULT: fake agent complete\n'
printf '%s\n' "${FAKE_PI_MARKER-FEATURE_IMPLEMENTED_COMPLETE}"
exit 0
"""


# Deterministic fake DeepSeek Harness headless backend. It records the
# complete argv and the permission mode selected by the wrapper, then emits
# an exact terminal completion marker. No real DSH/model process is started.
FAKE_DSH = r"""#!/usr/bin/env bash
if [[ -n "${FAKE_DSH_ARGV_LOG:-}" ]]; then
  printf '%s\n' "$@" > "$FAKE_DSH_ARGV_LOG"
fi
if [[ -n "${FAKE_DSH_PERMISSION_LOG:-}" ]]; then
  printf '%s\n' "${DSH_PERMISSION_MODE-}" > "$FAKE_DSH_PERMISSION_LOG"
fi
printf 'HANDOFF\n'
printf 'RESULT: fake DSH agent complete\n'
printf '%s\n' "${FAKE_DSH_MARKER-DSH_TEST_COMPLETE}"
exit 0
"""


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
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    fake_pi = fake_bin / "pi"
    fake_pi.write_text(FAKE_PI)
    fake_pi.chmod(0o755)

    fake_dsh = fake_bin / "dsh"
    fake_dsh.write_text(FAKE_DSH)
    fake_dsh.chmod(0o755)

    fake_dsh_patch = tmp_path / "fake-dsh-patch.yml"
    fake_dsh_patch.write_text("# deterministic fake DSH patch\n")

    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-b", "main")
    git(r, "config", "user.email", "t@example.invalid")
    git(r, "config", "user.name", "TrajectoryOS Test")
    (r / "tracked.py").write_text("v = 1\n")
    (r / ".gitignore").write_text(".trajectory-pi/\n")
    git(r, "add", "-A")
    git(r, "commit", "-m", "init", "--quiet")
    # NOTE: the repo is left on `main`. Read-only (VERIFY/REVIEW) modes
    # must be permitted even on a protected branch — that is the whole
    # point of first-class read-only verification.

    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    monkeypatch.setenv("TP_DSH_BIN", "dsh")
    monkeypatch.setenv("TP_DSH_PATCH", str(fake_dsh_patch))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    return r


def wrapper_env(**extra: str) -> dict[str, str]:
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
    meta = (run_dir(repo, proc) / "meta.txt").read_text()
    matches = [
        m.group(1)
        for m in re.finditer(r"^" + re.escape(key) + r"=(.*)$", meta, re.M)
    ]
    assert matches, f"missing '{key}=...' in meta.txt:\n{meta}"
    return matches[-1].strip()


def invocations(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [ln for ln in path.read_text().splitlines() if ln.strip()]


def index_identity(repo: Path) -> tuple[bytes, int]:
    p = repo / ".git" / "index"
    st = p.stat()
    return p.read_bytes(), st.st_mtime_ns


class FakeOllama:
    """Isolated local Ollama /api/chat stand-in returning PASS verdicts."""

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
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def dirty(repo: Path) -> None:
    """Create a dirty worktree (modified tracked file + new untracked)."""
    (repo / "tracked.py").write_text("v = 2\nchanged\n")
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "existing.py").write_text("x = 1\n")


# ---------------------------------------------------------------------------
# 1. Read-only VERIFY: no agent, no completion marker, COMPLETE on main
# ---------------------------------------------------------------------------


def test_verify_read_only_never_invokes_agent_on_dirty_main(repo: Path) -> None:
    inv_log = repo.parent / "pi_invocations.log"
    inv_log.unlink(missing_ok=True)
    dirty(repo)
    before = index_identity(repo)

    proc = run(
        repo,
        {"FAKE_PI_INVOCATION_LOG": str(inv_log)},
        "--mode", "VERIFY", "--no-review",
        "--interval", "1", "--no-notify", "--", "verify current work",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    # the Pi implementation agent was NEVER invoked
    assert invocations(Path(str(inv_log))) == [], (
        "read-only VERIFY must never invoke the agent:\n" + proc.stdout
    )
    # never classified as an incomplete agent run
    assert "INCOMPLETE_AGENT_RUN" not in proc.stdout
    # the worktree must be preserved untouched (no mutation policy)
    assert "changed" in (repo / "tracked.py").read_text()
    assert (repo / "src" / "existing.py").exists()
    # canonical mode recorded as explicit read-only
    assert meta_value(repo, proc, "run_mode") == "VERIFY"
    assert meta_value(repo, proc, "mutation_policy") == "read-only"
    assert meta_value(repo, proc, "changes_policy") == "forbidden"
    assert meta_value(repo, proc, "agent_classification") == "READ_ONLY_VERIFIED"
    # the real git index is never mutated
    assert before == index_identity(repo), "real git index must never be mutated"


def test_verify_read_only_reaches_complete_without_marker(repo: Path) -> None:
    dirty(repo)
    proc = run(
        repo,
        {},
        "--mode", "VERIFY", "--no-review",
        "--interval", "1", "--no-notify", "--", "verify current work",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # no implementation completion marker was required or present
    marker_present = "FEATURE_IMPLEMENTED_COMPLETE" in proc.stdout
    assert not marker_present
    # a clean verification is a green, non-failing terminal state
    state = line(proc, "Repository state")
    assert state in ("READY_FOR_COMMIT", "READY_FOR_REVIEW"), state
    assert "NEEDS_REVIEW" not in (line(proc, "Readiness reason"))
    assert (
        line(proc, "Readiness reason")
        == "ALL DETERMINISTIC GATES PASS; NO INDEPENDENT REVIEW RUN"
    )


def test_review_mode_reaches_ready_for_commit(repo: Path) -> None:
    git(repo, "checkout", "-b", "feature/verify")
    inv_log = repo.parent / "pi_invocations.log"
    inv_log.unlink(missing_ok=True)
    dirty(repo)
    server = FakeOllama()
    try:
        proc = run(
            repo,
            {"FAKE_PI_INVOCATION_LOG": str(inv_log),
             "TP_REVIEWER_URL": server.url},
            "--mode", "REVIEW",
            "--interval", "1", "--no-notify", "--", "review current work",
        )
    finally:
        server.close()

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert invocations(Path(str(inv_log))) == [], "REVIEW must not invoke agent"
    assert line(proc, "INDEPENDENT REVIEW") == "PASS"
    assert line(proc, "Repository state") == "READY_FOR_COMMIT"
    assert line(proc, "DECISION REQUIRED") == "GO COMMIT"
    assert meta_value(repo, proc, "run_mode") == "REVIEW"
    assert meta_value(repo, proc, "agent_classification") == "READ_ONLY_VERIFIED"


# ---------------------------------------------------------------------------
# 1b. PLAN: accepted as a first-class mode (read-only, model-heavy)
# ---------------------------------------------------------------------------


def test_plan_mode_is_accepted_and_recorded(repo: Path) -> None:
    """``--mode PLAN`` is accepted, recorded as the canonical mode, and
    carries the read-only mutation / changes contract."""
    git(repo, "checkout", "-b", "feature/plan")
    proc = run(
        repo,
        {},
        "--mode", "PLAN", "--no-review",
        "--interval", "1", "--no-notify", "--", "plan the work",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert meta_value(repo, proc, "run_mode") == "PLAN"
    assert meta_value(repo, proc, "run_mode_source").startswith("explicit")
    assert meta_value(repo, proc, "mutation_policy") == "read-only"
    assert meta_value(repo, proc, "changes_policy") == "forbidden"


# ---------------------------------------------------------------------------
# 2. Backward compatibility: --class derives mode and still invokes agent
# ---------------------------------------------------------------------------


def test_class_feature_back_compat_derives_implement_and_invokes_agent(
    repo: Path,
) -> None:
    git(repo, "checkout", "-b", "feature/compat")
    inv_log = repo.parent / "pi_invocations.log"
    inv_log.unlink(missing_ok=True)

    proc = run(
        repo,
        {"FAKE_PI_INVOCATION_LOG": str(inv_log)},
        "--class", "feature", "--no-review",
        "--interval", "1", "--no-notify", "--", "implement the feature",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    # writable mode DID invoke the agent exactly once
    assert len(invocations(Path(str(inv_log)))) == 1
    assert meta_value(repo, proc, "run_mode") == "IMPLEMENT"
    assert meta_value(repo, proc, "run_mode_source", ) .startswith("derived")
    assert meta_value(repo, proc, "mutation_policy") == "writable"
    assert meta_value(repo, proc, "changes_policy") == "permitted"


def test_class_smoke_back_compat_derives_smoke(repo: Path) -> None:
    git(repo, "checkout", "-b", "feature/smoke")
    proc = run(
        repo,
        {},
        "--class", "smoke", "--no-review",
        "--interval", "1", "--no-notify", "--", "smoke test",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert meta_value(repo, proc, "run_mode") == "SMOKE"
    assert meta_value(repo, proc, "mutation_policy") == "writable"
    # legacy smoke behavior unchanged: no-review zero-diff reach state
    assert line(proc, "Repository state") == "READY_FOR_REVIEW"


# ---------------------------------------------------------------------------
# 3. Unsafe combinations fail closed at parse time (exit 2)
# ---------------------------------------------------------------------------


def test_verify_with_require_changes_fails_closed(repo: Path) -> None:
    proc = run(
        repo,
        {},
        "--mode", "VERIFY", "--require-changes",
        "--", "verify",
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "unsafe combination" in (proc.stderr + proc.stdout).lower()
    assert "require-changes" in (proc.stderr + proc.stdout)


def test_review_with_repair_attempts_fails_closed(repo: Path) -> None:
    proc = run(
        repo,
        {},
        "--mode", "REVIEW", "--repair-attempts", "1",
        "--", "review",
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "unsafe combination" in (proc.stderr + proc.stdout).lower()
    assert "repair-attempts" in (proc.stderr + proc.stdout)


def test_invalid_mode_fails_closed(repo: Path) -> None:
    proc = run(repo, {}, "--mode", "BANANA", "--", "x")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "invalid mode" in (proc.stderr + proc.stdout).lower()


def test_plan_with_require_changes_fails_closed(repo: Path) -> None:
    proc = run(
        repo,
        {},
        "--mode", "PLAN", "--require-changes",
        "--", "plan",
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "unsafe combination" in (proc.stderr + proc.stdout).lower()
    assert "require-changes" in (proc.stderr + proc.stdout)


def test_plan_with_repair_attempts_fails_closed(repo: Path) -> None:
    proc = run(
        repo,
        {},
        "--mode", "PLAN", "--repair-attempts", "1",
        "--", "plan",
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "unsafe combination" in (proc.stderr + proc.stdout).lower()
    assert "repair-attempts" in (proc.stderr + proc.stdout)


# ---------------------------------------------------------------------------
# 4. Lifecycle evidence
# ---------------------------------------------------------------------------


def test_lifecycle_evidence_is_persisted_and_mode_aware(repo: Path) -> None:
    dirty(repo)
    proc = run(
        repo,
        {},
        "--mode", "VERIFY", "--no-review",
        "--interval", "1", "--no-notify", "--", "verify",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    rd = run_dir(repo, proc)

    jsonl = rd / "lifecycle.jsonl"
    assert jsonl.exists(), "lifecycle.jsonl must be persisted"
    states = [
        json.loads(ln)["state"]
        for ln in jsonl.read_text().splitlines()
        if ln.strip()
    ]
    assert states[0] == "RUNNING"
    assert "VALIDATING" in states
    assert "REVIEWING" in states
    assert states[-1] == "COMPLETE"
    # every line is valid JSON with the mode/class recorded
    first = json.loads(jsonl.read_text().splitlines()[0])
    assert first["mode"] == "VERIFY"
    assert first["class"] == "smoke" or "class" in first

    lj = rd / "lifecycle.json"
    assert lj.exists(), "lifecycle.json summary must be persisted"
    summary = json.loads(lj.read_text())
    assert summary["mode"] == "VERIFY"
    assert summary["mutation_policy"] == "read-only"
    assert summary["final_state"] == "COMPLETE"
    assert summary["exit_code"] == 0
    assert summary["readiness"] in ("READY_FOR_COMMIT", "READY_FOR_REVIEW")


# ---------------------------------------------------------------------------
# 5. Adaptive startup display
# ---------------------------------------------------------------------------


def test_startup_display_exposes_mode_and_policies(repo: Path) -> None:
    dirty(repo)
    proc = run(
        repo,
        {},
        "--mode", "VERIFY", "--no-review",
        "--interval", "1", "--no-notify", "--", "verify",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Canonical mode" in proc.stdout
    assert "VERIFY" in proc.stdout
    assert "Mutation policy read-only" in proc.stdout
    assert "Changes        forbidden" in proc.stdout
    assert "Validation     " in proc.stdout
    assert "Review         disabled" in proc.stdout
    assert "Repair attempts disabled" in proc.stdout


# ---------------------------------------------------------------------------
# 6. Wrapper source invariants
# ---------------------------------------------------------------------------


def test_source_contract_invariants() -> None:
    text = WRAPPER.read_text(encoding="utf-8")
    assert "--mode)" in text
    assert "READ_ONLY_MODE=0" in text
    # read-only verification must guard the agent invocation
    assert 'if (( READ_ONLY_MODE == 1 )); then' in text
    # read-only classification must never be an incomplete agent run
    assert "READ_ONLY_VERIFIED" in text
    assert 'CLASSIFICATION="READ_ONLY_VERIFIED"' in text
    # unsafe combinations fail closed
    assert "unsafe combination: read-only mode" in text
    # deterministic lifecycle evidence
    assert "lifecycle.jsonl" in text
    assert "emit_lifecycle" in text
    # mode-aware readiness gate
    assert '"$CLASSIFICATION" != "READ_ONLY_VERIFIED"' in text


def test_verify_on_protected_branch_is_allowed_but_writable_is_blocked(
    repo: Path,
) -> None:
    """On `main`, a read-only VERIFY is permitted but a writable
    implementation is refused (SAFETY STOP, exit 3)."""
    # read-only is allowed on the protected branch
    proc_ro = run(
        repo,
        {},
        "--mode", "VERIFY", "--no-review",
        "--interval", "1", "--no-notify", "--", "verify",
    )
    assert proc_ro.returncode == 0, proc_ro.stdout + proc_ro.stderr
    assert "SAFETY STOP" not in proc_ro.stdout

    # writable derivation (smoke) is refused on the protected branch
    proc_wr = run(
        repo,
        {},
        "--class", "smoke", "--no-review",
        "--interval", "1", "--no-notify", "--", "implement",
    )
    assert proc_wr.returncode == 3, proc_wr.stdout + proc_wr.stderr
    assert "SAFETY STOP" in proc_wr.stdout


# ---------------------------------------------------------------------------
# Agent-backend abstraction
# ---------------------------------------------------------------------------


def test_invalid_agent_backend_fails_closed(repo: Path) -> None:
    proc = run(
        repo,
        None,
        "--agent-backend",
        "not-a-backend",
        "--",
        "This must never invoke an agent.",
    )

    assert proc.returncode == 2
    assert "--agent-backend must be pi or dsh" in proc.stderr


def test_pi_remains_default_agent_backend(repo: Path, tmp_path: Path) -> None:
    invocation_log = tmp_path / "pi-invocations.log"

    proc = run(
        repo,
        {
            "FAKE_PI_INVOCATION_LOG": str(invocation_log),
            "FAKE_PI_MARKER": "PLAN_COMPLETE",
        },
        "--class",
        "smoke",
        "--mode",
        "PLAN",
        "--no-review",
        "--",
        "Inspect only. Do not modify files. Finish with PLAN_COMPLETE.",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert invocations(invocation_log) == ["AGENT"]
    assert meta_value(repo, proc, "agent_backend") == "pi"
    assert meta_value(repo, proc, "agent_classification") == "AGENT_COMPLETED"
    assert meta_value(repo, proc, "plan_worktree_state") == "UNCHANGED"


def test_dsh_plan_uses_headless_read_only_backend(
    repo: Path, tmp_path: Path
) -> None:
    argv_log = tmp_path / "dsh-argv.log"
    permission_log = tmp_path / "dsh-permission.log"
    query = (
        "Inspect only. Do not modify files. "
        "Finish with DSH_PLAN_COMPLETE."
    )

    proc = run(
        repo,
        {
            "FAKE_DSH_ARGV_LOG": str(argv_log),
            "FAKE_DSH_PERMISSION_LOG": str(permission_log),
            "FAKE_DSH_MARKER": "DSH_PLAN_COMPLETE",
        },
        "--class",
        "smoke",
        "--mode",
        "PLAN",
        "--agent-backend",
        "dsh",
        "--no-review",
        "--",
        query,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert meta_value(repo, proc, "agent_backend") == "dsh"
    assert meta_value(repo, proc, "agent_classification") == "AGENT_COMPLETED"
    assert meta_value(repo, proc, "plan_worktree_state") == "UNCHANGED"

    assert permission_log.read_text().strip() == "read-only"

    argv = argv_log.read_text().splitlines()
    assert argv == [
        "--profile",
        "headless",
        "--patch",
        os.environ["TP_DSH_PATCH"],
        query,
    ]


def test_dsh_implement_uses_workspace_write_backend(
    repo: Path, tmp_path: Path
) -> None:
    # Writable modes are intentionally not run on protected main.
    git(repo, "switch", "-c", "feature/test-dsh-backend")

    argv_log = tmp_path / "dsh-implement-argv.log"
    permission_log = tmp_path / "dsh-implement-permission.log"
    query = "Perform the bounded task and finish with DSH_IMPLEMENT_COMPLETE."

    proc = run(
        repo,
        {
            "FAKE_DSH_ARGV_LOG": str(argv_log),
            "FAKE_DSH_PERMISSION_LOG": str(permission_log),
            "FAKE_DSH_MARKER": "DSH_IMPLEMENT_COMPLETE",
        },
        "--class",
        "feature",
        "--mode",
        "IMPLEMENT",
        "--agent-backend",
        "dsh",
        "--no-review",
        "--",
        query,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert meta_value(repo, proc, "agent_backend") == "dsh"
    assert meta_value(repo, proc, "agent_classification") == "AGENT_COMPLETED"
    assert permission_log.read_text().strip() == "workspace-write"

    argv = argv_log.read_text().splitlines()
    assert argv == [
        "--profile",
        "headless",
        "--patch",
        os.environ["TP_DSH_PATCH"],
        query,
    ]
