"""Mission 003 — bounded mission orchestrator (local, deterministic, fail closed).

Composition of the durable store (:mod:`~trajectory_os.missions.store`) and
the pure state machine (:mod:`~trajectory_os.missions.flow`) into a
bounded, resumable execution loop:

    create -> [ decide -> (guards: budget, workspace, resource, head)
                -> sub-run (fresh context, bounded) -> evidence ]*
             -> COMPLETE | BLOCKED | FAILED

Guarantees (ADR-005):

* every step is bounded — session bound, sub-run bound, wall-clock bound,
  attempt bound, repair bound; the loop never spins;
* every state change is persisted atomically **before** the next side
  effect, so a crash between steps leaves an explicit, reconstructable
  record — reconstruction never guesses, never re-runs proven work, and
  fail-closes uncertain work (``UNPROVEN`` -> BLOCKED when budget runs out);
* same-worktree contention is fail-closed (an in-flight sub-run blocks a
  new launch with ``WORKSPACE_CONFLICT`` — it is never reaped implicitly);
* baseline/HEAD drift is detected read-only and blocks (``HEAD_DRIFT``);
* provider failures are classified, counted, and surfaced (never retried
  implicitly);
* human intervention is an explicit, budgeted action (``record_human_note``);
* benchmark + operator output are derived from the same canonical state
  (:mod:`~trajectory_os.missions.summary`).
"""

from __future__ import annotations

import datetime
import hashlib
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trajectory_os.missions import flow, model, store
from trajectory_os.missions.runner import (
    FakeRunner,
    PhaseRunner,
    ProcessPhaseRunner,
    SubrunRequest,
    SubrunResult,
)
from trajectory_os.runs import model as runs_model


def utc_now_iso() -> str:
    """Canonical UTC timestamp (same grammar as the V1.89 store)."""
    return (
        datetime.datetime.now(datetime.UTC)
        .replace(microsecond=0, tzinfo=None)
        .isoformat()
        + "Z"
    )


def _parse_iso(ts: str) -> datetime.datetime:
    """Parse the canonical timestamp; fail closed on non-canonical input."""
    try:
        dt = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError) as exc:
        raise store.MalformedMissionError(model.R_MALFORMED_STATE, "timestamp",
                                          repr(ts)) from exc
    return dt.replace(tzinfo=datetime.UTC)


class ConfigError(Exception):
    """Invalid mission configuration (fail closed before any state is written)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class MissionExists(Exception):
    """A mission with this identity already exists in the store."""

    def __init__(self, mission_id: str, root: str) -> None:
        super().__init__(f"mission {mission_id!r} already exists under {root!r}")
        self.mission_id = mission_id
        self.root = root


class HumanBudgetError(Exception):
    """Human intervention budget exhausted (bounded; surface, do not append)."""

    def __init__(self, mission_id: str, limit: int) -> None:
        super().__init__(f"mission {mission_id!r}: human intervention budget "
                         f"{limit} exhausted")
        self.mission_id = mission_id
        self.limit = limit


# --- configuration -------------------------------------------------------------


@dataclass(frozen=True)
class PhaseSpec:
    """One phase of a mission plan (bounded, explicit, dependency-wired)."""

    kind: str
    command: Sequence[str] = ()
    phase_id: str | None = None       # default: model.KIND_TO_PHASE_ID[kind]
    depends_on: Sequence[str] = ()
    resources: Mapping[str, Any] | None = None
    max_attempts: int | None = None   # default per kind
    mode: str | None = None           # default: model.KIND_TO_MODE[kind]

    def validate(self) -> None:
        if self.kind not in model.PHASE_KINDS:
            raise ConfigError("PHASE_KIND_INVALID", repr(self.kind))
        if self.command and not 1 <= len(self.command) <= model.MAX_COMMAND_PARTS:
            raise ConfigError("PHASE_COMMAND_INVALID", "part count out of bounds")
        for part in self.command:
            if not isinstance(part, str) or not (1 <= len(part) <= model.MAX_COMMAND_PART_LEN):
                raise ConfigError("PHASE_COMMAND_INVALID", repr(part))
        if self.depends_on and not 1 <= len(self.depends_on) <= model.MAX_DEPENDENCIES:
            raise ConfigError("PHASE_DEPENDENCIES_INVALID", "dependency count out of bounds")
        if self.max_attempts is not None and not (
            1 <= self.max_attempts <= model.MAX_ATTEMPTS_PER_PHASE):
            raise ConfigError("PHASE_MAX_ATTEMPTS_INVALID", repr(self.max_attempts))


@dataclass(frozen=True)
class MissionConfig:
    mission_id: str
    objective: str
    phase_specs: tuple[PhaseSpec, ...]
    repo_root: str | None = None
    cwd: str | None = None
    baseline_revision: str | None = None
    time_budget_s: int = model.DEFAULT_TIME_BUDGET_S
    repair_budget: int = model.MAX_REPAIR_ROUNDS
    subrun_budget: int | None = None   # default: bounded per plan
    resource_policy: Mapping[str, Any] | None = None

    def validate(self) -> None:
        if not model.ID_RE.fullmatch(self.mission_id):
            raise ConfigError("MISSION_ID_INVALID", repr(self.mission_id))
        if not (1 <= len(self.objective) <= model.MAX_OBJECTIVE_LEN):
            raise ConfigError("OBJECTIVE_INVALID", "length out of bounds")
        if not (model.MIN_TIME_BUDGET_S <= self.time_budget_s <= model.MAX_TIME_BUDGET_S):
            raise ConfigError("TIME_BUDGET_INVALID", repr(self.time_budget_s))
        if not (0 <= self.repair_budget <= model.MAX_REPAIR_ROUNDS):
            raise ConfigError("REPAIR_BUDGET_INVALID", repr(self.repair_budget))
        if self.subrun_budget is not None and not (
            1 <= self.subrun_budget <= model.MAX_SUBRUNS):
            raise ConfigError("SUBRUN_BUDGET_INVALID", repr(self.subrun_budget))
        if self.baseline_revision is not None and not (
            1 <= len(self.baseline_revision) <= model.MAX_REVISION_LEN):
            raise ConfigError("BASELINE_REVISION_INVALID", repr(self.baseline_revision))
        if not 1 <= len(self.phase_specs) <= model.MAX_PHASES:
            raise ConfigError("PHASE_PLAN_INVALID", "phase count out of bounds")
        for spec in self.phase_specs:
            spec.validate()


def default_phase_specs(
    commands: Mapping[str, Sequence[str]],
    *,
    repair_budget: int = model.MAX_REPAIR_ROUNDS,
    validate_resources: Mapping[str, Any] | None = None,
) -> tuple[PhaseSpec, ...]:
    """The canonical PLAN->IMPLEMENT->VALIDATE->REVIEW->CONSOLIDATE plan."""
    missing = [k for k in model.CANONICAL_SEQUENCE if (
        k not in commands or not list(commands[k]))]
    if missing:
        raise ConfigError("PHASE_PLAN_INVALID",
                          f"missing command(s) for: {missing}")
    specs: list[PhaseSpec] = []
    prev: str | None = None
    for kind in model.CANONICAL_SEQUENCE:
        pid = model.KIND_TO_PHASE_ID[kind]
        max_attempts = (1 + repair_budget
                        if kind in model.REPAIRABLE_KINDS
                        else min(2, model.MAX_ATTEMPTS_PER_PHASE))
        specs.append(PhaseSpec(
            kind=kind,
            command=list(commands[kind]),
            phase_id=pid,
            depends_on=(prev,) if prev else (),
            resources=validate_resources if kind == model.PH_VALIDATE else None,
            max_attempts=max_attempts,
        ))
        prev = pid
    return tuple(specs)


# --- creation -------------------------------------------------------------------


def _default_subrun_budget(config: MissionConfig) -> int:
    core = len(config.phase_specs)
    extra = config.repair_budget * 2 + 2  # repair rounds + their re-attempts
    return min(model.MAX_SUBRUNS, core + extra)


def create_mission(root: str, config: MissionConfig) -> store.MissionDoc:
    """Create the canonical mission state atomically (no execution yet)."""
    config.validate()
    if store.mission_paths(root, config.mission_id)["mission"].is_file():
        raise MissionExists(config.mission_id, root)
    paths = store.mission_paths(root, config.mission_id)
    now = utc_now_iso()

    # Dependency integrity: ids known, no self, no cycles (bounded DFS).
    ids = [(s.phase_id or model.KIND_TO_PHASE_ID[s.kind]) for s in config.phase_specs]
    ids_set = set(ids)
    if len(ids_set) != len(ids):
        raise ConfigError("PHASE_ID_DUPLICATE", repr(ids))
    for spec in config.phase_specs:
        pid = spec.phase_id or model.KIND_TO_PHASE_ID[spec.kind]
        for dep in spec.depends_on:
            if dep == pid:
                raise ConfigError("PHASE_DEPENDENCY_SELF", pid)
            if dep not in ids_set:
                raise ConfigError("PHASE_DEPENDENCY_UNKNOWN", dep)
    remaining = dict.fromkeys(ids, len(ids))
    for _ in ids:
        progressed = False
        for pid in list(remaining):
            deps = next(s.depends_on for s in config.phase_specs
                        if (s.phase_id or model.KIND_TO_PHASE_ID[s.kind]) == pid)
            if all(dep not in remaining for dep in deps):
                del remaining[pid]
                progressed = True
        if not remaining:
            break
        if not progressed:
            raise ConfigError("PHASE_DEPENDENCY_CYCLE", repr(sorted(remaining)))

    repairable_attempts = 1 + config.repair_budget
    phases: list[store.PhaseDoc] = []
    for spec in config.phase_specs:
        pid = spec.phase_id or model.KIND_TO_PHASE_ID[spec.kind]
        round_no = 0
        if pid.startswith(model.REPAIR_PHASE_ID_PREFIX):
            tail = pid[len(model.REPAIR_PHASE_ID_PREFIX):]
            if not tail.isdigit():
                raise ConfigError("PHASE_ID_INVALID", pid)
            round_no = int(tail)
        phases.append(store.PhaseDoc(
            phase_id=pid,
            kind=spec.kind,
            mode=spec.mode or model.KIND_TO_MODE[spec.kind],
            round=round_no,
            depends_on=list(spec.depends_on),
            command=list(spec.command),
            resources=dict(spec.resources) if spec.resources is not None else None,
            attempt=0,
            max_attempts=(spec.max_attempts
                          if spec.max_attempts is not None else
                          (repairable_attempts
                           # non-repairable phases: bounded plain retries,
                           # so an UNPROVEN sub-run can be re-evidenced on
                           # resume (1 < n <= MAX_ATTEMPTS_PER_PHASE)
                           if spec.kind in model.REPAIRABLE_KINDS else 2)),
            repairs_at_attempt=0,
            state=model.PS_PENDING,
            reason=model.R_OK,
            subrun_ids=[],
            started_at=None,
            finished_at=None,
        ))

    mission = store.MissionDoc(
        schema_version=model.SCHEMA_VERSION,
        mission_id=config.mission_id,
        objective=config.objective,
        repo_root=config.repo_root,
        cwd=config.cwd,
        baseline_revision=config.baseline_revision,
        created_at=now,
        started_at=None,
        finished_at=None,
        time_budget_s=config.time_budget_s,
        time_budget_deadline=None,
        repair_budget=config.repair_budget,
        subrun_budget=_default_subrun_budget(config) if config.subrun_budget is None
        else config.subrun_budget,
        subrun_started=0,
        subrun_completed=0,
        repairs_used=0,
        mission_state=model.MS_PLANNING,
        mission_reason=model.R_OK,
        phases=phases,
        subruns=[],
        human_notes=[],
        updated_at=now,
    )
    store.save_mission(mission, paths)
    store.append_event(paths, {"ts": now, "event": "created",
                               "phases": [p.phase_id for p in phases]})
    return mission


# --- guards ---------------------------------------------------------------------


#: Bound for the read-only HEAD probe (single local ``git rev-parse``).
#: Named here — not a per-call literal — so slow storage / loaded machines
#: hit the same explicit, auditable limit as everywhere else.  On timeout
#: the guard chain fail-closes (``assert_head_stable`` -> HEAD_DRIFT):
#: the launch is blocked and made explicit, never guessed through.
GIT_HEAD_TIMEOUT_S = 10.0


def git_head(repo_root: str) -> str | None:
    """Read-only ``git rev-parse HEAD`` (fail closed on error/timeout)."""
    # Inherit the operator's PATH so non-standard git locations work;
    # everything else stays pinned (deterministic, no lock side effects).
    path_env = os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin"
    try:
        proc = subprocess.run(  # noqa: S603 (fixed argv)
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            env={"GIT_OPTIONAL_LOCKS": "0", "PATH": path_env},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=GIT_HEAD_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    head = proc.stdout.decode("ascii", errors="replace").strip()
    return head if head else None


def _git_read(repo_root: str, *args: str) -> bytes | None:
    """Bounded read-only git invocation (fixed argv; no lock side effects)."""
    path_env = os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin"
    try:
        proc = subprocess.run(  # noqa: S603 (fixed argv, read-only subcommands)
            ["git", *args],
            cwd=repo_root,
            env={"GIT_OPTIONAL_LOCKS": "0", "PATH": path_env},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=GIT_HEAD_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def worktree_identity(repo_root: str) -> dict[str, Any]:
    """Fresh-context / worktree-identity evidence for one subrun (read-only).

    Deterministic: the ``HEAD`` revision plus a stable SHA-256 digest of
    the working-tree diff against ``HEAD`` (staged + unstaged tracked
    changes).  Both probes are read-only (``GIT_OPTIONAL_LOCKS=0``) and
    bounded; on probe failure the field is explicitly ``None`` — never
    fabricated, never guessed.
    """
    head = git_head(repo_root)
    diff = _git_read(repo_root, "diff", "HEAD")
    patch_sha = (
        hashlib.sha256(diff).hexdigest() if diff is not None else None
    )
    return {"head": head, "worktree_patch_sha256": patch_sha}


def admit_phase_resources(
    phase: store.PhaseDoc,
    policy: Mapping[str, Any] | None,
) -> None:
    """Deterministic resource admission against supplied operator evidence.

    No hardware probing.  Missing/invalid evidence fails closed — a
    deferred phase blocks the mission (``RESOURCE_UNAVAILABLE``), it never
    waits unboundedly.
    """
    needs = phase.resources
    if needs is None or policy is None:
        return  # resource policy is opt-in (deterministic admission)
    from trajectory_os.runs import resources as res
    try:
        requirement = res.ResourceRequirement.from_dict(needs)
        capacity = res.policy_from_evidence(policy)
    except res.ResourcePolicyError as exc:
        raise MissionGuardBlocked(model.R_RESOURCE_UNAVAILABLE,
                                  f"phase {phase.phase_id!r}: {exc}") from exc
    decision = res.evaluate(requirement, capacity, None)
    if decision.decision != runs_model.RES_DECISION_ALLOWED:
        raise MissionGuardBlocked(
            model.R_RESOURCE_UNAVAILABLE,
            f"phase {phase.phase_id!r} deferred: " + "; ".join(decision.reasons))


class MissionGuardBlocked(Exception):
    """A deterministic guard blocked progress (persisted as BLOCKED)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def assert_no_workspace_conflict(mission: store.MissionDoc,
                                 paths: dict[str, Path]) -> None:
    """Fail closed if any sub-run in this worktree is still in flight.

    Bounded by construction: a sub-run record is persisted ``RUNNING`` and
    then finalized exactly once — finalize saves the *terminal* record
    before persisting the phase out of ``RUNNING``, and attempts within a
    phase are strictly sequential. Hence an in-flight (``RUNNING``) record
    can only be the last sub-run of a phase left in ``RUNNING`` (the
    crash window between persisted launch and finalize). Candidates are
    therefore bounded to those phases (at most one in the sequential
    design) — no O(N) scan of every sub-run record on each launch.
    """
    for phase in mission.phases:
        if phase.state != model.PS_RUNNING or not phase.subrun_ids:
            continue
        record = store.load_subrun(paths, phase.subrun_ids[-1])
        if record.classification == model.CR_RUNNING:
            raise MissionGuardBlocked(
                model.R_WORKSPACE_CONFLICT,
                f"sub-run {record.subrun_id!r} in flight (reconstruct first)")


def assert_head_stable(mission: store.MissionDoc) -> None:
    if mission.baseline_revision is None or mission.repo_root is None:
        return
    head = git_head(mission.repo_root)
    if head is None:
        raise MissionGuardBlocked(model.R_HEAD_DRIFT,
                                  "cannot determine HEAD (fail closed)")
    if head != mission.baseline_revision:
        raise MissionGuardBlocked(
            model.R_HEAD_DRIFT,
            f"HEAD {head!r} != baseline {mission.baseline_revision!r}")


# --- run loop ---------------------------------------------------------------------

Clock = Callable[[], str]


@dataclass
class MissionReport:
    mission_id: str
    mission_state: str
    mission_reason: str
    stop: str                    # already_terminal | complete | blocked | failed | session_bound
    subrun: dict[str, Any] | None = None
    summary: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "mission_state": self.mission_state,
            "mission_reason": self.mission_reason,
            "stop": self.stop,
            "subrun": self.subrun,
            "summary": self.summary,
        }


def _load(root: str, mission_id: str) -> tuple[store.MissionDoc, dict[str, Path]]:
    mission, paths = store.load_mission(root, mission_id)
    return mission, dict(paths)


def _persist(mission: store.MissionDoc, paths: dict[str, Path]) -> None:
    mission.updated_at = utc_now_iso()
    store.save_mission(mission, paths)


def _set_mission_state(mission: store.MissionDoc, target: str, reason: str) -> None:
    flow.assert_transition(mission.mission_state, target)
    mission.mission_state = target
    mission.mission_reason = reason


def _next_state_label(mission: store.MissionDoc) -> str | None:
    """Mission-level state matching the next unsatisfied phase (or None)."""
    for phase in mission.phases:
        if phase.state == model.PS_PASSED:
            continue
        return model.KIND_TO_MISSION_STATE[phase.kind]
    return None


def run_phase(
    root: str,
    mission_id: str,
    runner: PhaseRunner,
    *,
    phase_id: str,
    subrun_timeout_s: int = model.DEFAULT_SUBRUN_TIMEOUT_S,
    resource_policy: Mapping[str, Any] | None = None,
    clock: Clock = utc_now_iso,
) -> store.MissionDoc:
    """Execute one bounded sub-run for one phase, persisting evidence at
    every step.  Returns the mission after the step (persisted)."""
    if not (model.MIN_SUBRUN_TIMEOUT_S <= subrun_timeout_s
            <= model.MAX_SUBRUN_TIMEOUT_S):
        raise ConfigError("SUBRUN_TIMEOUT_INVALID", repr(subrun_timeout_s))

    mission, paths = _load(root, mission_id)
    phase = mission.phase(phase_id)
    if phase.state not in (model.PS_PENDING, model.PS_FAILED,
                           model.PS_UNPROVEN, model.PS_RUNNING):
        raise MissionGuardBlocked(model.R_ILLEGAL_TRANSITION,
                                  f"phase {phase_id!r} state {phase.state!r}")
    if phase.state == model.PS_RUNNING:
        # in-flight evidence: fail closed (workspace contention)
        assert_no_workspace_conflict(mission, paths)

    now = clock()
    if mission.started_at is None:
        mission.started_at = now
        mission.time_budget_deadline = (_parse_iso(now)
                                        + datetime.timedelta(
                                            seconds=mission.time_budget_s)
                                       ).isoformat().replace("+00:00", "Z")
    deadline = mission.time_budget_deadline
    if deadline is not None and _parse_iso(now) >= _parse_iso(deadline):
        _set_mission_state(mission, model.MS_BLOCKED, model.R_TIME_BUDGET_EXHAUSTED)
        mission.finished_at = now
        _persist(mission, paths)
        store.append_event(paths, {"ts": now, "event": "blocked",
                                   "reason": model.R_TIME_BUDGET_EXHAUSTED})
        raise MissionGuardBlocked(model.R_TIME_BUDGET_EXHAUSTED,
                                  "wall-clock budget exhausted before launch")

    if mission.subrun_started >= mission.subrun_budget:
        # Register the deterministic next action (repair slot) as PENDING —
        # never launched (the budget is authoritative), so the blocked state
        # stays explicit, inspectable, and bounded by design.
        _create_repair_phase(
            mission, min(mission.repairs_used + 1, model.MAX_REPAIR_ROUNDS),
            paths)
        _set_mission_state(mission, model.MS_BLOCKED, model.R_SUBRUN_BUDGET_EXHAUSTED)
        mission.finished_at = now
        _persist(mission, paths)
        store.append_event(paths, {"ts": now, "event": "blocked",
                                   "reason": model.R_SUBRUN_BUDGET_EXHAUSTED})
        raise MissionGuardBlocked(model.R_SUBRUN_BUDGET_EXHAUSTED,
                                  "sub-run budget exhausted")

    # Deterministic guards (fail closed, never guessed):
    admit_phase_resources(phase, resource_policy)
    assert_head_stable(mission)
    assert_no_workspace_conflict(mission, paths)

    attempt = phase.attempt + 1
    subrun_id = f"{phase.phase_id}-a{attempt}"
    sp = store.subrun_paths(paths, subrun_id)

    flow.assert_transition(mission.mission_state,
                           model.KIND_TO_MISSION_STATE[phase.kind])
    if phase.state == model.PS_PENDING:
        phase.started_at = now
    phase.state = model.PS_RUNNING
    phase.reason = model.R_OK
    mission.mission_state = model.KIND_TO_MISSION_STATE[phase.kind]
    mission.mission_reason = model.R_OK
    _persist(mission, paths)
    store.append_event(paths, {"ts": now, "event": "phase_started",
                               "phase": phase.phase_id, "attempt": attempt})

    # Persist the sub-run record BEFORE launch (crash window -> explicit).
    created = store.SubrunDoc(
        subrun_id=subrun_id,
        phase_id=phase.phase_id,
        kind=phase.kind,
        mode=phase.mode,
        round=phase.round,
        attempt=attempt,
        command=list(phase.command),
        cwd=mission.cwd,
        started_at=now,
        finished_at=None,
        exit_code=None,
        classification=model.CR_RUNNING,
        stdout_file=str(sp["stdout"]),
        stderr_file=str(sp["stderr"]),
        resources=phase.resources,
    )
    store.save_subrun(created, paths)
    if subrun_id not in mission.subruns:
        mission.subruns.append(subrun_id)
    phase.subrun_ids.append(subrun_id)
    # Accounting invariant (persisted atomically with the record): every
    # persisted sub-run record is counted as started — even if the runner
    # then crashes before finalize. So started - completed == in-flight
    # holds by construction and the counters reflect the persisted
    # evidence, never lagging it. ``subrun_completed`` still increments in
    # ``finalize_subrun``, exactly once per terminal record.
    mission.subrun_started += 1
    _persist(mission, paths)

    # Evidence paths exist even if the runner dies before writing output
    # (crash window ⇒ explicit empty evidence, never missing paths). The
    # production runner overwrites the same files with real output.
    sp["stdout"].parent.mkdir(parents=True, exist_ok=True)
    sp["stdout"].touch()
    sp["stderr"].touch()

    request = SubrunRequest(
        mission_id=mission.mission_id,
        subrun_id=subrun_id,
        phase_id=phase.phase_id,
        kind=phase.kind,
        mode=phase.mode,
        round=phase.round,
        attempt=attempt,
        command=tuple(phase.command),
        cwd=mission.cwd,
        timeout_s=subrun_timeout_s,
        stdout_file=str(sp["stdout"]),
        stderr_file=str(sp["stderr"]),
        resources=phase.resources,
        semantic_required=model.is_model_heavy(phase.kind),
    )
    result = runner.run(request)
    return finalize_subrun(root, mission_id, subrun_id, phase_id, result,
                           clock=clock)


def finalize_subrun(
    root: str,
    mission_id: str,
    subrun_id: str,
    phase_id: str,
    result: SubrunResult,
    *,
    clock: Clock = utc_now_iso,
) -> store.MissionDoc:
    """Terminalize one sub-run + advance the mission (bounded, evidence-first)."""
    now = clock()
    mission, paths = _load(root, mission_id)
    record = store.load_subrun(paths, subrun_id)

    classification = result.classification
    if classification not in model.TERMINAL_SUBRUN_CLASSIFICATIONS:
        classification = model.CR_UNPROVEN
    record.classification = classification
    record.exit_code = result.exit_code
    record.finished_at = now

    # Mission 007: persist semantic outcome/provenance exactly when the
    # runner actually participated in the semantic contract. Deterministic
    # phases and legacy/scripted runners remain legacy-shaped.
    semantic_observed = any((
        result.semantic_status is not None,
        result.semantic_error is not None,
        result.semantic_agent_classification is not None,
        result.semantic_readiness is not None,
        result.semantic_reason is not None,
    ))
    if semantic_observed:
        record.semantic_aware = True
        record.semantic_status = result.semantic_status
        record.semantic_error = result.semantic_error
        record.semantic_agent_classification = (
            result.semantic_agent_classification)
        record.semantic_readiness = result.semantic_readiness
        record.semantic_reason = result.semantic_reason

    store.save_subrun(record, paths)

    phase = mission.phase(phase_id)
    phase.attempt = max(phase.attempt, record.attempt)
    # Invariant (depended on by flow._failure_decision): after finalize,
    # phase.repairs_at_attempt EXACTLY equals mission.repairs_used —
    # refreshed unconditionally (never conditional on a stale None), and
    # persisted in the SAME atomic save that moves the phase to its
    # terminal state.  A crash between sub-run launch and finalize leaves
    # the phase RUNNING with its pre-launch snapshot, and flow fails that
    # closed (STALE_EVIDENCE / workspace conflict) before any decision
    # reads the comparison — there is no window where the pair is half-updated.
    phase.repairs_at_attempt = mission.repairs_used
    phase.finished_at = now
    state_after = model.PS_PASSED if classification == model.CR_COMPLETED \
        else (model.PS_FAILED if classification == model.CR_FAILED
              else model.PS_UNPROVEN)
    phase.state = state_after
    phase.reason = (model.R_OK if state_after == model.PS_PASSED else
                    (model.R_SUBRUN_FAILED if state_after == model.PS_FAILED
                     else model.R_UNPROVEN_SUBRUN))

    # Completion is recorded exactly once per sub-run (started was already
    # counted when the record was persisted in run_phase); counting it here
    # — before the repair branch, which falls through for passed repairs —
    # rules out any double-count.
    mission.subrun_completed += 1

    if phase.kind == model.PH_REPAIR:
        mission.repairs_used += 1
        # A failed repair hard-stops (deterministic; the loop is bounded).
        if state_after != model.PS_PASSED:
            _set_mission_state(mission, model.MS_BLOCKED, model.R_REPAIR_FAILED)
            mission.finished_at = now
            _persist(mission, paths)
            store.append_event(paths, {"ts": now, "event": "blocked",
                                       "reason": model.R_REPAIR_FAILED,
                                       "phase": phase_id})
            return mission
        mission.mission_reason = model.R_OK

    provider_failure = classification in model.PROVIDER_FAILURE_CLASSIFICATIONS or (
        classification == model.CR_FAILED and (record.exit_code or 0)
        > model.MAX_DETERMINISTIC_FAILURE_EXIT)
    if provider_failure:
        # surfaced vs recovered is derived in the summary from final states
        pass

    if state_after == model.PS_PASSED:
        if all(p.state == model.PS_PASSED for p in mission.phases):
            _set_mission_state(mission, model.MS_COMPLETE, model.R_COMPLETE)
            mission.finished_at = now
        else:
            nxt = _next_state_label(mission)
            if nxt is not None:
                _set_mission_state(mission, nxt, model.R_OK)
    else:
        mission.mission_reason = phase.reason
    _persist(mission, paths)

    if state_after == model.PS_PASSED and phase.kind != model.PH_REPAIR:
        # Bounded evidence for later phases (identity, exit, attempt counts).
        worktree = (worktree_identity(mission.repo_root)
                    if mission.repo_root is not None else None)
        store.save_phase_evidence(paths, phase.phase_id, {
            "phase": phase.phase_id,
            "kind": phase.kind,
            "state": model.PS_PASSED,
            "attempt": phase.attempt,
            "last_subrun": record.to_dict(),
            # Mission 004: fresh-context / worktree-identity evidence for
            # the proven subrun (deterministic, read-only; None = no repo).
            "worktree": worktree,
        })

    store.append_event(paths, {"ts": now, "event": "phase_finished",
                               "phase": phase.phase_id,
                               "subrun": subrun_id,
                               "classification": classification,
                               "mission_state": mission.mission_state})
    return mission


def run_mission(
    root: str,
    mission_id: str,
    runner: PhaseRunner,
    *,
    subrun_timeout_s: int = model.DEFAULT_SUBRUN_TIMEOUT_S,
    max_session_subruns: int = model.MAX_SESSION_SUBRUNS,
    resource_policy: Mapping[str, Any] | None = None,
    clock: Clock = utc_now_iso,
) -> MissionReport:
    """Bounded execution loop: advance the mission until terminal or bounded.

    Deterministic: same canonical state + same runner behavior => same loop.
    Stops at the first of: COMPLETE / BLOCKED / FAILED, the wall-clock
    budget, the sub-run budget, or ``max_session_subruns`` (bounded wait).
    """
    if not (1 <= max_session_subruns <= model.MAX_SESSION_SUBRUNS):
        raise ConfigError("SESSION_BOUND_INVALID", repr(max_session_subruns))

    from trajectory_os.missions import summary as _summary

    mission, paths = _load(root, mission_id)
    session_subruns = 0
    last_subrun: dict[str, Any] | None = None

    while session_subruns < max_session_subruns:
        mission, paths = _load(root, mission_id)
        if mission.terminal():
            return MissionReport(
                mission_id=mission.mission_id,
                mission_state=mission.mission_state,
                mission_reason=mission.mission_reason,
                stop="already_terminal"
                if session_subruns == 0 else "terminal_after_subrun",
                subrun=last_subrun,
                summary=_summary.mission_summary(mission, paths),
            )

        decision = flow.decide(mission)
        if decision.action == "noop":
            return MissionReport(
                mission_id, mission.mission_state, decision.reason,
                "already_terminal", last_subrun,
                _summary.mission_summary(mission, paths))

        if decision.action in ("complete", "blocked", "failed"):
            target = decision.mission_state or model.MS_BLOCKED
            flow.assert_transition(mission.mission_state, target)
            mission.mission_state = target
            mission.mission_reason = decision.reason
            mission.finished_at = clock()
            _persist(mission, paths)
            store.append_event(paths, {"ts": mission.finished_at,
                                       "event": decision.action,
                                       "reason": decision.reason,
                                       "phase": decision.phase_id})
            return MissionReport(
                mission_id, target, decision.reason,
                "complete" if decision.action == "complete" else
                ("blocked" if decision.action == "blocked" else "failed"),
                last_subrun, _summary.mission_summary(mission, paths))

        if decision.action == "repair":
            _create_repair_phase(mission, decision.phase_round, paths)
            continue

        if decision.action == "run_phase" and decision.phase_id is not None:
            if (session_subruns == 0 and mission.subrun_started > 0
                    and not mission.terminal()):
                # Mission 004: a resume is an explicit, countable event —
                # this session is launching new work for a mission that a
                # prior session already started (reconstruction/resume
                # accounting is derived from this event log).
                store.append_event(paths, {"ts": clock(), "event": "resumed",
                                           "phase": decision.phase_id})
            phase = mission.phase(decision.phase_id)
            phase_id = phase.phase_id
            sp = store.subrun_paths(paths, f"{phase_id}-a{phase.attempt + 1}")
            # (guard side-effects happen inside run_phase before launch)
            last_subrun = {
                "subrun_id": None,
                "phase": phase_id,
                "stdout": str(sp["stdout"]),
                "stderr": str(sp["stderr"]),
            }
            try:
                mission = run_phase(
                    root, mission_id, runner,
                    phase_id=phase_id,
                    subrun_timeout_s=subrun_timeout_s,
                    resource_policy=resource_policy,
                    clock=clock,
                )
            except MissionGuardBlocked as exc:
                # Guards only fire before a new sub-run launches: persist
                # the blocked state and stop (deterministic).
                mission, paths = _load(root, mission_id)
                if not mission.terminal():
                    _set_mission_state(mission, model.MS_BLOCKED, exc.code)
                    mission.finished_at = clock()
                    _persist(mission, paths)
                store.append_event(paths, {"ts": clock(), "event": "blocked",
                                           "reason": exc.code})
                return MissionReport(
                    mission.mission_id, mission.mission_state, exc.code,
                    "blocked", last_subrun,
                    _summary.mission_summary(mission, paths))
            session_subruns += 1
            record = None
            try:
                m2, p2 = _load(root, mission_id)
                rec_id = m2.phase(phase_id).subrun_ids[-1]
                record = store.load_subrun(p2, rec_id).to_dict()
            except (store.MalformedMissionError, LookupError):
                record = None
            last_subrun = record or last_subrun
            continue

        # Unknown decision: fail closed (never guess).
        raise MissionGuardBlocked(
            model.R_MALFORMED_STATE, f"unknown decision {decision.action!r}")

    mission, paths = _load(root, mission_id)
    return MissionReport(
        mission_id=mission.mission_id,
        mission_state=mission.mission_state,
        mission_reason=mission.mission_reason,
        stop="session_bound",
        subrun=last_subrun,
        summary=_summary.mission_summary(mission, paths),
    )


def _canonical_repair_command(
        mission: store.MissionDoc,
        implement: store.PhaseDoc | None,
        round_no: int,
        paths: dict[str, Path],
) -> list[str] | None:
    """Derive a canonical trajectory-pi REPAIR argv from IMPLEMENT.

    This deliberately recognizes only the canonical wrapper contract.
    Custom/legacy commands return ``None`` and retain the historical
    fallback in ``_create_repair_phase``.
    """
    if implement is None or not implement.command:
        return None

    command = list(implement.command)

    def value_after(flag: str) -> str | None:
        if command.count(flag) != 1:
            return None
        idx = command.index(flag)
        if idx + 1 >= len(command):
            return None
        return command[idx + 1]

    if value_after("--mode") != "IMPLEMENT":
        return None
    if value_after("--class") != "feature":
        return None

    model_name = value_after("--model")
    if not model_name:
        return None

    if command.count("--") != 1:
        return None

    repair_prompt = paths["mission"].parent / "prompts" / "repair.txt"
    if not repair_prompt.is_file():
        return None

    tail = f"TrajectoryOS repair.{round_no}: {mission.objective}"
    if len(tail) > model.MAX_COMMAND_PART_LEN:
        tail = tail[:model.MAX_COMMAND_PART_LEN]

    return [
        command[0],
        "--no-notify",
        "--dirty-ok",
        "--class", "repair",
        "--mode", "REPAIR",
        "--model", model_name,
        "--prompt-file", str(repair_prompt),
        "--", tail,
    ]


def _create_repair_phase(mission: store.MissionDoc,
                         round_no: int,
                         paths: dict[str, Path]) -> None:
    pid = f"{model.REPAIR_PHASE_ID_PREFIX}{round_no}"
    if any(p.phase_id == pid for p in mission.phases):
        return
    # Command precedence:
    #   1. an already-proven explicit/dynamic REPAIR command;
    #   2. a canonical REPAIR command derived from canonical IMPLEMENT;
    #   3. historical IMPLEMENT/repairable-command fallback for legacy or
    #      custom mission configurations.
    command: list[str] = []
    resources: dict[str, object] | None = None

    repair_specs = [p for p in mission.phases if p.kind == model.PH_REPAIR]
    if repair_specs and repair_specs[0].command:
        command = list(repair_specs[0].command)
        if repair_specs[0].resources is not None:
            resources = dict(repair_specs[0].resources)

    implement = next(
        (p for p in mission.phases
         if p.kind == model.PH_IMPLEMENT and p.command),
        None,
    )

    if not command:
        derived = _canonical_repair_command(
            mission, implement, round_no, paths
        )
        if derived is not None:
            command = derived
            if implement is not None and implement.resources is not None:
                resources = dict(implement.resources)

    if not command and implement is not None:
        command = list(implement.command)
        if implement.resources is not None:
            resources = dict(implement.resources)

    if not command:
        for p in mission.phases:
            if p.kind in model.REPAIRABLE_KINDS and p.command:
                command = list(p.command)
                if p.resources is not None:
                    resources = dict(p.resources)
                break
    if not command:
        raise MissionGuardBlocked(
            model.R_MALFORMED_STATE, "no command available for repair phase")
    if round_no > model.MAX_REPAIR_ROUNDS:
        raise MissionGuardBlocked(model.R_REPAIR_BUDGET_EXHAUSTED,
                                  "repair round exceeds hard bound")
    mission.phases.append(store.PhaseDoc(
        phase_id=pid,
        kind=model.PH_REPAIR,
        mode=model.KIND_TO_MODE[model.PH_REPAIR],
        round=round_no,
        depends_on=[],
        command=command,
        resources=resources,
        attempt=0,
        max_attempts=1,
        repairs_at_attempt=mission.repairs_used,
        state=model.PS_PENDING,
        reason=model.R_OK,
        subrun_ids=[],
        started_at=None,
        finished_at=None,
    ))
    mission.updated_at = utc_now_iso()
    store.save_mission(mission, paths)
    store.append_event(paths, {"ts": mission.updated_at, "event": "repair_scheduled",
                               "phase": pid, "round": round_no})


# --- reconstruction / resume ---------------------------------------------------------


@dataclass
class ReconstructionReport:
    mission_id: str
    mission_state: str
    mission_reason: str
    resumable: bool
    proven_passed: list[str]
    explicit_unproven: list[str]
    failed: list[str]
    pending: list[str]
    next: dict[str, Any] | None
    summary: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "mission_state": self.mission_state,
            "mission_reason": self.mission_reason,
            "resumable": self.resumable,
            "proven_passed": self.proven_passed,
            "explicit_unproven": self.explicit_unproven,
            "failed": self.failed,
            "pending": self.pending,
            "next": self.next,
            "summary": self.summary,
        }


def reconstruct(root: str, mission_id: str,
                *, clock: Clock = utc_now_iso) -> ReconstructionReport:
    """Rebuild canonical state from durable evidence (deterministic).

    * proven PASSED phases (COMPLETED sub-run evidence) are kept — never
      re-run;
    * a sub-run with ``RUNNING`` (crashed mid-flight) is terminalized to
      ``UNPROVEN`` — explicit, fail closed, never guessed;
    * PASSED claim contradicted by evidence -> fail closed
      (``CONTRADICTION``);
    * terminal missions are validated, not mutated.
    """
    from trajectory_os.missions import summary as _summary

    mission, paths = _load(root, mission_id)
    now = clock()
    changed = False

    if mission.terminal():
        for phase in mission.phases:
            if not phase.subrun_ids:
                if phase.state == model.PS_PASSED:
                    raise store.MalformedMissionError(
                        model.R_CONTRADICTION, str(paths["mission"]),
                        f"phase {phase.phase_id!r} PASSED without evidence")
                continue
            last = store.load_subrun(paths, phase.subrun_ids[-1])
            if phase.state == model.PS_PASSED and last.classification \
                    != model.CR_COMPLETED:
                raise store.MalformedMissionError(
                    model.R_CONTRADICTION, str(paths["mission"]),
                    f"phase {phase.phase_id!r} PASSED but last sub-run "
                    f"{last.classification}")
        return ReconstructionReport(
            mission_id, mission.mission_state, mission.mission_reason,
            resumable=False,
            proven_passed=[p.phase_id for p in mission.phases
                           if p.state == model.PS_PASSED],
            explicit_unproven=[p.phase_id for p in mission.phases
                               if p.state == model.PS_UNPROVEN],
            failed=[p.phase_id for p in mission.phases
                    if p.state == model.PS_FAILED],
            pending=[],
            next=None,
            summary=_summary.mission_summary(mission, paths),
        )

    for phase in mission.phases:
        if phase.state == model.PS_PASSED:
            if phase.subrun_ids:
                last = store.load_subrun(paths, phase.subrun_ids[-1])
                if last.classification != model.CR_COMPLETED:
                    raise store.MalformedMissionError(
                        model.R_CONTRADICTION, str(paths["mission"]),
                        f"phase {phase.phase_id!r} PASSED but last sub-run "
                        f"{last.classification}")
            else:
                raise store.MalformedMissionError(
                    model.R_CONTRADICTION, str(paths["mission"]),
                    f"phase {phase.phase_id!r} PASSED without evidence")
            continue
        if not phase.subrun_ids:
            if phase.state == model.PS_RUNNING:
                # Launched neither nor evidenced: treat as never started.
                phase.state = model.PS_PENDING
                phase.reason = model.R_OK
                changed = True
            continue
        last = store.load_subrun(paths, phase.subrun_ids[-1])
        # Attempt ledger is authoritative: every attempted sub-run advances
        # the ledger so re-runs mint fresh ids (never overwrite evidence).
        phase.attempt = max(phase.attempt, last.attempt)
        if last.classification == model.CR_RUNNING:
            now_ts = now
            last.classification = model.CR_UNPROVEN
            if last.finished_at is None:
                last.finished_at = now_ts
            store.save_subrun(last, paths)
            phase.state = model.PS_UNPROVEN
            phase.reason = model.R_UNPROVEN_SUBRUN
            if phase.started_at is None:
                phase.started_at = now_ts
            if phase.finished_at is None:
                phase.finished_at = now_ts
            changed = True
        elif last.classification == model.CR_COMPLETED:
            phase.state = model.PS_PASSED
            phase.reason = model.R_OK
            if phase.finished_at is None:
                phase.finished_at = last.finished_at or now
            changed = True
        elif last.classification == model.CR_FAILED:
            if phase.state != model.PS_FAILED:
                phase.state = model.PS_FAILED
                phase.reason = model.R_SUBRUN_FAILED
                changed = True
        else:  # CRASHED / UNPROVEN
            if phase.state not in (model.PS_PASSED, model.PS_UNPROVEN):
                phase.state = model.PS_UNPROVEN
                phase.reason = model.R_UNPROVEN_SUBRUN
                changed = True
            elif not changed:
                changed = True

    if changed:
        # Finalize terminal conditions deterministically from evidence.
        decision = flow.decide(mission)
        if decision.action == "complete":
            flow.assert_transition(mission.mission_state, model.MS_COMPLETE)
            mission.mission_state = model.MS_COMPLETE
            mission.mission_reason = model.R_COMPLETE
            mission.finished_at = now
            changed = True
        elif decision.action in ("blocked", "failed"):
            target = decision.mission_state or model.MS_BLOCKED
            flow.assert_transition(mission.mission_state, target)
            mission.mission_state = target
            mission.mission_reason = decision.reason
            mission.finished_at = now
            changed = True

        mission.updated_at = now
        store.save_mission(mission, paths)
        store.append_event(paths, {"ts": now, "event": "reconstructed",
                                   "state": mission.mission_state})

    # Next deterministic action (informational; does not mutate).
    nxt = None
    if not mission.terminal():
        d = flow.decide(mission)
        if d.action not in ("noop", "repair"):
            nxt = d.to_dict()

    return ReconstructionReport(
        mission_id, mission.mission_state, mission.mission_reason,
        resumable=not mission.terminal(),
        proven_passed=[p.phase_id for p in mission.phases
                       if p.state == model.PS_PASSED],
        explicit_unproven=[p.phase_id for p in mission.phases
                           if p.state == model.PS_UNPROVEN],
        failed=[p.phase_id for p in mission.phases
                if p.state == model.PS_FAILED],
        pending=[p.phase_id for p in mission.phases
                 if p.state == model.PS_PENDING],
        next=nxt,
        summary=_summary.mission_summary(mission, paths),
    )


# --- human intervention surface (explicit, budgeted) --------------------------------


def record_human_note(root: str, mission_id: str, note: str) -> store.MissionDoc:
    """Record one explicit human intervention (bounded per mission)."""
    if not (1 <= len(note) <= model.MAX_NOTE_LEN):
        raise ConfigError("HUMAN_NOTE_INVALID", "length out of bounds")
    mission, paths = _load(root, mission_id)
    if len(mission.human_notes) >= model.MAX_HUMAN_INTERVENTIONS:
        raise HumanBudgetError(mission_id, model.MAX_HUMAN_INTERVENTIONS)
    now = utc_now_iso()
    mission.human_notes.append(store.HumanNote(note=note, at=now))
    mission.updated_at = now
    _persist(mission, paths)
    store.append_event(paths, {"ts": now, "event": "human_note",
                               "note_len": len(note)})
    return mission


def new_runner(kind: str, **kwargs: Any) -> PhaseRunner:
    """Runner factory (``process`` is the bounded production runner)."""
    if kind == "process":
        return ProcessPhaseRunner()
    if kind == "fake":
        return FakeRunner(**kwargs)
    raise ConfigError("RUNNER_INVALID", repr(kind))
