"""Integration tests for the trajectory-codex-pi wrapper (issue #168).

Every test drives the real ``scripts/trajectory-codex-pi`` wrapper inside an
isolated temporary Git repository with a deterministic mock delegate standing
in for ``scripts/trajectory-pi``. The mock delegate records its exact argv
and the ``PI_CODING_AGENT_DIR`` environment value, and exits with a
configurable status. The caller's Pi state is a fake ``$HOME/.pi/agent``
seeded with a distinctive ``models.json`` plus credential/secret state that
the wrapper must never read, copy, or mutate.

No network, no real Ollama, no real Pi, no credentials.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_WRAPPER = REPO_ROOT / "scripts" / "trajectory-codex-pi"

MODELS_CONTENT = b'{"providers": {"local-fake": {"models": {"fake-model": {}}}}}\n'

SECRET_AUTH_CONTENT = b'{"token": "SECRET-AUTH-DO-NOT-COPY"}\n'
SECRET_TRUST_CONTENT = b'{"trusted": ["SECRET-TRUST-DO-NOT-COPY"]}\n'
SECRET_SETTINGS_CONTENT = b'{"sandbox": "SECRET-SANDBOX-SETTING"}\n'

# Mock delegate for scripts/trajectory-pi: records the exact argv and the
# PI_CODING_AGENT_DIR it was invoked with; exits with DELEGATE_RC.
FAKE_DELEGATE = r'''#!/usr/bin/env bash
{
  printf 'PI_CODING_AGENT_DIR=%s\n' "${PI_CODING_AGENT_DIR:-UNSET}"
  printf 'HOME=%s\n' "${HOME:-UNSET}"
  printf 'PWCWD=%s\n' "$(pwd)"
  printf 'ARGC=%d\n' "$#"
  for a in "$@"; do
    printf 'ARG=%s\n' "$a"
  done
} > "${DELEGATE_CAPTURE:?DELEGATE_CAPTURE unset}"
exit "${DELEGATE_RC:-0}"
'''


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True
    )


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Fake HOME (with seed Pi state) + deterministic temp Git repository.

    The real wrapper is copied into the temp repo's scripts/ directory so
    it resolves the delegate relative to ITS OWN location — the in-repo
    mock delegate, never the real production trajectory-pi runner.
    """
    # --- fake caller Pi state (the only models.json + hostile secrets) ---
    fake_home = tmp_path / "fakehome"
    seed = fake_home / ".pi" / "agent"
    seed.mkdir(parents=True)
    (seed / "models.json").write_bytes(MODELS_CONTENT)
    (seed / "auth.json").write_bytes(SECRET_AUTH_CONTENT)
    (seed / "trust.json").write_bytes(SECRET_TRUST_CONTENT)
    (seed / "settings.json").write_bytes(SECRET_SETTINGS_CONTENT)
    (seed / "sessions").mkdir()

    # --- deterministic temporary git repository ---
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "TrajectoryOS Test")
    (repo / "tracked.py").write_text("v = 1\n")
    (repo / ".gitignore").write_text(".trajectory-pi/\n")

    # wrapper + mock delegate inside the temp repo (created BEFORE the
    # initial commit so the tree is fully tracked and clean afterwards;
    # the wrapper resolves the delegate relative to ITS OWN location, so
    # it always delegates to the mock, never the production runner)
    scripts = repo / "scripts"
    scripts.mkdir()
    shutil.copyfile(REAL_WRAPPER, scripts / "trajectory-codex-pi")
    (scripts / "trajectory-codex-pi").chmod(0o755)
    (scripts / "trajectory-pi").write_text(FAKE_DELEGATE)
    (scripts / "trajectory-pi").chmod(0o755)

    git(repo, "add", "-A")
    git(repo, "commit", "-m", "init", "--quiet")
    git(repo, "checkout", "-b", "feature/test")

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    return {
        "home": fake_home,
        "seed": seed,
        "repo": repo,
        "wrapper": scripts / "trajectory-codex-pi",
        "agent_dir": repo / ".trajectory-pi" / "pi-agent",
    }


def run_wrapper(
    sandbox: dict[str, Path],
    args: list[str],
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", str(sandbox["wrapper"]), *args],
        cwd=sandbox["repo"],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def capture_path(sandbox: dict[str, Path], name: str) -> Path:
    return sandbox["repo"].parent / f"delegate-capture-{name}.log"


def delegate_log(path: Path) -> str:
    assert path.exists(), f"delegate was NOT invoked (no capture log: {path})"
    return path.read_text()


# ---------------------------------------------------------------------------
# Models-only synchronization
# ---------------------------------------------------------------------------


def test_models_only_copied_byte_for_byte(sandbox: dict[str, Path]) -> None:
    log = capture_path(sandbox, "models")
    proc = run_wrapper(
        sandbox,
        ["--class", "smoke", "--", "do something"],
        {"DELEGATE_CAPTURE": str(log)},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    agent_dir: Path = sandbox["agent_dir"]
    assert agent_dir.is_dir()
    # the agent directory holds EXACTLY models.json and nothing else:
    # no auth.json, no trust.json, no settings, no sessions, no other
    # Pi state of any kind was copied in.
    contents = sorted(p.name for p in agent_dir.iterdir())
    assert contents == ["models.json"], contents

    copied = (agent_dir / "models.json").read_bytes()
    assert copied == MODELS_CONTENT, "models.json must be byte-for-byte"
    assert SECRET_AUTH_CONTENT not in copied

    delegate = delegate_log(log)
    assert f"PI_CODING_AGENT_DIR={agent_dir}" in delegate


def test_source_pi_state_never_read_or_mutated(sandbox: dict[str, Path]) -> None:
    """The caller's Pi state in $HOME is never read (beyond models.json),
    never copied, and never mutated."""
    seed: Path = sandbox["seed"]
    before = {p.name: p.read_bytes() for p in seed.iterdir() if p.is_file()}
    before["sessions"] = b"dir"

    log = capture_path(sandbox, "srcstate")
    proc = run_wrapper(
        sandbox,
        ["--class", "smoke", "--", "delegated task"],
        {"DELEGATE_CAPTURE": str(log)},
    )
    assert proc.returncode == 0, proc.stderr

    # source state is byte-identical after the run (read-only at most for
    # models.json, and the copy is verbatim)
    after = {p.name: p.read_bytes() for p in seed.iterdir() if p.is_file()}
    after["sessions"] = b"dir"
    assert after == before, "source $HOME/.pi/agent state must be unmutated"

    assert (seed / "auth.json").read_bytes() == SECRET_AUTH_CONTENT
    assert (seed / "trust.json").read_bytes() == SECRET_TRUST_CONTENT
    # and none of the secret state leaked into the repo
    repo_blob = str.encode(" ".join(
        p.read_bytes().decode("utf-8", "replace")
        for p in sandbox["repo"].glob(".trajectory-pi/pi-agent/*")
    ))
    assert b"SECRET-AUTH" not in repo_blob
    assert b"SECRET-TRUST" not in repo_blob
    assert b"SECRET-SANDBOX" not in repo_blob


def test_wrapper_copies_nothing_beyond_models_silently() -> None:
    """Static guarantee: no network tooling, no recursive sync, no
    credential/sandbox/settings path handling in the executable
    statements of the wrapper."""
    lines = [
        ln for ln in REAL_WRAPPER.read_text().splitlines() if not ln.lstrip().startswith("#")
    ]
    code = "\n".join(lines)
    for forbidden in (
        "curl", "wget", "rsync", "ssh ", "scp ", "telnet",
        "nc -", "auth.json", "trust.json", "settings.json",
        "cp -r", "tar", "pip install", "uv add",
    ):
        assert forbidden not in code, f"wrapper must not use/handle: {forbidden!r}"


# ---------------------------------------------------------------------------
# PI_CODING_AGENT_DIR
# ---------------------------------------------------------------------------


def test_correct_pi_coding_agent_dir(sandbox: dict[str, Path]) -> None:
    expected = (sandbox["repo"] / ".trajectory-pi" / "pi-agent")
    log = capture_path(sandbox, "agentdir")
    proc = run_wrapper(sandbox, [], {"DELEGATE_CAPTURE": str(log)})
    assert proc.returncode == 0, proc.stderr

    delegate = delegate_log(log)
    assert f"PI_CODING_AGENT_DIR={expected}\n" in delegate
    # it must be the repo-local path, NOT the caller's HOME
    assert f"PI_CODING_AGENT_DIR={sandbox['home']}" not in delegate


# ---------------------------------------------------------------------------
# Exact argument forwarding
# ---------------------------------------------------------------------------


def test_exact_argument_forwarding(sandbox: dict[str, Path]) -> None:
    args = [
        "--class", "feature",
        "--model", "qwen3.8-dev3090",
        "--no-notify",
        "--validate", "echo verify -n --",
        "--",
        "handle -n -- and 'quotes' $HOME `backticks`",
        "",  # empty-string argument must survive
        "-x",
    ]
    log = capture_path(sandbox, "args")
    proc = run_wrapper(sandbox, args, {"DELEGATE_CAPTURE": str(log)})
    assert proc.returncode == 0, proc.stderr

    delegate = delegate_log(log)
    assert f"ARGC={len(args)}\n" in delegate, delegate
    for a in args:
        expected_line = f"ARG={a}\n"
        assert expected_line in delegate, (
            f"argument lost or altered: {a!r}\n{delegate}"
        )
    # order-preserving check: the ARG lines appear in the exact order
    arg_lines = [
        ln for ln in delegate.splitlines() if ln.startswith("ARG=")
    ]
    assert [ln[len("ARG="):] for ln in arg_lines] == args


# ---------------------------------------------------------------------------
# Exit status propagation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rc", [1, 3, 4, 42, 99, 130, 143])
def test_nonzero_exit_status_propagated(sandbox: dict[str, Path], rc: int) -> None:
    log = capture_path(sandbox, f"rc{rc}")
    proc = run_wrapper(
        sandbox,
        ["--class", "smoke", "--", "task"],
        {"DELEGATE_CAPTURE": str(log), "DELEGATE_RC": str(rc)},
    )
    assert proc.returncode == rc, (
        f"wrapper must return exactly trajectory-pi's exit status {rc}, "
        f"got {proc.returncode}"
    )
    delegate_log(log)  # delegation did occur


# ---------------------------------------------------------------------------
# Fail-closed before delegation
# ---------------------------------------------------------------------------


def test_missing_source_models_fails_before_delegation(
    sandbox: dict[str, Path],
) -> None:
    (sandbox["seed"] / "models.json").unlink()
    log = capture_path(sandbox, "missing")
    log.unlink(missing_ok=True)

    proc = run_wrapper(sandbox, ["--", "task"])
    assert proc.returncode != 0, "wrapper must fail when the source is missing"

    assert "ERROR" in proc.stderr
    assert "models.json" in proc.stderr, proc.stderr
    assert "refus" in proc.stderr, proc.stderr
    assert not log.exists(), "delegate MUST not be invoked when source is missing"
    assert not sandbox["agent_dir"].exists(), (
        "no delegation side effects when source is missing"
    )


def test_non_regular_source_models_fails_before_delegation(
    sandbox: dict[str, Path],
) -> None:
    seed: Path = sandbox["seed"]
    (seed / "models.json").unlink()
    (seed / "models.json").mkdir()  # a directory at the models path
    log = capture_path(sandbox, "notaregular")
    log.unlink(missing_ok=True)

    proc = run_wrapper(sandbox, ["--", "task"])
    assert proc.returncode != 0
    assert "ERROR" in proc.stderr
    assert not log.exists(), "delegate MUST not be invoked for a bad source"


if os.geteuid() != 0:  # euid 0 bypasses read bits; skip where not meaningful

    def test_unreadable_source_models_fails_before_delegation(
        sandbox: dict[str, Path],
    ) -> None:
        (sandbox["seed"] / "models.json").chmod(0)
        log = capture_path(sandbox, "unreadable")
        log.unlink(missing_ok=True)

        proc = run_wrapper(sandbox, ["--", "task"])
        assert proc.returncode != 0
        assert "ERROR" in proc.stderr
        assert "readable" in proc.stderr, proc.stderr
        assert not log.exists(), "delegate MUST not be invoked for unreadable source"

        (sandbox["seed"] / "models.json").chmod(0o644)


# ---------------------------------------------------------------------------
# No commit/push/PR/merge; no network or sandbox configuration mutation
# ---------------------------------------------------------------------------


def test_no_git_workflow_behavior(sandbox: dict[str, Path]) -> None:
    repo: Path = sandbox["repo"]
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    commits_before = subprocess.run(
        ["git", "rev-list", "--all", "--count"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    branch_before = subprocess.run(
        ["git", "branch", "--show-current"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()

    log = capture_path(sandbox, "gitstate")
    proc = run_wrapper(
        sandbox, ["--class", "smoke", "--", "task"],
        {"DELEGATE_CAPTURE": str(log)},
    )
    assert proc.returncode == 0, proc.stderr
    delegate_log(log)

    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, env=env, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    commits_after = subprocess.run(
        ["git", "rev-list", "--all", "--count"], cwd=repo, env=env,
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    branch_after = subprocess.run(
        ["git", "branch", "--show-current"], cwd=repo, env=env, check=True,
        capture_output=True, text=True,
    ).stdout.strip()

    assert head_after == head_before, "no commit/HEAD movement allowed"
    assert commits_after == commits_before, "no new commits allowed"
    assert branch_after == branch_before, "no branch switching allowed"
    # no new refs, no remote/push state
    refs = subprocess.run(
        ["git", "for-each-ref"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.splitlines()
    assert all(not r.startswith("refs/remotes/") for r in refs), refs
    assert not (repo / ".git" / "ORIG_HEAD").exists()
    # working tree remains exactly as before (wrapper state is git-ignored)
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, env=env, check=True,
        capture_output=True, text=True,
    ).stdout
    assert status.strip() == "", f"working tree must be unchanged: {status!r}"


def test_no_network_or_sandbox_configuration_mutation(
    sandbox: dict[str, Path],
) -> None:
    """The wrapper must not set up any sandboxing/trust/settings state and
    must not touch the network: the run produces only models.json, and
    the caller's sandbox/settings/trust files are byte-identical after."""
    seed: Path = sandbox["seed"]
    sandbox_before = (seed / "settings.json").read_bytes()
    trust_before = (seed / "trust.json").read_bytes()

    log = capture_path(sandbox, "sandbox")
    proc = run_wrapper(
        sandbox, ["--class", "smoke", "--", "task"],
        {"DELEGATE_CAPTURE": str(log)},
    )
    assert proc.returncode == 0, proc.stderr
    delegate_log(log)

    # caller's sandbox/trust configuration untouched
    assert (seed / "settings.json").read_bytes() == sandbox_before
    assert (seed / "trust.json").read_bytes() == trust_before

    # no configuration of ANY kind materialized in the Pi agent dir:
    # only models.json, no settings/trust/auth/session files
    agent_contents = sorted(p.name for p in sandbox["agent_dir"].iterdir())
    assert agent_contents == ["models.json"], agent_contents
    forbidden = {"settings.json", "trust.json", "auth.json", "sessions",
                 "credentials.json"}
    assert not (forbidden & set(agent_contents))

    # no network artifacts: wrapper output shows no endpoint, no download
    output = proc.stdout + proc.stderr
    for token in ("http://", "https://", "Downloading", "ollama"):
        assert token not in output, f"unexpected network-ish output: {token!r}"
