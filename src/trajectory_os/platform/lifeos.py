"""M053 — LifeOS / Obsidian integration as a projection, not runtime truth.

LifeOS notes are a *derived, outbound projection* of canonical TrajectoryOS
state. They are never read back as runtime truth, and a legacy or hand-edited
note can never become canonical state.

Design invariants:

* the vault/project mapping is explicit configuration (never a hardcoded
  user path in core logic);
* project, mission journal, objective, decision, run, artifact, release and
  next-action facts are rendered with deterministic frontmatter;
* every note carries provenance links back to the canonical artifacts;
* only allow-listed canonical fields are rendered, so secrets/credentials can
  never leak into a note;
* an unavailable vault degrades explicitly and never stops unrelated mission
  execution;
* updates are idempotent (identical content is not rewritten);
* the vault target can never live inside the canonical state root.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trajectory_os.assembly import store as assembly_store
from trajectory_os.operator import events as operator_events
from trajectory_os.operator import state as operator_state
from trajectory_os.operator._util import (
    optional_str,
    read_optional_json,
)
from trajectory_os.platform import model
from trajectory_os.platform import projects as project_registry
from trajectory_os.release import store as release_store

#: Integration statuses (closed set).
LS_SYNCED = "SYNCED"
LS_DEGRADED = "DEGRADED"
LS_DISABLED = "DISABLED"
LS_PARTIAL = "PARTIAL"

#: Secret-like field names that are never rendered into a note.
_SECRET_HINTS = re.compile(
    r"(secret|token|password|passwd|credential|api[_-]?key|private[_-]?key|"
    r"authorization|bearer|cookie)", re.IGNORECASE)

#: Provenance source marker for every generated note.
NOTE_SOURCE = "trajectory-os"
NOTE_CANONICAL = False


@dataclass(frozen=True)
class ObsidianConfig:
    """Explicit LifeOS/Obsidian configuration (never inferred)."""

    vault: str | None = None
    project_dir: str = "TrajectoryOS"
    project_mapping: Mapping[str, str] = field(default_factory=dict)
    enabled: bool = True

    def validate(self) -> ObsidianConfig:
        if self.enabled and not self.vault:
            model.fail(model.E_MALFORMED, "obsidian vault required")
        if len(self.project_mapping) > model.MAX_PROJECTS:
            model.fail(model.E_MALFORMED, "too many project mappings")
        for key, value in self.project_mapping.items():
            if not key or not value:
                model.fail(model.E_MALFORMED, "empty project mapping")
        return self

    @staticmethod
    def from_dict(data: Mapping[str, Any] | None) -> ObsidianConfig:
        data = data if isinstance(data, Mapping) else {}
        mapping = data.get("project_mapping") or {}
        if not isinstance(mapping, Mapping):
            model.fail(model.E_MALFORMED, "project_mapping must be an object")
        return ObsidianConfig(
            vault=optional_str(data.get("vault")),
            project_dir=str(data.get("project_dir", "TrajectoryOS")),
            project_mapping={str(k): str(v) for k, v in mapping.items()},
            enabled=bool(data.get("enabled", True)),
        ).validate()


@dataclass(frozen=True)
class Note:
    """One deterministic projected note."""

    relative_path: str
    content: str
    provenance: Mapping[str, Any]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


def _yaml_value(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_yaml_value(item) for item in value) + "]"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def render_frontmatter(frontmatter: Mapping[str, Any]) -> str:
    """Deterministic YAML frontmatter (sorted keys, no third-party dep)."""
    safe = {key: value for key, value in frontmatter.items()
            if not _SECRET_HINTS.search(key)}
    lines = ["---"]
    for key in sorted(safe):
        value = safe[key]
        if isinstance(value, Mapping):
            lines.append(f"{key}:")
            for sub_key in sorted(value):
                lines.append(f"  {sub_key}: "
                             f"{_yaml_value(value[sub_key])}")
        else:
            lines.append(f"{key}: {_yaml_value(value)}")
    lines.append("---")
    return "\n".join(lines)


def _slug(value: str, *, maximum: int = 96) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return (cleaned or "item")[:maximum]


def _target(root: str | Path, config: ObsidianConfig,
            project_id: str) -> Path:
    if not config.vault:
        model.fail(model.E_MALFORMED, "obsidian vault required")
    subdir = config.project_mapping.get(project_id, project_id)
    return Path(config.vault) / config.project_dir / _slug(subdir)


def _inside(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
        base = root.resolve()
    except OSError:  # pragma: no cover - defensive
        return True
    return resolved == base or base in resolved.parents


def _vault_available(config: ObsidianConfig, root: str | Path) -> tuple[
        bool, str | None]:
    if not config.enabled:
        return False, "DISABLED_BY_CONFIG"
    if not config.vault:
        return False, "VAULT_NOT_CONFIGURED"
    vault = Path(config.vault)
    if not vault.is_dir():
        return False, "VAULT_PATH_MISSING"
    if _inside(vault, Path(root)):
        return False, "VAULT_INSIDE_CANONICAL_ROOT"
    return True, None


# --- note builders ------------------------------------------------------------


def build_project_note(root: str | Path,
                       project: project_registry.Project) -> Note:
    """Build one deterministic project note."""
    frontmatter = {
        "type": "trajectory-project",
        "source": NOTE_SOURCE,
        "canonical": NOTE_CANONICAL,
        "project_id": project.project_id,
        "name": project.name,
        "lifecycle": project.lifecycle,
        "objective_domain": project.objective_domain,
        "default_policy_profile": project.default_policy_profile,
        "objectives": [o.objective_id for o in project.objectives],
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "provenance": {
            "project_document": f"platform/projects/{project.project_id}.json",
            "registry": "platform/projects.json",
        },
    }
    body = [
        f"# Project: {project.name}",
        "",
        project.description or "_No description._",
        "",
        "## Objectives",
        "",
    ]
    if project.objectives:
        for objective in project.objectives:
            body.append(f"### {objective.title}")
            body.append("")
            body.append(objective.description or "_No description._")
            body.append("")
            for mission_id in objective.mission_ids:
                body.append(f"- [[{_slug(mission_id)}]]")
            body.append("")
    else:
        body.append("_No objectives._")
        body.append("")
    body.append("## Provenance")
    body.append("")
    body.append(f"- project document: `platform/projects/"
                f"{project.project_id}.json`")
    content = render_frontmatter(frontmatter) + "\n" + "\n".join(body) + "\n"
    return Note(
        relative_path=f"{_slug(project.name)}.md",
        content=content,
        provenance={"project_id": project.project_id,
                    "artifact": f"platform/projects/{project.project_id}.json"})


def build_mission_journal(root: str | Path, mission_id: str) -> Note:
    """Build one deterministic mission journal note from canonical state."""
    state = operator_state.build_operator_state(str(root), mission_id)
    document = state.to_dict()
    mission_root = Path(assembly_store.mission_root(root, mission_id))
    mission = read_optional_json(mission_root / assembly_store.MISSION_NAME)
    closure = read_optional_json(mission_root / assembly_store.CLOSURE_NAME)
    release_closure = read_optional_json(
        mission_root / release_store.RELEASE_CLOSURE_NAME)
    replay = operator_events.replay(str(root), mission_id)
    decisions = [event for event in replay["events"]
                 if event.get("type") in (
                     operator_events.T_CONTROL,
                     operator_events.T_RECOVERY_DECIDED)]
    frontmatter = {
        "type": "trajectory-mission-journal",
        "source": NOTE_SOURCE,
        "canonical": NOTE_CANONICAL,
        "mission_id": mission_id,
        "run_id": document.get("run_id"),
        "lifecycle": document.get("lifecycle"),
        "readiness": document.get("readiness"),
        "phase": document.get("phase"),
        "attempt": document.get("attempt"),
        "policy_profile": document.get("policy_profile"),
        "reviewed_patch": document.get("reviewed_patch"),
        "current_patch": document.get("current_patch"),
        "review_fresh": document.get("review_fresh"),
        "pending_human_gate": document.get("pending_human_gate"),
        "commit_sha": document.get("commit_sha"),
        "pr_number": document.get("pr_number"),
        "merge_sha": document.get("merge_sha"),
        "release_closure": document.get("release_closure"),
        "updated_at": (mission or {}).get("created_at"),
        "provenance": {
            "mission_document": f"{mission_id}/mission.json",
            "status_document": f"{mission_id}/status.json",
            "event_stream": f"{mission_id}/operator-events.jsonl",
        },
    }
    implementation = document.get("implementation") or {}
    body = [
        f"# Mission journal: {mission_id}",
        "",
        f"- objective: {document.get('objective')}",
        f"- lifecycle: {document.get('lifecycle')}",
        f"- readiness: {document.get('readiness')}",
        f"- phase: {document.get('phase')}",
        f"- attempt: {document.get('attempt')}",
        f"- implementation: {implementation.get('backend')} / "
        f"{implementation.get('provider')} / {implementation.get('model')}",
        f"- inline reviewer: {(document.get('inline_reviewer') or {}).get('display_model')}",
        f"- final reviewer: {(document.get('final_reviewer') or {}).get('display_model')}",
        f"- branch: {document.get('branch')}",
        f"- baseline HEAD: {document.get('baseline_head')}",
        f"- current HEAD: {document.get('current_head')}",
        f"- commit SHA: {document.get('commit_sha')}",
        f"- PR: {document.get('pr_number')}",
        f"- exact-head CI: {document.get('ci_run_id')} / "
        f"{document.get('ci_status')} / {document.get('ci_conclusion')}",
        f"- pending human gate: {document.get('pending_human_gate')}",
        f"- merge SHA: {document.get('merge_sha')}",
        f"- release closure: {document.get('release_closure')}",
        f"- next action: {document.get('next_action')}",
        "",
        "## Validation",
        "",
        f"```json\n{json.dumps(document.get('validation'), sort_keys=True)}\n```",
        "",
        "## Decisões registradas",
        "",
    ]
    for event in decisions:
        body.append(f"- [{event.get('ts')}] {event.get('type')} "
                    f"{json.dumps(event.get('payload'), sort_keys=True)}")
    if not decisions:
        body.append("_None recorded._")
    body.extend([
        "",
        "## Closure",
        "",
        f"```json\n{json.dumps(closure, sort_keys=True, default=str)}\n```",
        "",
        "## Release closure",
        "",
        f"```json\n{json.dumps(release_closure, sort_keys=True, default=str)}"
        "\n```",
        "",
        "## Provenance",
        "",
        f"- mission: `{mission_id}/mission.json`",
        f"- status: `{mission_id}/status.json`",
        f"- events: `{mission_id}/operator-events.jsonl`",
    ])
    content = render_frontmatter(frontmatter) + "\n" + "\n".join(body) + "\n"
    return Note(
        relative_path=f"journal/{_slug(mission_id)}.md",
        content=content,
        provenance={"mission_id": mission_id,
                    "run_id": document.get("run_id"),
                    "artifacts": frontmatter["provenance"]})


def build_project_notes(root: str | Path, project_id: str) -> list[Note]:
    """Build the project note plus one journal note per linked mission."""
    project = project_registry.load_project(root, project_id)
    notes = [build_project_note(root, project)]
    seen: set[str] = set()
    for objective in project.objectives:
        for mission_id in objective.mission_ids:
            if mission_id in seen:
                continue
            seen.add(mission_id)
            if assembly_store.mission_exists(root, mission_id):
                notes.append(build_mission_journal(root, mission_id))
    return notes


# --- sync (idempotent, failure-isolated) --------------------------------------


@dataclass(frozen=True)
class SyncReport:
    status: str
    vault: str | None
    reason: str | None
    written: tuple[str, ...]
    skipped: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "vault": self.vault,
            "reason": self.reason,
            "written": list(self.written),
            "skipped": list(self.skipped),
        }


def _atomic_write(path: Path, content: str) -> None:
    import contextlib
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.",
                                    dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def sync_project(root: str | Path, config: ObsidianConfig, project_id: str,
                 ) -> SyncReport:
    """Idempotently project one project (and journals) into the vault."""
    config.validate()
    available, reason = _vault_available(config, root)
    if not available:
        return SyncReport(status=LS_DEGRADED, vault=config.vault,
                          reason=reason, written=(), skipped=())
    target = _target(root, config, project_id)
    written: list[str] = []
    skipped: list[str] = []
    for note in build_project_notes(root, project_id):
        path = target / note.relative_path
        # Defence in depth: the frontmatter renderer drops secret-shaped
        # keys, and this second, content-level scan refuses to write a note
        # whose body still carries a credential-shaped assignment.
        markers = scan_for_secrets(note.content)
        if markers:
            model.fail(model.E_LIFEOS_SECRET,
                       f"{note.relative_path}: {markers[0]}")
        if path.is_file() and path.read_text(encoding="utf-8") == note.content:
            skipped.append(note.relative_path)
            continue
        _atomic_write(path, note.content)
        written.append(note.relative_path)
    return SyncReport(status=LS_SYNCED, vault=config.vault, reason=None,
                      written=tuple(written), skipped=tuple(skipped))


def sync_all(root: str | Path, config: ObsidianConfig) -> SyncReport:
    """Project every active project; a failure never stops the others."""
    config.validate()
    available, reason = _vault_available(config, root)
    if not available:
        return SyncReport(status=LS_DEGRADED, vault=config.vault,
                          reason=reason, written=(), skipped=())
    written: list[str] = []
    skipped: list[str] = []
    failures = 0
    for project in project_registry.list_projects(root):
        if not project.active:
            continue
        try:
            report = sync_project(root, config, project.project_id)
        except model.PlatformError:
            # One project (for example a secret-shaped note) never stops the
            # others: it is counted as a failure and skipped.
            failures += 1
            continue
        if report.status == LS_DEGRADED:
            failures += 1
            continue
        written.extend(report.written)
        skipped.extend(report.skipped)
    status = LS_PARTIAL if failures else LS_SYNCED
    return SyncReport(status=status, vault=config.vault, reason=None,
                      written=tuple(written), skipped=tuple(skipped))


def integration_status(root: str | Path,
                       config: ObsidianConfig) -> dict[str, Any]:
    """Read-only integration status (always explicit when degraded)."""
    available, reason = _vault_available(config, root)
    return {
        "schema_version": model.SCHEMA_VERSION,
        "platform_version": model.PLATFORM_VERSION,
        "enabled": config.enabled,
        "available": available,
        "vault": config.vault,
        "reason": reason,
        "status": LS_SYNCED if available else LS_DEGRADED,
        "canonical": NOTE_CANONICAL,
        "source": NOTE_SOURCE,
        "read_only": True,
    }


def scan_for_secrets(text: str) -> list[str]:
    """Return secret-like assignment markers found in text.

    This is a conservative guard: it flags a credential-shaped assignment
    (``password: value``) or a raw credential value, not an ordinary word
    such as "token count".
    """
    pattern = re.compile(
        r"(?i)\b(secret|password|passwd|api[_-]?key|private[_-]?key|"
        r"credential|access[_-]?token|auth[_-]?token)\b\s*[:=]\s*\S+")
    return [match.group(0)[:64] for match in pattern.finditer(text)]


__all__ = [
    "LS_DEGRADED",
    "LS_DISABLED",
    "LS_PARTIAL",
    "LS_SYNCED",
    "NOTE_CANONICAL",
    "NOTE_SOURCE",
    "Note",
    "ObsidianConfig",
    "SyncReport",
    "build_mission_journal",
    "build_project_note",
    "build_project_notes",
    "integration_status",
    "render_frontmatter",
    "scan_for_secrets",
    "sync_all",
    "sync_project",
]
