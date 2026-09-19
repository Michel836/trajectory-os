"""M030 — canonical live-run observability unit tests.

Covers the mission's regression contract:

1. validation PASS -> review REJECT -> repair IMPLEMENT -> final PASS,
   preserving historical vs current state;
2. ``--no-review`` never displays a phantom/default reviewer;
3. the final independent reviewer is shown in the correct role;
4. a lifecycle COMPLETE + BLOCKED run cannot render as ready/success;
5. an invalid model/provider stops at preflight before validation/review/repair;
6. unavailable telemetry is ``null`` + a stable reason, never guessed;
7. ``--follow`` exits on every terminal/readiness outcome;
8. CLI/TUI/Web projections consume the same canonical state model;
9. standard vs benchmark telemetry overhead is measured and reported.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from trajectory_os.agents import model as agent_model
from trajectory_os.benchmark import model as bench_model
from trajectory_os.benchmark import workloads as bench_workloads
from trajectory_os.benchmark.executor import (
    ExecutionOutcome,
    ExecutionRequest,
    FixtureExecutor,
)
from trajectory_os.observability import (
    follow as obs_follow,
)
from trajectory_os.observability import (
    model,
    preflight,
    projection,
    telemetry,
)
from trajectory_os.observability import run as obs_run

PASS_REVIEW = (
    "VERDICT: PASS\nBLOCKERS:\n- none\nMAJORS:\n- none\nMINORS:\n- none\n"
    "FINAL RECOMMENDATION: GO COMMIT\n")
REJECT_REVIEW = (
    "VERDICT: REJECT\nBLOCKERS:\n- missing edge-case handling\n"
    "MAJORS:\n- none\nMINORS:\n- none\nFINAL RECOMMENDATION: REPAIR\n")


def _config(tmp_path: Path, **overrides: Any) -> obs_run.LiveRunConfig:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    base: dict[str, Any] = {
        "run_id": "run",
        "root": str(tmp_path / "state"),
        "workspace": str(workspace),
        "backend": agent_model.BACKEND_PI,
        "provider": "deepseek",
        "model": "deepseek-flash",
        "workload": bench_workloads.by_id("small-targeted-repair"),
        "telemetry_mode": model.TELEMETRY_STANDARD,
        "max_repairs": 2,
        "require_review": True,
        "final_review_enabled": True,
        "mode": "fixture",
    }
    base.update(overrides)
    return obs_run.LiveRunConfig(**base)


def _coordinator(config: obs_run.LiveRunConfig,
                 responses: Sequence[str]) -> obs_run.RunCoordinator:
    return obs_run.RunCoordinator(
        config, executor=FixtureExecutor(interrupt_once=False),
        reviewer_factory=obs_run.scripted_reviewer_factory(list(responses)))


# --- 1. reject -> repair -> pass ---------------------------------------------


def test_reject_then_repair_then_pass_preserves_history(tmp_path: Path) -> None:
    result = _coordinator(
        _config(tmp_path, run_id="repair-run"),
        [REJECT_REVIEW, PASS_REVIEW]).run()
    status = result.status
    assert status.readiness == model.RD_READY_FOR_COMMIT
    assert status.state == model.LC_COMPLETE
    assert status.ready_for_commit is True
    assert status.success is True
    assert status.attempt == 1
    assert status.previous_gate == model.GATE_REVIEW
    assert status.previous_result == model.RESULT_PASS
    assert status.reviewed_patch is not None
    assert status.reviewed_patch == status.current_patch
    assert result.attempts == 2
    events = obs_run.store.load_events(
        obs_run.store.run_root(str(tmp_path / "state"), "repair-run"))
    review_results = [event["result"] for event in events
                      if event["kind"] == "REVIEW_COMPLETED"]
    assert review_results == [model.RESULT_REJECT, model.RESULT_PASS]
    # The historical rejected patch is preserved in the event stream.
    rejected = [event for event in events
                if event["kind"] == "REVIEW_COMPLETED"
                and event["result"] == model.RESULT_REJECT]
    assert rejected and rejected[0]["patch"]


def test_telemetry_reports_repair_and_reject_rates(tmp_path: Path) -> None:
    result = _coordinator(
        _config(tmp_path, run_id="rate-run"),
        [REJECT_REVIEW, PASS_REVIEW]).run()
    telemetry_document = result.telemetry
    assert telemetry_document is not None
    assert telemetry_document["derived"]["review_reject_rate"]["value"] == 0.5
    assert telemetry_document["derived"]["repair_rate"]["value"] == 0.5
    assert telemetry_document["waste"]["failed_cycle_count"] == 1


# --- 2. no-review never displays a phantom reviewer --------------------------


def test_no_review_never_displays_a_phantom_reviewer(tmp_path: Path) -> None:
    result = _coordinator(
        _config(tmp_path, run_id="no-review-run",
                require_review=False, final_review_enabled=False),
        []).run()
    status = result.status
    assert status.final_review_enabled is False
    assert status.final_reviewer.display_model is None
    assert status.inline_reviewer.display_model is None
    document = status.to_dict()
    rendered = projection.render_projection(
        document, target=projection.PROJECTION_CLI)
    assert "qwen3.6" not in rendered
    assert "qwen3.8" not in rendered
    assert result.status.ready_for_commit is True


# --- 3. final reviewer shown in the correct role -----------------------------


def test_final_reviewer_shown_in_correct_role(tmp_path: Path) -> None:
    result = _coordinator(
        _config(tmp_path, run_id="reviewer-run"),
        [PASS_REVIEW]).run()
    status = result.status
    final = status.final_reviewer
    assert final.role == model.ROLE_FINAL_INDEPENDENT_REVIEWER
    assert final.enabled is True
    assert final.active is True
    assert final.display_model == model.FINAL_REVIEWER_MODEL
    inline = status.inline_reviewer
    assert inline.role == model.ROLE_INLINE_REVIEWER
    assert inline.active is False
    assert inline.display_model is None


# --- 4. COMPLETE + BLOCKED is never ready/success ----------------------------


def test_blocked_complete_is_not_rendered_as_ready() -> None:
    status = model.CanonicalStatus.build(
        run_id="blocked-run", state=model.LC_COMPLETE,
        stage=model.STAGE_DONE, phase="DONE", attempt=0,
        current_backend="pi", current_provider="deepseek",
        current_model="deepseek-flash",
        inline_review_enabled=False,
        inline_reviewer=model.ReviewerStatus.disabled(
            model.ROLE_INLINE_REVIEWER),
        final_review_enabled=True,
        final_reviewer=model.ReviewerStatus(
            role=model.ROLE_FINAL_INDEPENDENT_REVIEWER, enabled=True,
            active=True, backend="ollama", provider="ollama",
            model=model.FINAL_REVIEWER_MODEL, reason=model.R_OK),
        previous_gate=model.GATE_REVIEW,
        previous_result=model.RESULT_REJECT, reviewed_patch="a" * 64,
        current_patch="a" * 64, next_action="operator: resolve blockers",
        last_meaningful_event_at=None, heartbeat_at=None,
        terminal_reason="blocking findings", readiness=model.RD_BLOCKED)
    document = status.to_dict()
    assert document["state"] == model.LC_COMPLETE
    assert document["success"] is False
    assert document["ready_for_commit"] is False
    view = projection.view_model(document)
    assert view.success is False
    assert "READY_FOR_COMMIT" not in view.banner
    for target in (projection.PROJECTION_CLI, projection.PROJECTION_TUI,
                   projection.PROJECTION_WEB):
        rendered = projection.render_projection(document, target=target)
        assert "READY_FOR_COMMIT" not in rendered


def test_inline_review_enabled_is_not_a_phantom(tmp_path: Path) -> None:
    result = _coordinator(
        _config(tmp_path, run_id="inline-run", inline_review_enabled=True,
                inline_reviewer_model=model.INLINE_REVIEWER_MODEL),
        [PASS_REVIEW]).run()
    inline = result.status.inline_reviewer
    assert inline.enabled is True
    assert inline.active is False
    assert inline.display_model is None


def test_reviewer_not_shown_active_when_validation_fails(tmp_path: Path) -> None:
    config = _config(tmp_path, run_id="valfail-run")
    # A validation command that always fails keeps the run away from review.
    workload = replace(
        config.workload,
        validation_command=("{python}", "-c", "raise SystemExit(1)"))
    config = replace(config, workload=workload)
    result = _coordinator(config, [PASS_REVIEW]).run()
    assert result.status.readiness == model.RD_FAILED
    assert result.status.final_reviewer.active is False
    assert result.status.final_reviewer.display_model is None


# --- 5. preflight fail-fast ---------------------------------------------------


@dataclass
class _CountingExecutor:
    calls: int = 0

    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        self.calls += 1
        raise AssertionError("executor must not run after a preflight reject")


def test_invalid_provider_model_stops_at_preflight(tmp_path: Path) -> None:
    executor = _CountingExecutor()
    config = _config(
        tmp_path, run_id="preflight-run", provider="ollama",
        model="deepseek-flash")
    coordinator = obs_run.RunCoordinator(
        config, executor=executor,
        reviewer_factory=obs_run.scripted_reviewer_factory([PASS_REVIEW]))
    result = coordinator.run()
    assert executor.calls == 0
    assert result.status.readiness == model.RD_BLOCKED
    assert result.status.state == model.LC_COMPLETE
    assert result.status.success is False
    assert result.status.terminal_reason_code == model.R_INVALID_MODEL_PROVIDER
    assert result.status.final_reviewer.display_model is None
    assert result.status.final_reviewer.active is False
    events = obs_run.store.load_events(
        obs_run.store.run_root(str(tmp_path / "state"), "preflight-run"))
    assert [event["kind"] for event in events] == ["PREFLIGHT_COMPLETED"]
    assert not any(event["kind"] in ("VALIDATION_COMPLETED",
                                     "REVIEW_COMPLETED")
                   for event in events)


def test_preflight_rejects_and_preserves_evidence() -> None:
    outcome = preflight.preflight(preflight.PreflightRequest(
        run_id="p", backend=agent_model.BACKEND_PI, provider="ollama",
        model="deepseek-flash", workspace="."))
    assert outcome.ok is False
    assert outcome.reason == model.R_INVALID_MODEL_PROVIDER
    assert outcome.checks
    assert any(not check["ok"] for check in outcome.checks)


# --- 6. unavailable telemetry is null + reason --------------------------------


def test_unavailable_telemetry_is_null_with_reason() -> None:
    records = [_record_with_empty_telemetry()]
    document = telemetry.aggregate_telemetry(
        run_id="na-run", mode=model.TELEMETRY_STANDARD, trials=records,
        identity={"run_id": "na-run"}, successful_tasks=0)
    ttft = document["metrics"]["ttft_ms"]
    assert ttft["value"] is None
    assert ttft["source"] == model.SRC_UNAVAILABLE
    assert ttft["reason"]
    derived = document["derived"]["tokens_per_successful_task"]
    assert derived["value"] is None
    assert derived["reason"]
    # A fabricated UNAVAILABLE value fails closed.
    import pytest

    with pytest.raises(model.ObservabilityError):
        model.Metric(name="m", value=10, source=model.SRC_UNAVAILABLE,
                     reason="r").validate()


def _record_with_empty_telemetry() -> bench_model.TrialRecord:
    from trajectory_os.benchmark import review as bench_review

    return bench_model.TrialRecord.build(
        benchmark_run_id="na-run", trial_id="t", workload_id="w",
        workload_class=bench_model.WC_SMALL_REPAIR, repetition=0,
        backend="pi", provider="deepseek", model="deepseek-flash",
        locality="remote", runtime_version=None, sdk_version=None,
        mode=bench_model.MODE_FIXTURE, status=bench_model.TS_BLOCKED,
        reason=bench_model.R_UNPROVEN, fail_closed_case=False,
        interrupted=False, resumed=False, agent={},
        validation=bench_model.ValidationOutcome(
            command=("true",), passed=False, exit_code=None, duration_ms=None,
            timed_out=False, output_sha256=None, reason=bench_model.R_NO_PATCH),
        review=bench_review.assess_invocation(bench_review.ReviewerInvocation(
            role=bench_model.ROLE_FINAL_INDEPENDENT_REVIEWER, backend="ollama",
            provider="ollama", model=bench_model.FINAL_REVIEWER_MODEL,
            active=False, reason=bench_model.R_UNPROVEN)),
        patch=bench_model.PatchIdentity.build(
            available=False, sha256=None, files_changed=None, insertions=None,
            deletions=None, reason=bench_model.R_NO_PATCH),
        telemetry=bench_model.TrialTelemetry.empty(),
        created_at="2026-01-01T00:00:00Z")


def test_telemetry_off_produces_no_metrics() -> None:
    document = telemetry.aggregate_telemetry(
        run_id="off-run", mode=model.TELEMETRY_OFF, trials=[],
        identity={"run_id": "off-run"})
    assert document["metrics"] == {}
    assert document["mode"] == model.TELEMETRY_OFF


# --- 7. follow exits on every terminal/readiness outcome ---------------------


def _document(state: str, readiness: str, terminal: bool) -> dict[str, Any]:
    return {
        "run_id": "follow-run", "state": state, "readiness": readiness,
        "terminal": terminal, "terminal_reason": "done",
    }


def _no_sleep(_: float) -> None:
    return None


def test_follow_exits_on_every_terminal_readiness() -> None:
    for readiness in sorted(model.TERMINAL_READINESS_STATES):
        state = (model.LC_COMPLETE
                 if readiness != model.RD_INDETERMINATE
                 else model.LC_RUNNING)
        document = _document(state, readiness, True)
        outcome = obs_follow.follow(
            lambda doc=document: doc, interval_s=0.0, max_polls=3,
            sleep=_no_sleep, notifier=None)
        assert outcome.exited is True
        assert outcome.polls == 1


def test_follow_exits_when_lifecycle_completes_without_readiness() -> None:
    document = _document(model.LC_COMPLETE, model.RD_UNKNOWN, True)
    outcome = obs_follow.follow(
        lambda: document, interval_s=0.0, max_polls=3, sleep=_no_sleep,
        notifier=None)
    assert outcome.exited is True


def test_follow_polls_until_terminal_then_bounded() -> None:
    frames = [
        _document(model.LC_RUNNING, model.RD_INDETERMINATE, False),
        _document(model.LC_COMPLETE, model.RD_READY_FOR_COMMIT, True),
    ]
    state = {"index": 0}

    def reader() -> dict[str, Any]:
        index = min(state["index"], len(frames) - 1)
        state["index"] += 1
        return frames[index]

    outcome = obs_follow.follow(reader, interval_s=0.0, max_polls=5,
                                sleep=_no_sleep, notifier=None)
    assert outcome.exited is True
    assert outcome.polls == 2


def test_follow_is_bounded_when_never_terminal() -> None:
    document = _document(model.LC_RUNNING, model.RD_INDETERMINATE, False)
    outcome = obs_follow.follow(
        lambda: document, interval_s=0.0, max_polls=4, sleep=_no_sleep,
        notifier=None)
    assert outcome.exited is False
    assert outcome.polls == 4
    assert outcome.reason == "FOLLOW_BOUND_REACHED"


# --- 8. shared canonical projection ------------------------------------------


def test_cli_tui_web_consume_the_same_canonical_state(tmp_path: Path) -> None:
    result = _coordinator(
        _config(tmp_path, run_id="projection-run"),
        [PASS_REVIEW]).run()
    document = result.status.to_dict()
    view = projection.view_model(document)
    cli = projection.render_projection(document,
                                      target=projection.PROJECTION_CLI)
    tui = projection.render_projection(document,
                                      target=projection.PROJECTION_TUI)
    web = projection.render_projection(document,
                                      target=projection.PROJECTION_WEB)
    assert view.readiness in cli
    assert view.banner in cli
    assert view.banner in tui
    assert f'data-ready-for-commit="{str(view.success).lower()}"' in web
    assert f'data-terminal="{str(view.terminal).lower()}"' in web
    # No surface can disagree: every field is projected from one view model.
    assert projection.view_model(document) == view


# --- 9. telemetry overhead is measured ---------------------------------------


def test_standard_vs_benchmark_overhead_is_measured() -> None:
    standard = telemetry.measure_overhead(
        model.TELEMETRY_STANDARD, lambda: [1, 2, 3])
    benchmark = telemetry.measure_overhead(
        model.TELEMETRY_BENCHMARK, lambda: list(range(50)))
    assert standard.mode == model.TELEMETRY_STANDARD
    assert benchmark.mode == model.TELEMETRY_BENCHMARK
    assert standard.collection_ms >= 0.0
    assert benchmark.collection_ms >= 0.0
    assert benchmark.sample_count == 50
    assert standard.sample_count == 3
    assert standard.to_dict()["mode"] == model.TELEMETRY_STANDARD


def test_next_action_and_readiness_derivation() -> None:
    assert model.next_action_for(
        model.LC_IMPLEMENTING, model.RD_INDETERMINATE) == (
            "await implementation result")
    assert model.next_action_for(
        model.LC_COMPLETE, model.RD_READY_FOR_COMMIT).startswith("operator")
    assert model.derive_readiness(
        lifecycle_state=model.LC_COMPLETE,
        trial_results=[model.RESULT_PASS]) == model.RD_READY_FOR_COMMIT
    assert model.derive_readiness(
        lifecycle_state=model.LC_COMPLETE,
        trial_results=[model.RESULT_BLOCKED]) == model.RD_BLOCKED
    assert model.derive_readiness(
        lifecycle_state=model.LC_RUNNING) == model.RD_INDETERMINATE
