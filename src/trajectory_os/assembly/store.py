"""M031 — durable mission-root persistence (one canonical mission root).

Layout under ``<root>/<mission_id>/``::

    mission.json     durable mission identity + intent + baseline
    plan.json        bounded mission plan
    closure.json     durable closure / reconstruction evidence
    events.jsonl     canonical M030 operator timeline (shared)
    status.json      canonical M030 authoritative status (single truth)
    telemetry.json   canonical M030 run telemetry
    summary.json     canonical M030 deterministic aggregate

The M030 artifacts are written by the canonical observability layer and are
never duplicated here. This module only adds the three mission-level
documents and reuses the canonical atomic JSON writer so every mission-root
write is atomic and fail-closed.
"""

from __future__ import annotations

from pathlib import Path

from trajectory_os.assembly import model
from trajectory_os.observability import store as obs_store

MISSION_NAME = "mission.json"
PLAN_NAME = "plan.json"
CLOSURE_NAME = "closure.json"


def mission_root(root: str | Path, mission_id: str) -> Path:
    return Path(root) / mission_id


def ensure_mission_root(root: str | Path, mission_id: str) -> Path:
    path = mission_root(root, mission_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def mission_exists(root: str | Path, mission_id: str) -> bool:
    return (mission_root(root, mission_id) / MISSION_NAME).is_file()


def plan_exists(root: str | Path, mission_id: str) -> bool:
    return (mission_root(root, mission_id) / PLAN_NAME).is_file()


def closure_exists(root: str | Path, mission_id: str) -> bool:
    return (mission_root(root, mission_id) / CLOSURE_NAME).is_file()


def write_mission(root: str | Path, mission: model.MissionDefinition) -> None:
    obs_store.write_json(mission_root(root, mission.mission_id) / MISSION_NAME,
                         mission.to_dict())


def load_mission(root: str | Path,
                 mission_id: str) -> model.MissionDefinition:
    path = mission_root(root, mission_id) / MISSION_NAME
    try:
        document = obs_store.read_json(path)
    except obs_store.CanonicalStoreError as exc:
        raise model.AssemblyError(model.R_MISSION_MISSING, str(exc)) from exc
    return model.MissionDefinition.from_dict(document)


def write_plan(root: str | Path, plan: model.MissionPlan) -> None:
    obs_store.write_json(mission_root(root, plan.mission_id) / PLAN_NAME,
                         plan.to_dict())


def load_plan(root: str | Path, mission_id: str) -> model.MissionPlan:
    path = mission_root(root, mission_id) / PLAN_NAME
    try:
        document = obs_store.read_json(path)
    except obs_store.CanonicalStoreError as exc:
        raise model.AssemblyError(model.R_MISSION_MISSING, str(exc)) from exc
    return model.MissionPlan.from_dict(document)


def write_closure(root: str | Path, closure: model.MissionClosure) -> None:
    obs_store.write_json(
        mission_root(root, closure.mission_id) / CLOSURE_NAME,
        closure.to_dict())


def load_closure(root: str | Path, mission_id: str) -> model.MissionClosure:
    path = mission_root(root, mission_id) / CLOSURE_NAME
    try:
        document = obs_store.read_json(path)
    except obs_store.CanonicalStoreError as exc:
        raise model.AssemblyError(model.R_MISSION_MISSING, str(exc)) from exc
    return model.MissionClosure.from_dict(document)


__all__ = [
    "CLOSURE_NAME",
    "MISSION_NAME",
    "PLAN_NAME",
    "closure_exists",
    "ensure_mission_root",
    "load_closure",
    "load_mission",
    "load_plan",
    "mission_exists",
    "mission_root",
    "plan_exists",
    "write_closure",
    "write_mission",
    "write_plan",
]
