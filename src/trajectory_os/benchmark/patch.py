"""M029 — exact, Git-free semantic patch identity (pure, deterministic).

The benchmark never performs a Git trust-boundary write. Instead it captures
a deterministic content snapshot of an isolated workspace before execution,
captures the same workspace after execution, and derives the exact unified
diff from those two snapshots. The diff digest is the trial's *final exact
semantic patch identity*; it is reproducible, filesystem-order independent,
and never guessed.

A workspace that cannot be read yields an explicitly unavailable patch
identity with a stable reason — never an empty/misleading digest.
"""

from __future__ import annotations

import difflib
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from trajectory_os.benchmark import model

#: Directories that are never part of a workspace semantic patch.
IGNORED_DIRS = frozenset({
    ".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    ".venv", "venv", "node_modules", ".trajectory-pi",
})

#: Hard bound on the number of files considered (fail closed on runaway).
MAX_FILES = 512

#: Hard bound on total snapshot bytes (fail closed on runaway).
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024

#: Stable reason codes.
REASON_EMPTY = "NO_WORKSPACE_FILES"
REASON_TOO_LARGE = "WORKSPACE_TOO_LARGE"
REASON_UNREADABLE = "WORKSPACE_UNREADABLE"


def _eligible(path: Path) -> bool:
    return not any(part in IGNORED_DIRS for part in path.parts)


@dataclass(frozen=True)
class WorkspaceSnapshot:
    """A deterministic content snapshot of one isolated workspace."""

    files: Mapping[str, bytes]

    def digest(self) -> str:
        tree = hashlib.sha256()
        for name in sorted(self.files):
            tree.update(name.encode("utf-8"))
            tree.update(b"\x00")
            tree.update(hashlib.sha256(self.files[name]).digest())
            tree.update(b"\x00")
        return tree.hexdigest()


def capture(root: str | Path) -> WorkspaceSnapshot:
    """Capture every eligible file under ``root`` (bounded, deterministic)."""
    base = Path(root)
    files: dict[str, bytes] = {}
    total = 0
    if not base.is_dir():
        return WorkspaceSnapshot(files={})
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(base)
        if not _eligible(relative) or len(files) >= MAX_FILES:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        total += len(data)
        if total > MAX_SNAPSHOT_BYTES:
            break
        files[relative.as_posix()] = data
    return WorkspaceSnapshot(files=files)


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _file_lines(data: bytes) -> list[str]:
    return _decode(data).splitlines()


def _file_diff(name: str, before: bytes | None,
               after: bytes | None) -> tuple[str, int, int]:
    """Deterministic single-file unified diff (line/text, +, -)."""
    before_lines = [] if before is None else _file_lines(before)
    after_lines = [] if after is None else _file_lines(after)
    fromfile = "/dev/null" if before is None else f"a/{name}"
    tofile = "/dev/null" if after is None else f"b/{name}"
    lines = list(difflib.unified_diff(
        before_lines, after_lines, fromfile=fromfile, tofile=tofile,
        lineterm=""))
    additions = sum(1 for line in lines
                    if line.startswith("+") and not line.startswith("+++"))
    deletions = sum(1 for line in lines
                    if line.startswith("-") and not line.startswith("---"))
    return "\n".join(lines), additions, deletions


@dataclass(frozen=True)
class PatchText:
    """The exact unified diff derived from two snapshots."""

    text: str
    files_changed: int
    insertions: int
    deletions: int
    added: tuple[str, ...]
    removed: tuple[str, ...]
    modified: tuple[str, ...]
    sha256: str


def compute_patch(baseline: WorkspaceSnapshot,
                  current: WorkspaceSnapshot) -> PatchText:
    """Deterministic exact unified diff between two snapshots (pure)."""
    names = sorted(set(baseline.files) | set(current.files))
    chunks: list[str] = []
    added: list[str] = []
    removed: list[str] = []
    modified: list[str] = []
    insertions = 0
    deletions = 0
    for name in names:
        before = baseline.files.get(name)
        after = current.files.get(name)
        if before == after:
            continue
        if before is None:
            added.append(name)
        elif after is None:
            removed.append(name)
        else:
            modified.append(name)
        chunk, chunk_additions, chunk_deletions = _file_diff(
            name, before, after)
        chunks.append(chunk)
        insertions += chunk_additions
        deletions += chunk_deletions
    text = "\n".join(chunks)
    return PatchText(
        text=text, files_changed=len(added) + len(removed) + len(modified),
        insertions=insertions, deletions=deletions, added=tuple(added),
        removed=tuple(removed), modified=tuple(modified),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())


def build_identity(baseline: WorkspaceSnapshot,
                   current: WorkspaceSnapshot) -> model.PatchIdentity:
    """Build the exact :class:`~trajectory_os.benchmark.model.PatchIdentity`."""
    if not baseline.files and not current.files:
        return model.PatchIdentity.build(
            available=False, sha256=None, files_changed=None,
            insertions=None, deletions=None, reason=REASON_EMPTY)
    patch = compute_patch(baseline, current)
    # ``untracked`` is the exact set of workspace paths the trial changed:
    # added, modified and removed. Removals are included so a protected-path
    # deletion can never evade the fail-closed protected-path matcher.
    changed = sorted(set(patch.added) | set(patch.modified) | set(patch.removed))
    return model.PatchIdentity.build(
        available=True, sha256=patch.sha256,
        files_changed=patch.files_changed, insertions=patch.insertions,
        deletions=patch.deletions,
        reason=model.R_OK, untracked=tuple(changed))
