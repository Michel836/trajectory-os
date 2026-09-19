"""M046 — one-screen operator product state (read-only projection).

:func:`build_operator_state` is a pure, read-only projection over the
canonical artifacts (mission/release/events/status). It never mutates product
state and repeated invocation with unchanged artifacts is byte-stable.

The projection is the single coherent operator view. It exposes, when known,
every listed fact and marks unknown facts explicitly (``None``) rather than
inventing them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trajectory_os.assembly import store as assembly_store
from trajectory_os.operator import events as operator_events
from trajectory_os.operator import model
from trajectory_os.operator import recovery as operator_recovery
from trajectory_os.operator._util import optional_int, optional_str, read_optional_json
from trajectory_os.release import evidence as release_evidence
from trajectory_os.release import model as release_model
from trajectory_os.release import store as release_store

#: Pending human gates (closed set).
GATE_NONE = "NONE"
GATE_GO_COMMIT_HANDOFF = "GO_COMMIT_HANDOFF_REQUIRED"
GATE_GO_COMMIT = "GO_COMMIT"
GATE_GO_MERGE_HANDOFF = "GO_MERGE_HANDOFF_REQUIRED"
GATE_GO_MERGE = "GO_MERGE"
GATE_CLOSURE = "RECORD_RELEASE_CLOSURE"

PENDING_GATES = frozenset({
    GATE_NONE, GATE_GO_COMMIT_HANDOFF, GATE_GO_COMMIT,
    GATE_GO_MERGE_HANDOFF, GATE_GO_MERGE, GATE_CLOSURE,
})


@dataclass(frozen=True)
class OperatorState:
    """One coherent, read-only operator product-state projection."""

    mission_id: str
    run_id: str
    objective: str | None
    policy_profile: str | None
    policy_id: str | None
    lifecycle: str | None
    readiness: str | None
    stage: str | None
    phase: str | None
    attempt: int | None
    implementation_backend: str | None
    implementation_provider: str | None
    implementation_model: str | None
    inline_reviewer: Mapping[str, Any] | None
    final_reviewer: Mapping[str, Any] | None
    validation: Mapping[str, Any] | None
    review_fresh: bool | None
    review_status: str | None
    review_reason: str | None
    reviewed_patch: str | None
    current_patch: str | None
    branch: str | None
    baseline_head: str | None
    current_head: str | None
    commit_sha: str | None
    remote_branch: str | None
    pr_number: int | None
    pr_head_sha: str | None
    pr_base_branch: str | None
    ci_run_id: str | None
    ci_status: str | None
    ci_conclusion: str | None
    pending_human_gate: str
    merge_sha: str | None
    release_closure: str | None
    last_meaningful_event: str | None
    last_event_type: str | None
    heartbeat: str | None
    recovery_stage: str | None
    recovery_action: str | None
    next_action: str | None
    schema_version: int = model.SCHEMA_VERSION
    operator_version: str = model.OPERATOR_VERSION

    def validate(self) -> OperatorState:
        if not self.mission_id:
            model.fail(model.E_STATE_MALFORMED, "mission_id required")
        if self.pending_human_gate not in PENDING_GATES:
            model.fail(model.E_STATE_MALFORMED,
                       f"gate {self.pending_human_gate!r}")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "operator_version": self.operator_version,
            "mission_id": self.mission_id,
            "run_id": self.run_id,
            "objective": self.objective,
            "policy_profile": self.policy_profile,
            "policy_id": self.policy_id,
            "lifecycle": self.lifecycle,
            "readiness": self.readiness,
            "stage": self.stage,
            "phase": self.phase,
            "attempt": self.attempt,
            "implementation": {
                "backend": self.implementation_backend,
                "provider": self.implementation_provider,
                "model": self.implementation_model,
            },
            "inline_reviewer": (None if self.inline_reviewer is None
                                else dict(self.inline_reviewer)),
            "final_reviewer": (None if self.final_reviewer is None
                               else dict(self.final_reviewer)),
            "validation": (None if self.validation is None
                           else dict(self.validation)),
            "review_fresh": self.review_fresh,
            "review_status": self.review_status,
            "review_reason": self.review_reason,
            "reviewed_patch": self.reviewed_patch,
            "current_patch": self.current_patch,
            "branch": self.branch,
            "baseline_head": self.baseline_head,
            "current_head": self.current_head,
            "commit_sha": self.commit_sha,
            "remote_branch": self.remote_branch,
            "pr_number": self.pr_number,
            "pr_head_sha": self.pr_head_sha,
            "pr_base_branch": self.pr_base_branch,
            "ci_run_id": self.ci_run_id,
            "ci_status": self.ci_status,
            "ci_conclusion": self.ci_conclusion,
            "pending_human_gate": self.pending_human_gate,
            "merge_sha": self.merge_sha,
            "release_closure": self.release_closure,
            "last_meaningful_event": self.last_meaningful_event,
            "last_event_type": self.last_event_type,
            "heartbeat": self.heartbeat,
            "recovery_stage": self.recovery_stage,
            "recovery_action": self.recovery_action,
            "next_action": self.next_action,
        }


def _reviewer_document(value: object) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {
        "enabled": bool(value.get("enabled", False)),
        "active": bool(value.get("active", False)),
        "display_model": optional_str(value.get("display_model")),
        "provider": optional_str(value.get("provider")),
        "reason": optional_str(value.get("reason")),
    }


def _validation_document(closure: Mapping[str, Any] | None,
                         ) -> Mapping[str, Any] | None:
    if closure is None:
        return None
    results = closure.get("validation_results")
    if not isinstance(results, list):
        return None
    passed = sum(1 for item in results
                 if isinstance(item, Mapping)
                 and item.get("result") in ("PASS", "passed", True))
    latest_reason: str | None = None
    for item in reversed(results):
        if isinstance(item, Mapping):
            latest_reason = optional_str(item.get("reason"))
            break
    return {
        "runs": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "latest_reason": latest_reason,
    }


def _pending_gate(*, ready: bool, release_stage: str | None,
                  ci_green: bool, merged: bool,
                  closure_present: bool) -> str:
    if closure_present:
        return GATE_NONE
    if merged:
        return GATE_CLOSURE
    if release_stage == release_model.RST_MERGE_HANDOFF:
        return GATE_GO_MERGE
    if ci_green:
        return GATE_GO_MERGE_HANDOFF
    if release_stage in (release_model.RST_PR_BOUND,
                         release_model.RST_CI_PENDING,
                         release_model.RST_CI_SUCCESS):
        return GATE_NONE
    if release_stage == release_model.RST_COMMIT_HANDOFF:
        return GATE_GO_COMMIT
    if ready:
        return GATE_GO_COMMIT_HANDOFF
    return GATE_NONE


def build_operator_state(
    root: str, mission_id: str,
    *,
    git_state: Mapping[str, Any] | None = None,
) -> OperatorState:
    """Build the one-screen read-only operator state (never mutates)."""
    if not assembly_store.mission_exists(root, mission_id):
        model.fail(model.E_STATE_MALFORMED,
                   f"mission {mission_id!r} not found")
    mission_root = Path(assembly_store.mission_root(root, mission_id))
    mission = read_optional_json(mission_root / assembly_store.MISSION_NAME)
    closure = read_optional_json(mission_root / assembly_store.CLOSURE_NAME)
    status = read_optional_json(mission_root / "status.json")
    policy = read_optional_json(mission_root / model.POLICY_NAME)
    routing = read_optional_json(mission_root / model.ROUTING_NAME)
    release_state = read_optional_json(
        mission_root / release_store.RELEASE_STATE_NAME)
    commit_result = read_optional_json(
        mission_root / release_store.COMMIT_RESULT_NAME)
    pr_binding = read_optional_json(
        mission_root / release_store.PR_BINDING_NAME)
    ci_status = read_optional_json(
        mission_root / release_store.CI_STATUS_NAME)
    merge_result = read_optional_json(
        mission_root / release_store.MERGE_RESULT_NAME)
    release_closure = read_optional_json(
        mission_root / release_store.RELEASE_CLOSURE_NAME)

    review_gate = _review_gate(root, mission_id)
    routing_impl = _mapping((routing or {}).get("implementation"))
    implementation_backend = (
        optional_str(routing_impl.get("backend"))
        or optional_str((mission or {}).get("backend")))
    implementation_provider = (
        optional_str(routing_impl.get("provider"))
        or optional_str((mission or {}).get("provider")))
    implementation_model = (
        optional_str(routing_impl.get("model"))
        or optional_str((mission or {}).get("model")))

    baseline_revision = None
    if mission is not None:
        baseline = _mapping(mission.get("baseline"))
        baseline_revision = optional_str(baseline.get("revision"))

    branch = None
    current_head = None
    if git_state is not None:
        branch = optional_str(git_state.get("branch"))
        current_head = optional_str(git_state.get("head_sha"))

    commit_sha = optional_str((commit_result or {}).get("commit_sha"))
    remote_branch = optional_str((commit_result or {}).get("branch"))
    pr_number = optional_int((pr_binding or {}).get("number"))
    pr_head = optional_str((pr_binding or {}).get("head_sha"))
    pr_base = optional_str((pr_binding or {}).get("base_branch"))
    ci_runs = (ci_status or {}).get("runs")
    ci_run_id = None
    ci_conclusion = None
    if isinstance(ci_runs, list) and ci_runs:
        first = ci_runs[0]
        if isinstance(first, Mapping):
            ci_run_id = optional_str(first.get("run_id"))
            ci_conclusion = optional_str(first.get("conclusion"))
    merge_sha = optional_str((merge_result or {}).get("merge_sha"))
    closure_status = optional_str((release_closure or {}).get("status"))

    ready = bool(review_gate.ready) if review_gate is not None else False
    ci_green = bool((ci_status or {}).get("green"))
    merged = bool((merge_result or {}).get("merged"))
    release_stage = optional_str((release_state or {}).get("stage"))

    decision: operator_recovery.RecoveryDecision | None = None
    try:
        decision = operator_recovery.decide_recovery(root, mission_id)
    except model.OperatorError:
        decision = None

    last_event_type = None
    replay_events = operator_events.derive_events(root, mission_id)
    if replay_events:
        last_event_type = replay_events[-1].type

    next_action = optional_str((status or {}).get("next_action"))
    if not next_action and decision is not None:
        next_action = decision.next_step

    state = OperatorState(
        mission_id=mission_id,
        run_id=str((status or {}).get("run_id") or mission_id),
        objective=optional_str((mission or {}).get("objective")),
        policy_profile=optional_str((policy or {}).get("profile")),
        policy_id=optional_str((policy or {}).get("policy_id")),
        lifecycle=optional_str((status or {}).get("state")),
        readiness=optional_str((status or {}).get("readiness")),
        stage=optional_str((status or {}).get("stage")),
        phase=optional_str((status or {}).get("phase")),
        attempt=optional_int((status or {}).get("attempt")),
        implementation_backend=implementation_backend,
        implementation_provider=implementation_provider,
        implementation_model=implementation_model,
        inline_reviewer=_reviewer_document(
            (status or {}).get("inline_reviewer")),
        final_reviewer=_reviewer_document(
            (status or {}).get("final_reviewer")),
        validation=_validation_document(closure),
        review_fresh=(review_gate.fresh_review if review_gate is not None
                      else None),
        review_status=(review_gate.review_outcome
                       if review_gate is not None else None),
        review_reason=(review_gate.review_reason
                       if review_gate is not None else None),
        reviewed_patch=optional_str((status or {}).get("reviewed_patch")),
        current_patch=optional_str((status or {}).get("current_patch")),
        branch=branch,
        baseline_head=baseline_revision,
        current_head=current_head,
        commit_sha=commit_sha,
        remote_branch=remote_branch,
        pr_number=pr_number,
        pr_head_sha=pr_head,
        pr_base_branch=pr_base,
        ci_run_id=ci_run_id,
        ci_status=optional_str((ci_status or {}).get("state")),
        ci_conclusion=ci_conclusion,
        pending_human_gate=_pending_gate(
            ready=ready, release_stage=release_stage, ci_green=ci_green,
            merged=merged, closure_present=closure_status is not None),
        merge_sha=merge_sha,
        release_closure=closure_status,
        last_meaningful_event=optional_str(
            (status or {}).get("last_meaningful_event_at")),
        last_event_type=last_event_type,
        heartbeat=optional_str((status or {}).get("heartbeat_at")),
        recovery_stage=(decision.stage if decision is not None else None),
        recovery_action=(decision.action if decision is not None else None),
        next_action=next_action,
    )
    return state.validate()


def _review_gate(root: str, mission_id: str) -> Any:
    try:
        evidence = release_evidence.load_mission_evidence(root, mission_id)
    except (release_model.ReleaseError, model.OperatorError, OSError):
        return None
    try:
        return release_evidence.derive_review_gate(evidence)
    except release_model.ReleaseError:
        return None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def render_text(state: OperatorState) -> str:
    """Render the one-screen operator state as deterministic plain text."""
    document = state.to_dict()

    def cell(value: object) -> str:
        if value is None:
            return "-"
        if isinstance(value, Mapping):
            model_name = value.get("display_model")
            return str(model_name) if model_name else "-"
        return str(value)

    implementation = document["implementation"]
    lines = [
        "=" * 72,
        f"TRAJECTORY-OS OPERATOR STATE  ({state.operator_version})",
        "=" * 72,
        f"objective        : {cell(state.objective)}",
        f"mission / run id : {state.mission_id} / {state.run_id}",
        f"policy profile   : {cell(state.policy_profile)}",
        f"lifecycle        : {cell(state.lifecycle)}",
        f"readiness        : {cell(state.readiness)}",
        f"stage / phase    : {cell(state.stage)} / {cell(state.phase)}",
        f"attempt          : {cell(state.attempt)}",
        "-" * 72,
        "implementation   : "
        f"{cell(implementation.get('backend'))} / "
        f"{cell(implementation.get('provider'))} / "
        f"{cell(implementation.get('model'))}",
        f"inline reviewer  : {cell(state.inline_reviewer)}",
        f"final reviewer   : {cell(state.final_reviewer)}",
        f"validation       : {cell(state.validation)}",
        f"review fresh     : {cell(state.review_fresh)}",
        f"review status    : {cell(state.review_status)} "
        f"({cell(state.review_reason)})",
        f"reviewed patch   : {cell(state.reviewed_patch)}",
        f"current patch    : {cell(state.current_patch)}",
        "-" * 72,
        f"branch           : {cell(state.branch)}",
        f"baseline HEAD    : {cell(state.baseline_head)}",
        f"current HEAD     : {cell(state.current_head)}",
        f"commit SHA       : {cell(state.commit_sha)}",
        f"remote branch    : {cell(state.remote_branch)}",
        f"PR               : {cell(state.pr_number)} "
        f"(head {cell(state.pr_head_sha)} -> {cell(state.pr_base_branch)})",
        f"exact-head CI    : run {cell(state.ci_run_id)} / "
        f"{cell(state.ci_status)} / {cell(state.ci_conclusion)}",
        f"pending gate     : {state.pending_human_gate}",
        f"merge SHA        : {cell(state.merge_sha)}",
        f"release closure  : {cell(state.release_closure)}",
        "-" * 72,
        f"last event       : {cell(state.last_event_type)} "
        f"@ {cell(state.last_meaningful_event)}",
        f"heartbeat        : {cell(state.heartbeat)}",
        f"recovery         : {cell(state.recovery_stage)} / "
        f"{cell(state.recovery_action)}",
        f"next action      : {cell(state.next_action)}",
        "=" * 72,
    ]
    return "\n".join(lines)


__all__ = [
    "GATE_CLOSURE",
    "GATE_GO_COMMIT",
    "GATE_GO_COMMIT_HANDOFF",
    "GATE_GO_MERGE",
    "GATE_GO_MERGE_HANDOFF",
    "GATE_NONE",
    "PENDING_GATES",
    "OperatorState",
    "build_operator_state",
    "render_text",
]
