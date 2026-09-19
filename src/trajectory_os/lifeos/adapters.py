"""M028 — explicit, disableable LifeOS adapters.

Three adapters ship in V0:

* ``obsidian`` — a Markdown note (with YAML frontmatter) written into an
  Obsidian vault directory;
* ``super-productivity`` — a Super Productivity import JSON document;
* ``json-manifest`` — a complete machine-readable manifest of the scoped
  exchange, suitable for a future LifeOS consumer.

Every adapter is a pure-ish writer: it receives an immutable payload plus an
explicit target directory, writes only there, and returns content-addressed
output records. Adapters never read or write the canonical stores.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from trajectory_os.lifeos import model

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def slug(value: str, *, maximum: int = 96) -> str:
    cleaned = _SAFE.sub("_", value).strip("._-") or "item"
    return cleaned[:maximum]


@dataclass(frozen=True)
class ExchangePayload:
    """Immutable scoped projection handed to one adapter."""

    goal_id: str
    exchange_id: str
    generated_at: str
    snapshot: Mapping[str, Any]
    events: tuple[Mapping[str, Any], ...]
    artifacts: tuple[Mapping[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "lifeos_version": model.LIFEOS_VERSION,
            "schema_version": model.SCHEMA_VERSION,
            "goal_id": self.goal_id,
            "exchange_id": self.exchange_id,
            "generated_at": self.generated_at,
            "snapshot": dict(self.snapshot),
            "events": [dict(event) for event in self.events],
            "artifacts": [dict(artifact) for artifact in self.artifacts],
        }


class LifeOSAdapter(Protocol):
    """Structural contract every LifeOS adapter satisfies."""

    kind: str

    def exchange(self, payload: ExchangePayload, *, target: str,
                 options: Mapping[str, str]) -> list[model.OutputFile]:
        ...


def _write_atomic(path: Path, data: bytes) -> model.OutputFile:
    """Atomically write bytes and return their content-addressed record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=f".tmp.{os.getpid()}",
        dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return model.OutputFile(
        path=str(path), sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data))


def _event_lines(events: Sequence[Mapping[str, Any]],
                 limit: int = 200) -> list[str]:
    lines: list[str] = []
    for event in list(events)[-limit:]:
        severity = event.get("severity")
        category = event.get("category")
        kind = event.get("kind")
        subject = event.get("subject") or "-"
        reason = event.get("reason") or ""
        timestamp = event.get("occurred_at") or "-"
        lines.append(
            f"- `{timestamp}` **{severity}** {category}/{kind} "
            f"`{subject}` {reason}".rstrip())
    return lines or ["- (no events)"]


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


class ObsidianAdapter:
    """Write a scoped Markdown note into an Obsidian vault directory."""

    kind = model.AK_OBSIDIAN

    def exchange(self, payload: ExchangePayload, *, target: str,
                 options: Mapping[str, str]) -> list[model.OutputFile]:
        folder = options.get("folder") or "TrajectoryOS"
        base = Path(target) / slug(folder, maximum=64)
        snapshot = payload.snapshot
        final = _mapping(snapshot.get("final"))
        proof = _mapping(snapshot.get("proof"))
        goal = _mapping(snapshot.get("goal"))
        title = f"{slug(payload.goal_id)}"
        frontmatter = [
            "---",
            f"trajectory_goal: {payload.goal_id}",
            f"trajectory_exchange: {payload.exchange_id}",
            f"trajectory_state: {final.get('state') or 'UNKNOWN'}",
            f"trajectory_reason: {final.get('reason') or 'UNKNOWN'}",
            f"trajectory_proof: {proof.get('proof_id') or ''}",
            f"generated_at: {payload.generated_at}",
            "---",
            "",
        ]
        body = [
            f"# TrajectoryOS — {payload.goal_id}",
            "",
            f"**Objective:** {goal.get('objective') or '-'}",
            "",
            f"**State:** {final.get('state')}/{final.get('reason')} "
            f"(complete={final.get('complete')})",
            "",
            f"**Proof:** `{proof.get('proof_id') or '-'}` "
            f"(stale={proof.get('stale')})",
            "",
            f"## Events ({len(payload.events)})",
            "",
            *_event_lines(payload.events),
            "",
            f"## Artifacts ({len(payload.artifacts)})",
            "",
        ]
        if payload.artifacts:
            body.extend(
                f"- `{artifact.get('kind')}` {artifact.get('name')} "
                f"`{str(artifact.get('content_sha256') or '')[:16]}`"
                for artifact in payload.artifacts)
        else:
            body.append("- (no artifacts)")
        body.append("")
        body.append("_Projection only. Authoritative state remains in the "
                    "canonical TrajectoryOS stores._")
        body.append("")
        text = "\n".join(frontmatter) + "\n".join(body)
        return [_write_atomic(base / f"{title}.md", text.encode("utf-8"))]


class SuperProductivityAdapter:
    """Write a Super Productivity import JSON document."""

    kind = model.AK_SUPER_PRODUCTIVITY

    def exchange(self, payload: ExchangePayload, *, target: str,
                 options: Mapping[str, str]) -> list[model.OutputFile]:
        project = options.get("project") or "TrajectoryOS"
        snapshot = payload.snapshot
        final = _mapping(snapshot.get("final"))
        tasks: list[dict[str, Any]] = []
        tasks.append({
            "id": f"trajectory-goal-{slug(payload.goal_id, maximum=48)}",
            "title": f"TrajectoryOS goal {payload.goal_id}",
            "notes": (f"state={final.get('state')}/{final.get('reason')} "
                      f"exchange={payload.exchange_id}"),
            "projectId": project,
            "isDone": bool(final.get("complete")),
            "tagIds": ["trajectory-os"],
        })
        for event in payload.events:
            if event.get("severity") not in ("WARNING", "CRITICAL"):
                continue
            tasks.append({
                "id": f"trajectory-{slug(str(event.get('event_id')), maximum=48)}",
                "title": f"[{event.get('category')}] "
                         f"{event.get('subject') or event.get('kind')}",
                "notes": str(event.get("reason") or ""),
                "projectId": project,
                "isDone": False,
                "tagIds": ["trajectory-os", str(event.get("severity")).lower()],
            })
        document = {
            "project": {"title": project},
            "tasks": tasks,
            "trajectory_os": {
                "goal_id": payload.goal_id,
                "exchange_id": payload.exchange_id,
                "generated_at": payload.generated_at,
                "task_count": len(tasks),
            },
        }
        data = (json.dumps(document, indent=2, sort_keys=True)
                + "\n").encode("utf-8")
        name = f"trajectory-os-{slug(payload.goal_id)}.json"
        return [_write_atomic(Path(target) / name, data)]


class JsonManifestAdapter:
    """Write the complete scoped exchange as a machine-readable manifest."""

    kind = model.AK_JSON_MANIFEST

    def exchange(self, payload: ExchangePayload, *, target: str,
                 options: Mapping[str, str]) -> list[model.OutputFile]:
        data = (json.dumps(payload.to_dict(), indent=2, sort_keys=True)
                + "\n").encode("utf-8")
        name = options.get("name") or f"{slug(payload.goal_id)}.lifeos.json"
        return [_write_atomic(Path(target) / slug(name, maximum=128), data)]


#: Registry of the shipped adapters (closed set, mirrors ``model``).
ADAPTERS: dict[str, LifeOSAdapter] = {
    model.AK_OBSIDIAN: ObsidianAdapter(),
    model.AK_SUPER_PRODUCTIVITY: SuperProductivityAdapter(),
    model.AK_JSON_MANIFEST: JsonManifestAdapter(),
}


def get_adapter(kind: str) -> LifeOSAdapter:
    adapter = ADAPTERS.get(kind)
    if adapter is None:
        raise model.LifeOSError(
            model.E_INVALID_CONFIG, f"unknown adapter kind {kind!r}")
    return adapter
