"""M036–M039 — durable release artifacts (one mission root, additive).

The release layer writes only *additive* documents into the canonical mission
root; it never rewrites the M030 ``status.json`` or the M031
``mission.json`` / ``plan.json`` / ``closure.json``. Layout additions::

    release-state.json     current release stage pointer
    commit-handoff.json    M036 deterministic GO COMMIT handoff
    commit-result.json     M036 authorized commit/push result
    pr-binding.json        M037 exact-head pull-request binding
    ci-status.json         M037 exact-head CI lookup (byte-idempotent)
    merge-handoff.json     M038 GO MERGE gate
    merge-result.json      M038 authoritative merge result
    release-closure.json   M039 release closure
    release-events.jsonl   append-only release action timeline

Release actions are recorded in their own append-only timeline so the
canonical M030 ``events.jsonl`` remains owned by the observability layer.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from trajectory_os.observability import store as obs_store
from trajectory_os.release import model

RELEASE_STATE_NAME = "release-state.json"
COMMIT_HANDOFF_NAME = "commit-handoff.json"
COMMIT_RESULT_NAME = "commit-result.json"
PR_BINDING_NAME = "pr-binding.json"
CI_STATUS_NAME = "ci-status.json"
MERGE_HANDOFF_NAME = "merge-handoff.json"
MERGE_RESULT_NAME = "merge-result.json"
RELEASE_CLOSURE_NAME = "release-closure.json"
RELEASE_EVENTS_NAME = "release-events.jsonl"

#: Bound on the release timeline (newest records retained on read).
MAX_RELEASE_EVENTS = 512


def _path(mission_root: Path, name: str) -> Path:
    return mission_root / name


def write_document(mission_root: Path, name: str,
                   payload: Mapping[str, Any]) -> None:
    obs_store.write_json(_path(mission_root, name), payload)


def read_document(mission_root: Path, name: str) -> dict[str, Any]:
    return obs_store.read_json(_path(mission_root, name))


def exists(mission_root: Path, name: str) -> bool:
    return _path(mission_root, name).is_file()


def write_state(mission_root: Path, state: model.ReleaseState) -> None:
    write_document(mission_root, RELEASE_STATE_NAME, state.to_dict())


def load_state(mission_root: Path) -> model.ReleaseState:
    return model.ReleaseState.from_dict(
        read_document(mission_root, RELEASE_STATE_NAME))


def write_commit_handoff(mission_root: Path,
                         handoff: model.CommitHandoff) -> None:
    write_document(mission_root, COMMIT_HANDOFF_NAME, handoff.to_dict())


def load_commit_handoff(mission_root: Path) -> model.CommitHandoff:
    return model.CommitHandoff.from_dict(
        read_document(mission_root, COMMIT_HANDOFF_NAME))


def write_commit_result(mission_root: Path,
                        result: model.CommitResult) -> None:
    write_document(mission_root, COMMIT_RESULT_NAME, result.to_dict())


def load_commit_result(mission_root: Path) -> model.CommitResult:
    return model.CommitResult.from_dict(
        read_document(mission_root, COMMIT_RESULT_NAME))


def write_pr_binding(mission_root: Path,
                     binding: model.PullRequestBinding) -> None:
    write_document(mission_root, PR_BINDING_NAME, binding.to_dict())


def load_pr_binding(mission_root: Path) -> model.PullRequestBinding:
    return model.PullRequestBinding.from_dict(
        read_document(mission_root, PR_BINDING_NAME))


def write_ci_status(mission_root: Path, status: model.CiStatus) -> None:
    write_document(mission_root, CI_STATUS_NAME, status.to_dict())


def load_ci_status(mission_root: Path) -> model.CiStatus:
    return model.CiStatus.from_dict(read_document(mission_root, CI_STATUS_NAME))


def write_merge_handoff(mission_root: Path,
                        handoff: model.MergeHandoff) -> None:
    write_document(mission_root, MERGE_HANDOFF_NAME, handoff.to_dict())


def load_merge_handoff(mission_root: Path) -> model.MergeHandoff:
    return model.MergeHandoff.from_dict(
        read_document(mission_root, MERGE_HANDOFF_NAME))


def write_merge_result(mission_root: Path, result: model.MergeResult) -> None:
    write_document(mission_root, MERGE_RESULT_NAME, result.to_dict())


def load_merge_result(mission_root: Path) -> model.MergeResult:
    return model.MergeResult.from_dict(
        read_document(mission_root, MERGE_RESULT_NAME))


def write_release_closure(mission_root: Path,
                          closure: model.ReleaseClosure) -> None:
    write_document(mission_root, RELEASE_CLOSURE_NAME, closure.to_dict())


def load_release_closure(mission_root: Path) -> model.ReleaseClosure:
    return model.ReleaseClosure.from_dict(
        read_document(mission_root, RELEASE_CLOSURE_NAME))


def append_release_event(mission_root: Path, record: Mapping[str, Any]) -> None:
    path = _path(mission_root, RELEASE_EVENTS_NAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dict(record), sort_keys=True, separators=(",", ":"),
                      default=str)
    with path.open("ab") as handle:
        handle.write(line.encode("utf-8") + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_release_events(mission_root: Path) -> list[dict[str, Any]]:
    path = _path(mission_root, RELEASE_EVENTS_NAME)
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise model.ReleaseError(
            model.R_MALFORMED,
            f"release events unreadable: {type(exc).__name__}") from exc
    out: list[dict[str, Any]] = []
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        try:
            document = json.loads(line)
        except json.JSONDecodeError as exc:
            raise model.ReleaseError(
                model.R_MALFORMED,
                f"release event line {index}") from exc
        if not isinstance(document, dict):
            raise model.ReleaseError(model.R_MALFORMED,
                                     f"release event line {index}")
        out.append(document)
    return out[-MAX_RELEASE_EVENTS:]


def artifact_paths(mission_root: Path) -> dict[str, str]:
    """Record the release artifact paths that exist (never invented)."""
    names = (
        RELEASE_STATE_NAME, COMMIT_HANDOFF_NAME,
        COMMIT_RESULT_NAME, PR_BINDING_NAME, CI_STATUS_NAME,
        MERGE_HANDOFF_NAME, MERGE_RESULT_NAME, RELEASE_CLOSURE_NAME,
        RELEASE_EVENTS_NAME,
    )
    return {name: str(_path(mission_root, name)) for name in names
            if _path(mission_root, name).exists()}


__all__ = [
    "CI_STATUS_NAME",
    "COMMIT_HANDOFF_NAME",
    "COMMIT_RESULT_NAME",
    "MAX_RELEASE_EVENTS",
    "MERGE_HANDOFF_NAME",
    "MERGE_RESULT_NAME",
    "PR_BINDING_NAME",
    "RELEASE_CLOSURE_NAME",
    "RELEASE_EVENTS_NAME",
    "RELEASE_STATE_NAME",
    "append_release_event",
    "artifact_paths",
    "exists",
    "load_ci_status",
    "load_commit_handoff",
    "load_commit_result",
    "load_merge_handoff",
    "load_merge_result",
    "load_pr_binding",
    "load_release_closure",
    "load_release_events",
    "load_state",
    "read_document",
    "write_ci_status",
    "write_commit_handoff",
    "write_commit_result",
    "write_document",
    "write_merge_handoff",
    "write_merge_result",
    "write_pr_binding",
    "write_release_closure",
    "write_state",
]
