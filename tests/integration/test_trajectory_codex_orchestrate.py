"""Integration tests for the human-gated Codex orchestration CLI (issue #170).

Every test drives the REAL ``scripts/trajectory-codex-orchestrate`` inside an
isolated temporary Git repository with deterministic mocks standing in for the
two delegates:

* ``scripts/trajectory-codex-pi`` — records its exact argv, optionally
  writes "latest Pi run artifacts" (``.trajectory-pi/runs/<stamp>/meta.txt``)
  with a configurable readiness outcome, and exits with a configurable status;
* ``scripts/trajectory_gate.py`` — records its exact argv and exits with a
  configurable status.

The remote for ``push`` is a local bare repository (no network). No real
Pi, no real Ollama, no credentials, no real repository mutation outside the
temporary sandbox.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_CLI = REPO_ROOT / "scripts" / "trajectory-codex-orchestrate"

BRANCH = "feature/orch"
PROTECTED = "main"

# Exit-code contract of the CLI under test (its documented contract).
EXIT_USAGE = 2
EXIT_PRECONDITION = 3
EXIT_GATE_STOP = 5
EXIT_AUTH_REFUSED = 6
EXIT_BLOCKED = 7


# ---------------------------------------------------------------------------
# Deterministic mocks (record argv/env, controlled outcomes via env vars)
# ---------------------------------------------------------------------------

FAKE_PI_DELEGATE = r"""#!/usr/bin/env bash
set -u
repo="$(git rev-parse --show-toplevel 2>/dev/null)" || repo="$(pwd)"
if [[ -n "${DELEGATE_CAPTURE:-}" ]]; then
  {
    printf 'ARGC=%d\n' "$#"
    for a in "$@"; do printf 'ARG=%s\n' "$a"; done
  } > "${DELEGATE_CAPTURE}"
fi
mode="${DELEGATE_MODE:-ready}"
if [[ "$mode" != "no_artifacts" && -n "$repo" ]]; then
  stamp="20250101-${DELEGATE_STAMP:-000000}"
  dir="$repo/.trajectory-pi/runs/$stamp"
  mkdir -p "$dir"
  if [[ "$mode" != "no_meta" ]]; then
    b="$(git -C "$repo" branch --show-current)"
    h="$(git -C "$repo" rev-parse HEAD)"
    if [[ "${DELEGATE_MODE:-ready}" == "needs_review" ]]; then
      cat > "$dir/meta.txt" <<EOF
run_class=smoke
branch=$b
head_before=$h
agent_classification=INCOMPLETE_AGENT_RUN
repository_readiness=NEEDS_REVIEW
readiness_reason=CONFIGURED VALIDATION FAILED
validation=FAIL
snapshot_status=COMPLETE
diff_check=PASS
decision_required=HUMAN REVIEW REQUIRED
EOF
    else
      cat > "$dir/meta.txt" <<EOF
run_class=smoke
branch=$b
head_before=$h
agent_classification=AGENT_COMPLETED
repository_readiness=READY_FOR_COMMIT
readiness_reason=ALL DETERMINISTIC GATES PASS
validation=PASS
snapshot_status=COMPLETE
diff_check=PASS
decision_required=GO COMMIT
EOF
    fi
  fi
fi
exit "${DELEGATE_RC:-0}"
"""

FAKE_GATE = r'''#!/usr/bin/env python3
import os
import sys

argv = sys.argv[1:]
cap = os.environ.get("GATE_CAPTURE")
if cap:
    with open(cap, "a", encoding="utf-8") as handle:
        handle.write("ARGV=%r\n" % (argv,))
        handle.write("CWD=%s\n" % os.getcwd())
sys.exit(int(os.environ.get("GATE_RC", "0")))
'''

SECRET_AUTH_CONTENT = b'{"token": "SECRET-AUTH-DO-NOT-TOUCH"}\n'
SECRET_TRUST_CONTENT = b'{"trusted": ["SECRET-TRUST-DO-NOT-TOUCH"]}\n'
SECRET_SETTINGS_CONTENT = b'{"sandbox": "SECRET-SANDBOX-SETTING"}\n'

FAKE_STATUS_DELEGATE = r"""#!/usr/bin/env bash
set -u
cap="${STATUS_CAPTURE:-}"
if [[ -n "$cap" ]]; then
  {
    printf 'ARGC=%d\n' "$#"
    for a in "$@"; do printf 'ARG=%s\n' "$a"; done
  } > "$cap"
fi
# Also record cwd to confirm no cwd manipulation
printf '%s\n' "$(pwd)" > "${STATUS_CWD_CAPTURE:-/dev/null}"
exit "${STATUS_RC:-0}"
"""


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def git_out(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def snapshot_git_state(repo: Path) -> dict[str, str]:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    return {
        "head": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
            capture_output=True, text=True, env=env,
        ).stdout.strip(),
        "commits": subprocess.run(
            ["git", "rev-list", "--all", "--count"], cwd=repo, check=True,
            capture_output=True, text=True, env=env,
        ).stdout.strip(),
        "branch": subprocess.run(
            ["git", "branch", "--show-current"], cwd=repo, check=True,
            capture_output=True, text=True, env=env,
        ).stdout.strip(),
        "status": subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, check=True,
            capture_output=True, text=True, env=env,
        ).stdout,
        "index": subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=repo, check=True,
            capture_output=True, text=True, env=env,
        ).stdout.strip(),
    }


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    fake_home = tmp_path / "fakehome"
    seed = fake_home / ".pi" / "agent"
    seed.mkdir(parents=True)
    (seed / "models.json").write_text('{"providers": {}}\n')
    (seed / "auth.json").write_bytes(SECRET_AUTH_CONTENT)
    (seed / "trust.json").write_bytes(SECRET_TRUST_CONTENT)
    (seed / "settings.json").write_bytes(SECRET_SETTINGS_CONTENT)

    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "TrajectoryOS Test")
    (repo / ".gitignore").write_text(".trajectory-pi/\n.artifacts/\n")
    (repo / "app").mkdir()
    (repo / "app" / "calc.py").write_text("v = 1\n")
    (repo / "other").mkdir()
    (repo / "other" / "lib.py").write_text("u = 1\n")

    scripts = repo / "scripts"
    scripts.mkdir()
    shutil.copyfile(REAL_CLI, scripts / "trajectory-codex-orchestrate")
    (scripts / "trajectory-codex-orchestrate").chmod(0o755)
    (scripts / "trajectory-codex-pi").write_text(FAKE_PI_DELEGATE)
    (scripts / "trajectory-codex-pi").chmod(0o755)
    (scripts / "trajectory_gate.py").write_text(FAKE_GATE)
    (scripts / "trajectory_gate.py").chmod(0o755)
    (scripts / "trajectory-pi-status").write_text(FAKE_STATUS_DELEGATE)
    (scripts / "trajectory-pi-status").chmod(0o755)

    git(repo, "add", "-A")
    git(repo, "commit", "-m", "init", "--quiet")

    # local bare remote (no network involved anywhere)
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)],
        check=True, capture_output=True,
    )
    git(repo, "remote", "add", "origin", str(bare))
    (repo / "other" / "lib.py").write_text("u = 2\n")
    git(repo, "commit", "-am", "seed remote", "--quiet")
    git(repo, "push", "origin", "refs/heads/main")
    git(repo, "push", "origin", f"HEAD:refs/heads/{BRANCH}")
    git(repo, "checkout", "-b", BRANCH)

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    for k in (
        "DELEGATE_RC", "DELEGATE_MODE", "DELEGATE_STAMP", "GATE_RC",
        "STATUS_RC", "STATUS_CAPTURE", "STATUS_CWD_CAPTURE",
    ):
        monkeypatch.delenv(k, raising=False)
    return {
        "home": fake_home,
        "seed": seed,
        "repo": repo,
        "cli": scripts / "trajectory-codex-orchestrate",
        "bare": bare,
    }


def run_cli(
    sandbox: dict[str, Path],
    args: list[str],
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        [
            "bash",
            str(sandbox["cli"]),
            *args,
        ],
        cwd=sandbox["repo"],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def capture_path(sandbox: dict[str, Path], name: str) -> Path:
    return sandbox["repo"].parent / f"cap-{name}.log"


def gate_args(path: Path) -> list[str]:
    raw = path.read_text()
    m = re.search(r"^ARGV=(.*)\n", raw)
    assert m, f"gate was NOT invoked (no capture log: {path})"
    return eval(m.group(1))  # mock-produced literal list of strings


def head_of(sandbox: dict[str, Path], ref: str) -> str:
    return git_out(sandbox["bare"], "rev-parse", f"refs/heads/{ref}")


def seed_meta(
    sandbox: dict[str, Path],
    stamp: str,
    *,
    readiness: str = "READY_FOR_COMMIT",
    validation: str = "PASS",
    diff_check: str = "PASS",
    snapshot_status: str = "COMPLETE",
    classification: str = "AGENT_COMPLETED",
    decision: str = "GO COMMIT",
    branch: str | None = None,
    head_before: str | None = None,
) -> Path:
    """Seed a prior (older or otherwise) Pi run artifact directory."""
    repo: Path = sandbox["repo"]
    run_dir = repo / ".trajectory-pi" / "runs" / stamp
    run_dir.mkdir(parents=True)
    meta = run_dir / "meta.txt"
    meta.write_text(
        "\n".join(
            [
                "run_class=smoke",
                f"branch={branch or BRANCH}",
                f"head_before={head_before or git_out(repo, 'rev-parse', 'HEAD')}",
                f"agent_classification={classification}",
                f"repository_readiness={readiness}",
                "readiness_reason=seeded artifact",
                f"validation={validation}",
                f"snapshot_status={snapshot_status}",
                f"diff_check={diff_check}",
                f"decision_required={decision}",
            ]
        )
        + "\n"
    )
    return meta


def assert_git_unchanged(
    sandbox: dict[str, Path] | Path, before: dict[str, str]
) -> None:
    repo = sandbox["repo"] if isinstance(sandbox, dict) else sandbox
    after = snapshot_git_state(repo)
    for key in ("head", "commits", "branch", "status", "index"):
        assert after[key] == before[key], (
            f"git state must be unchanged (key={key!r}): {before[key]!r} -> {after[key]!r}"
        )


# ---------------------------------------------------------------------------
# usage / invocation fail-closed
# ---------------------------------------------------------------------------


def test_usage_without_subcommand(sandbox: dict[str, Path]) -> None:
    proc = run_cli(sandbox, [])
    assert proc.returncode == EXIT_USAGE, proc.stdout + proc.stderr
    assert "Usage" in proc.stdout


def test_unknown_subcommand_refused(sandbox: dict[str, Path]) -> None:
    before = snapshot_git_state(sandbox["repo"])
    proc = run_cli(sandbox, ["launch", "--branch", BRANCH])
    assert proc.returncode == EXIT_USAGE
    assert "unknown command" in proc.stderr
    assert_git_unchanged(sandbox, before)


def test_implement_requires_branch(sandbox: dict[str, Path]) -> None:
    before = snapshot_git_state(sandbox["repo"])
    proc = run_cli(sandbox, ["implement", "--", "build stuff"])
    assert proc.returncode == EXIT_USAGE
    assert "--branch" in proc.stderr
    assert not capture_path(sandbox, "nb").exists()
    assert_git_unchanged(sandbox, before)


# ---------------------------------------------------------------------------
# IMPLEMENT: delegation, exact forwarding, evidence, STOPS before GO COMMIT
# ---------------------------------------------------------------------------


def test_implement_forwards_exact_arguments(sandbox: dict[str, Path]) -> None:
    args = [
        "--class", "feature",
        "--model", "qwen-fake",
        "--no-notify",
        "--",
        "handle -n -- and 'quotes' $HOME `backticks`",
        "",  # empty-string argument must survive
        "-x",
    ]
    log = capture_path(sandbox, "impl-args")
    proc = run_cli(
        sandbox,
        ["implement", "--branch", BRANCH, *args],
        {"DELEGATE_CAPTURE": str(log)},
    )
    assert proc.returncode == EXIT_GATE_STOP, proc.stdout + proc.stderr
    delegate = log.read_text()
    assert f"ARGC={len(args)}\n" in delegate, delegate
    arg_lines = [ln[len("ARG="):] for ln in delegate.splitlines() if ln.startswith("ARG=")]
    assert arg_lines == args, f"argv altered: {arg_lines!r} != {args!r}"


def test_implement_ready_evidence_stops_before_go_commit(sandbox: dict[str, Path]) -> None:
    """Happy path: delegate succeeds, latest artifacts say READY_FOR_COMMIT,
    and the CLI validates the evidence but performs NO commit and stops at
    the GO COMMIT human gate (no authorize, no git mutation, no gate call)."""
    before = snapshot_git_state(sandbox["repo"])
    log = capture_path(sandbox, "impl-ready")
    glog = capture_path(sandbox, "impl-ready-gate")
    glog.unlink(missing_ok=True)

    proc = run_cli(
        sandbox,
        ["implement", "--branch", BRANCH, "--class", "smoke", "--", "build"],
        {"DELEGATE_CAPTURE": str(log)},
    )
    assert proc.returncode == EXIT_GATE_STOP, proc.stdout + proc.stderr

    out = proc.stdout
    assert "EVIDENCE VALIDATED" in out, out
    assert "READY_FOR_COMMIT" in out
    assert "GO COMMIT" in out
    assert "HUMAN" in out

    # delegate invoked exactly once with the forwarded args
    delegate = log.read_text()
    assert "ARG=-n" not in delegate  # sanity: real forwarding happened
    assert "ARG=smoke" in delegate
    assert "ARG=build" in delegate

    # gate NOT invoked (implement never runs the gate)
    assert not glog.exists(), "trajectory_gate must not run in the implement phase"

    # no commit of any kind, no index/HEAD/branch movement
    assert_git_unchanged(sandbox, before)


def test_implement_consumes_latest_run_artifacts(sandbox: dict[str, Path]) -> None:
    """The LATEST run (lexicographic STAMP) must govern, even when an older
    run has better evidence."""
    seed_meta(sandbox, "20250101-000000")  # older: READY_FOR_COMMIT
    log = capture_path(sandbox, "impl-latest")
    proc = run_cli(
        sandbox,
        ["implement", "--branch", BRANCH, "--", "t"],
        {
            "DELEGATE_CAPTURE": str(log),
            "DELEGATE_MODE": "needs_review",  # newest run: validation FAILED
            "DELEGATE_STAMP": "000001",
        },
    )
    assert proc.returncode == EXIT_BLOCKED, proc.stdout + proc.stderr
    assert "NEEDS_REVIEW" in proc.stdout or "NEEDS_REVIEW" in proc.stdout, proc.stdout
    assert "FAIL" in proc.stdout, proc.stdout
    assert "20250101-000001" in proc.stdout, "must name the LATEST run dir"
    assert "GO COMMIT is not evidenced" in proc.stdout


def test_implement_bad_evidence_blocked_no_commit(sandbox: dict[str, Path]) -> None:
    before = snapshot_git_state(sandbox["repo"])
    proc = run_cli(
        sandbox,
        ["implement", "--branch", BRANCH, "--", "t"],
        {"DELEGATE_MODE": "needs_review"},
    )
    assert proc.returncode == EXIT_BLOCKED, proc.stdout + proc.stderr
    assert "EVIDENCE NOT VALIDATED" in proc.stdout, proc.stdout
    assert "validation=FAIL" in proc.stdout, proc.stdout
    assert_git_unchanged(sandbox, before)


def test_implement_missing_artifacts_blocked(sandbox: dict[str, Path]) -> None:
    before = snapshot_git_state(sandbox["repo"])
    proc = run_cli(
        sandbox,
        ["implement", "--branch", BRANCH, "--", "t"],
        {"DELEGATE_MODE": "no_artifacts"},
    )
    assert proc.returncode == EXIT_BLOCKED, proc.stdout + proc.stderr
    assert "no Pi run artifacts" in proc.stdout or "no Pi run directory" in proc.stdout
    assert "GO COMMIT is not evidenced" in proc.stdout
    assert_git_unchanged(sandbox, before)


def test_implement_meta_missing_keys_blocked(sandbox: dict[str, Path]) -> None:
    runs = sandbox["repo"] / ".trajectory-pi" / "runs" / "20250101-000000"
    runs.mkdir(parents=True)
    (runs / "meta.txt").write_text("run_class=smoke\nbranch=feature/orch\n")
    proc = run_cli(
        sandbox,
        ["implement", "--branch", BRANCH, "--", "t"],
        {"DELEGATE_MODE": "no_meta", "DELEGATE_STAMP": "000000"},
    )
    assert proc.returncode == EXIT_BLOCKED, proc.stdout + proc.stderr
    assert "READY_FOR_COMMIT" in proc.stdout  # the wanted-but-missing value is reported


def test_implement_delegate_nonzero_propagated_exactly(sandbox: dict[str, Path]) -> None:
    for rc in (1, 42, 130):
        log = capture_path(sandbox, f"impl-rc{rc}")
        proc = run_cli(
            sandbox,
            ["implement", "--branch", BRANCH, "--", "t"],
            {"DELEGATE_CAPTURE": str(log), "DELEGATE_RC": str(rc)},
        )
        assert proc.returncode == rc, (
            f"delegate exit {rc} must be propagated exactly, got {proc.returncode}"
        )
        assert log.exists(), "delegate must have been invoked"


def test_implement_branch_mismatch_refused_before_delegation(sandbox: dict[str, Path]) -> None:
    log = capture_path(sandbox, "impl-mismatch")
    before = snapshot_git_state(sandbox["repo"])
    proc = run_cli(
        sandbox,
        ["implement", "--branch", "feature/other", "--", "t"],
        {"DELEGATE_CAPTURE": str(log)},
    )
    assert proc.returncode == EXIT_PRECONDITION, proc.stdout + proc.stderr
    assert "branch mismatch" in proc.stderr, proc.stderr
    assert not log.exists(), "delegate MUST not run when the branch does not match"
    assert_git_unchanged(sandbox, before)


def test_implement_detached_head_refused(sandbox: dict[str, Path]) -> None:
    repo: Path = sandbox["repo"]
    git(repo, "checkout", "--detach", "--quiet")
    log = capture_path(sandbox, "impl-detached")
    proc = run_cli(sandbox, ["implement", "--branch", "detached", "--", "t"])
    git(repo, "checkout", "--quiet", BRANCH)
    assert proc.returncode == EXIT_PRECONDITION, proc.stdout + proc.stderr
    assert "detached" in proc.stderr, proc.stderr
    assert not log.exists()


def test_implement_missing_delegate_refused(sandbox: dict[str, Path]) -> None:
    (sandbox["repo"] / "scripts" / "trajectory-codex-pi").unlink()
    proc = run_cli(sandbox, ["implement", "--branch", BRANCH, "--", "t"])
    assert proc.returncode == EXIT_PRECONDITION, proc.stdout + proc.stderr
    assert "trajectory-codex-pi" in proc.stderr


# ---------------------------------------------------------------------------
# COMMIT: authorization, exact scope, gate boundary (stops before GO PUSH)
# ---------------------------------------------------------------------------


def modified_files(repo: Path) -> dict[str, str]:
    assert (repo / "app" / "calc.py").write_text("v = 2\n")
    return {"app/calc.py": "v = 2\n"}


def test_commit_without_authorization_refused_no_git_mutation(sandbox: dict[str, Path]) -> None:
    modified_files(sandbox["repo"])
    before = snapshot_git_state(sandbox["repo"])
    glog = capture_path(sandbox, "commit-noauth-gate")
    glog.unlink(missing_ok=True)

    proc = run_cli(
        sandbox,
        ["commit", "--branch", BRANCH, "--scope", "app/calc.py",
         "--message", "V1.64 test change"],
    )
    assert proc.returncode == EXIT_AUTH_REFUSED, proc.stdout + proc.stderr
    assert "GO COMMIT" in proc.stderr, proc.stderr
    # no staging, no commit, no gate invocation
    assert_git_unchanged(sandbox, before)
    assert not glog.exists()


def test_commit_wrong_authorization_phrase_refused(sandbox: dict[str, Path]) -> None:
    modified_files(sandbox["repo"])
    before = snapshot_git_state(sandbox["repo"])
    for bad in ("GO-PUSH", "GO COMMIT!", "go commit", "GO PR"):
        proc = run_cli(
            sandbox,
            ["commit", "--branch", BRANCH, "--scope", "app/calc.py",
             "--message", "x", "--authorize", bad],
        )
        assert proc.returncode == EXIT_AUTH_REFUSED, (bad, proc.stdout, proc.stderr)
        assert "exact phrase" in proc.stderr
    assert_git_unchanged(sandbox, before)


def test_commit_missing_scope_or_message_usage(sandbox: dict[str, Path]) -> None:
    before = snapshot_git_state(sandbox["repo"])
    p1 = run_cli(
        sandbox,
        ["commit", "--branch", BRANCH, "--message", "x", "--authorize", "GO COMMIT"],
    )
    p2 = run_cli(
        sandbox,
        ["commit", "--branch", BRANCH, "--scope", "app/calc.py",
         "--authorize", "GO COMMIT"],
    )
    assert p1.returncode == EXIT_USAGE, p1.stderr
    assert "scope" in p1.stderr
    assert p2.returncode == EXIT_USAGE, p2.stderr
    assert "message" in p2.stderr
    assert_git_unchanged(sandbox, before)


def test_commit_authorizes_exact_scope_and_stops_before_push(sandbox: dict[str, Path]) -> None:
    """Authorized commit: exactly the scoped file is committed; canonical
    push-check is invoked with exact argv; the CLI STOPS before GO PUSH
    (no remote ref movement)."""
    modified_files(sandbox["repo"])

    commits_before = int(git_out(sandbox["repo"], "rev-list", "--count", "HEAD"))
    bare_branch_before = head_of(sandbox, BRANCH)

    glog = capture_path(sandbox, "commit-gate")
    glog.unlink(missing_ok=True)
    proc = run_cli(
        sandbox,
        ["commit", "--branch", BRANCH, "--scope", "app/calc.py",
         "--message", "V1.64 staged test change", "--authorize", "GO COMMIT"],
        {"GATE_CAPTURE": str(glog)},
    )
    assert proc.returncode == EXIT_GATE_STOP, proc.stdout + proc.stderr

    # the commit happened with the EXACT message and EXACT scope file
    assert int(git_out(sandbox["repo"], "rev-list", "--count", "HEAD")) == commits_before + 1
    assert git_out(sandbox["repo"], "log", "-1", "--format=%s") == "V1.64 staged test change"
    files = git_out(sandbox["repo"], "show", "--name-only", "--format=", "HEAD")
    assert sorted(files.split()) == ["app/calc.py"], files

    # canonical gate invoked with exact argv
    argv = gate_args(glog)
    assert argv[0] == "push-check", argv
    assert f"--repo={sandbox['repo'].as_posix()}" in argv, argv
    assert f"--expected-branch={BRANCH}" in argv, argv
    assert "--allow=app/calc.py" in argv, argv
    # scope is passed as one explicit prefix-authorized gate argument
    assert sum(arg.startswith("--allow=") for arg in argv) == 1

    # GATE BOUNDARY: it STOPS before GO PUSH — the remote branch did NOT move
    assert head_of(sandbox, BRANCH) == bare_branch_before, (
        "commit phase must never push"
    )
    assert "GO PUSH" in proc.stdout, proc.stdout
    assert "HUMAN" in proc.stdout


def test_commit_out_of_scope_change_refused(sandbox: dict[str, Path]) -> None:
    repo: Path = sandbox["repo"]
    modified_files(repo)
    (repo / "other" / "lib.py").write_text("u = 3\n")
    before = snapshot_git_state(repo)
    proc = run_cli(
        sandbox,
        ["commit", "--branch", BRANCH, "--scope", "app/calc.py",
         "--message", "x", "--authorize", "GO COMMIT"],
    )
    assert proc.returncode == EXIT_BLOCKED, proc.stdout + proc.stderr
    assert "outside the authorized scope" in proc.stderr, proc.stderr
    assert "other/lib.py" in proc.stderr, proc.stderr
    assert_git_unchanged(repo, before)


def test_commit_untracked_out_of_scope_refused(sandbox: dict[str, Path]) -> None:
    modified_files(sandbox["repo"])
    (sandbox["repo"] / "rogue/extra.txt").parent.mkdir(parents=True, exist_ok=True)
    (sandbox["repo"] / "rogue/extra.txt").write_text("nope\n")
    before = snapshot_git_state(sandbox["repo"])
    glog = capture_path(sandbox, "commit-untracked-gate")
    glog.unlink(missing_ok=True)
    proc = run_cli(
        sandbox,
        ["commit", "--branch", BRANCH, "--scope", "app/calc.py",
         "--message", "x", "--authorize", "GO COMMIT"],
        {"GATE_CAPTURE": str(glog)},
    )
    assert proc.returncode == EXIT_BLOCKED, proc.stdout + proc.stderr
    assert "rogue/extra.txt" in proc.stderr, proc.stderr
    assert not glog.exists()
    assert_git_unchanged(sandbox, before)


def test_commit_scope_directory_prefix_allowed(sandbox: dict[str, Path]) -> None:
    repo: Path = sandbox["repo"]
    modified_files(repo)
    (repo / "app" / "util.py").write_text("def f():\n    return 2\n")
    proc = run_cli(
        sandbox,
        ["commit", "--branch", BRANCH, "--scope", "app",
         "--message", "dir scope", "--authorize", "GO COMMIT"],
    )
    assert proc.returncode == EXIT_GATE_STOP, proc.stdout + proc.stderr
    files = git_out(repo, "show", "--name-only", "--format=", "HEAD")
    assert sorted(files.split()) == ["app/calc.py", "app/util.py"], files


def test_commit_empty_scope_authorized_refused(sandbox: dict[str, Path]) -> None:
    """Authorization alone must not commit when nothing in scope changed."""
    glog = capture_path(sandbox, "commit-empty-gate")
    glog.unlink(missing_ok=True)
    proc = run_cli(
        sandbox,
        ["commit", "--branch", BRANCH, "--scope", "app/calc.py",
         "--message", "x", "--authorize", "GO COMMIT"],
        {"GATE_CAPTURE": str(glog)},
    )
    assert proc.returncode == EXIT_BLOCKED, proc.stdout + proc.stderr
    assert "nothing to commit within the authorized scope" in proc.stderr, proc.stderr
    assert not glog.exists()


def test_commit_gate_blocked_propagates_exactly(sandbox: dict[str, Path]) -> None:
    modified_files(sandbox["repo"])
    glog = capture_path(sandbox, "commit-gateblocked")
    proc = run_cli(
        sandbox,
        ["commit", "--branch", BRANCH, "--scope", "app/calc.py",
         "--message", "x", "--authorize", "GO COMMIT"],
        {"GATE_CAPTURE": str(glog), "GATE_RC": "10"},
    )
    assert proc.returncode == 10, proc.stdout + proc.stderr  # gate's exact code
    assert gate_args(glog)[0] == "push-check"
    # commit itself happened (authorization was exact) — but the phase
    # is BLOCKED at the gate and STOPs before GO PUSH with no push.
    head = git_out(sandbox["bare"], "rev-parse", f"refs/heads/{BRANCH}")
    local = git_out(sandbox["repo"], "rev-parse", "HEAD")
    assert head != local, "block must be BEFORE any push, yet a commit was made"
    assert "STOP before GO PUSH" in proc.stdout


def test_commit_on_protected_branch_refused(sandbox: dict[str, Path]) -> None:
    repo: Path = sandbox["repo"]
    git(repo, "checkout", "--quiet", PROTECTED)
    (repo / "app" / "calc.py").write_text("v = 99\n")
    before = snapshot_git_state(repo)
    proc = run_cli(
        sandbox,
        ["commit", "--branch", PROTECTED, "--scope", "app/calc.py",
         "--message", "x", "--authorize", "GO COMMIT"],
    )
    assert proc.returncode == EXIT_PRECONDITION, proc.stdout + proc.stderr
    assert "protected branch" in proc.stderr, proc.stderr
    assert_git_unchanged(repo, before)
    git(repo, "checkout", "--quiet", BRANCH)


def test_commit_branch_mismatch_refused(sandbox: dict[str, Path]) -> None:
    modified_files(sandbox["repo"])
    before = snapshot_git_state(sandbox["repo"])
    proc = run_cli(
        sandbox,
        ["commit", "--branch", "feature/other", "--scope", "app/calc.py",
         "--message", "x", "--authorize", "GO COMMIT"],
    )
    assert proc.returncode == EXIT_PRECONDITION
    assert "branch mismatch" in proc.stderr
    assert_git_unchanged(sandbox, before)


# ---------------------------------------------------------------------------
# PUSH: authorization, plain push, HEAD verification, gate boundary (GO PR)
# ---------------------------------------------------------------------------


def test_push_without_authorization_no_remote_mutation(sandbox: dict[str, Path]) -> None:
    (sandbox["repo"] / "app" / "calc.py").write_text("v = 5\n")
    git(sandbox["repo"], "commit", "-am", "awaiting push", "--quiet")
    remote_before = head_of(sandbox, BRANCH)
    glog = capture_path(sandbox, "push-noauth-gate")
    glog.unlink(missing_ok=True)

    proc = run_cli(
        sandbox,
        ["push", "--branch", BRANCH],
        {"GATE_CAPTURE": str(glog)},
    )
    assert proc.returncode == EXIT_AUTH_REFUSED, proc.stdout + proc.stderr
    assert "GO PUSH" in proc.stderr, proc.stderr
    assert head_of(sandbox, BRANCH) == remote_before, "no push before authorization"
    assert not glog.exists(), "gate must not run before authorization"


def test_push_wrong_phrase_refused(sandbox: dict[str, Path]) -> None:
    proc = run_cli(
        sandbox,
        ["push", "--branch", BRANCH, "--authorize", "GO COMMIT"],
    )
    assert proc.returncode == EXIT_AUTH_REFUSED
    assert "exact phrase" in proc.stderr


def test_push_authorization_pushes_plain_and_stops_before_pr(sandbox: dict[str, Path]) -> None:
    repo: Path = sandbox["repo"]
    (repo / "app" / "calc.py").write_text("v = 6\n")
    git(repo, "commit", "-am", "ready to push", "--quiet")
    local_head = git_out(repo, "rev-parse", "HEAD")
    remote_before = head_of(sandbox, BRANCH)
    assert remote_before != local_head

    glog = capture_path(sandbox, "push-gate")
    glog.unlink(missing_ok=True)
    proc = run_cli(
        sandbox,
        ["push", "--branch", BRANCH, "--authorize", "GO PUSH"],
        {"GATE_CAPTURE": str(glog)},
    )
    assert proc.returncode == EXIT_GATE_STOP, proc.stdout + proc.stderr

    # plain (non-force) remote update happened and remote HEAD == local HEAD
    assert head_of(sandbox, BRANCH) == local_head, "remote HEAD must equal local HEAD"
    assert "remote HEAD verified" in proc.stdout, proc.stdout

    # canonical pr-check invoked with exact argv
    argv = gate_args(glog)
    assert argv[0] == "pr-check", argv
    assert f"--repo={sandbox['repo'].as_posix()}" in argv
    assert f"--expected-branch={BRANCH}" in argv

    # GATE BOUNDARY: stopped before GO PR; no pull request creation of any kind
    assert "GO PR" in proc.stdout, proc.stdout
    assert "HUMAN" in proc.stdout, proc.stdout
    assert "pull" not in proc.stdout.lower() or "will not create a pull" in proc.stdout


def test_push_missing_remote_refused(sandbox: dict[str, Path]) -> None:
    proc = run_cli(
        sandbox,
        ["push", "--branch", BRANCH, "--remote", "nofurthing", "--authorize", "GO PUSH"],
    )
    assert proc.returncode == EXIT_PRECONDITION, proc.stdout + proc.stderr
    assert "remote" in proc.stderr.lower()


def test_push_non_fast_forward_refused_and_propagated(sandbox: dict[str, Path]) -> None:
    """A plain push must fail (non-zero propagated) when the remote branch
    advanced beyond local — no reconciliation, no forced update."""
    repo: Path = sandbox["repo"]
    # advance the remote branch with a foreign commit (local worktree, temp refs)
    git(repo, "fetch", "origin", BRANCH)
    git(repo, "checkout", "--quiet", "-b", "remote-ahead", "FETCH_HEAD")
    (repo / "app" / "calc.py").write_text("v = 77\n")
    git(repo, "commit", "-am", "remote side commit", "--quiet")
    git(repo, "push", "origin", f"remote-ahead:refs/heads/{BRANCH}")
    git(repo, "checkout", "--quiet", BRANCH)
    git(repo, "branch", "-D", "remote-ahead")

    before = snapshot_git_state(repo)
    proc = run_cli(
        sandbox,
        ["push", "--branch", BRANCH, "--authorize", "GO PUSH"],
    )
    assert proc.returncode != 0
    assert proc.returncode != EXIT_GATE_STOP, (
        "a rejected push must not reach the gate as success"
    )
    assert "plain push failed" in proc.stdout, proc.stdout
    # and the CLI must not have issued any workaround: the local state is untouched
    assert_git_unchanged(repo, before)


def test_push_on_protected_branch_refused(sandbox: dict[str, Path]) -> None:
    repo: Path = sandbox["repo"]
    git(repo, "checkout", "--quiet", PROTECTED)
    proc = run_cli(sandbox, ["push", "--branch", PROTECTED, "--authorize", "GO PUSH"])
    git(repo, "checkout", "--quiet", BRANCH)
    assert proc.returncode == EXIT_PRECONDITION
    assert "protected branch" in proc.stderr


# ---------------------------------------------------------------------------
# MERGE-CHECK: explicit evidence, canonical gate, NEVER merges
# ---------------------------------------------------------------------------


def test_merge_check_invokes_gate_with_evidence_and_never_merges(sandbox: dict[str, Path]) -> None:
    repo: Path = sandbox["repo"]
    main_before = git_out(repo, "rev-parse", "refs/heads/main")

    ev = repo / "evidence.json"
    ev.write_text(
        json.dumps(
            {
                "head": git_out(repo, "rev-parse", "HEAD"),
                "ci": "success",
                "reviews_resolved": True,
                "mergeable": "clean",
            }
        )
    )
    before = snapshot_git_state(repo)

    glog = capture_path(sandbox, "merge-gate")
    glog.unlink(missing_ok=True)
    proc = run_cli(
        sandbox,
        ["merge-check", "--branch", BRANCH, "--evidence", "evidence.json"],
        {"GATE_CAPTURE": str(glog)},
    )
    assert proc.returncode == EXIT_GATE_STOP, proc.stdout + proc.stderr

    argv = gate_args(glog)
    assert argv[0] == "merge-check", argv
    assert f"--repo={sandbox['repo'].as_posix()}" in argv
    assert f"--expected-branch={BRANCH}" in argv
    assert f"--evidence={ev.resolve().as_posix()}" in argv, argv

    # NEVER merges: main is untouched, no new commits anywhere
    assert git_out(repo, "rev-parse", "refs/heads/main") == main_before
    assert_git_unchanged(repo, before)
    assert "NEVER merges" in proc.stdout or "never merges" in proc.stdout
    assert "GO MERGE" in proc.stdout


def test_merge_check_missing_evidence_refused(sandbox: dict[str, Path]) -> None:
    glog = capture_path(sandbox, "merge-noevidence")
    glog.unlink(missing_ok=True)
    proc = run_cli(
        sandbox,
        ["merge-check", "--branch", BRANCH, "--evidence", "does/not/exist.json"],
        {"GATE_CAPTURE": str(glog)},
    )
    assert proc.returncode == EXIT_PRECONDITION, proc.stdout + proc.stderr
    assert "evidence" in proc.stderr.lower()
    assert not glog.exists(), "gate must not run without a usable evidence file"


def test_merge_check_requires_evidence_flag(sandbox: dict[str, Path]) -> None:
    proc = run_cli(sandbox, ["merge-check", "--branch", BRANCH])
    assert proc.returncode == EXIT_USAGE
    assert "--evidence" in proc.stderr


def test_merge_check_gate_blocked_propagates(sandbox: dict[str, Path]) -> None:
    repo: Path = sandbox["repo"]
    (repo / "evidence.json").write_text(
        json.dumps({"head": "x", "ci": "pending", "reviews_resolved": False, "mergeable": "dirty"})
    )
    glog = capture_path(sandbox, "merge-blocked")
    proc = run_cli(
        sandbox,
        ["merge-check", "--branch", BRANCH, "--evidence", "evidence.json"],
        {"GATE_CAPTURE": str(glog), "GATE_RC": "10"},
    )
    assert proc.returncode == 10, proc.stdout + proc.stderr
    assert "no merge of any kind" in proc.stdout, proc.stdout
    assert gate_args(glog)[0] == "merge-check"


# ---------------------------------------------------------------------------
# Static guarantees + credential / sandbox state isolation
# ---------------------------------------------------------------------------


def test_static_no_forbidden_git_actions_or_network_tooling() -> None:
    code = REAL_CLI.read_text()
    executable = "\n".join(
        ln for ln in code.splitlines() if not ln.lstrip().startswith("#")
    )
    for forbidden in (
        # forbidden git behavior (exact command shapes)
        "push -f",
        "push --force",
        "force-with-lease",
        "+refs",
        "git merge",
        " git pull",
        "pull request --",
        "git rebase",
        "git reset",
        "git restore",
        "git clean",
        "git stash",
        "git am ",
        "--amend",
        "commit --amend",
        "git checkout",
        "git switch",
        # network / sync tooling
        "curl",
        "wget",
        "rsync",
        " telnet",
        "nc -",
        "ssh ",
        "scp ",
        # credential / sandbox state handling
        "auth.json",
        "trust.json",
        "settings.json",
        "PI_CODING_AGENT_DIR",
        "models.json",
        ".pi/agent",
        "password",
        "credential",
        "SECRET",
    ):
        assert forbidden not in executable, f"CLI must not contain: {forbidden!r}"

    # plain push only: exactly one push line, plain refspec, no flags
    push_lines = [
        ln.strip()
        for ln in executable.splitlines()
        if re.search(r"\bgit\b.*\bpush\b", ln) or (
            "push" in ln and "refs/heads" in ln
        )
    ]
    relevant = [ln for ln in push_lines if "refs/heads/$expected_branch" in ln]
    assert len(relevant) == 1, push_lines
    assert "--force" not in relevant[0] and " -f" not in relevant[0]
    assert "+refs/heads" not in relevant[0]

    # remote verification is explicitly `git ls-remote` (authoritative read)
    assert "ls-remote" in executable
    # canonical gate reuse: the CLI never re-implements the checks itself
    assert "trajectory_gate" in code
    assert "--expected-branch" in executable


def test_no_credential_or_sandbox_state_mutation(sandbox: dict[str, Path]) -> None:
    seed: Path = sandbox["seed"]
    before = {p.name: p.read_bytes() for p in seed.iterdir()}

    (sandbox["repo"] / "app" / "calc.py").write_text("v = 3\n")
    proc = run_cli(
        sandbox,
        ["commit", "--branch", BRANCH, "--scope", "app/calc.py",
         "--message", "isolation", "--authorize", "GO COMMIT"],
    )
    assert proc.returncode == EXIT_GATE_STOP, proc.stdout + proc.stderr

    after = {p.name: p.read_bytes() for p in seed.iterdir()}
    assert after == before, "caller Pi state must be byte-identical"

    # and the CLI wrote no Pi/sandbox configuration into the repo
    state = sandbox["repo"] / ".trajectory-pi"
    if state.exists():
        names = {p.name for p in state.rglob("*") if p.is_file()}
        forbidden = {"auth.json", "trust.json", "settings.json", "credentials.json"}
        assert not (forbidden & names), names

    # no network-ish output at all
    out = proc.stdout + proc.stderr
    for token in ("Downloading", "ollama", "http://", "https://", "api."):
        assert token not in out, f"unexpected network-ish output: {token!r}"


def test_implementation_delegation_env_isolation(sandbox: dict[str, Path]) -> None:
    """The CLI forwards the delegate's argv but never injects credentials,
    agent dirs, or sandbox settings of its own (it stays a pure orchestrator)."""
    log = capture_path(sandbox, "impl-env")
    proc = run_cli(
        sandbox,
        ["implement", "--branch", BRANCH, "--", "t"],
        {"DELEGATE_CAPTURE": str(log)},
    )
    assert proc.returncode == EXIT_GATE_STOP, proc.stdout + proc.stderr
    # the captured argv contains exactly what we chose to forward and nothing
    # the CLI added to the argument vector
    delegate = log.read_text()
    arg_lines = [
        ln[len("ARG="):] for ln in delegate.splitlines() if ln.startswith("ARG=")
    ]
    assert arg_lines == ["--", "t"], arg_lines


def test_cli_documents_no_autonomous_state_machine() -> None:
    code = REAL_CLI.read_text()
    assert "no autonomous state machine" in code.lower()
    # every phase must be gated by exact authorization or an explicit stop
    assert '"GO COMMIT"' in code
    assert '"GO PUSH"' in code
    assert "GO MERGE" in code
    assert "GO PR" in code


# ---------------------------------------------------------------------------
# STATUS: read-only passthrough to trajectory-pi-status (issue #174)
# ---------------------------------------------------------------------------


def test_status_forwards_exact_arguments(sandbox: dict[str, Path]) -> None:
    """All status-reader args are forwarded byte-for-argument to the delegate."""
    args = [
        "--run", "20250101-120000",
        "--runs-root", ".trajectory-pi/runs",
        "--json",
        "--now", "2026-01-01T00:00:00Z",
        "--stale-after", "120",
        "--max-files", "10",
        "--worktree-live",
    ]
    cap = capture_path(sandbox, "status-args")
    cwd_cap = capture_path(sandbox, "status-cwd")
    proc = run_cli(
        sandbox,
        ["status", *args],
        {"STATUS_CAPTURE": str(cap), "STATUS_CWD_CAPTURE": str(cwd_cap)},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    delegate = cap.read_text()
    assert f"ARGC={len(args)}\n" in delegate, delegate
    arg_lines = [
        ln[len("ARG="):] for ln in delegate.splitlines() if ln.startswith("ARG=")
    ]
    assert arg_lines == args, f"argv altered: {arg_lines!r} != {args!r}"


def test_status_forwards_empty_arglist(sandbox: dict[str, Path]) -> None:
    """status with no extra args: the delegate is invoked with zero args."""
    cap = capture_path(sandbox, "status-noargs")
    proc = run_cli(
        sandbox,
        ["status"],
        {"STATUS_CAPTURE": str(cap)},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    delegate = cap.read_text()
    assert "ARGC=0\n" in delegate, delegate


def test_status_forwards_special_characters_unchanged(sandbox: dict[str, Path]) -> None:
    """Args with spaces, dashes, and special chars must pass through intact."""
    args = [
        "--now", "2026-01-01T00:00:00+00:00",
        "--run", "a-run name with spaces",
        "--max-files", "99",
        "-x",  # hypothetical single-dash flag
    ]
    cap = capture_path(sandbox, "status-special")
    proc = run_cli(
        sandbox,
        ["status", *args],
        {"STATUS_CAPTURE": str(cap)},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    delegate = cap.read_text()
    arg_lines = [
        ln[len("ARG="):] for ln in delegate.splitlines() if ln.startswith("ARG=")
    ]
    assert arg_lines == args, f"special chars mangled: {arg_lines!r} != {args!r}"


def test_status_read_only_no_git_mutation(sandbox: dict[str, Path]) -> None:
    """status must NEVER mutate git state: HEAD, branch, commits, index, status."""
    before = snapshot_git_state(sandbox["repo"])
    cap = capture_path(sandbox, "status-readonly")
    proc = run_cli(
        sandbox,
        ["status", "--json", "--now", "2026-01-01T00:00:00Z"],
        {"STATUS_CAPTURE": str(cap)},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert cap.exists(), "delegate must have been invoked"
    assert_git_unchanged(sandbox, before)


def test_status_no_gate_invocation(sandbox: dict[str, Path]) -> None:
    """The status subcommand must NEVER trigger trajectory_gate."""
    glog = capture_path(sandbox, "status-gate")
    glog.unlink(missing_ok=True)
    cap = capture_path(sandbox, "status-nogate")
    proc = run_cli(
        sandbox,
        ["status", "--json"],
        {"STATUS_CAPTURE": str(cap), "GATE_CAPTURE": str(glog)},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert cap.exists(), "status delegate must be invoked"
    assert not glog.exists(), "gate must NEVER be invoked by status"


def test_status_on_protected_branch_allowed(sandbox: dict[str, Path]) -> None:
    """Status observation is branch-independent; even on main it works."""
    repo: Path = sandbox["repo"]
    git(repo, "checkout", "--quiet", PROTECTED)
    cap = capture_path(sandbox, "status-main")
    proc = run_cli(
        sandbox,
        ["status", "--version"],
        {"STATUS_CAPTURE": str(cap)},
    )
    git(repo, "checkout", "--quiet", BRANCH)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert cap.exists(), "delegate must be invoked even on protected branch"


def test_status_on_detached_head_allowed(sandbox: dict[str, Path]) -> None:
    """Status is a pure observation; it must work even with a detached HEAD."""
    repo: Path = sandbox["repo"]
    git(repo, "checkout", "--detach", "--quiet")
    cap = capture_path(sandbox, "status-detached")
    proc = run_cli(
        sandbox,
        ["status"],
        {"STATUS_CAPTURE": str(cap)},
    )
    git(repo, "checkout", "--quiet", BRANCH)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert cap.exists()


def test_status_missing_delegate_refused(sandbox: dict[str, Path]) -> None:
    """When trajectory-pi-status is absent or unreadable, fail-closed."""
    delegate_path = sandbox["repo"] / "scripts" / "trajectory-pi-status"
    delegate_path.unlink()
    before = snapshot_git_state(sandbox["repo"])
    proc = run_cli(sandbox, ["status", "--json"])
    assert proc.returncode == EXIT_PRECONDITION, proc.stdout + proc.stderr
    assert "status delegate" in proc.stderr, proc.stderr
    assert "not found" in proc.stderr or "not executable" in proc.stderr
    assert_git_unchanged(sandbox, before)


def test_status_nonzero_exit_propagated_exactly(sandbox: dict[str, Path]) -> None:
    """The delegate's exit code must propagate EXACTLY (1, 2, 42, 130, 255)."""
    for rc in (1, 2, 42, 130, 255):
        cap = capture_path(sandbox, f"status-rc{rc}")
        proc = run_cli(
            sandbox,
            ["status", "--json"],
            {"STATUS_CAPTURE": str(cap), "STATUS_RC": str(rc)},
        )
        assert proc.returncode == rc, (
            f"status delegate exit {rc} must propagate exactly, got {proc.returncode}"
        )
        assert cap.exists(), f"delegate must have been invoked (rc={rc})"


def test_status_zero_exit_propagated(sandbox: dict[str, Path]) -> None:
    """Success exit (0) must also propagate exactly."""
    cap = capture_path(sandbox, "status-ok")
    proc = run_cli(
        sandbox,
        ["status", "--now", "2026-01-01T00:00:00Z"],
        {"STATUS_CAPTURE": str(cap), "STATUS_RC": "0"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert cap.exists()


def test_status_is_never_authorization(sandbox: dict[str, Path]) -> None:
    """Even after a successful status call, the commit gate still requires
    the exact authorization phrase — status does NOT unlock any gate."""
    # Run status first (succeeds)
    cap = capture_path(sandbox, "status-then-gate")
    proc_status = run_cli(
        sandbox,
        ["status", "--json"],
        {"STATUS_CAPTURE": str(cap)},
    )
    assert proc_status.returncode == 0

    # Now attempt commit WITHOUT authorization — must still be refused
    modified_files(sandbox["repo"])
    before = snapshot_git_state(sandbox["repo"])
    proc_commit = run_cli(
        sandbox,
        ["commit", "--branch", BRANCH, "--scope", "app/calc.py",
         "--message", "x"],
    )
    assert proc_commit.returncode == EXIT_AUTH_REFUSED, proc_commit.stdout + proc_commit.stderr
    assert "GO COMMIT" in proc_commit.stderr
    assert_git_unchanged(sandbox, before)


def test_status_output_mentions_delegate_and_read_only(sandbox: dict[str, Path]) -> None:
    """The CLI's own output documents the delegation and read-only nature."""
    cap = capture_path(sandbox, "status-output")
    proc = run_cli(
        sandbox,
        ["status", "--json"],
        {"STATUS_CAPTURE": str(cap)},
    )
    assert proc.returncode == 0
    out = proc.stdout + proc.stderr
    assert "trajectory-pi-status" in out, f"must name the delegate in output: {out!r}"
    assert "read-only" in out.lower(), f"must document read-only: {out!r}"


def test_status_static_no_forbidden_commands() -> None:
    """Static analysis: the status path must not contain any forbidden
    Git mutation, network, or credential operations."""
    code = REAL_CLI.read_text()
    status_section_start = code.index("cmd_status()")
    status_section = code[status_section_start:status_section_start + 2000]  # generous window
    for forbidden in (
        "git add", "git commit", "git push", "git merge", "git pull",
        "git rebase", "git reset", "git restore", "git clean", "git stash",
        "git checkout", "git switch","+refs",
        "curl", "wget", "ssh ", "scp ",
        "auth.json", "trust.json", "settings.json", "credential",
        "password", "SECRET",
        "require_worktree", "require_expected_branch",
        "AUTH_COMMIT", "AUTH_PUSH",
    ):
        assert forbidden not in status_section, (
            f"status path must not contain: {forbidden!r}"
        )


def test_status_delegates_to_status_reader_not_pi_or_gate(sandbox: dict[str, Path]) -> None:
    """The status command invokes trajectory-pi-status ONLY, never the Pi
    delegate or trajectory_gate (verified by only the status cap existing)."""
    pi_cap = capture_path(sandbox, "status-pi").unlink(missing_ok=True)
    pi_cap = capture_path(sandbox, "status-pi")
    glog = capture_path(sandbox, "status-gate2").unlink(missing_ok=True)
    glog = capture_path(sandbox, "status-gate2")
    status_cap = capture_path(sandbox, "status-only")

    proc = run_cli(
        sandbox,
        ["status", "--json"],
        {
            "STATUS_CAPTURE": str(status_cap),
            "DELEGATE_CAPTURE": str(pi_cap),
            "GATE_CAPTURE": str(glog),
        },
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert status_cap.exists(), "status delegate MUST be invoked"
    assert not pi_cap.exists(), "Pi delegate must NOT be invoked by status"
    assert not glog.exists(), "gate must NOT be invoked by status"


def test_status_preserves_existing_behavior(sandbox: dict[str, Path]) -> None:
    """Adding the status subcommand must not alter implement/commit/push/
    merge-check behavior: the existing gates and exit codes are unchanged."""
    # implement still works exactly as before
    cap = capture_path(sandbox, "status-preserve-impl")
    proc = run_cli(
        sandbox,
        ["implement", "--branch", BRANCH, "--", "t"],
        {"DELEGATE_CAPTURE": str(cap)},
    )
    assert proc.returncode == EXIT_GATE_STOP, proc.stdout + proc.stderr
    assert "READY_FOR_COMMIT" in proc.stdout

    # commit still requires auth
    modified_files(sandbox["repo"])
    proc2 = run_cli(
        sandbox,
        ["commit", "--branch", BRANCH, "--scope", "app/calc.py",
         "--message", "x"],
    )
    assert proc2.returncode == EXIT_AUTH_REFUSED

    # push still requires auth
    proc3 = run_cli(sandbox, ["push", "--branch", BRANCH])
    assert proc3.returncode == EXIT_AUTH_REFUSED

    # merge-check still requires evidence
    proc4 = run_cli(sandbox, ["merge-check", "--branch", BRANCH])
    assert proc4.returncode == EXIT_USAGE
