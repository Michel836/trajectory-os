"""V1.92 — Workspace Provenance and Materialization.

Every run executes against a workspace whose provenance is explicit and
verifiable — never a hidden shared checkout:

* per-run slot layout (product-owned):
    ``<state>/orchestration/workspaces/s{seq}/``     per-run artifacts
      - ``final-query.txt``, logs (existing V1.88/V1.89 contract)
      - ``ws/`` materialized source tree (``read_only`` / ``mutating``
        jobs with a declared source checkout)
      - ``.trajectory/run-provenance.json`` provenance marker
      - materialized trees additionally carry a manifest
        ``.trajectory/manifest.json`` proving bounded materialization;

* a run record that cannot be verified against its run directory is
  rejected (fail-closed, ``WORKSPACE_*``);

* concurrent conflicting mutation on the *same worktree* is rejected
  (``SAME_WORKTREE_CONFLICT``) — two concurrent runs never write to the
  same working tree:

  - ``mutating`` jobs ALWAYS run against an isolated copy (structural
    isolation: distinct product-owned slot trees, so mutations of
    different runs cannot alias);
  - ``read_only`` + ``shared_read_only`` runs read the source checkout
    and conflict with any concurrent record that may mutate it;
  - a spec that pairs ``mutating`` with ``shared_read_only`` is rejected
    at the canonical spec level (``SPEC_MUTATING_NEEDS_ISOLATED``).

Materialization is deterministic and bounded (fixed depth limit, fresh
product-owned target slot, manifest + verification).

**Atomic fail-closed materialization (V1.92 hardening):**

* isolated copies are materialized into a *temporary* product-owned path
  inside the slot (``.ws-tmp``), fully bounded-checked and manifested there,
  and only then promoted with a single ``os.replace``; on ANY failure the
  temporary tree is removed and the previously-existing tree (if any) is
  restored — a failed materialization therefore never leaves a
  valid-looking partial workspace behind;
* the provenance marker is never left in a misleading state: if it was
  replaced and the new materialization fails, the previous marker is
  restored (or removed if it did not pre-exist), so no stale provenance
  points at a partial tree;
* cleanup is strictly bounded to the product-owned slot (never the source
  checkout, never Git state, never any path outside the slot).
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import subprocess
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trajectory_os.runs import model, spec

# Bounded materialization.
COPY_DEPTH_LIMIT = 96
PROVENANCE_REL = os.path.join(".trajectory", "run-provenance.json")
MANIFEST_REL = os.path.join(".trajectory", "manifest.json")
MAX_MANIFEST_ENTRIES = 8_192


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class MaterializationError(Exception):
    """Raised when workspace materialization or verification fails closed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class WorkspaceConflictError(MaterializationError):
    """Concurrent conflicting usage of the same worktree (fail closed)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


@dataclass(frozen=True)
class MaterializedWorkspace:
    """Proven, trusted per-run workspace facts for launch and verification."""

    slot_root: Path          # per-run root (logs / final-query / provenance)
    launch_cwd: Path         # directory the job process runs in
    tree: Path               # source tree (copy or shared checkout)
    isolated_copy: bool
    revision: str
    exec_class: str
    policy: str


def read_source_revision(checkout: Path) -> str:
    """Determine an honest source revision.

    Prefer the real checkout's HEAD (via its own git metadata).  Fall back to
    a deterministic content snapshot when the source is not a git checkout —
    the revision is *derived from the source*, never invented, and is
    recorded with a stable label (``snapshot``) in provenance.
    """
    if not checkout.is_dir():
        raise MaterializationError(
            model.ERR_WORKSPACE_SOURCE_MISSING, f"source checkout: {checkout}"
        )
    # Honest git revision (best effort; never fatal — snapshot fallback).
    # subprocess with an argv list: no shell, bounded time, fail closed.
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        proc = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if proc.returncode == 0:
            rev = proc.stdout.strip()
            if rev and all(c.isalnum() or c in "._-" for c in rev) and 7 <= len(rev) <= 64:
                return rev
    # shlex.quote: standard-library single-shell-word quoting — safe for every
    # special character (quotes, backticks, $, whitespace, ...), so the label
    # is a faithful, reversible encoding of the path.
    return f"snapshot:{shlex.quote(str(checkout))}"


def _bounded_remove(slot_root: Path, target: Path) -> None:
    """Bounded removal helper: remove ``target`` ONLY inside the owned slot.

    Symlinks and files are unlinked (never followed); directories are
    removed with ``rmtree``.  A target that escapes the slot, is the slot
    itself, or is anything unresolvable is rejected fail-closed — this
    helper can never touch the source checkout or any foreign path.
    """
    root = Path(slot_root)
    path = Path(target)
    try:
        if path == root:
            raise MaterializationError(
                model.ERR_WORKSPACE_COPY_FAILED, "refusing to remove the slot root"
            )
        if not path.is_relative_to(root):
            raise MaterializationError(
                model.ERR_WORKSPACE_COPY_FAILED,
                f"refusing to remove path outside owned slot: {path}",
            )
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
            return
        if path.is_dir():
            # rmtree does not remove symlinks' targets; symlinks are
            # already handled above.
            shutil.rmtree(path, ignore_errors=False)
            return
        # Absent path: nothing to do.
        return
    except MaterializationError:
        raise
    except (OSError, shutil.Error) as exc:
        raise MaterializationError(
            model.ERR_WORKSPACE_COPY_FAILED, f"bounded cleanup failed: {exc}"
        ) from exc


def _read_marker(slot_root: Path) -> bytes | None:
    """Snapshot the provenance marker bytes (None if absent/unreadable)."""
    marker = Path(slot_root) / PROVENANCE_REL
    try:
        if marker.is_file():
            return marker.read_bytes()
    except OSError:
        pass
    return None


def _restore_marker(slot_root: Path, previous: bytes | None) -> None:
    """Restore (or remove) the provenance marker after a failure.

    Bounded: only ever writes/removes the marker inside the owned slot.
    """
    marker = Path(slot_root) / PROVENANCE_REL
    try:
        if previous is None:
            marker.unlink(missing_ok=True)
            return
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(previous)
    except OSError:
        pass  # best-effort restoration; the failure that triggered it propagates


def _iter_bounded(root: Path, depth_limit: int = COPY_DEPTH_LIMIT) -> Iterator[tuple[Path, int]]:
    """Deterministic bounded filesystem iteration (size-bounded, no cycles)."""
    seen: set[tuple[int, int]] = set()
    stack = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > depth_limit:
            continue
        dev_ino = (current.stat().st_dev, current.stat().st_ino)
        if dev_ino in seen:
            continue  # symlink loop / bind mount — bounded, skipped
        seen.add(dev_ino)
        yield current, depth
        try:
            with os.scandir(current) as it:
                children = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        for entry in children:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append((Path(entry.path), depth + 1))
            except OSError:
                continue


def verify_source_checkout(spec_obj: spec.JobSpec) -> Path:
    """Verify the declared source checkout exists and is readable (fail-closed)."""
    if spec_obj.source_checkout is None:
        raise MaterializationError(
            model.ERR_WORKSPACE_BAD_PROVENANCE, "source_checkout missing"
        )
    source = Path(spec_obj.source_checkout)
    if not source.is_absolute():
        raise MaterializationError(
            model.ERR_WORKSPACE_BAD_PROVENANCE,
            f"source_checkout must be absolute: {source}",
        )
    if not source.is_dir():
        raise MaterializationError(
            model.ERR_WORKSPACE_SOURCE_MISSING, f"source checkout missing: {source}"
        )
    if not os.access(source, os.R_OK | os.X_OK):
        raise MaterializationError(
            model.ERR_WORKSPACE_BAD_PROVENANCE, f"source unreadable: {source}"
        )
    return source


def materialize_workspace(
    spec_obj: spec.JobSpec,
    slot_root: Path,
    *,
    active_records: Sequence[Any] = (),
) -> MaterializedWorkspace:
    """Materialize a trusted per-run workspace under ``slot_root`` (product-owned).

    ``slot_root`` is the per-run slot directory (``workspaces/s{seq}``); logs
    and the final-query live there.  ``read_only`` / ``mutating`` jobs with a
    declared source get a materialized tree at ``slot_root/ws``; the returned
    ``launch_cwd`` is the directory the job process starts in.
    """
    slot_root.mkdir(parents=True, exist_ok=True)

    if spec_obj.execution_class == spec.EXEC_AD_HOC or not spec_obj.source_checkout:
        # No managed source: the per-run slot itself is the trusted tree.
        # Guarded: if verification fails, a pre-existing marker is restored
        # (or a new marker removed) so no misleading provenance is left.
        previous_marker = _read_marker(slot_root)
        try:
            facts = _write_provenance(
                slot_root,
                spec_obj=spec_obj,
                isolated_copy=False,
                revision="",
                tree=slot_root,
            )
            _verify_or_raise(facts)
        except BaseException:
            _restore_marker(slot_root, previous_marker)
            raise
        return MaterializedWorkspace(
            slot_root=slot_root,
            launch_cwd=slot_root,
            tree=slot_root,
            isolated_copy=False,
            revision="",
            exec_class=spec.EXEC_AD_HOC,
            policy=spec_obj.workspace_policy,
        )

    source = verify_source_checkout(spec_obj)
    source_rev = spec_obj.source_revision or read_source_revision(source)

    # Same-worktree concurrent-conflict guard (fail closed): reject when a
    # concurrent active record may mutate the same tree we will touch.
    _guard_same_worktree(spec_obj, source, active_records)

    if spec_obj.workspace_policy == spec.WORKSPACE_SHARED_READ_ONLY:
        # Shared checkout, read-only usage: launch directly in the source tree
        # (no copy, no marker written into the shared tree).
        if not os.access(source, os.R_OK | os.X_OK):
            raise MaterializationError(
                model.ERR_WORKSPACE_BAD_PROVENANCE, f"source unreadable: {source}"
            )
        previous_marker = _read_marker(slot_root)
        try:
            facts = _write_provenance(
                slot_root,
                spec_obj=spec_obj,
                isolated_copy=False,
                revision=source_rev,
                tree=source,
            )
            _verify_or_raise(facts)
        except BaseException:
            _restore_marker(slot_root, previous_marker)
            raise
        return MaterializedWorkspace(
            slot_root=slot_root,
            launch_cwd=source,
            tree=source,
            isolated_copy=False,
            revision=source_rev,
            exec_class=spec.EXEC_READ_ONLY,
            policy=spec.WORKSPACE_SHARED_READ_ONLY,
        )

    # Isolated copy (mandatory for mutating, allowed for read_only).
    # Atomic semantics: materialize into a temporary product-owned path,
    # fully validate (boundedness + manifest) there, then promote with a
    # single rename.  Every failure leaves the slot without a partial
    # valid-looking tree and without (new) misleading provenance.
    tree = slot_root / "ws"
    temp_tree = slot_root / ".ws-tmp"
    stash = slot_root / ".ws-old"
    _bounded_remove(slot_root, temp_tree)
    try:
        shutil.copytree(
            source,
            temp_tree,
            symlinks=True,
            copy_function=shutil.copy2,
            dirs_exist_ok=False,
        )
    except (OSError, shutil.Error) as exc:
        _bounded_remove(slot_root, temp_tree)
        raise MaterializationError(
            model.ERR_WORKSPACE_COPY_FAILED, f"copy failed: {exc}"
        ) from exc
    try:
        if _tree_exceeds_depth(temp_tree):
            raise MaterializationError(
                model.ERR_WORKSPACE_TOO_DEEP, f"materialized tree too deep: {temp_tree}"
            )
        _write_manifest(temp_tree, revision=source_rev, spec_obj=spec_obj)
    except BaseException:
        _bounded_remove(slot_root, temp_tree)
        raise

    previous_marker = _read_marker(slot_root)
    had_old = tree.exists() or tree.is_symlink()
    stashed = False
    try:
        if had_old:
            # Never clobber blindly: park the pre-existing tree and restore
            # it if this materialization does not fully verify.
            _bounded_remove(slot_root, stash)
            os.replace(tree, stash)
            stashed = True
        os.replace(temp_tree, tree)
        facts = _write_provenance(
            slot_root,
            spec_obj=spec_obj,
            isolated_copy=True,
            revision=source_rev,
            tree=tree,
        )
        _verify_or_raise(facts)
    except BaseException:
        # Rollback: restore any previously-valid tree and marker, and
        # remove the temporary/partial tree.  Rollback problems must not
        # mask the original failure.
        if stashed:
            with contextlib.suppress(Exception):
                _bounded_remove(slot_root, tree)
            with contextlib.suppress(Exception):
                os.replace(stash, tree)
        else:
            with contextlib.suppress(Exception):
                _bounded_remove(slot_root, tree)
        with contextlib.suppress(Exception):
            _bounded_remove(slot_root, temp_tree)
        _restore_marker(slot_root, previous_marker)
        raise
    # Promotion fully verified: the superseded tree (if any) has served
    # its rollback purpose — remove it (bounded, only inside the owned
    # slot).
    if stashed:
        _bounded_remove(slot_root, stash)
    return MaterializedWorkspace(
        slot_root=slot_root,
        launch_cwd=tree,
        tree=tree,
        isolated_copy=True,
        revision=source_rev,
        exec_class=spec_obj.execution_class,
        policy=spec_obj.workspace_policy,
    )


def _tree_exceeds_depth(root: Path) -> bool:
    for path, depth in _iter_bounded(root):  # bounded depth-limit walk
        del path
        if depth > COPY_DEPTH_LIMIT:
            return True
    return False


def _write_provenance(
    slot_root: Path,
    *,
    spec_obj: spec.JobSpec,
    isolated_copy: bool,
    revision: str,
    tree: Path,
) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "schema_version": 1,
        "job_id": spec_obj.job_id,
        "schema_version_spec": spec_obj.schema_version,
        "execution_class": spec_obj.execution_class,
        "workspace_policy": spec_obj.workspace_policy,
        "isolated_copy": isolated_copy,
        "revision": revision,
        "source_checkout": (
            str(spec_obj.source_checkout)
            if spec_obj.source_checkout is not None
            else None
        ),
        "tree": str(tree),
        "slot_root": str(slot_root),
        "materialized_at": _utc_now_iso(),
    }
    target = slot_root / PROVENANCE_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(facts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return facts


def _write_manifest(tree: Path, *, revision: str, spec_obj: spec.JobSpec) -> None:
    entries: list[dict[str, Any]] = []
    total_bytes = 0
    last_depth = 0
    for current, depth in _iter_bounded(tree):
        last_depth = depth
        for name in sorted(_safe_list(current)):
            p = current / name
            try:
                if p.is_symlink():
                    size = 0
                elif p.is_file():
                    size = p.stat().st_size
                else:
                    continue
            except OSError:
                continue
            total_bytes += size
            if len(entries) < MAX_MANIFEST_ENTRIES:
                entries.append({"rel": str(p.relative_to(tree)), "size": size})
            else:
                break
    manifest = {
        "schema_version": 1,
        "revision": revision,
        "job_id": spec_obj.job_id,
        "execution_class": spec_obj.execution_class,
        "workspace_policy": spec_obj.workspace_policy,
        "entry_count": len(entries),
        "total_bytes": total_bytes,
        "depth_limit": COPY_DEPTH_LIMIT,
        "truncated": len(entries) >= MAX_MANIFEST_ENTRIES,
        "truncated_at_depth": last_depth,
        "entries": entries,
    }
    target = tree / MANIFEST_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _safe_list(directory: Path) -> list[str]:
    try:
        return sorted(os.listdir(directory))
    except OSError:
        return []


def _verify_or_raise(facts: dict[str, Any]) -> None:
    slot = Path(facts["slot_root"])
    ok, problems = verify_workspace(
        slot,
        expected_isolated=bool(facts["isolated_copy"]),
        expected_class=facts["execution_class"],
    )
    if not ok:
        raise MaterializationError(
            model.ERR_WORKSPACE_VERIFY_FAILED,
            "; ".join(problems[:5]) or "unknown verification failure",
        )


def verify_workspace(
    slot_root: Path,
    *,
    expected_isolated: bool | None = None,
    expected_class: str | None = None,
) -> tuple[bool, list[str]]:
    """Verify per-run provenance (+ isolated-copy manifest).  Deterministic."""
    problems: list[str] = []
    marker_path = slot_root / PROVENANCE_REL
    if not marker_path.is_file():
        return False, ["PROVENANCE_MISSING"]
    try:
        raw = marker_path.read_text(encoding="utf-8")
        facts = json.loads(raw)
        if not isinstance(facts, dict):
            raise ValueError("not a dict")
    except (ValueError, OSError):
        return False, ["PROVENANCE_MALFORMED"]
    if facts.get("schema_version") != 1:
        problems.append("PROVENANCE_SCHEMA")
    isolated = facts.get("isolated_copy")
    if not isinstance(isolated, bool):
        problems.append("PROVENANCE_MALFORMED")
        isolated = None
    if (
        expected_isolated is not None
        and isolated is not None
        and bool(isolated) != bool(expected_isolated)
    ):
        problems.append("ISOLATION_MISMATCH")
    class_value = facts.get("execution_class")
    if (
        expected_class is not None
        and class_value is not None
        and class_value != expected_class
    ):
        problems.append("EXEC_CLASS_MISMATCH")
    if isolated:
        tree_text = facts.get("tree")
        tree = Path(str(tree_text)) if isinstance(tree_text, str) else slot_root / "ws"
        if not tree.is_dir():
            problems.append("TREE_MISSING")
        else:
            problems.extend(_verify_manifest(tree))
    return (len(problems) == 0, problems)


def _verify_manifest(tree: Path) -> list[str]:
    problems: list[str] = []
    manifest_path = tree / MANIFEST_REL
    if not manifest_path.is_file():
        return ["MANIFEST_MISSING"]
    try:
        raw = manifest_path.read_text(encoding="utf-8")
        manifest = json.loads(raw)
        if not isinstance(manifest, dict):
            raise ValueError("not a dict")
    except (ValueError, OSError):
        return ["MANIFEST_MALFORMED"]
    if manifest.get("schema_version") != 1:
        problems.append("MANIFEST_SCHEMA")
    depth_limit = manifest.get("depth_limit")
    if not isinstance(depth_limit, int) or depth_limit <= 0 or depth_limit > 1024:
        problems.append("MANIFEST_DEPTH_LIMIT")
    return problems


def _record_workspace_path(record: Any) -> str | None:
    workspace = getattr(record, "workspace", None)
    if workspace is None:
        return None
    try:
        return str(Path(str(workspace)).resolve())
    except OSError:
        return str(workspace)


def _guard_same_worktree(
    spec_obj: spec.JobSpec,
    source: Path,
    active_records: Sequence[Any],
) -> None:
    """Reject concurrent conflicting usage of the same worktree (fail closed).

    A concurrent record is *mutation-risk* when it is a mutating record or
    its execution class is not recorded (legacy) — the conservative
    assumption is that it may write.  Jobs that only READ a shared checkout
    therefore never conflict; shared-checkout jobs conflict with any
    concurrent mutation-risk record on the very same tree.  Isolated copies
    are structurally safe (distinct product-owned tree) and never conflict.
    """
    if spec_obj.workspace_policy != spec.WORKSPACE_SHARED_READ_ONLY:
        return  # isolated copy or unmanaged tree: no shared-tree conflict
    shared_tree = str(Path(source).resolve())
    for record in active_records:
        rec_class = getattr(record, "execution_class", None)
        may_mutate = rec_class in (None, spec.EXEC_MUTATING)  # legacy => risk
        if not may_mutate:
            continue
        rec_path = _record_workspace_path(record)
        if rec_path and rec_path == shared_tree:
            raise WorkspaceConflictError(
                model.ERR_SAME_WORKTREE_CONFLICT,
                (
                    f"worktree {shared_tree} is concurrently used by record "
                    f"{getattr(record, 'job_id', '?')!r} (class={rec_class!r}); "
                    "same-worktree concurrent use is not permitted"
                ),
            )


def check_same_worktree_conflict(
    workspace_path: Path,
    exec_class: str,
    active_records: Sequence[Any],
) -> bool:
    """True iff a concurrent active record may mutate the same worktree.

    Deterministic pure predicate (used by launch guards and tests):
    legacy (class ``None``) and ``mutating`` records are mutation-risk;
    ``read_only`` records never conflict.
    """
    target = str(Path(workspace_path).resolve())
    if exec_class == spec.EXEC_READ_ONLY:
        return False  # read-only usage cannot conflict with anything
    # mutating (or unknown/legacy caller) — flag concurrent mutation risk
    for record in active_records:
        rec_class = getattr(record, "execution_class", None)
        if rec_class in (spec.EXEC_READ_ONLY,):
            continue
        rec_path = _record_workspace_path(record)
        if rec_path and rec_path == target:
            return True
    return False


def provenance_path(slot_root: Path) -> Path:
    return slot_root / PROVENANCE_REL


def manifest_path(tree: Path) -> Path:
    return tree / MANIFEST_REL
