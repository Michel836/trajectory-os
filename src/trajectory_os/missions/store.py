"""Mission 003 — durable mission store (atomic, strict, fail-closed).

Persistence layout (all under ``<root>/missions/<mission_id>/``)::

    mission.json                     canonical mission state (strict schema 1)
    events.jsonl                     bounded event evidence log (append-only)
    subruns/<subrun_id>.json         per sub-run evidence record (strict)
    subruns/<subrun_id>.stdout.log   sub-run stdout evidence
    subruns/<subrun_id>.stderr.log   sub-run stderr evidence
    evidence/<phase_id>.json         bounded phase evidence for later phases

Rules (ADR-005):

* every write is atomic (temp file + fsync + rename) or append-only JSONL;
* every read is strict: unknown fields, unknown states, unknown schema
  versions, and structurally invalid documents fail closed with
  :class:`MalformedMissionError` — never guess, never repair data silently;
* no DB, no daemon, no locks beyond the atomic-rename semantics of the
  underlying filesystem (single mission owner at a time — same-worktree
  contention is detected and fail-closed, not serialized implicitly).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trajectory_os.missions import model
from trajectory_os.runs.store import atomic_write_json


class MalformedMissionError(Exception):
    """Persisted mission evidence violates the canonical schema (fail closed)."""

    def __init__(self, code: str, path: str, detail: str = "") -> None:
        super().__init__(f"{code}: {path}" + (f" ({detail})" if detail else ""))
        self.code = code
        self.path = path
        self.detail = detail


class MissionNotFound(Exception):
    """The requested mission identity does not exist in the store."""

    def __init__(self, mission_id: str, root: str) -> None:
        super().__init__(f"no mission {mission_id!r} under {root!r}")
        self.mission_id = mission_id
        self.root = root


# --- paths ---------------------------------------------------------------------


def mission_root(root: str | Path, mission_id: str) -> Path:
    return Path(root) / "missions" / mission_id


def mission_paths(root: str | Path, mission_id: str) -> dict[str, Path]:
    base = mission_root(root, mission_id)
    return {
        "root": base,
        "mission": base / "mission.json",
        "events": base / "events.jsonl",
        "subruns": base / "subruns",
        "evidence": base / "evidence",
    }


def subrun_paths(paths: Mapping[str, Path], subrun_id: str) -> dict[str, Path]:
    """Record + evidence file paths for one sub-run."""
    base = Path(paths["subruns"])
    return {
        "record": base / f"{subrun_id}.json",
        "stdout": base / f"{subrun_id}.stdout.log",
        "stderr": base / f"{subrun_id}.stderr.log",
    }


def evidence_path(paths: dict[str, Path], phase_id: str) -> Path:
    return Path(paths["evidence"]) / f"{phase_id}.json"


def ensure_layout(paths: dict[str, Path]) -> None:
    for key in ("root", "subruns", "evidence"):
        Path(paths[key]).mkdir(parents=True, exist_ok=True)


# --- strict value checks -------------------------------------------------------


def _chk(cond: bool, code: str, path: str, detail: str = "") -> None:
    if not cond:
        raise MalformedMissionError(code, path, detail)


def _str(v: Any, path: str, detail: str = "") -> str:
    if not (isinstance(v, str) and v):
        raise MalformedMissionError(model.R_MALFORMED_STATE, path, detail)
    return v


def _int(v: Any, path: str, detail: str = "",
         *, maximum: int | None = None) -> int:
    """Strict non-negative integer (counters, attempts, budgets, ids).

    ``v >= 0`` is part of the helper contract: every persisted counter is a
    count and must never be negative.  Integer fields with an additional
    *inclusive* upper bound (e.g. ``exit_code``: 0..255) pass ``maximum``
    so the range stays in one auditable check instead of an ad-hoc inline
    re-test that can drift from the helper's contract.
    """
    if not (isinstance(v, int) and not isinstance(v, bool) and v >= 0
            and (maximum is None or v <= maximum)):
        raise MalformedMissionError(model.R_MALFORMED_STATE, path, detail)
    return v


def _opt_str(v: Any, path: str, detail: str = "") -> str | None:
    if v is None:
        return None
    return _str(v, path, detail)


def _str_list(v: Any, path: str, detail: str = "") -> list[str]:
    _chk(isinstance(v, list) and all(isinstance(x, str) for x in v),
         model.R_MALFORMED_STATE, path, detail)
    return list(v)


def _opt_dict(v: Any, path: str, detail: str = "") -> dict[str, Any] | None:
    if v is None:
        return None
    _chk(isinstance(v, dict) and all(isinstance(k, str) for k in v),
         model.R_MALFORMED_STATE, path, detail)
    return dict(v)


def _known_keys(doc: Mapping[str, Any], allowed: tuple[str, ...], path: str) -> None:
    unknown = set(doc) - set(allowed)
    _chk(not unknown, model.R_MALFORMED_STATE, path,
         f"unknown field(s): {sorted(unknown)}")


# --- phase --------------------------------------------------------------------

_PHASE_KEYS = (
    "phase_id", "kind", "mode", "round", "depends_on", "command",
    "resources", "attempt", "max_attempts", "repairs_at_attempt",
    "state", "reason", "subrun_ids", "started_at", "finished_at",
)


@dataclass
class PhaseDoc:
    phase_id: str
    kind: str
    mode: str
    round: int
    depends_on: list[str]
    command: list[str]
    resources: dict[str, Any] | None
    attempt: int
    max_attempts: int
    repairs_at_attempt: int
    state: str
    reason: str
    subrun_ids: list[str]
    started_at: str | None
    finished_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase_id": self.phase_id,
            "kind": self.kind,
            "mode": self.mode,
            "round": self.round,
            "depends_on": list(self.depends_on),
            "command": list(self.command),
            "resources": dict(self.resources) if self.resources is not None else None,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "repairs_at_attempt": self.repairs_at_attempt,
            "state": self.state,
            "reason": self.reason,
            "subrun_ids": list(self.subrun_ids),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @staticmethod
    def from_dict(doc: Mapping[str, Any], path: str) -> PhaseDoc:
        _known_keys(doc, _PHASE_KEYS, path)
        return PhaseDoc(
            phase_id=_str(doc["phase_id"], path, "phase_id"),
            kind=_str(doc["kind"], path, "kind"),
            mode=_str(doc["mode"], path, "mode"),
            round=_int(doc["round"], path, "round"),
            depends_on=_str_list(doc["depends_on"], path, "depends_on"),
            command=_str_list(doc["command"], path, "command"),
            resources=_opt_dict(doc["resources"], path, "resources"),
            attempt=_int(doc["attempt"], path, "attempt"),
            max_attempts=_int(doc["max_attempts"], path, "max_attempts"),
            repairs_at_attempt=_int(doc["repairs_at_attempt"], path, "repairs_at_attempt"),
            state=_str(doc["state"], path, "state"),
            reason=_str(doc["reason"], path, "reason"),
            subrun_ids=_str_list(doc["subrun_ids"], path, "subrun_ids"),
            started_at=_opt_str(doc["started_at"], path, "started_at"),
            finished_at=_opt_str(doc["finished_at"], path, "finished_at"),
        )

    def terminal(self) -> bool:
        return self.state in model.TERMINAL_PHASE_STATES

    def depends_satisfied(self, states: Mapping[str, str]) -> bool:
        return all(states.get(dep) == model.PS_PASSED for dep in self.depends_on)


# --- sub-run -------------------------------------------------------------------

_SUBRUN_KEYS = (
    "subrun_id", "phase_id", "kind", "mode", "round", "attempt",
    "command", "cwd", "started_at", "finished_at", "exit_code",
    "classification", "stdout_file", "stderr_file", "resources",
)


@dataclass
class SubrunDoc:
    subrun_id: str
    phase_id: str
    kind: str
    mode: str
    round: int
    attempt: int
    command: list[str]
    cwd: str | None
    started_at: str
    finished_at: str | None
    exit_code: int | None
    classification: str
    stdout_file: str
    stderr_file: str
    resources: dict[str, Any] | None

    @property
    def provider_failure(self) -> bool:
        return (
            self.classification in model.PROVIDER_FAILURE_CLASSIFICATIONS
            or (self.classification == model.CR_FAILED
                and self.exit_code is not None
                and self.exit_code > model.MAX_DETERMINISTIC_FAILURE_EXIT)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "subrun_id": self.subrun_id,
            "phase_id": self.phase_id,
            "kind": self.kind,
            "mode": self.mode,
            "round": self.round,
            "attempt": self.attempt,
            "command": list(self.command),
            "cwd": self.cwd,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "classification": self.classification,
            "stdout_file": self.stdout_file,
            "stderr_file": self.stderr_file,
            "resources": dict(self.resources) if self.resources is not None else None,
        }

    @staticmethod
    def from_dict(doc: Mapping[str, Any], path: str) -> SubrunDoc:
        _known_keys(doc, _SUBRUN_KEYS, path)
        # exit_code is optional (absent => CRASHED); when present it is an
        # integer exit status in 0..255.  Validated through the same _int
        # helper as the counters (lower bound) plus its inclusive maximum,
        # so the range contract lives in one place.
        exit_code = doc["exit_code"]
        if exit_code is not None:
            exit_code = _int(exit_code, path, "exit_code", maximum=255)
        return SubrunDoc(
            subrun_id=_str(doc["subrun_id"], path, "subrun_id"),
            phase_id=_str(doc["phase_id"], path, "phase_id"),
            kind=_str(doc["kind"], path, "kind"),
            mode=_str(doc["mode"], path, "mode"),
            round=_int(doc["round"], path, "round"),
            attempt=_int(doc["attempt"], path, "attempt"),
            command=_str_list(doc["command"], path, "command"),
            cwd=_opt_str(doc["cwd"], path, "cwd"),
            started_at=_str(doc["started_at"], path, "started_at"),
            finished_at=_opt_str(doc["finished_at"], path, "finished_at"),
            exit_code=None if exit_code is None else int(exit_code),
            classification=_str(doc["classification"], path, "classification"),
            stdout_file=_str(doc["stdout_file"], path, "stdout_file"),
            stderr_file=_str(doc["stderr_file"], path, "stderr_file"),
            resources=_opt_dict(doc["resources"], path, "resources"),
        )


# --- human notes ---------------------------------------------------------------

_NOTE_KEYS = ("note", "at")


@dataclass(frozen=True)
class HumanNote:
    note: str
    at: str

    def to_dict(self) -> dict[str, Any]:
        return {"note": self.note, "at": self.at}

    @staticmethod
    def from_dict(doc: Mapping[str, Any], path: str) -> HumanNote:
        _known_keys(doc, _NOTE_KEYS, path)
        return HumanNote(note=_str(doc["note"], path, "note"), at=_str(doc["at"], path, "at"))


# --- mission --------------------------------------------------------------------

_MISSION_KEYS = (
    "schema_version", "mission_id", "objective", "repo_root", "cwd",
    "baseline_revision", "created_at", "started_at", "finished_at",
    "time_budget_s", "time_budget_deadline",
    "repair_budget", "subrun_budget",
    "subrun_started", "subrun_completed", "repairs_used",
    "mission_state", "mission_reason",
    "phases", "subruns", "human_notes", "updated_at",
)


@dataclass
class MissionDoc:
    schema_version: int
    mission_id: str
    objective: str
    repo_root: str | None
    cwd: str | None
    baseline_revision: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    time_budget_s: int
    time_budget_deadline: str | None
    repair_budget: int
    subrun_budget: int
    subrun_started: int
    subrun_completed: int
    repairs_used: int
    mission_state: str
    mission_reason: str
    phases: list[PhaseDoc] = field(default_factory=list)
    subruns: list[str] = field(default_factory=list)
    human_notes: list[HumanNote] = field(default_factory=list)
    updated_at: str = ""

    def phase(self, phase_id: str) -> PhaseDoc:
        for p in self.phases:
            if p.phase_id == phase_id:
                return p
        raise KeyLookupErrorNo(phase_id)

    def phase_states(self) -> dict[str, str]:
        return {p.phase_id: p.state for p in self.phases}

    def terminal(self) -> bool:
        return self.mission_state in model.TERMINAL_MISSION_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mission_id": self.mission_id,
            "objective": self.objective,
            "repo_root": self.repo_root,
            "cwd": self.cwd,
            "baseline_revision": self.baseline_revision,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "time_budget_s": self.time_budget_s,
            "time_budget_deadline": self.time_budget_deadline,
            "repair_budget": self.repair_budget,
            "subrun_budget": self.subrun_budget,
            "subrun_started": self.subrun_started,
            "subrun_completed": self.subrun_completed,
            "repairs_used": self.repairs_used,
            "mission_state": self.mission_state,
            "mission_reason": self.mission_reason,
            "phases": [p.to_dict() for p in self.phases],
            "subruns": list(self.subruns),
            "human_notes": [n.to_dict() for n in self.human_notes],
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(doc: Mapping[str, Any], path: str) -> MissionDoc:
        ver = doc.get("schema_version")
        _chk(ver == model.SCHEMA_VERSION, model.R_MALFORMED_STATE, path,
             f"schema_version={ver!r}")
        _known_keys(doc, _MISSION_KEYS, path)
        phases_raw = doc["phases"]
        _chk(isinstance(phases_raw, list), model.R_MALFORMED_STATE, path, "phases")
        for i, pdoc in enumerate(phases_raw):
            path_i = f"{path}[phases/{i}]"
            _chk(isinstance(pdoc, dict), model.R_MALFORMED_STATE, path_i)
            PhaseDoc.from_dict(pdoc, path_i)  # strict per-phase validation
            kind = pdoc.get("kind")
            _chk(isinstance(kind, str) and kind in model.PHASE_KINDS,
                 model.R_MALFORMED_STATE, path_i, f"kind={kind!r}")
            state = pdoc.get("state")
            _chk(isinstance(state, str) and state in model.PHASE_STATES,
                 model.R_MALFORMED_STATE, path_i, f"state={state!r}")
        notes_raw = doc["human_notes"]
        _chk(isinstance(notes_raw, list), model.R_MALFORMED_STATE, path, "human_notes")
        state = doc["mission_state"]
        _chk(isinstance(state, str) and state in model.MISSION_STATES,
             model.R_MALFORMED_STATE, path, f"mission_state={state!r}")
        reason = doc["mission_reason"]
        _chk(isinstance(reason, str) and reason in model.REASON_CODES,
             model.R_MALFORMED_STATE, path, f"mission_reason={reason!r}")
        return MissionDoc(
            schema_version=model.SCHEMA_VERSION,
            mission_id=_str(doc["mission_id"], path, "mission_id"),
            objective=_str(doc["objective"], path, "objective"),
            repo_root=_opt_str(doc["repo_root"], path, "repo_root"),
            cwd=_opt_str(doc["cwd"], path, "cwd"),
            baseline_revision=_opt_str(doc["baseline_revision"], path, "baseline_revision"),
            created_at=_str(doc["created_at"], path, "created_at"),
            started_at=_opt_str(doc["started_at"], path, "started_at"),
            finished_at=_opt_str(doc["finished_at"], path, "finished_at"),
            time_budget_s=_int(doc["time_budget_s"], path, "time_budget_s"),
            time_budget_deadline=_opt_str(doc["time_budget_deadline"], path,
                                          "time_budget_deadline"),
            repair_budget=_int(doc["repair_budget"], path, "repair_budget"),
            subrun_budget=_int(doc["subrun_budget"], path, "subrun_budget"),
            subrun_started=_int(doc["subrun_started"], path, "subrun_started"),
            subrun_completed=_int(doc["subrun_completed"], path, "subrun_completed"),
            repairs_used=_int(doc["repairs_used"], path, "repairs_used"),
            mission_state=str(state),
            mission_reason=str(reason),
            phases=[
                PhaseDoc.from_dict(pdoc, f"{path}[phases/{i}]")
                for i, pdoc in enumerate(phases_raw)
            ],
            subruns=_str_list(doc["subruns"], path, "subruns"),
            human_notes=[
                HumanNote.from_dict(n, f"{path}[human_notes/{i}]")
                for i, n in enumerate(notes_raw)
            ],
            updated_at=_str(doc["updated_at"], path, "updated_at"),
        )


class KeyLookupErrorNo(LookupError):
    """Raised when a referenced phase id is missing from the mission."""

    def __init__(self, phase_id: str) -> None:
        super().__init__(f"phase {phase_id!r} not in mission")
        self.phase_id = phase_id


# --- save / load ----------------------------------------------------------------


def save_mission(mission: MissionDoc, paths: dict[str, Path]) -> None:
    ensure_layout(paths)
    atomic_write_json(paths["mission"], mission.to_dict())


def load_mission(root: str | Path, mission_id: str) -> tuple[MissionDoc, dict[str, Path]]:
    paths = mission_paths(root, mission_id)
    p = paths["mission"]
    if not Path(p).is_file():
        raise MissionNotFound(mission_id, str(root))
    try:
        raw = json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MalformedMissionError(model.R_MALFORMED_STATE, str(p), type(exc).__name__) from exc
    _chk(isinstance(raw, dict), model.R_MALFORMED_STATE, str(p), "top-level object")
    return MissionDoc.from_dict(raw, str(p)), paths


def save_subrun(subrun: SubrunDoc, paths: dict[str, Path]) -> None:
    sp = subrun_paths(paths, subrun.subrun_id)
    sp["record"].parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(sp["record"], subrun.to_dict())


def load_subrun(paths: dict[str, Path], subrun_id: str) -> SubrunDoc:
    sp = subrun_paths(paths, subrun_id)
    try:
        raw = json.loads(sp["record"].read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MalformedMissionError(model.R_MALFORMED_STATE, str(sp["record"]),
                                    type(exc).__name__) from exc
    _chk(isinstance(raw, dict), model.R_MALFORMED_STATE, str(sp["record"]),
         "top-level object")
    return SubrunDoc.from_dict(raw, str(sp["record"]))


# --- phase evidence (bounded payload handed to later phases) --------------------


def save_phase_evidence(paths: dict[str, Path], phase_id: str, payload: dict[str, Any]) -> None:
    ep = evidence_path(paths, phase_id)
    ep.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(ep, payload)


def load_phase_evidence(paths: dict[str, Path], phase_id: str) -> dict[str, Any]:
    ep = evidence_path(paths, phase_id)
    try:
        raw = json.loads(ep.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MalformedMissionError(model.R_MALFORMED_STATE, str(ep), type(exc).__name__) from exc
    _chk(isinstance(raw, dict), model.R_MALFORMED_STATE, str(ep), "top-level object")
    return dict(raw)


# --- event log (bounded, append-only) ---------------------------------------------


def _read_events(paths: dict[str, Path]) -> list[dict[str, Any]]:
    p = paths["events"]
    if not Path(p).is_file():
        return []
    events: list[dict[str, Any]] = []
    try:
        text = Path(p).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise MalformedMissionError(model.R_MALFORMED_STATE, str(p), type(exc).__name__) from exc
    for i, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MalformedMissionError(model.R_MALFORMED_STATE, str(p), f"line {i}") from exc
        _chk(isinstance(doc, dict), model.R_MALFORMED_STATE, str(p), f"line {i}")
        events.append(doc)
    return events


def _exceeds_event_limit(paths: dict[str, Path]) -> bool:
    """Bounded overflow check: is line MAX_EVENT_LOG + 1 present?

    Reads at most ``MAX_EVENT_LOG + 1`` raw lines (no JSON parsing):
    once the log is trimmed it holds exactly ``MAX_EVENT_LOG`` entries,
    so the check cost is a small hard constant, not O(log size).
    """
    p = Path(paths["events"])
    with p.open("rb") as handle:
        for _ in range(model.MAX_EVENT_LOG + 1):
            if handle.readline() == b"":
                return False
    return True


def _trim_events_atomic(paths: dict[str, Path]) -> None:
    """Rewrite the event log with only the newest MAX_EVENT_LOG entries.

    Strictly validates the existing log (fail closed on corruption), then
    replaces it atomically (temp file + fsync + os.replace), so a crash
    can never truncate or partially write the evidence log.
    """
    p = Path(paths["events"])
    raw = _read_events(paths)
    trimmed = raw[-model.MAX_EVENT_LOG:]
    lines = [json.dumps(doc, sort_keys=True, separators=(",", ":")) for doc in trimmed]
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{p.name}.", suffix=f".tmp.{os.getpid()}", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, str(p))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def append_event(paths: dict[str, Path], event: Mapping[str, Any]) -> None:
    p = Path(paths["events"])
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dict(event), sort_keys=True, separators=(",", ":"))
    # True append-only write (bounded I/O per ADR-005): one small write +
    # fsync per event; the history is never read, re-parsed, and rewritten
    # on every append.
    with p.open("ab") as handle:
        handle.write(line.encode("utf-8") + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    # Bounded log: only when the log overflows MAX_EVENT_LOG is the atomic
    # rewrite performed, keeping the newest MAX_EVENT_LOG entries. The
    # overflow check is bounded (small hard line cap, bytes only), so the
    # amortized cost is O(1) per event with one full rewrite per
    # MAX_EVENT_LOG appends instead of on every append.
    if _exceeds_event_limit(paths):
        _trim_events_atomic(paths)


def load_events(paths: dict[str, Path]) -> list[dict[str, Any]]:
    return _read_events(paths)
