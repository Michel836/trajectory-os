"""M031 — end-to-end mission assembly model (pure, fail closed).

This module owns the durable *mission* value objects that wrap the existing
M023–M030 components. It deliberately does **not** model status: the single
canonical status truth remains :mod:`trajectory_os.observability.model`. A
mission is a durable identity + objective + plan + closure around one
canonical run (``mission_id == run_id``).

Canonical flow::

    Mission -> Preflight -> Plan -> Execution -> Validation
            -> Review -> Repair -> Human Gate -> Closure

Design invariants:

* the mission identity is immutable and survives every phase, including
  interruption/resume;
* lifecycle and readiness remain the M030 distinction — this model never
  invents a readiness;
* planning is bounded and durable; the retry/repair budget is explicit;
* automation never performs a Git trust-boundary write, so a trust policy
  that requests one is rejected at validation time;
* the human gate is always ``READY_FOR_COMMIT``; no model may configure an
  autonomous commit.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn

from trajectory_os.agents import model as agent_model
from trajectory_os.benchmark import model as bench_model
from trajectory_os.benchmark import workloads as bench_workloads
from trajectory_os.observability import model as obs_model

#: Schema version of every durable M031 document.
SCHEMA_VERSION = 1

#: Human/machine assembly version string (additive).
ASSEMBLY_VERSION = "m035.1"

# --- mission phases (closed set) ---------------------------------------------

MP_INTAKE = "INTAKE"
MP_PREFLIGHT = "PREFLIGHT"
MP_PLAN = "PLAN"
MP_EXECUTION = "EXECUTION"
MP_VALIDATION = "VALIDATION"
MP_REVIEW = "REVIEW"
MP_REPAIR = "REPAIR"
MP_HUMAN_GATE = "HUMAN_GATE"
MP_CLOSURE = "CLOSURE"

MISSION_PHASES = frozenset({
    MP_INTAKE, MP_PREFLIGHT, MP_PLAN, MP_EXECUTION, MP_VALIDATION,
    MP_REVIEW, MP_REPAIR, MP_HUMAN_GATE, MP_CLOSURE,
})

#: The canonical mission phase order (the default bounded plan).
CANONICAL_PHASE_SEQUENCE: tuple[str, ...] = (
    MP_INTAKE, MP_PREFLIGHT, MP_PLAN, MP_EXECUTION, MP_VALIDATION,
    MP_REVIEW, MP_REPAIR, MP_HUMAN_GATE, MP_CLOSURE,
)

# --- stable reason codes ------------------------------------------------------

R_OK = "OK"
R_MISSION_EXISTS = "MISSION_EXISTS"
R_MISSION_MISSING = "MISSION_MISSING"
R_INVALID_MISSION = "INVALID_MISSION"
R_INVALID_POLICY = "INVALID_TRUST_POLICY"
R_UNBOUNDED_PLAN = "UNBOUNDED_PLAN"
R_HUMAN_GATE_VIOLATION = "HUMAN_GATE_VIOLATION"
R_STALE_REVIEW = "STALE_REVIEW"
R_NO_ACTIVE_REVIEWER = "NO_ACTIVE_REVIEWER"
R_MALFORMED = "MALFORMED_MISSION_DOCUMENT"
R_STALE_STATE = "STALE_STATE"
R_INCOMPLETE_STATE = "INCOMPLETE_STATE"
R_IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
R_CONTROL_REFUSED = "CONTROL_REFUSED"
R_CONTROL_UNSUPPORTED = "CONTROL_UNSUPPORTED"

#: Mission ids follow the same filesystem-safe discipline as canonical runs.
MISSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: Hard bounds (fail closed beyond them).
MAX_OBJECTIVE_LEN = 16384
MAX_CONSTRAINTS = 64
MAX_CONSTRAINT_LEN = 1024
MAX_DOD_ITEMS = 64
MAX_DOD_LEN = 1024


class AssemblyError(Exception):
    """A malformed, untrusted, or out-of-policy mission document."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _fail(code: str, detail: str = "") -> NoReturn:
    raise AssemblyError(code, detail)


def utc_now() -> str:
    from datetime import UTC, datetime

    return (datetime.now(UTC).replace(microsecond=0).isoformat()
            .replace("+00:00", "Z"))


# --- trust policy -------------------------------------------------------------


@dataclass(frozen=True)
class TrustPolicy:
    """The mission's explicit trust contract.

    ``allow_git_trust_writes`` exists only so a malformed request can be
    *rejected* deterministically: automation must never commit, push or
    merge, so any policy requesting it is invalid.
    """

    require_review: bool = True
    final_reviewer_model: str = obs_model.FINAL_REVIEWER_MODEL
    inline_review_enabled: bool = False
    max_repairs: int = 2
    stop_at: str = obs_model.RD_READY_FOR_COMMIT
    allow_git_trust_writes: bool = False

    def validate(self) -> TrustPolicy:
        if self.allow_git_trust_writes:
            _fail(R_INVALID_POLICY,
                  "automation must never perform a Git trust-boundary write")
        if self.max_repairs < 0:
            _fail(R_INVALID_POLICY, "max_repairs must be >= 0")
        if self.stop_at != obs_model.RD_READY_FOR_COMMIT:
            _fail(R_INVALID_POLICY,
                  f"the human gate must stop at READY_FOR_COMMIT: {self.stop_at!r}")
        if not isinstance(self.require_review, bool):
            _fail(R_INVALID_POLICY, "require_review must be a boolean")
        if self.require_review and not self.final_reviewer_model:
            _fail(R_INVALID_POLICY, "review requires an explicit reviewer model")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "require_review": self.require_review,
            "final_reviewer_model": self.final_reviewer_model,
            "inline_review_enabled": self.inline_review_enabled,
            "max_repairs": self.max_repairs,
            "stop_at": self.stop_at,
            "allow_git_trust_writes": self.allow_git_trust_writes,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> TrustPolicy:
        return TrustPolicy(
            require_review=bool(data.get("require_review", True)),
            final_reviewer_model=str(
                data.get("final_reviewer_model",
                         obs_model.FINAL_REVIEWER_MODEL)),
            inline_review_enabled=bool(data.get("inline_review_enabled",
                                                False)),
            max_repairs=int(data.get("max_repairs", 2)),
            stop_at=str(data.get("stop_at", obs_model.RD_READY_FOR_COMMIT)),
            allow_git_trust_writes=bool(data.get("allow_git_trust_writes",
                                                 False)),
        ).validate()


# --- baseline -----------------------------------------------------------------


@dataclass(frozen=True)
class MissionBaseline:
    """The exact starting repository baseline (read-only, never guessed)."""

    revision: str | None
    workspace_digest: str
    captured_at: str
    reason: str = ""

    def validate(self) -> MissionBaseline:
        if not self.workspace_digest:
            _fail(R_INVALID_MISSION, "workspace_digest required")
        if self.revision is not None and not self.revision:
            _fail(R_INVALID_MISSION, "empty revision must be None")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "workspace_digest": self.workspace_digest,
            "captured_at": self.captured_at,
            "reason": self.reason or None,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> MissionBaseline:
        return MissionBaseline(
            revision=_opt_str(data.get("revision")),
            workspace_digest=str(data.get("workspace_digest", "")),
            captured_at=str(data.get("captured_at", "")),
            reason=_opt_str(data.get("reason")) or "",
        ).validate()


# --- mission definition -------------------------------------------------------


@dataclass(frozen=True)
class MissionDefinition:
    """One durable mission identity and its bounded intent."""

    mission_id: str
    objective: str
    constraints: tuple[str, ...]
    definition_of_done: tuple[str, ...]
    backend: str
    provider: str | None
    model: str | None
    trust_policy: TrustPolicy
    baseline: MissionBaseline
    workspace: str
    workload_id: str
    mode: str
    telemetry_mode: str
    created_at: str
    timeout_s: int = 600
    schema_version: int = SCHEMA_VERSION
    assembly_version: str = ASSEMBLY_VERSION
    workload: Mapping[str, Any] | None = None

    def validate(self) -> MissionDefinition:
        if not isinstance(self.mission_id, str) or not (
                MISSION_ID_RE.fullmatch(self.mission_id)):
            _fail(R_INVALID_MISSION, f"mission_id {self.mission_id!r}")
        if not self.objective.strip():
            _fail(R_INVALID_MISSION, "objective required")
        if len(self.objective) > MAX_OBJECTIVE_LEN:
            _fail(R_INVALID_MISSION, "objective exceeds bound")
        if len(self.constraints) > MAX_CONSTRAINTS:
            _fail(R_INVALID_MISSION, "too many constraints")
        for item in self.constraints:
            if not item.strip() or len(item) > MAX_CONSTRAINT_LEN:
                _fail(R_INVALID_MISSION, f"invalid constraint {item[:64]!r}")
        if not self.definition_of_done:
            _fail(R_INVALID_MISSION, "definition_of_done required")
        if len(self.definition_of_done) > MAX_DOD_ITEMS:
            _fail(R_INVALID_MISSION, "too many definition_of_done items")
        for item in self.definition_of_done:
            if not item.strip() or len(item) > MAX_DOD_LEN:
                _fail(R_INVALID_MISSION, f"invalid DoD item {item[:64]!r}")
        if self.backend not in agent_model.BACKENDS:
            _fail(R_INVALID_MISSION, f"unknown backend {self.backend!r}")
        if self.mode not in bench_model.EXECUTION_MODES:
            _fail(R_INVALID_MISSION, f"invalid mode {self.mode!r}")
        if self.timeout_s < 1:
            _fail(R_INVALID_MISSION, "timeout_s must be >= 1")
        if self.telemetry_mode not in obs_model.TELEMETRY_MODES:
            _fail(R_INVALID_MISSION,
                  f"invalid telemetry_mode {self.telemetry_mode!r}")
        if not self.workspace:
            _fail(R_INVALID_MISSION, "workspace required")
        if not self.workload_id:
            _fail(R_INVALID_MISSION, "workload_id required")
        # Fail closed on an unknown workload *at intake*: an embedded custom
        # workload is validated verbatim, otherwise the canonical registry is
        # the single authority.
        if self.workload is not None:
            embedded = bench_model.WorkloadSpec.from_dict(self.workload)
            if embedded.workload_id != self.workload_id:
                _fail(R_INVALID_MISSION,
                      "embedded workload_id does not match mission")
        else:
            bench_workloads.by_id(self.workload_id)
        self.trust_policy.validate()
        self.baseline.validate()
        return self

    @staticmethod
    def build(**kwargs: Any) -> MissionDefinition:
        return MissionDefinition(**kwargs).validate()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "assembly_version": self.assembly_version,
            "mission_id": self.mission_id,
            "objective": self.objective,
            "constraints": list(self.constraints),
            "definition_of_done": list(self.definition_of_done),
            "backend": self.backend,
            "provider": self.provider,
            "model": self.model,
            "trust_policy": self.trust_policy.to_dict(),
            "baseline": self.baseline.to_dict(),
            "workspace": self.workspace,
            "workload_id": self.workload_id,
            "mode": self.mode,
            "telemetry_mode": self.telemetry_mode,
            "created_at": self.created_at,
            "timeout_s": self.timeout_s,
            "workload": (None if self.workload is None
                         else dict(self.workload)),
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> MissionDefinition:
        if not isinstance(data, Mapping):
            _fail(R_MALFORMED, "mission must be an object")
        try:
            trust = data.get("trust_policy")
            baseline = data.get("baseline")
            if not isinstance(trust, Mapping):
                _fail(R_MALFORMED, "trust_policy required")
            if not isinstance(baseline, Mapping):
                _fail(R_MALFORMED, "baseline required")
            return MissionDefinition(
                mission_id=str(data["mission_id"]),
                objective=str(data["objective"]),
                constraints=tuple(str(x) for x in data.get("constraints", ())),
                definition_of_done=tuple(
                    str(x) for x in data.get("definition_of_done", ())),
                backend=str(data["backend"]),
                provider=_opt_str(data.get("provider")),
                model=_opt_str(data.get("model")),
                trust_policy=TrustPolicy.from_dict(trust),
                baseline=MissionBaseline.from_dict(baseline),
                workspace=str(data["workspace"]),
                workload_id=str(data["workload_id"]),
                mode=str(data["mode"]),
                telemetry_mode=str(data["telemetry_mode"]),
                created_at=str(data["created_at"]),
                timeout_s=int(data.get("timeout_s", 600)),
                schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
                assembly_version=str(data.get("assembly_version",
                                            ASSEMBLY_VERSION)),
                workload=(dict(data["workload"])
                          if isinstance(data.get("workload"), Mapping)
                          else None),
            ).validate()
        except KeyError as exc:
            _fail(R_MALFORMED, f"missing field {exc}")
        except (TypeError, ValueError) as exc:
            _fail(R_MALFORMED, f"{type(exc).__name__}: {exc}")


# --- plan ---------------------------------------------------------------------


@dataclass(frozen=True)
class PlanStep:
    """One bounded, ordered mission step."""

    step_id: str
    phase: str
    description: str
    validation_gate: str | None = None
    reviewer_role: str | None = None
    optional: bool = False

    def validate(self) -> PlanStep:
        if not self.step_id:
            _fail(R_INVALID_MISSION, "plan step_id required")
        if self.phase not in MISSION_PHASES:
            _fail(R_INVALID_MISSION, f"unknown plan phase {self.phase!r}")
        if self.reviewer_role is not None and (
                self.reviewer_role not in obs_model.ACTOR_ROLES):
            _fail(R_INVALID_MISSION,
                  f"unknown reviewer role {self.reviewer_role!r}")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "phase": self.phase,
            "description": self.description,
            "validation_gate": self.validation_gate,
            "reviewer_role": self.reviewer_role,
            "optional": self.optional,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> PlanStep:
        return PlanStep(
            step_id=str(data["step_id"]),
            phase=str(data["phase"]),
            description=str(data.get("description", "")),
            validation_gate=_opt_str(data.get("validation_gate")),
            reviewer_role=_opt_str(data.get("reviewer_role")),
            optional=bool(data.get("optional", False)),
        ).validate()


@dataclass(frozen=True)
class MissionPlan:
    """A bounded, durable plan for one mission."""

    mission_id: str
    steps: tuple[PlanStep, ...]
    expected_validation_gates: tuple[str, ...]
    reviewer_role: str
    retry_budget: int
    backend_intent: Mapping[str, Any]
    expected_terminal_conditions: tuple[str, ...]
    created_at: str
    schema_version: int = SCHEMA_VERSION
    assembly_version: str = ASSEMBLY_VERSION

    def validate(self) -> MissionPlan:
        if not self.steps:
            _fail(R_UNBOUNDED_PLAN, "plan requires at least one step")
        if self.retry_budget < 0:
            _fail(R_UNBOUNDED_PLAN, "retry_budget must be >= 0")
        if not self.expected_terminal_conditions:
            _fail(R_UNBOUNDED_PLAN, "terminal conditions required")
        for step in self.steps:
            step.validate()
        return self

    @staticmethod
    def build(**kwargs: Any) -> MissionPlan:
        return MissionPlan(**kwargs).validate()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "assembly_version": self.assembly_version,
            "mission_id": self.mission_id,
            "steps": [step.to_dict() for step in self.steps],
            "expected_validation_gates": list(self.expected_validation_gates),
            "reviewer_role": self.reviewer_role,
            "retry_budget": self.retry_budget,
            "backend_intent": dict(sorted(self.backend_intent.items())),
            "expected_terminal_conditions": list(
                self.expected_terminal_conditions),
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> MissionPlan:
        if not isinstance(data, Mapping):
            _fail(R_MALFORMED, "plan must be an object")
        try:
            steps = data.get("steps")
            if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes)):
                _fail(R_MALFORMED, "plan steps required")
            return MissionPlan(
                mission_id=str(data["mission_id"]),
                steps=tuple(PlanStep.from_dict(step)
                            for step in steps
                            if isinstance(step, Mapping)),
                expected_validation_gates=tuple(
                    str(x) for x in data.get("expected_validation_gates", ())),
                reviewer_role=str(data["reviewer_role"]),
                retry_budget=int(data["retry_budget"]),
                backend_intent=dict(data.get("backend_intent", {})),
                expected_terminal_conditions=tuple(
                    str(x) for x in data.get(
                        "expected_terminal_conditions", ())),
                created_at=str(data["created_at"]),
                schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
                assembly_version=str(data.get("assembly_version",
                                            ASSEMBLY_VERSION)),
            ).validate()
        except KeyError as exc:
            _fail(R_MALFORMED, f"missing field {exc}")
        except (TypeError, ValueError) as exc:
            _fail(R_MALFORMED, f"{type(exc).__name__}: {exc}")


def mission_workload(
    mission: MissionDefinition,
) -> bench_model.WorkloadSpec:
    """Resolve the workload for one mission (embedded custom, else registry)."""
    if mission.workload is not None:
        return bench_model.WorkloadSpec.from_dict(mission.workload)
    return bench_workloads.by_id(mission.workload_id)


def build_plan(mission: MissionDefinition, *, created_at: str | None = None,
               ) -> MissionPlan:
    """Deterministically build the bounded plan for one mission (pure).

    The plan references the canonical workload validation command and the
    mission's backend intent; it never duplicates a backend abstraction.
    """
    workload = mission_workload(mission)
    gate = " ".join(workload.validation_command)
    reviewer_role = (obs_model.ROLE_FINAL_INDEPENDENT_REVIEWER
                     if mission.trust_policy.require_review else "")
    steps: list[PlanStep] = [
        PlanStep(step_id="intake", phase=MP_INTAKE,
                 description="Persist the durable mission definition."),
        PlanStep(step_id="preflight", phase=MP_PREFLIGHT,
                 description="Fail-fast provider/model/workspace preflight."),
        PlanStep(step_id="plan", phase=MP_PLAN,
                 description="Persist the bounded mission plan."),
        PlanStep(step_id="implement", phase=MP_EXECUTION,
                 description=f"Execute workload {mission.workload_id} "
                             "through the provider-neutral backend."),
        PlanStep(step_id="validate", phase=MP_VALIDATION,
                 validation_gate=gate,
                 description="Run the deterministic validation gate."),
        PlanStep(step_id="review", phase=MP_REVIEW,
                 reviewer_role=(reviewer_role or None),
                 description="Run the strict independent final review."),
        PlanStep(step_id="repair", phase=MP_REPAIR, optional=True,
                 description="Bounded repair of review/validation findings."),
        PlanStep(step_id="human_gate", phase=MP_HUMAN_GATE,
                 description="Stop at READY_FOR_COMMIT; never commit."),
        PlanStep(step_id="closure", phase=MP_CLOSURE,
                 description="Persist durable closure evidence."),
    ]
    return MissionPlan.build(
        mission_id=mission.mission_id,
        steps=tuple(steps),
        expected_validation_gates=(gate,),
        reviewer_role=reviewer_role,
        retry_budget=mission.trust_policy.max_repairs,
        backend_intent={
            "backend": mission.backend,
            "provider": mission.provider,
            "model": mission.model,
            "mode": mission.mode,
        },
        expected_terminal_conditions=(
            obs_model.RD_READY_FOR_COMMIT, obs_model.RD_BLOCKED,
            obs_model.RD_FAILED, obs_model.RD_CANCELLED),
        created_at=created_at or mission.created_at,
    )


# --- closure ------------------------------------------------------------------


@dataclass(frozen=True)
class MissionClosure:
    """The durable closure evidence for one mission (reconstruction-ready)."""

    mission_id: str
    objective: str
    definition_of_done: tuple[str, ...]
    constraints: tuple[str, ...]
    baseline: Mapping[str, Any]
    plan: Mapping[str, Any] | None
    steps_executed: tuple[str, ...]
    attempts: int
    repairs: int
    validation_results: tuple[Mapping[str, Any], ...]
    review_results: tuple[Mapping[str, Any], ...]
    current_patch: str | None
    reviewed_patch: str | None
    telemetry_summary: Mapping[str, Any] | None
    lifecycle: str
    readiness: str
    terminal_reason: str | None
    terminal_reason_code: str | None
    next_action: str
    artifacts: Mapping[str, str]
    created_at: str
    schema_version: int = SCHEMA_VERSION
    assembly_version: str = ASSEMBLY_VERSION

    def validate(self) -> MissionClosure:
        if not self.mission_id:
            _fail(R_MALFORMED, "closure mission_id required")
        if self.readiness not in obs_model.READINESS_STATES:
            _fail(R_MALFORMED, f"closure readiness {self.readiness!r}")
        if self.lifecycle not in obs_model.LIFECYCLE_STATES:
            _fail(R_MALFORMED, f"closure lifecycle {self.lifecycle!r}")
        # A closure can never claim readiness without a completed lifecycle.
        if (self.readiness == obs_model.RD_READY_FOR_COMMIT
                and self.lifecycle != obs_model.LC_COMPLETE):
            _fail(R_MALFORMED,
                  "closure READY_FOR_COMMIT requires COMPLETE lifecycle")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "assembly_version": self.assembly_version,
            "mission_id": self.mission_id,
            "objective": self.objective,
            "definition_of_done": list(self.definition_of_done),
            "constraints": list(self.constraints),
            "baseline": dict(self.baseline),
            "plan": (None if self.plan is None else dict(self.plan)),
            "steps_executed": list(self.steps_executed),
            "attempts": self.attempts,
            "repairs": self.repairs,
            "validation_results": [dict(x) for x in self.validation_results],
            "review_results": [dict(x) for x in self.review_results],
            "current_patch": self.current_patch,
            "reviewed_patch": self.reviewed_patch,
            "telemetry_summary": (None if self.telemetry_summary is None
                                  else dict(self.telemetry_summary)),
            "lifecycle": self.lifecycle,
            "readiness": self.readiness,
            "terminal_reason": self.terminal_reason,
            "terminal_reason_code": self.terminal_reason_code,
            "next_action": self.next_action,
            "artifacts": dict(sorted(self.artifacts.items())),
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> MissionClosure:
        if not isinstance(data, Mapping):
            _fail(R_MALFORMED, "closure must be an object")
        try:
            plan = data.get("plan")
            telemetry = data.get("telemetry_summary")
            validation = data.get("validation_results", ())
            review = data.get("review_results", ())
            return MissionClosure(
                mission_id=str(data["mission_id"]),
                objective=str(data["objective"]),
                definition_of_done=tuple(
                    str(x) for x in data.get("definition_of_done", ())),
                constraints=tuple(str(x) for x in data.get("constraints", ())),
                baseline=dict(data.get("baseline", {})),
                plan=(dict(plan) if isinstance(plan, Mapping) else None),
                steps_executed=tuple(
                    str(x) for x in data.get("steps_executed", ())),
                attempts=int(data.get("attempts", 0)),
                repairs=int(data.get("repairs", 0)),
                validation_results=tuple(
                    dict(x) for x in validation
                    if isinstance(x, Mapping)),
                review_results=tuple(
                    dict(x) for x in review if isinstance(x, Mapping)),
                current_patch=_opt_str(data.get("current_patch")),
                reviewed_patch=_opt_str(data.get("reviewed_patch")),
                telemetry_summary=(dict(telemetry)
                                   if isinstance(telemetry, Mapping) else None),
                lifecycle=str(data["lifecycle"]),
                readiness=str(data["readiness"]),
                terminal_reason=_opt_str(data.get("terminal_reason")),
                terminal_reason_code=_opt_str(
                    data.get("terminal_reason_code")),
                next_action=str(data.get("next_action", "")),
                artifacts={str(k): str(v)
                           for k, v in data.get("artifacts", {}).items()},
                created_at=str(data.get("created_at", "")),
                schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
                assembly_version=str(data.get("assembly_version",
                                            ASSEMBLY_VERSION)),
            ).validate()
        except KeyError as exc:
            _fail(R_MALFORMED, f"missing field {exc}")
        except (TypeError, ValueError) as exc:
            _fail(R_MALFORMED, f"{type(exc).__name__}: {exc}")


# --- mission id generation ----------------------------------------------------


def generate_mission_id(objective: str, created_at: str) -> str:
    """Deterministic, readable mission id (stable for an objective + second)."""
    compact = created_at.replace("-", "").replace(":", "").replace("Z", "")
    digest = hashlib.sha256(objective.encode("utf-8")).hexdigest()[:8]
    return f"m031-{compact}-{digest}"


# --- helpers ------------------------------------------------------------------


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


__all__ = [
    "ASSEMBLY_VERSION",
    "CANONICAL_PHASE_SEQUENCE",
    "MAX_CONSTRAINTS",
    "MAX_DOD_ITEMS",
    "MAX_OBJECTIVE_LEN",
    "MISSION_ID_RE",
    "MISSION_PHASES",
    "MP_CLOSURE",
    "MP_EXECUTION",
    "MP_HUMAN_GATE",
    "MP_INTAKE",
    "MP_PLAN",
    "MP_PREFLIGHT",
    "MP_REPAIR",
    "MP_REVIEW",
    "MP_VALIDATION",
    "R_HUMAN_GATE_VIOLATION",
    "R_IDENTITY_MISMATCH",
    "R_INCOMPLETE_STATE",
    "R_INVALID_MISSION",
    "R_INVALID_POLICY",
    "R_MALFORMED",
    "R_MISSION_EXISTS",
    "R_MISSION_MISSING",
    "R_NO_ACTIVE_REVIEWER",
    "R_OK",
    "R_STALE_REVIEW",
    "R_STALE_STATE",
    "R_UNBOUNDED_PLAN",
    "SCHEMA_VERSION",
    "AssemblyError",
    "MissionBaseline",
    "MissionClosure",
    "MissionDefinition",
    "MissionPlan",
    "PlanStep",
    "TrustPolicy",
    "build_plan",
    "generate_mission_id",
    "mission_workload",
    "utc_now",
]
