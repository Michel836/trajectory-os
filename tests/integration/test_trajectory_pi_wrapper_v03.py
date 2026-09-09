"""Integration tests for the trajectory-pi V0.3 wrapper (issue #148).

Every test drives the real ``scripts/trajectory-pi`` wrapper inside an
isolated temporary Git repository with a deterministic fake ``pi`` on
PATH (agent mode emulates file work; reviewer mode emits a configurable
verdict). The reviewer fake is read-only unless a test explicitly
configures a mutation to prove staleness detection.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "scripts" / "trajectory-pi"

FAKE_PI = r'''#!/usr/bin/env bash
# The fake pi is ONLY ever the writable agent. Any invocation is logged
# so tests can prove the reviewer is a separate direct Ollama call and
# does NOT launch a second Pi process.
if [[ -n "${FAKE_PI_INVOCATION_LOG:-}" ]]; then
  printf 'AGENT\n' >> "$FAKE_PI_INVOCATION_LOG"
fi

# ---------------- agent mode ----------------
if [[ "${FAKE_PI_TRAP_INT:-0}" == "1" ]]; then
  trap 'printf INT > .fake-pi-int; exit 130' INT
fi

if [[ -n "${FAKE_PI_APPEND_FILE:-}" ]]; then
  printf 'AGENT_EDIT\n' >> "${FAKE_PI_APPEND_FILE}"
fi
if [[ "${FAKE_PI_GIT_STATUS:-0}" == "1" ]]; then
  # Emulate an external stat-cache/index refresh: touch a CLEAN tracked
  # file (other.py; content and staged blob unchanged) and run a
  # semantically neutral git command (`git add <identical file>`).
  # This rewrites .git/index (stat-cache refresh — the same physical
  # change that stat-refreshing observation commands like `git status`
  # cause on git versions that rewrite the index from status) WITHOUT
  # changing what is actually staged: `git ls-files --stage` output stays
  # byte-identical. This is exactly the raw-vs-semantic index case.
  touch other.py
  git add other.py >/dev/null 2>&1 || true
fi
if [[ "${FAKE_PI_STAGE:-0}" == "1" ]]; then
  # agent intentionally stages its work into the REAL git index
  git add -A
fi
if [[ -n "${FAKE_PI_CREATE_FILE:-}" ]]; then
  mkdir -p "$(dirname "${FAKE_PI_CREATE_FILE}")"
  if [[ -n "${FAKE_PI_CREATE_HEX:-}" ]]; then
    python3 -c 'import sys; open(sys.argv[1],"wb").write(bytes.fromhex(sys.argv[2]))' \
      "${FAKE_PI_CREATE_FILE}" "${FAKE_PI_CREATE_HEX}"
  else
    printf '%s\n' "${FAKE_PI_CREATE_CONTENT:-hello}" > "${FAKE_PI_CREATE_FILE}"
  fi
fi
if [[ -n "${FAKE_PI_WS_FILE:-}" ]]; then
  printf 'trailing space   \n' > "${FAKE_PI_WS_FILE}"
fi
if [[ -n "${FAKE_PI_DELETE_FILE:-}" ]]; then
  rm -f "${FAKE_PI_DELETE_FILE}"
fi
if [[ -n "${FAKE_PI_RENAME:-}" ]]; then
  old="${FAKE_PI_RENAME%% *}"
  new="${FAKE_PI_RENAME#* }"
  mv "$old" "$new"
fi
if [[ -n "${FAKE_PI_CHMOD_FILE:-}" ]]; then
  chmod "${FAKE_PI_CHMOD:-755}" "${FAKE_PI_CHMOD_FILE}"
fi
if [[ -n "${FAKE_PI_LINK:-}" ]]; then
  mkdir -p "$(dirname "${FAKE_PI_LINK}")"
  ln -sf "${FAKE_PI_LINK_TARGET:-tracked.py}" "${FAKE_PI_LINK}"
fi
if [[ -n "${FAKE_PI_RESTORE_FILE:-}" ]]; then
  printf '%s' "${FAKE_PI_RESTORE_CONTENT:-}" > "${FAKE_PI_RESTORE_FILE}"
fi
if [[ -n "${FAKE_PI_BULK:-}" ]]; then
  # bulk work with real content so build_snapshot has a non-trivial
  # time window (diff over several tens of MB)
  mkdir -p bulk
  python3 -c '
import sys
n = int(sys.argv[1])
line = "fillerline" * 256 + "\n"  # 4 KB, no trailing whitespace
for i in range(n):
    with open("bulk/f%04d.txt" % i, "w") as fh:
        fh.write(line * 21)           # ~86 KB per file (~43 MB total at 500)
' "$FAKE_PI_BULK"
fi
if [[ "${FAKE_PI_COMMIT:-0}" == "1" ]]; then
  # Deterministic HEAD movement barrier.
  #
  # The wrapper's observations run with GIT_OPTIONAL_LOCKS=0 and therefore
  # never acquire the real index lock; a real agent hit with transient
  # index-lock contention (from other concurrent git activity) retries;
  # emulate exactly that. This is a barrier (wait for a contending git to
  # release the lock), NOT an arbitrary fixed sleep, so the commit lands
  # deterministically and HEAD genuinely moves inside the intended run
  # window.
  #
  # The production HEAD-movement check in the wrapper is left completely
  # untouched -- we only make the fake agent robust to the (legitimate) lock
  # contention that its own concurrent git invocation can see.
  committed=0
  for _ in $(seq 1 40); do
    if git add -A >/dev/null 2>&1 \
       && git commit -m "agent commit" --quiet >/dev/null 2>&1; then
      committed=1
      break
    fi
    # transient index.lock contention (or a commit that found the tree clean
    # because the append has not flushed yet): yield briefly and retry
    sleep 0.05
  done
  if [[ "$committed" != "1" ]]; then
    # Never let the test pass silently without a real HEAD movement.
    printf 'FAKE_PI: could not commit (index lock contention)\n' >&2
    exit 1
  fi
fi

# bash defers trapped-signal handlers until the current command finishes,
# so sleep in BEATS (0.2s): trapped INT/TERM is observed within one beat,
# mirroring how a real (node-based) pi reacts to signals promptly.
if [[ -n "${FAKE_PI_SLEEP:-}" && "${FAKE_PI_SLEEP}" != "0" ]]; then
  FAKE_DEADLINE=$(( SECONDS + FAKE_PI_SLEEP ))
  while (( SECONDS < FAKE_DEADLINE )); do
    sleep 0.2
  done
elif [[ -n "${FAKE_PI_SLEEP:-}" && "${FAKE_PI_SLEEP}" != "0.2" ]]; then
  sleep "${FAKE_PI_SLEEP}"
fi

if [[ "${FAKE_PI_RC:-0}" != "0" ]]; then
  exit "${FAKE_PI_RC}"
fi

printf 'HANDOFF\n'
printf 'TASK: fake agent task\n'
printf 'RESULT: complete\n'
# Issue #152 regression knobs (all optional; defaults keep the
# historical behavior):
#   FAKE_PI_MARKER - the exact completion marker line to print
#                    (empty string = no marker at all)
#   FAKE_PI_TAIL   - extra output emitted AFTER the marker (makes the
#                    marker non-terminal)
printf '%s\n' "${FAKE_PI_MARKER-FEATURE_IMPLEMENTED_COMPLETE}"
if [[ -n "${FAKE_PI_TAIL:-}" ]]; then
  printf '%s\n' "$FAKE_PI_TAIL"
fi
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

REVIEW_FAIL = (
    "VERDICT: FAIL\n"
    "\n"
    "BLOCKERS:\n"
    "- tracked.py changes behavior without tests\n"
    "\n"
    "MAJORS:\n"
    "- none\n"
    "\n"
    "MINORS:\n"
    "- none\n"
    "\n"
    "FINAL RECOMMENDATION: FIX\n"
)


REVIEW_CONTRADICTORY = {
    # Every response below claims PASS + GO COMMIT but contains a real
    # blocking finding, a missing, or a duplicated required section.
    # None of them may ever be parsed as PASS / grant READY_FOR_COMMIT.
    "con-blocker": (
        "VERDICT: PASS\n"
        "\n"
        "BLOCKERS:\n"
        "- concrete release-blocking defect\n"
        "\n"
        "MAJORS:\n"
        "- none\n"
        "\n"
        "MINORS:\n"
        "- none\n"
        "\n"
        "FINAL RECOMMENDATION: GO COMMIT\n"
    ),
    "con-major": (
        "VERDICT: PASS\n"
        "\n"
        "BLOCKERS:\n"
        "- none\n"
        "\n"
        "MAJORS:\n"
        "- concrete release-blocking defect\n"
        "\n"
        "MINORS:\n"
        "- none\n"
        "\n"
        "FINAL RECOMMENDATION: GO COMMIT\n"
    ),
    "con-both": (
        "VERDICT: PASS\n"
        "\n"
        "BLOCKERS:\n"
        "- blocker finding\n"
        "\n"
        "MAJORS:\n"
        "- major finding\n"
        "\n"
        "MINORS:\n"
        "- none\n"
        "\n"
        "FINAL RECOMMENDATION: GO COMMIT\n"
    ),
    "con-mixed-none": (
        "VERDICT: PASS\n"
        "\n"
        "BLOCKERS:\n"
        "- None\n"
        "- secret committed in patch\n"
        "\n"
        "MAJORS:\n"
        "- none\n"
        "\n"
        "MINORS:\n"
        "- none\n"
        "\n"
        "FINAL RECOMMENDATION: GO COMMIT\n"
    ),
    "con-missing-blockers": (
        "VERDICT: PASS\n"
        "\n"
        "MAJORS:\n"
        "- none\n"
        "\n"
        "MINORS:\n"
        "- none\n"
        "\n"
        "FINAL RECOMMENDATION: GO COMMIT\n"
    ),
    "con-missing-majors": (
        "VERDICT: PASS\n"
        "\n"
        "BLOCKERS:\n"
        "- none\n"
        "\n"
        "MINORS:\n"
        "- none\n"
        "\n"
        "FINAL RECOMMENDATION: GO COMMIT\n"
    ),
    "con-duplicated-blockers": (
        "VERDICT: PASS\n"
        "\n"
        "BLOCKERS:\n"
        "- None\n"
        "\n"
        "BLOCKERS:\n"
        "- None\n"
        "\n"
        "MAJORS:\n"
        "- none\n"
        "\n"
        "MINORS:\n"
        "- none\n"
        "\n"
        "FINAL RECOMMENDATION: GO COMMIT\n"
    ),
    # Strict parser structure completion (issue #148): prose coexisting
    # with the empty marker in BLOCKERS/MAJORS is a real finding even
    # when a '- None' line is also present, and the canonical headings
    # must appear exactly once in canonical order.
    "con-prose-before-none-majors": (
        "VERDICT: PASS\n"
        "\n"
        "BLOCKERS:\n"
        "- none\n"
        "\n"
        "MAJORS:\n"
        "There is a concrete release-blocking defect.\n"
        "- None\n"
        "\n"
        "MINORS:\n"
        "- none\n"
        "\n"
        "FINAL RECOMMENDATION: GO COMMIT\n"
    ),
    "con-prose-after-none-majors": (
        "VERDICT: PASS\n"
        "\n"
        "BLOCKERS:\n"
        "- none\n"
        "\n"
        "MAJORS:\n"
        "- None\n"
        "extra prose finding\n"
        "\n"
        "MINORS:\n"
        "- none\n"
        "\n"
        "FINAL RECOMMENDATION: GO COMMIT\n"
    ),
    "con-prose-before-none-blockers": (
        "VERDICT: PASS\n"
        "\n"
        "BLOCKERS:\n"
        "Real release-blocking defect.\n"
        "- None\n"
        "\n"
        "MAJORS:\n"
        "- none\n"
        "\n"
        "MINORS:\n"
        "- none\n"
        "\n"
        "FINAL RECOMMENDATION: GO COMMIT\n"
    ),
    "con-wrong-heading-order": (
        "VERDICT: PASS\n"
        "\n"
        "MAJORS:\n"
        "- none\n"
        "\n"
        "BLOCKERS:\n"
        "- none\n"
        "\n"
        "MINORS:\n"
        "- none\n"
        "\n"
        "FINAL RECOMMENDATION: GO COMMIT\n"
    ),
}


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
    (r / "other.py").write_text("other = 1\n")
    (r / ".gitignore").write_text(".trajectory-pi/\n")
    git(r, "add", "-A")
    git(r, "commit", "-m", "init", "--quiet")
    git(r, "checkout", "-b", "feature/test")

    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    return r


def run(
    repo: Path,
    env: dict[str, str] | None = None,
    *args: str,
    timeout: int = 180,
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


def canonical_block_reported(proc: subprocess.CompletedProcess) -> None:
    """All canonical V0.3 report lines must be present (in order)."""
    keys = [
        "HEAD BEFORE:",
        "HEAD AFTER:",
        "SEMANTIC STAGING AFTER AGENT:",
        "SEMANTIC STAGING FINAL:",
        "RAW INDEX AFTER AGENT:",
        "RAW INDEX FINAL:",
        "SNAPSHOT/REVIEW INDEX SAFETY:",
        "WORKTREE SNAPSHOT:",
        "CHANGED FILES:",
        "TRACKED MODIFIED:",
        "UNTRACKED:",
        "DELETED/RENAMED:",
        "PATCH SHA256:",
        "DIFF CHECK:",
        "INDEPENDENT REVIEW:",
        "REVIEWED PATCH SHA256:",
        "POST-REVIEW VERIFY:",
        "FINAL PATCH SHA256:",
        "REVIEW VERDICT:",
        "DECISION REQUIRED:",
    ]
    pos = -1
    for key in keys:
        nxt = proc.stdout.find(key + " ", pos + 1)
        assert nxt != -1, f"canonical line '{key}' missing or out of order"
        pos = nxt


def patch_sha(repo: Path, rd: Path) -> str:
    data = Path(rd / "worktree.patch").read_bytes()
    import hashlib

    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Snapshot completeness
# ---------------------------------------------------------------------------


def test_tracked_modification_full_artifacts(repo: Path) -> None:
    proc = run(
        repo,
        {"FAKE_PI_APPEND_FILE": "tracked.py"},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review", "--", "edit tracked.py",
    )
    assert proc.returncode == 0, proc.stdout
    rd = run_dir(repo, proc)

    for artifact in (
        "worktree.patch",
        "worktree.patch.sha256",
        "worktree-stat.txt",
        "worktree-numstat.txt",
        "worktree-status.txt",
        "worktree-files.txt",
        "worktree-metadata.txt",
        "diff-check.txt",
        "meta.txt",
        "pi.log",
    ):
        assert (rd / artifact).exists() or (rd / artifact).is_file(), artifact

    patch = (rd / "worktree.patch").read_text()
    assert "tracked.py" in patch

    stat = (rd / "worktree-stat.txt").read_text()
    assert "tracked.py | 1 +" in stat

    numstat = (rd / "worktree-numstat.txt").read_text()
    assert re.search(r"^1\t0\ttracked\.py$", numstat, re.M), numstat

    files = (rd / "worktree-files.txt").read_text()
    assert files.startswith("M"), files

    metadata = (rd / "worktree-metadata.txt").read_text()
    assert "snapshot_status=COMPLETE" in metadata
    assert "modified=1" in metadata

    diff_check = (rd / "diff-check.txt").read_text()
    assert "DIFF CHECK: PASS" in diff_check

    assert line(proc, "CHANGED FILES") == "1"
    assert line(proc, "TRACKED MODIFIED") == "1"
    assert line(proc, "UNTRACKED") == "0"
    assert line(proc, "DIFF CHECK") == "PASS"
    assert line(proc, "SEMANTIC STAGING AFTER AGENT") == "UNCHANGED"
    assert line(proc, "SEMANTIC STAGING FINAL") == "UNCHANGED"
    assert line(proc, "RAW INDEX AFTER AGENT") == "UNCHANGED"
    assert line(proc, "RAW INDEX FINAL") == "UNCHANGED"
    assert line(proc, "SNAPSHOT/REVIEW INDEX SAFETY") == "PASS"
    canonical_block_reported(proc)
    assert line(proc, "PATCH SHA256") == patch_sha(repo, rd)


def test_untracked_file_in_every_artifact(repo: Path) -> None:
    proc = run(
        repo,
        {"FAKE_PI_CREATE_FILE": "sub/dir/new.py"},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review", "--", "create new.py",
    )
    assert proc.returncode == 0, proc.stdout
    rd = run_dir(repo, proc)

    patch = (rd / "worktree.patch").read_text()
    assert "sub/dir/new.py" in patch
    assert "new file" in patch

    stat = (rd / "worktree-stat.txt").read_text()
    assert "sub/dir/new.py" in stat

    numstat = (rd / "worktree-numstat.txt").read_text()
    assert "sub/dir/new.py" in numstat

    status = (rd / "worktree-status.txt").read_text()
    assert re.search(r"\?\?\s+sub/", status)

    files = (rd / "worktree-files.txt").read_text()
    assert "A" in files and "sub/dir/new.py" in files

    check = (rd / "diff-check.txt").read_text()
    assert "DIFF CHECK: PASS" in check

    assert line(proc, "CHANGED FILES") == "1"
    assert line(proc, "UNTRACKED") == "1"


def test_deletion_tracked_file(repo: Path) -> None:
    proc = run(
        repo,
        {"FAKE_PI_DELETE_FILE": "other.py"},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review", "--", "delete other.py",
    )
    assert proc.returncode == 0, proc.stdout
    rd = run_dir(repo, proc)

    files = (rd / "worktree-files.txt").read_text()
    assert files.startswith("D"), files

    stat = (rd / "worktree-stat.txt").read_text()
    assert "other.py" in stat and "1 -" in stat

    assert line(proc, "CHANGED FILES") == "1"
    assert "deleted 1" in line(proc, "DELETED/RENAMED")


def test_rename(repo: Path) -> None:
    proc = run(
        repo,
        {"FAKE_PI_RENAME": "tracked.py renamed.py"},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review", "--", "rename tracked.py",
    )
    assert proc.returncode == 0, proc.stdout
    rd = run_dir(repo, proc)

    files = (rd / "worktree-files.txt").read_text()
    assert re.search(r"^R\d*", files, re.M), files
    assert "renamed.py" in files

    assert line(proc, "CHANGED FILES") == "1"
    assert "renamed 1" in line(proc, "DELETED/RENAMED")


def test_binary_new_file(repo: Path) -> None:
    blob = bytes(range(64))  # mixed, includes NUL
    proc = run(
        repo,
        {
            "FAKE_PI_CREATE_FILE": "assets/blob.bin",
            "FAKE_PI_CREATE_HEX": blob.hex(),
        },
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review", "--", "add binary asset",
    )
    assert proc.returncode == 0, proc.stdout
    rd = run_dir(repo, proc)

    patch_bytes = (rd / "worktree.patch").read_bytes()
    patch_text = patch_bytes.decode("utf-8", "replace")
    assert "assets/blob.bin" in patch_text
    assert "GIT binary patch" in patch_text, patch_text[:400]

    # the patch must reference a blob whose content is exactly the file:
    # that blob identity is what --binary content reconstructs.
    blob_sha = subprocess.run(
        ["git", "hash-object", "assets/blob.bin"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    m = re.search(r"index 0+[.][.](?P<sha>[0-9a-f]{40})", patch_text)
    assert m, patch_text[:400]
    assert m.group("sha") == blob_sha

    assert line(proc, "CHANGED FILES") == "1"
    assert line(proc, "PATCH SHA256") == patch_sha(repo, rd)


def test_executable_bit_change(repo: Path) -> None:
    proc = run(
        repo,
        {"FAKE_PI_CHMOD_FILE": "tracked.py"},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review", "--", "make executable",
    )
    assert proc.returncode == 0, proc.stdout
    rd = run_dir(repo, proc)

    patch = (rd / "worktree.patch").read_text()
    assert "mode" in patch, patch
    assert line(proc, "CHANGED FILES") == "1"


def test_symlink(repo: Path) -> None:
    proc = run(
        repo,
        {"FAKE_PI_LINK": "links/alias"},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review", "--", "add symlink",
    )
    assert proc.returncode == 0, proc.stdout
    rd = run_dir(repo, proc)

    files = (rd / "worktree-files.txt").read_text()
    assert "links/alias" in files
    patch = (rd / "worktree.patch").read_text()
    assert "links/alias" in patch
    assert line(proc, "CHANGED FILES") == "1"


def test_unusual_filenames_round_trip(repo: Path) -> None:
    names = ["wei  rd double-space.txt", "wei rd\ttab.txt", "ünïcødé.txt"]
    import shutil as _shutil

    fake_pi = _shutil.which("pi")
    assert fake_pi
    for name in names:
        subprocess.run(
            [fake_pi],
            cwd=repo,
            env={**os.environ, "FAKE_PI_CREATE_FILE": name},
            check=True,
            capture_output=True,
        )
        assert (repo / name).exists()

    proc = run(
        repo,
        {"FAKE_NOOP": "1"},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review", "--dirty-ok", "--", "inspect weird filenames",
    )
    assert proc.returncode == 0, proc.stdout
    rd = run_dir(repo, proc)

    files = (rd / "worktree-files.txt").read_text()
    for name in names:
        # inventory is %r-quoted: names with tabs/backslashes appear escaped
        assert name in files or repr(name) in files, (name, files)
    # Strong check: the patch must be applicable to a fresh repo at the same
    # HEAD and reproduce EXACTLY the special filenames and their content
    # (git C-style-quotes special paths in textual patches, so raw substring
    # matching would be wrong).
    import tempfile

    with tempfile.TemporaryDirectory(dir=repo.parent) as dest:
        subprocess.run(
            ["git", "clone", "--quiet", str(repo), dest],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "-C", dest, "apply", "--stat", str(rd / "worktree.patch")],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "-C", dest, "apply", str(rd / "worktree.patch")],
            check=True, capture_output=True, text=True,
        )
        for name in names:
            f = Path(dest) / name
            assert f.exists(), (name, sorted(os.listdir(dest)))
            assert f.read_text() == "hello\n"

    assert line(proc, "CHANGED FILES") == "3"
    assert line(proc, "PATCH SHA256") == patch_sha(repo, rd)


def test_patch_sha_reproducible_across_independent_snapshots(repo: Path) -> None:
    first = run(
        repo,
        {"FAKE_PI_CREATE_FILE": "stable.py"},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review", "--", "round one",
    )
    assert first.returncode == 0
    sha_one = line(first, "PATCH SHA256")

    second = run(
        repo,
        {"FAKE_NOOP": "1"},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review", "--dirty-ok", "--", "round two, same tree",
    )
    assert second.returncode == 0
    sha_two = line(second, "PATCH SHA256")

    assert sha_one == sha_two
    assert re.fullmatch(r"[0-9a-f]{64}", sha_one)


# ---------------------------------------------------------------------------
# Index safety
# ---------------------------------------------------------------------------


def test_real_index_unchanged_and_work_preserved(repo: Path) -> None:
    index_before = (repo / ".git" / "index").read_bytes()

    proc = run(
        repo,
        {"FAKE_PI_APPEND_FILE": "tracked.py"},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review", "--", "edit tracked.py",
    )
    assert proc.returncode == 0, proc.stdout

    index_after = (repo / ".git" / "index").read_bytes()
    assert index_before == index_after, "real git index must never be mutated"

    # nothing may be staged in the real index after the run
    assert (
        subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=repo, check=False
        ).returncode
        == 0
    ), "real index must contain no staged entries"

    # agent work must still be an UNSTAGED worktree change
    content = (repo / "tracked.py").read_text()
    assert "AGENT_EDIT" in content

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout
    assert " M tracked.py" in status, status

    assert line(proc, "SEMANTIC STAGING AFTER AGENT") == "UNCHANGED"
    assert line(proc, "SEMANTIC STAGING FINAL") == "UNCHANGED"
    assert line(proc, "RAW INDEX AFTER AGENT") == "UNCHANGED"
    assert line(proc, "RAW INDEX FINAL") == "UNCHANGED"
    assert line(proc, "SNAPSHOT/REVIEW INDEX SAFETY") == "PASS"


def test_pre_staged_work_preserved(repo: Path) -> None:
    # human pre-stages a tracked change before the agent starts
    (repo / "tracked.py").write_text("v = 42\n")
    git(repo, "add", "tracked.py")

    # semantic fingerprint of the EXACT pre-run staging state, computed
    # independently (NUL-safe byte stream of the index entries)
    expected_stream = subprocess.run(
        ["git", "ls-files", "--stage", "-z"], cwd=repo, check=True,
        capture_output=True,
    ).stdout
    import hashlib

    expected_sha = hashlib.sha256(expected_stream).hexdigest()

    proc = run(
        repo,
        {"FAKE_PI_CREATE_FILE": "newfile.py"},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review", "--dirty-ok", "--", "add newfile while staged change exists",
    )
    assert proc.returncode == 0, proc.stdout

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout
    assert "M  tracked.py" in status, f"pre-staged change lost: {status!r}"

    # the staged content must ALSO be captured in the snapshot
    rd = run_dir(repo, proc)
    assert "v = 42" in (rd / "worktree.patch").read_text()

    # semantic staging identity: the pre-staged work is exactly represented
    # in the fingerprint, and the agent (untracked newfile.py only) left
    # the staged semantics unchanged
    meta = (rd / "meta.txt").read_text()
    m = re.search(r"semantic_index_before=([0-9a-f]{64})", meta)
    assert m, "semantic_index_before missing in meta.txt"
    assert m.group(1) == expected_sha, (
        "semantic fingerprint must be the sha256 of the exact "
        "NUL-safe `git ls-files --stage -z` byte stream"
    )
    entries = [e for e in expected_stream.split(b"\0") if e]
    assert any(e.endswith(b"tracked.py") for e in entries)
    # newfile.py is untracked at capture time: NOT part of staging
    assert not any(e.endswith(b"newfile.py") for e in entries)
    assert line(proc, "SEMANTIC STAGING AFTER AGENT") == "UNCHANGED"
    assert line(proc, "SEMANTIC STAGING FINAL") == "UNCHANGED"
    assert line(proc, "SNAPSHOT/REVIEW INDEX SAFETY") == "PASS"


def test_temp_index_removed_on_success(repo: Path) -> None:
    proc = run(
        repo,
        {"FAKE_PI_APPEND_FILE": "tracked.py"},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--", "edit tracked.py",
    )
    assert proc.returncode == 0
    rd = run_dir(repo, proc)
    assert not (rd / ".snapshot-index").exists()
    assert not (rd / ".snapshot-index-verify").exists()


# ---------------------------------------------------------------------------
# Dirty-resume semantics
# ---------------------------------------------------------------------------


def test_dirty_resume_edited_file_counts(repo: Path) -> None:
    # start dirty (recovery scenario)
    (repo / "tracked.py").write_text("v = 1\npart1\n")

    proc = run(
        repo,
        {"FAKE_PI_APPEND_FILE": "tracked.py"},
        "--class", "repair", "--interval", "1", "--no-notify",
        "--no-review", "--dirty-ok", "--", "continue interrupted work",
    )
    assert proc.returncode == 0, proc.stdout

    assert "Baseline files 1" in proc.stdout
    content = (repo / "tracked.py").read_text()
    assert "part1" in content and "AGENT_EDIT" in content


def test_dirty_resume_restored_file(repo: Path) -> None:
    # start dirty, then the agent restores the file back to HEAD content
    (repo / "tracked.py").write_text("v = 1\nbroken\n")

    proc = run(
        repo,
        {"FAKE_PI_RESTORE_FILE": "tracked.py", "FAKE_PI_RESTORE_CONTENT": "v = 1\n"},
        "--class", "repair", "--interval", "1", "--no-notify",
        "--no-review", "--dirty-ok", "--", "restore tracked.py",
    )
    assert proc.returncode == 0, proc.stdout

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout
    assert status.strip() == "", f"file should be back to HEAD content: {status!r}"


# ---------------------------------------------------------------------------
# Complete diff check
# ---------------------------------------------------------------------------


def test_trailing_whitespace_blocks_ready(repo: Path) -> None:
    proc = run(
        repo,
        {"FAKE_PI_WS_FILE": "ws.py"},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review", "--", "create ws.py",
    )
    assert proc.returncode == 0, proc.stdout
    rd = run_dir(repo, proc)

    assert "ws.py" in (rd / "worktree-files.txt").read_text()
    assert "DIFF CHECK: FAIL" in (rd / "diff-check.txt").read_text()
    assert line(proc, "DIFF CHECK") == "FAIL"
    assert line(proc, "REPOSITORY STATE").upper() in {
        "BLOCKED",
    }, proc.stdout
    assert "GO COMMIT" not in re.search(r"DECISION REQUIRED: (.+)", proc.stdout).group(1)


# ---------------------------------------------------------------------------
# HEAD-movement protection
# ---------------------------------------------------------------------------


def test_head_change_blocks(repo: Path) -> None:
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()

    proc = run(
        repo,
        {"FAKE_PI_APPEND_FILE": "tracked.py", "FAKE_PI_COMMIT": "1"},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review", "--", "edit and (wrongly) commit",
    )

    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    assert head_after != head_before

    assert line(proc, "HEAD AFTER") != line(proc, "HEAD BEFORE")
    assert line(proc, "REPOSITORY STATE") == "BLOCKED"
    assert line(proc, "DECISION REQUIRED") == "HUMAN INTERVENTION REQUIRED"


# ---------------------------------------------------------------------------
# Index identity: SEMANTIC staging (authoritative) vs RAW bytes (diagnostic)
# Issue #148 final index-evidence fix.
# ---------------------------------------------------------------------------


def _index_identity(repo: Path) -> tuple[bytes, int]:
    p = repo / ".git" / "index"
    st = p.stat()
    return p.read_bytes(), st.st_mtime_ns


def test_raw_index_refresh_does_not_block_identical_semantic_staging(
    repo: Path,
) -> None:
    """Scenarios 1 + 5: the agent runs a plain `git status`, refreshing the
    stat cache and physically rewriting .git/index WITHOUT changing what is
    staged. The raw index bytes MUST change (deterministic here because the
    stat cache is stale after the agent edit); readiness must NOT be blocked
    or downgraded for that, and the report must present this as a pure
    diagnostic, never as a semantic mutation.
    """
    proc = run(
        repo,
        {"FAKE_PI_APPEND_FILE": "tracked.py", "FAKE_PI_GIT_STATUS": "1"},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review", "--", "edit tracked.py and casually run git status",
    )
    assert proc.returncode == 0, proc.stdout

    # semantic staging identity: identical everywhere
    assert line(proc, "SEMANTIC STAGING AFTER AGENT") == "UNCHANGED"
    assert line(proc, "SEMANTIC STAGING FINAL") == "UNCHANGED"
    # raw physical bytes: really changed (stale stat cache was refreshed)
    assert line(proc, "RAW INDEX AFTER AGENT") == "CHANGED"
    # snapshot/review layer left staging alone
    assert line(proc, "SNAPSHOT/REVIEW INDEX SAFETY") == "PASS"

    # NOT blocked by the raw byte refresh: fully diagnostic
    assert line(proc, "REPOSITORY STATE") == "READY_FOR_REVIEW"
    assert "RAW INDEX BYTES: CHANGED (stat-cache/physical refresh possible)" \
        in proc.stdout
    assert "SEMANTIC STAGING: UNCHANGED" in proc.stdout
    assert "SEMANTIC STAGING CHANGED DURING AGENT PHASE" not in proc.stdout
    assert "SEMANTIC STAGING CHANGED DURING SNAPSHOT/REVIEW PHASE" \
        not in proc.stdout


def test_wrapper_observations_never_rewrite_real_index(repo: Path) -> None:
    """Scenario 2: with a DELIBERATELY stale stat cache, the wrapper's own
    observational commands (git status / status_fingerprint / changed_count
    heartbeats) run with GIT_OPTIONAL_LOCKS=0 and must not refresh/rewrite
    the real index during the entire run. A plain (unguarded) `git status`
    would rewrite the stale index, so this scenario is discriminating.
    """
    # make the tracked file's stat identity stale BEFORE the run so any
    # index refresh by the wrapper would have to rewrite .git/index
    (repo / "tracked.py").write_bytes(b"v = 1\npre\n")
    before_bytes, before_mtime = _index_identity(repo)

    # agent sleeps long enough for several heartbeats (interval=1s) to run
    # their git status observations against the stale stat cache
    proc = run(
        repo,
        {"FAKE_PI_SLEEP": "3"},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--dirty-ok", "--no-review", "--", "let the wrapper observe repeatedly",
    )
    assert proc.returncode == 0, proc.stdout

    after_bytes, after_mtime = _index_identity(repo)
    assert before_bytes == after_bytes, (
        "wrapper observations (GIT_OPTIONAL_LOCKS=0) must not rewrite "
        "the real git index"
    )
    assert before_mtime == after_mtime, (
        "wrapper observations (GIT_OPTIONAL_LOCKS=0) must not rewrite "
        "the real git index"
    )

    assert line(proc, "RAW INDEX AFTER AGENT") == "UNCHANGED"
    assert line(proc, "RAW INDEX FINAL") == "UNCHANGED"
    assert line(proc, "SEMANTIC STAGING AFTER AGENT") == "UNCHANGED"
    assert line(proc, "SEMANTIC STAGING FINAL") == "UNCHANGED"
    assert line(proc, "SNAPSHOT/REVIEW INDEX SAFETY") == "PASS"


def test_observation_git_invocations_use_git_optional_locks_zero(
    repo: Path, monkeypatch
) -> None:
    """Mechanical proof for the GIT_OPTIONAL_LOCKS design: every one of the
    wrapper's observation-only git commands (git status / status_fingerprint
    / git ls-files) runs with GIT_OPTIONAL_LOCKS=0 — so they can never
    refresh or write the real index — while the temp-index mutating commands
    (read-tree / add -A under GIT_INDEX_FILE) are NOT given the flag
    (it must not be set blindly on commands that need ordinary locking).

    A git shim on PATH records, for every invocation, the
    GIT_OPTIONAL_LOCKS value and the command line."""
    real_git = subprocess.run(
        ["bash", "-c", "command -v git"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    shim_dir = repo.parent / "gitshim"
    shim_dir.mkdir(exist_ok=True)
    log = shim_dir / "git_env.log"
    log.unlink(missing_ok=True)
    shim = shim_dir / "git"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        + 'printf \'%s\\n\' "GOL=${GIT_OPTIONAL_LOCKS:-UNSET} cmd=$*" >> ' + repr(str(log)) + "\n"
        + 'exec ' + repr(real_git) + ' "$@"\n'
    )
    shim.chmod(0o755)

    current = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", f"{shim_dir}:{current}")

    proc = run(
        repo,
        {"FAKE_PI_APPEND_FILE": "tracked.py"},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review", "--", "edit tracked.py",
    )
    assert proc.returncode == 0, proc.stdout

    lines = log.read_text().splitlines()

    def cmds_with(prefix: str) -> list[tuple[str, str]]:
        out = []
        for ln in lines:
            m = re.match(r"GOL=(\S+) cmd=(.*)$", ln)
            assert m, f"unparsable shim log line: {ln!r}"
            gol, cmd = m.group(1), m.group(2)
            if cmd.startswith(prefix):
                out.append((gol, cmd))
        return out

    statuses = cmds_with("status")
    lsfiles = cmds_with("ls-files")
    assert statuses, "expected at least one `git status` observation"
    assert lsfiles, "expected at least one `git ls-files` observation"

    # observation-only commands ALWAYS under GIT_OPTIONAL_LOCKS=0
    for gol, cmd in statuses + lsfiles:
        assert gol == "0", (
            f"observation command {cmd!r} must run with GIT_OPTIONAL_LOCKS=0, "
            f"got {gol!r}"
        )

    # temp-index mutating commands: NOT given the flag (no blind setting)
    for gol, cmd in cmds_with("read-tree") + cmds_with("add -A"):
        assert gol == "UNSET", (
            f"{cmd!r} must not be given GIT_OPTIONAL_LOCKS blindly, got {gol!r}"
        )


def test_agent_semantic_staging_change_detected(repo: Path) -> None:
    """Scenario 3: the agent deliberately stages its work into the REAL git
    index (`git add -A`). The SEMANTIC staging identity therefore changed
    during the agent phase; the wrapper must detect it, attribute it to
    agent/external (not to the snapshot), and forbid READY_FOR_COMMIT.
    """
    proc = run(
        repo,
        {"FAKE_PI_APPEND_FILE": "tracked.py", "FAKE_PI_STAGE": "1"},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review", "--", "edit and stage tracked.py",
    )
    assert proc.returncode == 0, proc.stdout

    # staging really changed in the real index
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout
    assert "M  tracked.py" in status, f"staging not detected: {status!r}"

    # semantic identity: changed during the agent phase, and NOT changed
    # again by the snapshot/review phase (it left staging alone)
    assert line(proc, "SEMANTIC STAGING AFTER AGENT") == "CHANGED_BY_AGENT_OR_EXTERNAL"
    assert line(proc, "SEMANTIC STAGING FINAL") == "UNCHANGED"
    assert line(proc, "SNAPSHOT/REVIEW INDEX SAFETY") == "PASS"
    assert line(proc, "REPOSITORY STATE") == "NEEDS_REVIEW"
    assert line(proc, "DECISION REQUIRED") != "GO COMMIT"
    assert "SEMANTIC STAGING CHANGED DURING AGENT PHASE" in proc.stdout
    assert "by agent/external, not snapshot logic" in proc.stdout
    # no false attribution to the snapshot phase
    assert "SEMANTIC STAGING CHANGED DURING SNAPSHOT/REVIEW PHASE" \
        not in proc.stdout


# ---------------------------------------------------------------------------
# Direct independent review (separate from Pi)
# ---------------------------------------------------------------------------


class FakeOllama:
    """An isolated local Ollama ``/api/chat`` stand-in.

    It is the ONLY reviewer the wrapper reaches, so any reviewer traffic is
    provably on the direct Ollama path (not a second Pi process). It records
    every request and lets each test pick a reviewer scenario:
      pass / fail / malformed / timeout / error / mutate / stage-index /
      blank-content / missing-content / nonstring-content
    """

    TEXT = {
        "pass": REVIEW_PASS,
        "fail": REVIEW_FAIL,
        "malformed": "Reviewed; broadly OK but I have no structured verdict.",
        "mutate": REVIEW_PASS,
        "stage-index": REVIEW_PASS,
        "timeout": REVIEW_PASS,
        "blank-content": "   ",
        "nonstring-content": "   ",
    }
    TEXT.update(REVIEW_CONTRADICTORY)

    def __init__(self, repo: Path, mode: str = "pass",
                 sleep_s: float = 0.0) -> None:
        self.repo = repo
        self.mode = mode
        self.sleep_s = sleep_s
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(n).decode("utf-8", "replace")
                with outer.lock:
                    outer.requests.append(
                        {"path": self.path, "mode": outer.mode, "body": body}
                    )

                if outer.mode == "timeout":
                    time.sleep(outer.sleep_s)
                if outer.mode == "error":
                    self._send(500, b"reviewer exploded")
                    return
                if outer.mode in ("mutate", "stage-index"):
                    outer._hostile()

                # Ollama /api/chat success shape: the visible review text
                # lives in message.content only (reasoning content, when
                # present, must never be used as the verdict).
                if outer.mode == "blank-content":
                    response = {"model": "fake", "message": {"role": "assistant",
                               "content": outer.TEXT[outer.mode],
                               "thinking": REVIEW_PASS}, "done": True}
                elif outer.mode == "missing-content":
                    response = {"model": "fake", "message": {"role": "assistant",
                               "thinking": REVIEW_PASS}, "done": True}
                elif outer.mode == "nonstring-content":
                    response = {"model": "fake", "message": {"role": "assistant",
                               "content": 42, "thinking": REVIEW_PASS},
                               "done": True}
                else:
                    response = {"model": "fake", "message": {"role": "assistant",
                               "content": outer.TEXT[outer.mode]},
                               "done": True}

                self._send(
                    200,
                    json.dumps(response).encode("utf-8"),
                )

            def _send(self, status: int, data: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):  # silence request logging
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True,
        )
        self.thread.start()

    def _hostile(self) -> None:
        if self.mode == "mutate":
            with open(os.path.join(self.repo, "other.py"), "a") as fh:
                fh.write("REVIEWER_MUTATION\n")
        elif self.mode == "stage-index":
            # hostile: stage the worktree into the REAL git index without
            # changing any worktree content (all patch SHAs stay identical)
            subprocess.run(
                ["git", "add", "-A"], cwd=self.repo, capture_output=True,
            )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def count(self, path: str) -> int:
        with self.lock:
            return sum(1 for r in self.requests if r.get("path") == path)

    def request_bodies(self) -> list[str]:
        with self.lock:
            return [r["body"] for r in self.requests]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def review_env(repo: Path, server: FakeOllama, **extra: str) -> dict[str, str]:
    """Env for a review-enabled run: point the direct reviewer at the fake
    Ollama and record every fake-pi (agent) invocation OUTSIDE the repo."""
    env = {
        "TP_REVIEWER_URL": server.url,
        "FAKE_PI_APPEND_FILE": "tracked.py",
        "FAKE_PI_INVOCATION_LOG": str(repo.parent / "pi_invocations.log"),
    }
    env.update(extra)
    return env


def _fresh_invocation_log(repo: Path) -> Path:
    path = repo.parent / "pi_invocations.log"
    path.unlink(missing_ok=True)
    return path


def _agent_invocations(repo: Path) -> int:
    path = repo.parent / "pi_invocations.log"
    if not path.exists():
        return 0
    return sum(
        1 for line in path.read_text().splitlines() if line.strip() == "AGENT"
    )


def test_review_pass_yields_ready_for_commit(repo: Path) -> None:
    server = FakeOllama(repo, mode="pass")
    _fresh_invocation_log(repo)
    try:
        proc = run(
            repo, review_env(repo, server),
            "--class", "smoke", "--review", "--interval", "1", "--no-notify",
            "--", "edit tracked.py",
        )
    finally:
        server.close()

    assert proc.returncode == 0, proc.stdout
    rd = run_dir(repo, proc)

    assert line(proc, "INDEPENDENT REVIEW") == "PASS"
    assert line(proc, "REVIEW VERDICT") == "GO COMMIT"
    assert line(proc, "REPOSITORY STATE") == "READY_FOR_COMMIT"
    assert line(proc, "DECISION REQUIRED") == "GO COMMIT"

    # final identity chain + index safety (semantic authoritative, raw
    # diagnostic only)
    assert line(proc, "POST-REVIEW VERIFY") == "PASS"
    assert line(proc, "FINAL PATCH SHA256") == line(proc, "PATCH SHA256")
    assert line(proc, "SEMANTIC STAGING AFTER AGENT") == "UNCHANGED"
    assert line(proc, "SEMANTIC STAGING FINAL") == "UNCHANGED"
    assert line(proc, "RAW INDEX AFTER AGENT") == "UNCHANGED"
    assert line(proc, "RAW INDEX FINAL") == "UNCHANGED"
    assert line(proc, "SNAPSHOT/REVIEW INDEX SAFETY") == "PASS"

    # verdict must be bound to the exact reviewed patch identity
    assert line(proc, "REVIEWED PATCH SHA256") == line(proc, "PATCH SHA256")
    assert line(proc, "REVIEWED PATCH SHA256") == patch_sha(repo, rd)

    meta = (rd / "review-meta.txt").read_text()
    assert "reviewer_mode=direct-ollama" in meta
    assert "review_status=PASS" in meta
    assert "reviewed_patch_sha256=" in meta

    final_meta = (rd / "final-verify-meta.txt").read_text()
    assert "final_verify_status=PASS" in final_meta
    assert "canonical_patch_sha256=" in final_meta
    assert "final_patch_sha256=" in final_meta


def test_review_fail_blocks(repo: Path) -> None:
    server = FakeOllama(repo, mode="fail")
    try:
        proc = run(
            repo, review_env(repo, server),
            "--class", "smoke", "--review", "--interval", "1", "--no-notify",
            "--", "edit tracked.py",
        )
    finally:
        server.close()
    assert proc.returncode == 0, proc.stdout

    assert "FAIL" in line(proc, "INDEPENDENT REVIEW")
    assert line(proc, "REVIEW VERDICT") == "FIX"
    assert line(proc, "REPOSITORY STATE") == "NEEDS_REVIEW"
    assert line(proc, "DECISION REQUIRED") == "HUMAN REVIEW REQUIRED"


def test_review_malformed_never_passes(repo: Path) -> None:
    server = FakeOllama(repo, mode="malformed")
    try:
        proc = run(
            repo, review_env(repo, server),
            "--class", "smoke", "--review", "--interval", "1", "--no-notify",
            "--", "edit tracked.py",
        )
    finally:
        server.close()
    assert proc.returncode == 0, proc.stdout

    assert "REJECTED" in line(proc, "INDEPENDENT REVIEW")
    assert line(proc, "REPOSITORY STATE") == "NEEDS_REVIEW"
    assert line(proc, "DECISION REQUIRED") == "HUMAN REVIEW REQUIRED"


@pytest.mark.parametrize(
    ["mode"],
    [[key] for key in sorted(REVIEW_CONTRADICTORY)],
    ids=sorted(REVIEW_CONTRADICTORY),
)
def test_contradictory_reviewer_response_never_ready_for_commit(
    repo: Path, mode: str
) -> None:
    """A reviewer may say VERDICT: PASS + GO COMMIT while simultaneously
    listing real blockers/majors (real local Ollama reviews have done
    exactly this). Such a contradictory response must NEVER be parsed as
    PASS and READY_FOR_COMMIT must be impossible: the review reports
    non-PASS and the repository state is NEEDS_REVIEW, never
    READY_FOR_COMMIT."""
    server = FakeOllama(repo, mode=mode)
    try:
        proc = run(
            repo, review_env(repo, server),
            "--class", "smoke", "--review", "--interval", "1", "--no-notify",
            "--", "edit tracked.py",
        )
    finally:
        server.close()
    assert proc.returncode == 0, proc.stdout

    review_status = line(proc, "INDEPENDENT REVIEW")
    assert review_status != "PASS", (
        "a contradictory reviewer response was treated as review PASS"
    )
    assert "PASS" not in review_status.upper().split(), review_status
    state = line(proc, "REPOSITORY STATE")
    assert "READY_FOR_COMMIT" not in state, (
        f"contradictory reviewer response (mode={mode}) yielded: {state}"
    )
    assert state == "NEEDS_REVIEW"
    assert line(proc, "DECISION REQUIRED") != "GO COMMIT"


def test_review_timeout_never_passes(repo: Path) -> None:
    server = FakeOllama(repo, mode="timeout", sleep_s=4)
    try:
        proc = run(
            repo, review_env(repo, server),
            "--class", "smoke", "--review", "--interval", "1", "--no-notify",
            "--review-timeout", "1",
            "--", "edit tracked.py",
        )
    finally:
        server.close()
    assert proc.returncode == 0, proc.stdout

    assert line(proc, "INDEPENDENT REVIEW") == "TIMEOUT"
    assert line(proc, "REPOSITORY STATE") == "NEEDS_REVIEW"
    assert line(proc, "DECISION REQUIRED") != "GO COMMIT"


def test_reviewer_error_never_passes(repo: Path) -> None:
    server = FakeOllama(repo, mode="error")
    try:
        proc = run(
            repo, review_env(repo, server),
            "--class", "smoke", "--review", "--interval", "1", "--no-notify",
            "--", "edit tracked.py",
        )
    finally:
        server.close()
    assert proc.returncode == 0, proc.stdout

    assert line(proc, "INDEPENDENT REVIEW") == "ERROR"
    assert line(proc, "REPOSITORY STATE") == "NEEDS_REVIEW"


@pytest.mark.parametrize("mode", ["blank-content", "missing-content",
                                 "nonstring-content"])
def test_blank_or_missing_visible_content_never_passes(
    repo: Path, mode: str,
) -> None:
    """When message.content is blank / missing / non-string — even if a
    PASS-shaped reasoning blob is present in the response — the reviewer
    MUST end ERROR/REJECTED, never PASS, and never READY_FOR_COMMIT.
    This proves the verdict is derived ONLY from visible message.content."""
    server = FakeOllama(repo, mode=mode)
    _fresh_invocation_log(repo)
    try:
        proc = run(
            repo, review_env(repo, server),
            "--class", "smoke", "--review", "--interval", "1", "--no-notify",
            "--", "edit tracked.py",
        )
    finally:
        server.close()
    assert proc.returncode == 0, proc.stdout

    review_line = line(proc, "INDEPENDENT REVIEW")
    assert review_line in ("ERROR", "REJECTED"), review_line
    assert line(proc, "REPOSITORY STATE") == "NEEDS_REVIEW"
    assert line(proc, "DECISION REQUIRED") != "GO COMMIT"
    # the request still went to the direct /api/chat endpoint exactly once
    assert server.count("/api/chat") == 1, server.requests
    # fake pi still invoked exactly once (agent only)
    assert _agent_invocations(repo) == 1


def test_review_stale_when_worktree_changes_during_review(repo: Path) -> None:
    server = FakeOllama(repo, mode="mutate")
    try:
        proc = run(
            repo, review_env(repo, server),
            "--class", "smoke", "--review", "--interval", "1", "--no-notify",
            "--", "edit tracked.py",
        )
    finally:
        server.close()
    assert proc.returncode == 0, proc.stdout

    assert "STALE" in line(proc, "INDEPENDENT REVIEW")
    assert line(proc, "REPOSITORY STATE") == "NEEDS_REVIEW"
    assert line(proc, "DECISION REQUIRED") != "GO COMMIT"


def test_final_semantic_index_evidence_covers_review_phase(repo: Path) -> None:
    """The FINAL semantic staging fingerprint must cover the snapshot+review
    phase, not merely the agent phase. (Issue #148, tests 4 and 6.)

    The reviewer host (hostile stand-in) stages the worktree into the REAL
    git index but changes no worktree content: every patch SHA stays
    identical, the review stays PASS/FRESH, and final identity verify passes.
    Without the post-review SEMANTIC staging fingerprint the wrapper would
    (wrongly) emit READY_FOR_COMMIT. The raw index bytes also change here,
    but the authoritative gate is the semantic staging identity.
    """
    server = FakeOllama(repo, mode="stage-index")
    try:
        proc = run(
            repo, review_env(repo, server),
            "--class", "smoke", "--review", "--interval", "1", "--no-notify",
            "--", "edit tracked.py",
        )
    finally:
        server.close()
    assert proc.returncode == 0, proc.stdout

    # staging was untouched during the agent phase ...
    assert line(proc, "SEMANTIC STAGING AFTER AGENT") == "UNCHANGED"
    assert line(proc, "RAW INDEX AFTER AGENT") == "UNCHANGED"
    # ...but the reviewer phase mutated what is actually staged
    assert line(proc, "SEMANTIC STAGING FINAL") == "CHANGED_BY_SNAPSHOT_REVIEW_PHASE"
    assert line(proc, "RAW INDEX FINAL") == "CHANGED"
    assert line(proc, "SNAPSHOT/REVIEW INDEX SAFETY") == "FAIL"

    assert line(proc, "INDEPENDENT REVIEW") == "PASS"
    assert line(proc, "POST-REVIEW VERIFY") == "PASS"

    assert line(proc, "REPOSITORY STATE") == "NEEDS_REVIEW"
    assert line(proc, "DECISION REQUIRED") != "GO COMMIT"
    assert "SEMANTIC STAGING CHANGED DURING SNAPSHOT/REVIEW PHASE" in proc.stdout
    assert "NOT proven for this run" in proc.stdout
    assert "PROVEN for this run" not in proc.stdout


def test_final_identity_catches_post_review_mutation(repo: Path) -> None:
    """The final pre-readiness identity check closes the post-review TOCTOU
    window: a detached mutator fires only AFTER the wrapper has written
    review-meta.txt (i.e. after the review's freshness check) but BEFORE
    readiness is derived. READY_FOR_COMMIT must not be emitted.
    """
    server = FakeOllama(repo, mode="pass")
    runs_glob = ".trajectory-pi/runs/*/review-meta.txt"
    mutator = subprocess.Popen(
        ["bash", "-c",
         "until compgen -G \"$1\" | grep -q .; do sleep 0.002; done; "
         "printf 'LATE_MUTATION\\n' >> \"$2\"",
         "_", runs_glob, "other.py"],
        cwd=str(repo),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        proc = run(
            repo, review_env(repo, server, FAKE_PI_BULK="500"),
            "--class", "smoke", "--review", "--interval", "1", "--no-notify",
            "--", "edit tracked.py",
        )
    finally:
        server.close()
        mutator.wait(timeout=5)
    assert proc.returncode == 0, proc.stdout
    rd = run_dir(repo, proc)

    # review was PASS and FRESH at its own verification time...
    assert line(proc, "INDEPENDENT REVIEW") == "PASS"
    review_meta = (rd / "review-meta.txt").read_text()
    assert "review_status=PASS" in review_meta
    assert "review_stale=FRESH" in review_meta

    # ...but the final pre-readiness identity verification caught the late
    # mutation
    assert line(proc, "POST-REVIEW VERIFY") == "MISMATCH"
    final_meta = (rd / "final-verify-meta.txt").read_text()
    assert "final_verify_status=MISMATCH" in final_meta
    canon = re.search(r"canonical_patch_sha256=([0-9a-f]{64})", final_meta)
    final = re.search(r"final_patch_sha256=([0-9a-f]{64})", final_meta)
    assert canon and final
    assert canon.group(1) != final.group(1), (
        "initial and final patch shas must differ after the late mutation"
    )

    assert line(proc, "REPOSITORY STATE") == "NEEDS_REVIEW"
    assert line(proc, "DECISION REQUIRED") != "GO COMMIT"
    assert "POST-REVIEW IDENTITY MISMATCH" in proc.stdout
    assert canon.group(1) in proc.stdout
    assert final.group(1) in proc.stdout


def test_reviewer_traffic_goes_to_direct_ollama_not_second_pi(repo: Path) -> None:
    """FOCUSED separation proof: the reviewer hits the direct Ollama endpoint
    (with the exact patch identity AND the exact patch content in the request)
    while fake pi is invoked EXACTLY once (agent only) — never as a second Pi.
    """
    server = FakeOllama(repo, mode="pass")
    _fresh_invocation_log(repo)
    try:
        proc = run(
            repo, review_env(repo, server),
            "--class", "smoke", "--review", "--interval", "1", "--no-notify",
            "--", "edit tracked.py",
        )
    finally:
        server.close()
    assert proc.returncode == 0, proc.stdout
    rd = run_dir(repo, proc)

    # 1) The reviewer made exactly one direct Ollama call to /api/chat.
    assert server.count("/api/chat") == 1, server.requests
    assert server.count("/api/generate") == 0, server.requests
    bodies = server.request_bodies()
    assert len(bodies) == 1
    payload = json.loads(bodies[0])

    # 2) /api/chat transport contract.
    assert payload["stream"] is False
    assert payload["think"] is False
    assert "prompt" not in payload, "no /api/generate-style prompt key"
    # Ollama /api/chat expects generation parameters under "options",
    # NOT at the top level of the request body.
    assert "temperature" not in payload, "no root-level temperature"
    assert payload["options"]["temperature"] == 0
    assert payload["model"] == "qwen3.6:27b"
    assert len(payload["messages"]) == 1
    assert payload["messages"][0]["role"] == "user"
    prompt = payload["messages"][0]["content"]

    # 3) The exact reviewed patch identity and exact patch content travelled
    # in the reviewer request.
    sha = line(proc, "PATCH SHA256")
    assert sha in prompt
    patch_text = (rd / "worktree.patch").read_text()
    assert "tracked.py" in prompt
    assert "AGENT_EDIT" in prompt
    # the substantive patch body (present verbatim in the canonical patch)
    assert patch_text.split("\n", 1)[1][:60] in prompt

    # 4) fake pi was launched EXACTLY once (agent only). The reviewer was NOT
    # executed as a second Pi process.
    assert _agent_invocations(repo) == 1, _agent_invocations(repo)


def test_review_disabled_ready_for_review(repo: Path) -> None:
    proc = run(
        repo,
        {"FAKE_PI_APPEND_FILE": "tracked.py"},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review", "--", "edit tracked.py",
    )
    assert proc.returncode == 0, proc.stdout

    assert line(proc, "INDEPENDENT REVIEW") == "DISABLED"
    assert line(proc, "REPOSITORY STATE") == "READY_FOR_REVIEW"
    assert line(proc, "DECISION REQUIRED") == "HUMAN REVIEW REQUIRED"


# ---------------------------------------------------------------------------
# Review defaults per run class (smoke off, feature/complex/repair on)
# ---------------------------------------------------------------------------


def test_smoke_default_disables_review_and_ollama(repo: Path) -> None:
    """smoke + no flag => reviewer NOT run and Ollama NOT contacted."""
    server = FakeOllama(repo, mode="pass")
    try:
        env = {"FAKE_PI_APPEND_FILE": "tracked.py",
               "TP_REVIEWER_URL": server.url}
        proc = run(
            repo, env,
            "--class", "smoke", "--interval", "1", "--no-notify",
            "--", "edit tracked.py",
        )
    finally:
        server.close()
    assert proc.returncode == 0, proc.stdout

    assert line(proc, "INDEPENDENT REVIEW") == "DISABLED"
    assert line(proc, "REPOSITORY STATE") == "READY_FOR_REVIEW"
    # the direct review endpoint must have been contacted ZERO times
    assert server.count("/api/chat") == 0, server.requests


def test_smoke_review_flag_invokes_direct_ollama(repo: Path) -> None:
    server = FakeOllama(repo, mode="pass")
    _fresh_invocation_log(repo)
    try:
        proc = run(
            repo, review_env(repo, server),
            "--class", "smoke", "--review", "--interval", "1", "--no-notify",
            "--", "edit tracked.py",
        )
    finally:
        server.close()
    assert proc.returncode == 0, proc.stdout

    assert line(proc, "INDEPENDENT REVIEW") == "PASS"
    assert server.count("/api/chat") == 1
    assert _agent_invocations(repo) == 1


def test_feature_default_enables_review(repo: Path) -> None:
    server = FakeOllama(repo, mode="pass")
    try:
        proc = run(
            repo, review_env(repo, server),
            "--class", "feature", "--interval", "1", "--no-notify",
            "--", "edit tracked.py",
        )
    finally:
        server.close()
    assert proc.returncode == 0, proc.stdout

    assert line(proc, "INDEPENDENT REVIEW") == "PASS"
    assert server.count("/api/chat") == 1


def test_complex_default_enables_review(repo: Path) -> None:
    server = FakeOllama(repo, mode="pass")
    try:
        proc = run(
            repo, review_env(repo, server),
            "--class", "complex", "--interval", "1", "--no-notify",
            "--", "edit tracked.py",
        )
    finally:
        server.close()
    assert proc.returncode == 0, proc.stdout

    assert line(proc, "INDEPENDENT REVIEW") == "PASS"
    assert server.count("/api/chat") == 1


def test_repair_recovery_default_enables_review(repo: Path) -> None:
    server = FakeOllama(repo, mode="pass")
    try:
        proc = run(
            repo, review_env(repo, server),
            "--class", "repair", "--interval", "1", "--no-notify",
            "--", "edit tracked.py",
        )
    finally:
        server.close()
    assert proc.returncode == 0, proc.stdout

    assert line(proc, "INDEPENDENT REVIEW") == "PASS"
    assert server.count("/api/chat") == 1


# ---------------------------------------------------------------------------
# Validation hooks
# ---------------------------------------------------------------------------


def test_validation_pass_keeps_ready(repo: Path) -> None:
    server = FakeOllama(repo, mode="pass")
    try:
        proc = run(
            repo, review_env(repo, server),
            "--class", "smoke", "--review", "--interval", "1", "--no-notify",
            "--validate", "true",
            "--", "edit tracked.py",
        )
    finally:
        server.close()
    assert proc.returncode == 0, proc.stdout
    assert line(proc, "VALIDATION") == "PASS"
    assert line(proc, "REPOSITORY STATE") == "READY_FOR_COMMIT"


def test_validation_failure_blocks_ready_for_commit(repo: Path) -> None:
    server = FakeOllama(repo, mode="pass")
    try:
        proc = run(
            repo, review_env(repo, server),
            "--class", "smoke", "--review", "--interval", "1", "--no-notify",
            "--validate", "bash -c 'exit 7'",
            "--", "edit tracked.py",
        )
    finally:
        server.close()
    assert proc.returncode == 0, proc.stdout
    rd = run_dir(repo, proc)
    assert "RESULT: FAIL (exit 7)" in (rd / "validation-0.txt").read_text()
    assert line(proc, "VALIDATION") == "FAIL"
    assert line(proc, "REPOSITORY STATE") == "NEEDS_REVIEW"
    assert line(proc, "DECISION REQUIRED") != "GO COMMIT"


# ---------------------------------------------------------------------------
# Failures, signals, safety
# ---------------------------------------------------------------------------


def test_agent_failure_exit_preserved(repo: Path) -> None:
    proc = run(
        repo,
        {"FAKE_PI_RC": "1"},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review", "--", "fail immediately",
    )
    assert proc.returncode == 1

    assert line(proc, "AGENT CLASSIFICATION") == "AGENT_FAILED"
    assert line(proc, "REPOSITORY STATE") == "NEEDS_REVIEW"
    assert line(proc, "DECISION REQUIRED") == "HUMAN REVIEW REQUIRED"


def test_incomplete_run_needs_review(repo: Path) -> None:
    # rc=0 but no structured completion evidence in the log
    import shutil as _shutil

    fake = Path(_shutil.which("pi"))
    fake.write_text(
        FAKE_PI.replace(
            "FEATURE_IMPLEMENTED_COMPLETE", "just done, no marker"
        ).replace("HANDOFF", "notes")
    )
    fake.chmod(0o755)

    proc = run(
        repo,
        {"FAKE_PI_APPEND_FILE": "tracked.py"},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review", "--", "no handoff",
    )
    assert proc.returncode == 0
    assert line(proc, "AGENT CLASSIFICATION") == "INCOMPLETE_AGENT_RUN"
    assert line(proc, "REPOSITORY STATE") == "NEEDS_REVIEW"


def test_refuses_main_branch(repo: Path) -> None:
    git(repo, "checkout", "main")
    proc = run(
        repo,
        {"FAKE_PI_APPEND_FILE": "tracked.py"},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--", "run on main",
    )
    git(repo, "checkout", "feature/test")

    assert proc.returncode == 3
    assert "SAFETY STOP" in proc.stdout


def test_dirty_protection_without_flag(repo: Path) -> None:
    (repo / "tracked.py").write_text("v = 99\n")
    proc = run(
        repo,
        {"FAKE_PI_APPEND_FILE": "tracked.py"},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--", "dirty run without --dirty-ok",
    )
    assert proc.returncode == 4
    assert "SAFETY STOP" in proc.stdout


def test_sigint_forwarding_exit_code_and_cleanup(repo: Path) -> None:
    import shutil

    SIGINT_COPIES_BEFORE = bootstrap_copies()

    full_env = wrapper_env()
    process = subprocess.Popen(
        ["bash", str(WRAPPER),
         "--class", "smoke", "--interval", "1", "--no-notify",
         "--no-review", "--", "long agent work"],
        cwd=repo,
        env={**full_env, "FAKE_PI_SLEEP": "8", "FAKE_PI_TRAP_INT": "1"},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # the fake pi child sits in sleep(8) once started; wait for it to
    # exist as a process before signalling the wrapper.
    deadline = time.time() + 6
    while time.time() < deadline:
        if process.poll() is not None:
            break
        r = subprocess.run(
            ["pgrep", "-f", "fakebin/pi"], capture_output=True, check=False
        )
        if r.returncode == 0:
            break
        time.sleep(0.1)

    process.send_signal(signal.SIGINT)
    try:
        out, err = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        pytest.fail("wrapper did not exit after SIGINT")

    assert process.returncode == 130, f"rc={process.returncode}\n{out}\n{err}"

    # the pi child's trapped handler may complete a beat after the wrapper
    # returns; allow it up to 5s to write its marker
    deadline = time.time() + 5
    while time.time() < deadline and not (repo / ".fake-pi-int").exists():
        time.sleep(0.1)
    assert (repo / ".fake-pi-int").exists(), "signal must reach the pi child"

    # no commit was made
    count = subprocess.run(
        ["git", "rev-list", "--count", "main..HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    assert count == "0"

    # temporary index resources must be cleaned even on signal exit
    runs = list((repo / ".trajectory-pi" / "runs").glob("*"))
    assert runs, "run dir must exist"
    latest = max(runs, key=lambda p: p.name)
    assert not (latest / ".snapshot-index").exists()
    assert not (latest / ".snapshot-index-verify").exists()
    # the private immutable bootstrap copy must also be cleaned
    assert bootstrap_copies() - SIGINT_COPIES_BEFORE == set()
    shutil.rmtree(repo / ".trajectory-pi", ignore_errors=True)


def test_review_failure_cleanup(repo: Path) -> None:
    server = FakeOllama(repo, mode="error")
    try:
        proc = run(
            repo, review_env(repo, server),
            "--class", "smoke", "--review", "--interval", "1", "--no-notify",
            "--", "edit tracked.py",
        )
    finally:
        server.close()
    assert proc.returncode == 0
    rd = run_dir(repo, proc)
    assert not (rd / ".snapshot-index").exists()
    assert not (rd / ".snapshot-index-verify").exists()
    assert line(proc, "INDEPENDENT REVIEW") == "ERROR"


# ---------------------------------------------------------------------------
# Prompt-file compatibility (V0.2.1 behavior preserved)
# ---------------------------------------------------------------------------


def test_prompt_file_with_explicit_query_accepted(repo: Path) -> None:
    task = repo.parent / "task.md"
    task.write_text("# Task\nDo the thing.\n")
    proc = run(
        repo,
        {"FAKE_PI_APPEND_FILE": "tracked.py"},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review",
        "--prompt-file", str(task),
        "--", "Implement the attached specification.",
    )
    assert proc.returncode == 0, proc.stdout
    assert "final_query_source=explicit" in (
        run_dir(repo, proc) / "meta.txt"
    ).read_text()


def test_prompt_file_only_uses_fallback_query(repo: Path) -> None:
    task = repo.parent / "task.md"
    task.write_text("# Task\nDo the thing.\n")
    proc = run(
        repo,
        {"FAKE_PI_APPEND_FILE": "tracked.py"},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review",
        "--prompt-file", str(task),
        # no explicit query after --
    )
    assert proc.returncode == 0, proc.stdout
    meta = (run_dir(repo, proc) / "meta.txt").read_text()
    assert "final_query_source=fallback" in meta


# ---------------------------------------------------------------------------
# Self-hosting: a running wrapper must not depend on mutable entry-file
# bytes once writable agent execution has begun (issue #148).
# ---------------------------------------------------------------------------


_BOOTSTRAP_TEMPLATE = "trajectory-pi.*"


def wrapper_env(**extra: str) -> dict[str, str]:
    """Caller environment for launching a fresh wrapper.

    Wrapper-internal bootstrap state (TRAJECTORY_PI_BOOTSTRAP /
    TRAJECTORY_PI_BOOTSTRAP_COPY) belongs to Trajectory-Pi, not the
    caller; tests that observe the bootstrap hop must not inherit a
    guard value from an ambient environment, or the wrapper would take
    the recursion-guard fast path and the bootstrap would silently not
    be exercised (issue #152). Explicit extras always win.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("TRAJECTORY_PI_BOOTSTRAP", "TRAJECTORY_PI_BOOTSTRAP_COPY")
    }
    env.update(extra)
    return env


def bootstrap_copies() -> set[str]:
    tmp = Path(os.environ.get("TMPDIR", "/tmp"))
    return {p.name for p in tmp.glob(_BOOTSTRAP_TEMPLATE)}


def _wait_for_fake_pi(process: subprocess.Popen[str], deadline_s: int = 12) -> None:
    """Wait until the fake pi child (writable agent execution) is running."""
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        if process.poll() is not None:
            break
        r = subprocess.run(
            ["pgrep", "-f", "fakebin/pi"], capture_output=True, check=False
        )
        if r.returncode == 0:
            return
        time.sleep(0.1)
    pytest.fail("fake pi child never appeared (wrapper died early)")


def test_live_entry_script_destruction_does_not_corrupt_wrapper(
    repo: Path, tmp_path: Path
) -> None:
    """The entry point a caller launches is destroyed/truncated by the
    "live agent" mid-run; the running wrapper must keep executing from
    its private immutable copy and finish a full deterministic run.
    """
    entry_dir = tmp_path / "entry-scripts"
    entry_dir.mkdir()
    entry = entry_dir / "trajectory-pi"
    entry.write_text(Path(WRAPPER).read_text())
    entry.chmod(0o755)

    copies_before = bootstrap_copies()

    env = wrapper_env(
        FAKE_PI_SLEEP="4",
        FAKE_PI_APPEND_FILE="tracked.py",
    )
    proc = subprocess.Popen(
        ["bash", str(entry),
         "--class", "smoke", "--interval", "1", "--no-notify",
         "--no-review", "--", "self-hosting survival"],
        cwd=repo,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    _wait_for_fake_pi(proc)

    # THE HAZARD UNDER REPAIR: the executing script file is rewritten
    # (then truncated/syntax-corrupted) while the wrapper is live.
    entry.write_text("# destroyed by the live agent\nexit 97\n")
    entry.write_text("syntax corruption( mid-read\n   garbage\n")

    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, f"rc={proc.returncode}\n{out}\n{err}"
    cp = subprocess.CompletedProcess(
        args=["bash", str(entry)], returncode=proc.returncode,
        stdout=out, stderr=err,
    )

    # The live wrapper ran the FULL deterministic pipeline from the
    # immutable copy: run/status artifacts and snapshot survived.
    rd = run_dir(repo, cp)
    for artifact in (
        "pi.log", "meta.txt", "status.log", "worktree.patch",
        "worktree-metadata.txt", "diff-check.txt",
    ):
        assert (rd / artifact).is_file(), artifact
    assert line(cp, "WORKTREE SNAPSHOT") == "COMPLETE"
    assert line(cp, "DIFF CHECK") == "PASS"

    # No "No such file or directory" / parse corruption artifacts.
    assert "No such file or directory" not in err, err

    # The on-disk entry file really WAS rewritten mid-run (proof the
    # live process executed from its private copy, not this file).
    assert "syntax corruption(" in entry.read_text()

    # Private bootstrap copy cleaned after the run.
    assert bootstrap_copies() - copies_before == set()


def test_bootstrap_is_exactly_one_hop_and_copy_cleaned(repo: Path) -> None:
    """During the run exactly one private bootstrap copy must exist at
    any moment (recursion guard: no re-bootstrap loop), and it must be
    cleaned up afterwards.
    """
    copies_before = bootstrap_copies()
    env = wrapper_env(
        FAKE_PI_SLEEP="4",
        FAKE_PI_APPEND_FILE="tracked.py",
    )
    proc = subprocess.Popen(
        ["bash", str(WRAPPER),
         "--class", "smoke", "--interval", "1", "--no-notify",
         "--no-review", "--", "single-hop bootstrap check"],
        cwd=repo,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    seen_active = False
    deadline = time.time() + 12
    while time.time() < deadline and proc.poll() is None:
        active = bootstrap_copies() - copies_before
        assert len(active) <= 1, f"recursive bootstrap loop detected: {active}"
        if active:
            seen_active = True
        time.sleep(0.2)

    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, f"rc={proc.returncode}\n{out}\n{err}"
    assert seen_active, "no bootstrap copy was ever observed (bootstrap disabled?)"
    assert bootstrap_copies() - copies_before == set()


def test_recursion_guard_env_runs_without_rebootstrap(repo: Path) -> None:
    """With the recursion guard set the wrapper must not create another
    private copy (no exec hop) and the fast path still works.
    """
    copies_before = bootstrap_copies()
    p = subprocess.run(
        ["bash", str(WRAPPER), "--version"],
        cwd=repo,
        env=wrapper_env(TRAJECTORY_PI_BOOTSTRAP="1"),
        capture_output=True,
        text=True,
    )
    assert p.returncode == 0
    assert p.stdout.strip() == "trajectory-pi 0.3.1"
    assert bootstrap_copies() - copies_before == set()


def test_sigterm_forwarded_exit_code_and_bootstrap_copy_cleaned(repo: Path) -> None:
    copies_before = bootstrap_copies()
    env = wrapper_env(
        FAKE_PI_SLEEP="8",
    )
    proc = subprocess.Popen(
        ["bash", str(WRAPPER),
         "--class", "smoke", "--interval", "1", "--no-notify",
         "--no-review", "--", "term self-host check"],
        cwd=repo,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    _wait_for_fake_pi(proc)
    proc.send_signal(signal.SIGTERM)
    try:
        out, err = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("wrapper did not exit after SIGTERM")

    assert proc.returncode == 143, f"rc={proc.returncode}\n{out}\n{err}"
    assert bootstrap_copies() - copies_before == set()


# ---------------------------------------------------------------------------
# Issue #152 regressions: bootstrap-env isolation + exact terminal
# completion marker (V0.3.1 readiness repair)
# ---------------------------------------------------------------------------


def test_validate_commands_do_not_inherit_bootstrap_env(repo: Path) -> None:
    """V1.57 failure mode: wrapper-internal bootstrap state leaked into
    configured --validate commands. TRAJECTORY_PI_BOOTSTRAP /
    TRAJECTORY_PI_BOOTSTRAP_COPY exist only to protect the live immutable
    bootstrap process and must not appear in the validation child
    environment."""
    guard = (
        '[ -z "${TRAJECTORY_PI_BOOTSTRAP:-}" ]'
        ' && [ -z "${TRAJECTORY_PI_BOOTSTRAP_COPY:-}" ]'
        ' && echo CLEAN_ENV || { echo LEAKED_ENV; exit 9; }'
    )
    proc = run(
        repo,
        {"FAKE_PI_APPEND_FILE": "tracked.py"},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review",
        "--validate", guard,
        "--", "validate env isolation",
    )
    assert proc.returncode == 0, proc.stdout
    rd = run_dir(repo, proc)
    body = (rd / "validation-0.txt").read_text()
    # The echoed command line itself contains the guard text; judge the
    # child's actual output lines only.
    output = body.split("\n", 1)[1]
    assert "CLEAN_ENV" in output and "LEAKED_ENV" not in output
    assert line(proc, "VALIDATION") == "PASS"
    assert line(proc, "REPOSITORY STATE") != "NEEDS_REVIEW"


def test_validate_preserves_caller_bootstrap_state(repo: Path) -> None:
    """Issue #152 caller-vs-internal contract: when the ORIGINAL CALLER
    explicitly supplies bootstrap-related state in its own environment,
    validation must receive the caller's ORIGINAL values verbatim -- never
    the wrapper's own bootstrap-hop values (the private copy path the
    wrapper generated for itself), and never a silent rewrite."""
    caller_bs = "caller-bootstrap-marker"
    caller_copy = "/caller/supplied/private/copy.sh"
    guard = (
        '[ "${TRAJECTORY_PI_BOOTSTRAP:-_unset_}" = "' + caller_bs + '" ]'
        ' && [ "${TRAJECTORY_PI_BOOTSTRAP_COPY:-_unset_}" = "'
        + caller_copy + '" ]'
        ' && echo CALLER_PRESERVED || { echo WRONG_ENV; exit 9; }'
    )
    proc = run(
        repo,
        {
            "FAKE_PI_APPEND_FILE": "tracked.py",
            "TRAJECTORY_PI_BOOTSTRAP": caller_bs,
            "TRAJECTORY_PI_BOOTSTRAP_COPY": caller_copy,
        },
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review",
        "--validate", guard,
        "--", "preserve caller env",
    )
    assert proc.returncode == 0, proc.stdout
    rd = run_dir(repo, proc)
    body = (rd / "validation-0.txt").read_text()
    output = body.split("\n", 1)[1]
    assert "CALLER_PRESERVED" in output and "WRONG_ENV" not in output
    assert line(proc, "VALIDATION") == "PASS"
    assert line(proc, "REPOSITORY STATE") != "NEEDS_REVIEW"


def test_validate_preserves_caller_guard_without_injecting_copy(
    repo: Path,
) -> None:
    """Issue #152 edge: a caller that supplied only the recursion guard
    (TRAJECTORY_PI_BOOTSTRAP=1) gets that value back VERBATIM in
    validation; because the caller never set a copy value, the wrapper must
    not inject a wrapper-generated copy path for a value the caller did
    not supply. The guard must survive as the caller's intent."""
    guard = (
        '[ "${TRAJECTORY_PI_BOOTSTRAP:-_unset_}" = "1" ]'
        " && [ -z \"${TRAJECTORY_PI_BOOTSTRAP_COPY:-}\" ]"
        " && echo GUARD_PRESERVED || { echo WRONG_ENV; exit 9; }"
    )
    proc = run(
        repo,
        {
            "FAKE_PI_APPEND_FILE": "tracked.py",
            "TRAJECTORY_PI_BOOTSTRAP": "1",
        },
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review",
        "--validate", guard,
        "--", "preserve guard",
    )
    assert proc.returncode == 0, proc.stdout
    rd = run_dir(repo, proc)
    body = (rd / "validation-0.txt").read_text()
    output = body.split("\n", 1)[1]
    assert "GUARD_PRESERVED" in output and "WRONG_ENV" not in output
    assert line(proc, "VALIDATION") == "PASS"
    assert line(proc, "REPOSITORY STATE") != "NEEDS_REVIEW"


def test_version_marker_exact_terminal_is_completed(repo: Path) -> None:
    """V1.57 failure mode: the authoritative captured handoff ended with
    the exact terminal marker ``V1.57_COMPLETE`` yet the wrapper
    classified the run as INCOMPLETE_AGENT_RUN (the old marker grammar
    rejected the version dot)."""
    proc = run(
        repo,
        {"FAKE_PI_MARKER": "V1.57_COMPLETE"},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review", "--", "exact terminal marker",
    )
    assert proc.returncode == 0, proc.stdout
    assert line(proc, "AGENT CLASSIFICATION") == "AGENT_COMPLETED"
    assert line(proc, "REPOSITORY STATE") != "NEEDS_REVIEW"


def test_marker_appearing_earlier_not_terminal_fails_closed(repo: Path) -> None:
    """A marker is only terminal evidence when it is the LAST non-blank
    line of the captured output. Marker followed by any more output =>
    fail closed."""
    proc = run(
        repo,
        {"FAKE_PI_MARKER": "V1.57_COMPLETE",
         "FAKE_PI_TAIL": "post-marker noise that follows the marker"},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review", "--", "non-terminal marker",
    )
    assert proc.returncode == 0, proc.stdout
    assert line(proc, "AGENT CLASSIFICATION") == "INCOMPLETE_AGENT_RUN"
    assert line(proc, "REPOSITORY STATE") == "NEEDS_REVIEW"
    assert line(proc, "DECISION REQUIRED") != "GO COMMIT"


def test_near_miss_marker_fails_closed(repo: Path) -> None:
    """Near-miss markers (extra trailing punctuation) must never be
    accepted as the exact terminal marker."""
    proc = run(
        repo,
        {"FAKE_PI_MARKER": "V1.57_COMPLETE."},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review", "--", "near miss marker",
    )
    assert proc.returncode == 0, proc.stdout
    assert line(proc, "AGENT CLASSIFICATION") == "INCOMPLETE_AGENT_RUN"
    assert line(proc, "REPOSITORY STATE") == "NEEDS_REVIEW"
    assert line(proc, "DECISION REQUIRED") != "GO COMMIT"


def test_missing_marker_fails_closed(repo: Path) -> None:
    """No completion marker at all => fail closed (INCOMPLETE run)."""
    proc = run(
        repo,
        {"FAKE_PI_MARKER": ""},
        "--class", "smoke", "--interval", "1", "--no-notify",
        "--no-review", "--", "missing marker",
    )
    assert proc.returncode == 0, proc.stdout
    assert line(proc, "AGENT CLASSIFICATION") == "INCOMPLETE_AGENT_RUN"
    assert line(proc, "REPOSITORY STATE") == "NEEDS_REVIEW"
    assert line(proc, "DECISION REQUIRED") != "GO COMMIT"
