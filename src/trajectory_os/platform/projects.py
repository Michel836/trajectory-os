"""M048 — workspace and project registry (durable identity above missions).

A **project** is the durable identity that sits above objectives and missions.
It never replaces a canonical mission/release/artifact document: it stores
only project metadata plus explicit linkage to the canonical mission ids, and
mission/release/artifact facts are always *derived* read-only from the
canonical M030/M031/M036–M047 stores.

Design invariants:

* ``project_id`` is stable for the life of the project and is a
  domain-separated digest of the project name when not supplied explicitly;
* the default policy and routing preferences are explicit project metadata;
  the environment is never consulted and an environment-derived mutation
  fails closed;
* active/archived lifecycle is explicit; an archived project is read-only;
* deterministic schema version and reconstruction from the per-project
  documents (the index is a derived convenience, never truth);
* every read is a pure projection.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from trajectory_os.assembly import store as assembly_store
from trajectory_os.operator import model as operator_model
from trajectory_os.operator._util import (
    append_jsonl,
    digest,
    optional_str,
    read_optional_json,
    utc_now,
    write_json,
)
from trajectory_os.operator.policy import PROFILES
from trajectory_os.platform import model
from trajectory_os.release import store as release_store

#: Domain id for one project identity.
PROJECT_ID_DOMAIN = "trajectory-os.platform-project-id.v1"

#: Project lifecycle (closed set).
PL_ACTIVE = "ACTIVE"
PL_ARCHIVED = "ARCHIVED"
LIFECYCLES = frozenset({PL_ACTIVE, PL_ARCHIVED})

#: Explicit mutation sources; the environment is never accepted.
SOURCE_OPERATOR = "operator"
SOURCE_ENVIRONMENT = "environment"


def project_id_for(name: str) -> str:
    """Deterministic stable project id for an explicit project name."""
    return digest({"name": name}, domain=PROJECT_ID_DOMAIN)


def _require_str(value: object, field_name: str, *,
                 maximum: int = model.MAX_STR_LEN,
                 optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        model.fail(model.E_PROJECT_INVALID, f"{field_name} required")
    if len(value) > maximum:
        model.fail(model.E_PROJECT_INVALID, f"{field_name} too long")
    return value


@dataclass(frozen=True)
class Objective:
    """One project objective that groups canonical missions."""

    objective_id: str
    title: str
    description: str = ""
    mission_ids: tuple[str, ...] = ()

    def validate(self) -> Objective:
        _require_str(self.objective_id, "objective_id")
        _require_str(self.title, "objective title")
        if len(self.mission_ids) > model.MAX_MISSIONS_PER_OBJECTIVE:
            model.fail(model.E_PROJECT_INVALID, "too many missions")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective_id": self.objective_id,
            "title": self.title,
            "description": self.description,
            "mission_ids": list(self.mission_ids),
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> Objective:
        raw = data.get("mission_ids") or []
        if not isinstance(raw, list):
            model.fail(model.E_MALFORMED, "mission_ids must be a list")
        return Objective(
            objective_id=str(data.get("objective_id", "")),
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            mission_ids=tuple(str(item) for item in raw),
        ).validate()


@dataclass(frozen=True)
class Project:
    """Durable project metadata (never canonical mission/release truth)."""

    project_id: str
    name: str
    description: str
    objective_domain: str
    workspace: str
    repository: str | None
    default_policy_profile: str
    routing_preferences: Mapping[str, Any] = field(default_factory=dict)
    lifecycle: str = PL_ACTIVE
    objectives: tuple[Objective, ...] = ()
    created_at: str = ""
    updated_at: str = ""
    schema_version: int = model.SCHEMA_VERSION
    platform_version: str = model.PLATFORM_VERSION

    def validate(self) -> Project:
        _require_str(self.project_id, "project_id")
        _require_str(self.name, "name")
        _require_str(self.objective_domain, "objective_domain")
        _require_str(self.workspace, "workspace")
        if len(self.project_id) > 128:
            model.fail(model.E_PROJECT_INVALID, "project_id too long")
        if self.lifecycle not in LIFECYCLES:
            model.fail(model.E_PROJECT_INVALID, f"lifecycle {self.lifecycle!r}")
        if self.default_policy_profile not in PROFILES:
            model.fail(model.E_PROJECT_INVALID,
                       f"policy {self.default_policy_profile!r}")
        if len(self.objectives) > model.MAX_OBJECTIVES:
            model.fail(model.E_PROJECT_INVALID, "too many objectives")
        seen: set[str] = set()
        for objective in self.objectives:
            objective.validate()
            if objective.objective_id in seen:
                model.fail(model.E_PROJECT_INVALID,
                           f"duplicate objective {objective.objective_id!r}")
            seen.add(objective.objective_id)
        if len(self.routing_preferences) > model.MAX_ROUTING_PREFS:
            model.fail(model.E_PROJECT_INVALID,
                       "too many routing preferences")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "platform_version": self.platform_version,
            "project_id": self.project_id,
            "name": self.name,
            "description": self.description,
            "objective_domain": self.objective_domain,
            "workspace": self.workspace,
            "repository": self.repository,
            "default_policy_profile": self.default_policy_profile,
            "routing_preferences": dict(sorted(self.routing_preferences.items())),
            "lifecycle": self.lifecycle,
            "objectives": [objective.to_dict() for objective in self.objectives],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @property
    def active(self) -> bool:
        return self.lifecycle == PL_ACTIVE

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> Project:
        version = data.get("schema_version", model.SCHEMA_VERSION)
        if version != model.SCHEMA_VERSION:
            model.fail(model.E_UNSUPPORTED_VERSION, str(version))
        raw_objectives = data.get("objectives") or []
        if not isinstance(raw_objectives, list):
            model.fail(model.E_MALFORMED, "objectives must be a list")
        prefs = data.get("routing_preferences") or {}
        if not isinstance(prefs, Mapping):
            model.fail(model.E_MALFORMED, "routing_preferences must be object")
        return Project(
            project_id=str(data.get("project_id", "")),
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            objective_domain=str(data.get("objective_domain", "")),
            workspace=str(data.get("workspace", "")),
            repository=optional_str(data.get("repository")),
            default_policy_profile=str(
                data.get("default_policy_profile", "")),
            routing_preferences=dict(prefs),
            lifecycle=str(data.get("lifecycle", PL_ACTIVE)),
            objectives=tuple(
                Objective.from_dict(item)
                for item in raw_objectives if isinstance(item, Mapping)),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            schema_version=int(version),
        ).validate()


# --- storage ------------------------------------------------------------------


def projects_dir(root: str | Path) -> Path:
    return Path(root) / model.PLATFORM_DIR / model.PROJECTS_DIR


def project_path(root: str | Path, project_id: str) -> Path:
    return projects_dir(root) / f"{project_id}.json"


def project_index_path(root: str | Path) -> Path:
    return Path(root) / model.PLATFORM_DIR / model.PROJECT_INDEX_NAME


def create_project(
    root: str | Path,
    *,
    name: str,
    description: str = "",
    objective_domain: str = "general",
    workspace: str,
    repository: str | None = None,
    default_policy_profile: str = "release",
    routing_preferences: Mapping[str, Any] | None = None,
    project_id: str | None = None,
    source: str = SOURCE_OPERATOR,
    clock: Any = utc_now,
) -> Project:
    """Create and persist one explicit project identity."""
    if source == SOURCE_ENVIRONMENT:
        model.fail(model.E_PROJECT_ENVIRONMENT,
                   "projects must be created from explicit operator input")
    root_path = Path(root)
    identifier = project_id or project_id_for(name)
    path = project_path(root_path, identifier)
    if path.is_file():
        model.fail(model.E_PROJECT_EXISTS, identifier)
    stamp = clock()
    project = Project(
        project_id=identifier,
        name=name,
        description=description,
        objective_domain=objective_domain,
        workspace=workspace,
        repository=repository,
        default_policy_profile=default_policy_profile,
        routing_preferences=dict(routing_preferences or {}),
        lifecycle=PL_ACTIVE,
        objectives=(),
        created_at=stamp,
        updated_at=stamp,
    ).validate()
    persist_project(root_path, project)
    return project


def persist_project(root: str | Path, project: Project) -> None:
    project.validate()
    path = project_path(root, project.project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, project.to_dict())
    _rebuild_index(root)


def load_project(root: str | Path, project_id: str) -> Project:
    document = read_optional_json(project_path(root, project_id))
    if document is None:
        model.fail(model.E_PROJECT_MISSING, project_id)
    project = Project.from_dict(document)
    if project.project_id != project_id:
        model.fail(model.E_PROJECT_INVALID,
                   f"project_id {project.project_id!r} != {project_id!r}")
    return project


def update_project(
    root: str | Path,
    project_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    objective_domain: str | None = None,
    workspace: str | None = None,
    repository: str | None = None,
    default_policy_profile: str | None = None,
    routing_preferences: Mapping[str, Any] | None = None,
    source: str = SOURCE_OPERATOR,
    clock: Any = utc_now,
) -> Project:
    """Update explicit project metadata (archived projects are read-only)."""
    if source == SOURCE_ENVIRONMENT:
        model.fail(model.E_PROJECT_ENVIRONMENT,
                   "projects must be updated from explicit operator input")
    current = load_project(root, project_id)
    if not current.active:
        model.fail(model.E_PROJECT_ARCHIVED, project_id)
    updated = replace(
        current,
        name=name if name is not None else current.name,
        description=(description if description is not None
                     else current.description),
        objective_domain=(objective_domain if objective_domain is not None
                          else current.objective_domain),
        workspace=workspace if workspace is not None else current.workspace,
        repository=repository if repository is not None else current.repository,
        default_policy_profile=(default_policy_profile
                                if default_policy_profile is not None
                                else current.default_policy_profile),
        routing_preferences=(dict(routing_preferences)
                             if routing_preferences is not None
                             else current.routing_preferences),
        updated_at=clock(),
    ).validate()
    persist_project(root, updated)
    return updated


def archive_project(root: str | Path, project_id: str, *,
                    clock: Any = utc_now) -> Project:
    """Archive a project (idempotent, never deletes history)."""
    current = load_project(root, project_id)
    if not current.active:
        return current
    archived = replace(current, lifecycle=PL_ARCHIVED,
                       updated_at=clock()).validate()
    persist_project(root, archived)
    return archived


# --- objectives and mission linkage -------------------------------------------


def add_objective(root: str | Path, project_id: str, *,
                  objective_id: str, title: str, description: str = "",
                  clock: Any = utc_now) -> Project:
    current = load_project(root, project_id)
    if not current.active:
        model.fail(model.E_PROJECT_ARCHIVED, project_id)
    if any(o.objective_id == objective_id for o in current.objectives):
        model.fail(model.E_PROJECT_INVALID,
                   f"objective {objective_id!r} exists")
    objective = Objective(objective_id=objective_id, title=title,
                          description=description).validate()
    updated = replace(current,
                      objectives=(*current.objectives, objective),
                      updated_at=clock()).validate()
    persist_project(root, updated)
    return updated


def link_mission(root: str | Path, project_id: str, *,
                 objective_id: str, mission_id: str,
                 clock: Any = utc_now) -> Project:
    """Link one canonical mission id under a project objective (idempotent)."""
    current = load_project(root, project_id)
    if not current.active:
        model.fail(model.E_PROJECT_ARCHIVED, project_id)
    found = False
    objectives: list[Objective] = []
    for objective in current.objectives:
        if objective.objective_id == objective_id:
            found = True
            if mission_id in objective.mission_ids:
                objectives.append(objective)
                continue
            objectives.append(replace(
                objective,
                mission_ids=(*objective.mission_ids, mission_id)))
        else:
            objectives.append(objective)
    if not found:
        model.fail(model.E_PROJECT_INVALID,
                   f"objective {objective_id!r} not found")
    updated = replace(current, objectives=tuple(objectives),
                      updated_at=clock()).validate()
    persist_project(root, updated)
    _record_link(root, project_id, objective_id, mission_id, clock)
    return updated


def _record_link(root: str | Path, project_id: str, objective_id: str,
                 mission_id: str, clock: Any) -> None:
    path = (Path(root) / model.PLATFORM_DIR / "linkage.jsonl")
    append_jsonl(path, {
        "schema_version": model.SCHEMA_VERSION,
        "project_id": project_id,
        "objective_id": objective_id,
        "mission_id": mission_id,
        "linked_at": clock(),
    })


# --- read-only projections ----------------------------------------------------


def list_projects(root: str | Path) -> list[Project]:
    """Read-only deterministic project list (never mutates)."""
    directory = projects_dir(root)
    if not directory.is_dir():
        return []
    projects: list[Project] = []
    for path in sorted(directory.glob("*.json")):
        document = read_optional_json(path)
        if document is None:
            continue
        try:
            projects.append(Project.from_dict(document))
        except model.PlatformError:
            continue
    return sorted(projects, key=lambda project: project.project_id)


def project_linkage(root: str | Path, project_id: str) -> dict[str, Any]:
    """Derive project -> objectives -> missions -> releases -> artifacts.

    The linkage is a read-only projection over the canonical mission roots.
    Missing canonical artifacts are reported as unknown, never invented.
    """
    project = load_project(root, project_id)
    objectives: list[dict[str, Any]] = []
    for objective in project.objectives:
        missions: list[dict[str, Any]] = []
        for mission_id in objective.mission_ids:
            missions.append(_mission_linkage(root, mission_id))
        objectives.append({
            **objective.to_dict(),
            "missions": missions,
        })
    return {
        "schema_version": model.SCHEMA_VERSION,
        "platform_version": model.PLATFORM_VERSION,
        "project": project.to_dict(),
        "objectives": objectives,
        "read_only": True,
    }


def _mission_linkage(root: str | Path, mission_id: str) -> dict[str, Any]:
    mission_root = Path(assembly_store.mission_root(root, mission_id))
    status = read_optional_json(mission_root / "status.json")
    release_state = read_optional_json(
        mission_root / release_store.RELEASE_STATE_NAME)
    commit_result = read_optional_json(
        mission_root / release_store.COMMIT_RESULT_NAME)
    merge_result = read_optional_json(
        mission_root / release_store.MERGE_RESULT_NAME)
    release_closure = read_optional_json(
        mission_root / release_store.RELEASE_CLOSURE_NAME)
    routing = read_optional_json(mission_root / operator_model.ROUTING_NAME)
    policy = read_optional_json(mission_root / operator_model.POLICY_NAME)
    return {
        "mission_id": mission_id,
        "present": assembly_store.mission_exists(root, mission_id),
        "lifecycle": (status or {}).get("state"),
        "readiness": (status or {}).get("readiness"),
        "phase": (status or {}).get("phase"),
        "policy_profile": (policy or {}).get("profile"),
        "implementation": (routing or {}).get("implementation"),
        "release_stage": (release_state or {}).get("stage"),
        "commit_sha": (commit_result or {}).get("commit_sha"),
        "merge_sha": (merge_result or {}).get("merge_sha"),
        "release_status": (release_closure or {}).get("status"),
    }


def project_status(root: str | Path, project_id: str) -> dict[str, Any]:
    """Read-only project status projection."""
    linkage = project_linkage(root, project_id)
    missions = [mission for objective in linkage["objectives"]
                for mission in objective["missions"]]
    active = sum(1 for mission in missions
                 if mission["lifecycle"] in ("RUNNING", "IMPLEMENTING",
                                             "VALIDATING", "REVIEWING",
                                             "REPAIRING", "PREFLIGHT"))
    blocked = sum(1 for mission in missions
                  if mission["readiness"] == "BLOCKED")
    ready = sum(1 for mission in missions
                if mission["readiness"] == "READY_FOR_COMMIT")
    return {
        "schema_version": model.SCHEMA_VERSION,
        "platform_version": model.PLATFORM_VERSION,
        "project_id": project_id,
        "name": linkage["project"]["name"],
        "lifecycle": linkage["project"]["lifecycle"],
        "default_policy_profile": linkage["project"]["default_policy_profile"],
        "objectives": len(linkage["objectives"]),
        "missions": len(missions),
        "active_missions": active,
        "blocked_missions": blocked,
        "ready_for_commit": ready,
        "read_only": True,
    }


# --- index and reconstruction -------------------------------------------------


def _rebuild_index(root: str | Path) -> dict[str, Any]:
    projects = list_projects(root)
    index = {
        "schema_version": model.SCHEMA_VERSION,
        "platform_version": model.PLATFORM_VERSION,
        "project_ids": [project.project_id for project in projects],
        "projects": {
            project.project_id: {
                "name": project.name,
                "lifecycle": project.lifecycle,
                "default_policy_profile": project.default_policy_profile,
            }
            for project in projects
        },
    }
    write_json(project_index_path(root), index)
    return index


def reconstruct_projects(root: str | Path) -> dict[str, Any]:
    """Deterministically reconstruct the index from per-project documents.

    Reads every project document strictly, fails closed on a corrupt document,
    and rebuilds the derived index. Returns the reconstructed summary.
    """
    directory = projects_dir(root)
    if not directory.is_dir():
        return {"projects": 0, "reconstructed": True, "project_ids": []}
    project_ids: list[str] = []
    for path in sorted(directory.glob("*.json")):
        document = read_optional_json(path)
        if document is None:
            model.fail(model.E_CORRUPTION, str(path))
        project = Project.from_dict(document)
        project.validate()
        expected = path.stem
        if project.project_id != expected:
            model.fail(model.E_PROJECT_INVALID,
                       f"{path}: {project.project_id!r} != {expected!r}")
        project_ids.append(project.project_id)
    _rebuild_index(root)
    return {
        "projects": len(project_ids),
        "reconstructed": True,
        "project_ids": project_ids,
    }


__all__ = [
    "LIFECYCLES",
    "PL_ACTIVE",
    "PL_ARCHIVED",
    "PROJECT_ID_DOMAIN",
    "SOURCE_ENVIRONMENT",
    "SOURCE_OPERATOR",
    "Objective",
    "Project",
    "add_objective",
    "archive_project",
    "create_project",
    "link_mission",
    "list_projects",
    "load_project",
    "persist_project",
    "project_id_for",
    "project_index_path",
    "project_linkage",
    "project_path",
    "project_status",
    "projects_dir",
    "reconstruct_projects",
    "update_project",
]
