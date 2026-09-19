"""M036–M039 — strict Git adapter for the operator-authorized release layer.

The release layer performs the *only* Git trust-boundary writes in the whole
system, and only after an explicit operator authorization
(:mod:`trajectory_os.release.authorization`). Everything else (implementation,
review, runtime control, observation) uses read-only Git or no Git at all.

This module owns:

* :class:`GitState` — a read-only snapshot (branch, HEAD, worktree dirtiness);
* :class:`GitAdapter` — the strict abstraction the release service depends on;
* :class:`LocalGitAdapter` — the real ``git`` CLI implementation;
* :class:`FakeGitAdapter` — a deterministic in-memory implementation for
  tests and the deterministic acceptance matrix.

Both write operations (:meth:`GitAdapter.commit_all`,
:meth:`GitAdapter.push`) require a validated
:class:`~trajectory_os.release.authorization.ReleaseAuthorization` bound to
``GO_COMMIT``; a missing/mismatched authorization raises before any Git
process is spawned.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from trajectory_os.release import model
from trajectory_os.release.authorization import ReleaseAuthorization

DEFAULT_REMOTE = "origin"
GIT_TIMEOUT_S = 60


@dataclass(frozen=True)
class GitState:
    """A read-only snapshot of one repository (never guessed)."""

    branch: str | None
    head_sha: str | None
    clean: bool
    porcelain: tuple[str, ...]
    available: bool
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "branch": self.branch,
            "head_sha": self.head_sha,
            "clean": self.clean,
            "porcelain": list(self.porcelain),
            "available": self.available,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class GitCommitResult:
    commit_sha: str
    branch: str
    message: str

    def to_dict(self) -> dict[str, object]:
        return {"commit_sha": self.commit_sha, "branch": self.branch,
                "message": self.message}


@dataclass(frozen=True)
class GitPushResult:
    branch: str
    remote: str
    pushed_sha: str

    def to_dict(self) -> dict[str, object]:
        return {"branch": self.branch, "remote": self.remote,
                "pushed_sha": self.pushed_sha}


class GitAdapter(Protocol):
    """Strict Git abstraction (read state + authorized write only)."""

    def read_state(self) -> GitState: ...

    def commit_all(self, message: str, *,
                   authorization: ReleaseAuthorization) -> GitCommitResult: ...

    def push(self, branch: str, expected_head: str, *,
             remote: str = DEFAULT_REMOTE,
             authorization: ReleaseAuthorization) -> GitPushResult: ...


def _require_go_commit(authorization: ReleaseAuthorization) -> None:
    authorization.validate()
    if authorization.action != model.GATE_GO_COMMIT:
        raise model.ReleaseError(
            model.R_UNAUTHORIZED,
            f"commit/push require a {model.GATE_GO_COMMIT} authorization, "
            f"got {authorization.action!r}")


# --- real implementation ------------------------------------------------------


@dataclass
class LocalGitAdapter:
    """Real ``git`` CLI adapter (read state + authorized commit/push)."""

    repo: str
    remote: str = DEFAULT_REMOTE
    timeout_s: int = GIT_TIMEOUT_S

    def _git(self, args: Sequence[str], *,
             check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["git", *args], cwd=self.repo, capture_output=True,
                text=True, timeout=self.timeout_s, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise model.ReleaseError(
                model.R_ADAPTER_UNAVAILABLE,
                f"git unavailable: {type(exc).__name__}") from exc
        if check and proc.returncode != 0:
            raise model.ReleaseError(
                model.R_COMMIT_FAILED,
                f"git {' '.join(args[:2])} failed: "
                f"{proc.stderr.strip()[:256]}")
        return proc

    def read_state(self) -> GitState:
        head = self._git(["rev-parse", "HEAD"], check=False)
        if head.returncode != 0:
            return GitState(branch=None, head_sha=None, clean=False,
                            porcelain=(), available=False,
                            reason="not a git work tree")
        branch = self._git(["rev-parse", "--abbrev-ref", "HEAD"], check=False)
        porcelain = self._git(["status", "--porcelain"], check=False)
        changes = tuple(
            line for line in porcelain.stdout.splitlines() if line.strip())
        return GitState(
            branch=(branch.stdout.strip() or None)
            if branch.returncode == 0 else None,
            head_sha=head.stdout.strip() or None,
            clean=not changes,
            porcelain=changes,
            available=True,
        )

    def commit_all(self, message: str, *,
                   authorization: ReleaseAuthorization) -> GitCommitResult:
        _require_go_commit(authorization)
        if not message.strip() or len(message) > model.MAX_MESSAGE_LEN:
            raise model.ReleaseError(model.R_COMMIT_FAILED,
                                     "invalid commit message")
        state = self.read_state()
        if not state.available or state.head_sha is None:
            raise model.ReleaseError(model.R_COMMIT_FAILED, state.reason)
        self._git(["add", "-A"])
        commit = self._git(["commit", "-m", message], check=False)
        if commit.returncode != 0:
            raise model.ReleaseError(
                model.R_COMMIT_FAILED,
                f"git commit failed: {commit.stderr.strip()[:256]}")
        after = self.read_state()
        if after.head_sha is None or after.head_sha == state.head_sha:
            raise model.ReleaseError(model.R_COMMIT_FAILED,
                                     "commit did not advance HEAD")
        return GitCommitResult(commit_sha=after.head_sha,
                               branch=after.branch or "", message=message)

    def push(self, branch: str, expected_head: str, *,
             remote: str = DEFAULT_REMOTE,
             authorization: ReleaseAuthorization) -> GitPushResult:
        _require_go_commit(authorization)
        state = self.read_state()
        if state.head_sha != expected_head:
            raise model.ReleaseError(
                model.R_HEAD_MOVED,
                f"local HEAD {state.head_sha} != expected {expected_head}")
        pushed = self._git(["push", remote, f"HEAD:refs/heads/{branch}"],
                           check=False)
        if pushed.returncode != 0:
            raise model.ReleaseError(
                model.R_PUSH_FAILED,
                f"git push failed: {pushed.stderr.strip()[:256]}")
        return GitPushResult(branch=branch, remote=remote,
                             pushed_sha=expected_head)


# --- deterministic implementation ---------------------------------------------


@dataclass
class FakeGitAdapter:
    """Deterministic in-memory Git adapter for tests and acceptance.

    ``remote_heads`` models the remote branch tips so expected-head
    protection is provable without any network or real repository.
    """

    branch: str = "feature-branch"
    base_branch: str = "main"
    head_sha: str = "0" * 40
    dirty: bool = True
    available: bool = True
    reason: str = ""
    remote: str = DEFAULT_REMOTE
    remote_heads: dict[str, str] = field(default_factory=dict)
    commits: list[tuple[str, str]] = field(default_factory=list)
    pushes: list[tuple[str, str]] = field(default_factory=list)

    def read_state(self) -> GitState:
        return GitState(
            branch=self.branch if self.available else None,
            head_sha=self.head_sha if self.available else None,
            clean=not self.dirty,
        porcelain=(" M fake",) if self.dirty else (),
            available=self.available,
            reason=self.reason,
        )

    def commit_all(self, message: str, *,
                   authorization: ReleaseAuthorization) -> GitCommitResult:
        _require_go_commit(authorization)
        if not message.strip() or len(message) > model.MAX_MESSAGE_LEN:
            raise model.ReleaseError(model.R_COMMIT_FAILED,
                                     "invalid commit message")
        if not self.available or self.head_sha is None:
            raise model.ReleaseError(model.R_COMMIT_FAILED,
                                     self.reason or "git unavailable")
        parent = self.head_sha
        payload = f"commit\x00{message}\x00{parent}".encode()
        self.head_sha = hashlib.sha256(payload).hexdigest()[:40]
        self.dirty = False
        self.commits.append((self.head_sha, message))
        return GitCommitResult(commit_sha=self.head_sha, branch=self.branch,
                               message=message)

    def push(self, branch: str, expected_head: str, *,
             remote: str = DEFAULT_REMOTE,
             authorization: ReleaseAuthorization) -> GitPushResult:
        _require_go_commit(authorization)
        if self.head_sha != expected_head:
            raise model.ReleaseError(
                model.R_HEAD_MOVED,
                f"local HEAD {self.head_sha} != expected {expected_head}")
        self.remote_heads[branch] = expected_head
        self.pushes.append((branch, expected_head))
        return GitPushResult(branch=branch, remote=remote,
                             pushed_sha=expected_head)

    def advance_head(self, *, branch: str | None = None,
                     head_sha: str | None = None,
                     dirty: bool | None = None) -> None:
        """Test helper: simulate a moved branch/HEAD or worktree change."""
        if branch is not None:
            self.branch = branch
        if head_sha is not None:
            self.head_sha = head_sha
        if dirty is not None:
            self.dirty = dirty


__all__ = [
    "DEFAULT_REMOTE",
    "FakeGitAdapter",
    "GitAdapter",
    "GitCommitResult",
    "GitPushResult",
    "GitState",
    "LocalGitAdapter",
]
