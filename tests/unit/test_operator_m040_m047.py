"""M040–M047 unit tests — policy, routing, events, recovery and state."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from trajectory_os.assembly import store as assembly_store
from trajectory_os.observability import model as obs_model
from trajectory_os.operator import events as operator_events
from trajectory_os.operator import model, policy, recovery, routing, state
from trajectory_os.operator._util import digest


class FixedClock:
    def __init__(self, start: int = 0) -> None:
        self.value = start

    def __call__(self) -> str:
        self.value += 1
        return f"2026-09-24T00:00:{self.value:02d}Z"


# --- policy -------------------------------------------------------------------


def test_release_profile_preserves_both_human_gates() -> None:
    resolved = policy.release_policy(clock=FixedClock())
    assert resolved.policy.go_commit_required is True
    assert resolved.policy.go_merge_required is True
    assert resolved.policy.exact_head_ci_required is True


@pytest.mark.parametrize("field", [
    "go_commit_required", "go_merge_required", "exact_head_ci_required",
])
def test_release_gate_override_fails_closed(field: str) -> None:
    with pytest.raises(model.OperatorError) as excinfo:
        policy.release_policy(clock=FixedClock(), overrides={field: False})
    assert excinfo.value.code == model.E_POLICY_INVALID


def test_policy_resolution_is_deterministic() -> None:
    first = policy.release_policy(clock=FixedClock())
    second = policy.release_policy(clock=FixedClock())
    assert first.policy_id == second.policy_id
    assert first.policy.to_dict() == second.policy.to_dict()


def test_environment_cannot_mutate_policy() -> None:
    with pytest.raises(model.OperatorError) as excinfo:
        policy.resolve_policy("release", source="environment",
                              clock=FixedClock())
    assert excinfo.value.code == model.E_POLICY_ENVIRONMENT


def test_unknown_policy_profile_is_refused() -> None:
    with pytest.raises(model.OperatorError) as excinfo:
        policy.resolve_policy("nope", clock=FixedClock())
    assert excinfo.value.code == model.E_POLICY_PROFILE_UNKNOWN


def test_unknown_override_field_is_refused() -> None:
    with pytest.raises(model.OperatorError) as excinfo:
        policy.resolve_policy("safe", clock=FixedClock(),
                              overrides={"target_branch": "main"})
    assert excinfo.value.code == model.E_POLICY_OVERRIDE_INVALID


def test_policy_identity_is_content_addressed() -> None:
    resolved = policy.release_policy(clock=FixedClock())
    assert resolved.policy_id == digest(resolved.policy.to_dict(),
                                        domain=policy.POLICY_DOMAIN)


# --- routing ------------------------------------------------------------------


def test_final_reviewer_identity_is_exact() -> None:
    resolved = policy.release_policy(clock=FixedClock())
    decision = routing.resolve_routing("m", resolved, clock=FixedClock())
    assert decision.final_review.enabled is True
    assert decision.final_review.model == "qwen3.8:27b-q4_K_M"
    assert decision.final_review.display_model == "qwen3.8:27b-q4_K_M"
    assert decision.inline_review.enabled is False
    assert decision.inline_review.display_model is None


def test_unavailable_backend_fails_closed_without_fallback() -> None:
    resolved = policy.release_policy(clock=FixedClock())
    unavailable = routing.BackendCapability(
        backend="pi", provider="deepseek", model="deepseek-flash",
        available=False, supports_implementation=False, supports_review=False,
        reason="probe failed")
    with pytest.raises(model.OperatorError) as excinfo:
        routing.resolve_routing("m", resolved,
                                capabilities={"pi": unavailable},
                                clock=FixedClock())
    assert excinfo.value.code == model.E_ROUTING_UNAVAILABLE


def test_explicit_fallback_is_deterministic_and_persisted(
    tmp_path: Path,
) -> None:
    resolved = policy.resolve_policy(policy.PROFILE_BENCHMARK,
                                     clock=FixedClock())
    unavailable = routing.BackendCapability(
        backend="pi", provider="deepseek", model="deepseek-flash",
        available=False, supports_implementation=False, supports_review=False,
        reason="probe failed")
    available = routing.BackendCapability(
        backend="deepseek-harness", provider="deepseek-official",
        model="deepseek-flash", available=True,
        supports_implementation=True, supports_review=False,
        reason="available")
    decision = routing.resolve_routing(
        "m", resolved, capabilities={"pi": unavailable,
                                     "deepseek-harness": available},
        clock=FixedClock())
    assert decision.fallback_used is True
    assert decision.fallback_to == "deepseek-harness"
    assert decision.fallback_reason
    routing.persist_routing(str(tmp_path), "m", decision)
    loaded = routing.load_routing(str(tmp_path), "m")
    assert loaded is not None
    assert loaded.fallback_reason == decision.fallback_reason
    assert len(routing.routing_history(str(tmp_path), "m")) == 1


def test_legacy_phantom_reviewer_is_refused() -> None:
    with pytest.raises(model.OperatorError) as excinfo:
        policy.release_policy(
            clock=FixedClock(),
            overrides={"final_reviewer_model": "qwen3.6"})
    assert excinfo.value.code in (model.E_ROUTING_PHANTOM_REVIEWER,
                                  model.E_POLICY_INVALID)


def test_disabled_reviewer_never_displays_a_phantom_model() -> None:
    resolved = policy.resolve_policy(
        policy.PROFILE_SAFE, clock=FixedClock(),
        overrides={"final_review_required": False})
    decision = routing.resolve_routing("m", resolved, clock=FixedClock())
    assert decision.final_review.enabled is False
    assert decision.final_review.model is None
    assert decision.final_review.display_model is None


# --- events -------------------------------------------------------------------


def _event(**overrides: object) -> operator_events.OperatorEvent:
    kwargs: dict[str, object] = {
        "mission_id": "m", "type": operator_events.T_MISSION_STATUS,
        "source": operator_events.S_RUNTIME, "payload": {"a": 1},
        "ts": "2026-01-01T00:00:00Z", "sequence": 1}
    kwargs.update(overrides)
    return operator_events.OperatorEvent.build(**kwargs)  # type: ignore[arg-type]


def test_event_identity_excludes_clock_and_sequence() -> None:
    first = _event(ts="2026-01-01T00:00:00Z", sequence=1)
    second = _event(ts="2026-01-02T00:00:00Z", sequence=9)
    assert first.event_id == second.event_id


def test_recorded_event_deduplicates_deterministically(
    tmp_path: Path,
) -> None:
    root = str(tmp_path)
    first = operator_events.record_event(
        root, mission_id="m", type=operator_events.T_MISSION_CREATED,
        source=operator_events.S_MISSION, payload={"objective": "x"})
    second = operator_events.record_event(
        root, mission_id="m", type=operator_events.T_MISSION_CREATED,
        source=operator_events.S_MISSION, payload={"objective": "x"})
    assert first.event_id == second.event_id
    assert len(operator_events.load_events(root, "m")) == 1


def test_replay_is_read_only_and_idempotent(tmp_path: Path) -> None:
    root = str(tmp_path)
    assembly_store.ensure_mission_root(root, "m")
    operator_events.record_event(
        root, mission_id="m", type=operator_events.T_CONTROL,
        source=operator_events.S_CONTROL, payload={"action": "PAUSE"})
    before = _digest(tmp_path / "m")
    first = operator_events.replay(root, "m")
    second = operator_events.replay(root, "m")
    after = _digest(tmp_path / "m")
    assert before == after
    assert first["replay_id"] == second["replay_id"]
    assert first["event_count"] >= 1


def _digest(path: Path) -> str:
    material = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_file():
            material.update(str(item.relative_to(path)).encode("utf-8"))
            material.update(item.read_bytes())
    return material.hexdigest()


# --- recovery -----------------------------------------------------------------


def test_recovery_discovers_completed_closure(tmp_path: Path) -> None:
    root = str(tmp_path)
    mission_root = assembly_store.ensure_mission_root(root, "m")
    (mission_root / assembly_store.MISSION_NAME).write_text(
        "{}", encoding="utf-8")
    from trajectory_os.release import store as release_store

    (mission_root / release_store.RELEASE_CLOSURE_NAME).write_text(
        json.dumps({"mission_id": "m", "status": "CLOSED", "commit_sha": "a"}),
        encoding="utf-8")
    decision = recovery.decide_recovery(root, "m", clock=FixedClock())
    assert decision.action == recovery.RA_ALREADY_COMPLETE
    assert decision.terminal is True
    assert decision.idempotent is True
    assert recovery.IRREVERSIBLE_CLOSURE in decision.already_done


def test_recovery_fails_closed_on_local_remote_contradiction(
    tmp_path: Path,
) -> None:
    root = str(tmp_path)
    mission_root = assembly_store.ensure_mission_root(root, "m")
    (mission_root / assembly_store.MISSION_NAME).write_text(
        "{}", encoding="utf-8")
    from trajectory_os.release import store as release_store

    (mission_root / release_store.RELEASE_STATE_NAME).write_text(
        json.dumps({"mission_id": "m", "stage": "COMMITTED",
                    "commit_sha": "a" * 40, "reason": "OK",
                    "updated_at": "t"}),
        encoding="utf-8")
    decision = recovery.decide_recovery(root, "m", clock=FixedClock())
    assert decision.action == recovery.RA_BLOCKED
    assert decision.contradictions
    assert decision.fail_closed is True


def test_recovery_record_is_idempotent(tmp_path: Path) -> None:
    root = str(tmp_path)
    mission_root = assembly_store.ensure_mission_root(root, "m")
    (mission_root / assembly_store.MISSION_NAME).write_text(
        "{}", encoding="utf-8")
    decision = recovery.decide_recovery(root, "m", clock=FixedClock())
    path = recovery.record_recovery(root, decision)
    first = path.read_bytes()
    recovery.record_recovery(root, decision)
    assert path.read_bytes() == first


# --- state --------------------------------------------------------------------


def test_pending_gate_transitions() -> None:
    assert state._pending_gate(  # type: ignore[attr-defined]
        ready=True, release_stage=None, ci_green=False, merged=False,
        closure_present=False) == state.GATE_GO_COMMIT_HANDOFF
    assert state._pending_gate(  # type: ignore[attr-defined]
        ready=False, release_stage="MERGE_HANDOFF", ci_green=True,
        merged=False, closure_present=False) == state.GATE_GO_MERGE
    assert state._pending_gate(  # type: ignore[attr-defined]
        ready=False, release_stage="MERGED", ci_green=True, merged=True,
        closure_present=False) == state.GATE_CLOSURE
    assert state._pending_gate(  # type: ignore[attr-defined]
        ready=False, release_stage=None, ci_green=False, merged=False,
        closure_present=True) == state.GATE_NONE


def test_state_projection_is_read_only(tmp_path: Path) -> None:
    root = str(tmp_path)
    mission_root = assembly_store.ensure_mission_root(root, "m")
    (mission_root / assembly_store.MISSION_NAME).write_text(
        json.dumps({"mission_id": "m", "objective": "o"}), encoding="utf-8")
    before = _digest(mission_root)
    projected = state.build_operator_state(root, "m")
    state.render_text(projected)
    after = _digest(mission_root)
    assert before == after
    assert projected.mission_id == "m"
    assert projected.readiness != obs_model.RD_READY_FOR_COMMIT
