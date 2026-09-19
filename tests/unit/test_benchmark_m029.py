"""M029 — Pi vs DeepSeek Harness benchmark unit tests.

Covers the deterministic, fail-closed and observability contracts:
metric provenance (never invented), aggregation, exact patch identity,
reviewer identity (no stale default), fail-closed trials and the
interruption/resume lifecycle.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from trajectory_os.agents import model as agent_model
from trajectory_os.benchmark import (
    aggregate,
    engine,
    metrics,
    model,
    patch,
    status,
    store,
    workloads,
)
from trajectory_os.benchmark import cli as bench_cli
from trajectory_os.benchmark import executor as bench_executor
from trajectory_os.benchmark import review as bench_review
from trajectory_os.missions import review_protocol


def _fixture_engine(root: str, run_id: str, *, violate: bool = False,
                    workload_ids: tuple[str, ...] | None = None,
                    ) -> engine.BenchmarkEngine:
    config = engine.RunConfig(
        root=root, benchmark_run_id=run_id, mode=model.MODE_FIXTURE,
        workloads=workloads.select(workload_ids),
        backends=(agent_model.BACKEND_PI,
                  agent_model.BACKEND_DEEPSEEK_HARNESS),
        repetitions=1, target_provider="deepseek",
        target_model="deepseek-flash",
        environment=engine.environment_snapshot())
    return engine.BenchmarkEngine(
        config, executor=bench_executor.FixtureExecutor(
            violate_fail_closed=violate),
        reviewer_factory=engine.fixture_reviewer_factory(
            engine.default_passing_review()))


def _fixture_record(**overrides: object) -> model.TrialRecord:
    base: dict[str, object] = {
        "benchmark_run_id": "run",
        "trial_id": "trial",
        "workload_id": "small-targeted-repair",
        "workload_class": model.WC_SMALL_REPAIR,
        "repetition": 0,
        "backend": agent_model.BACKEND_PI,
        "provider": "deepseek",
        "model": "deepseek-flash",
        "locality": "remote",
        "runtime_version": None,
        "sdk_version": None,
        "mode": model.MODE_FIXTURE,
        "status": model.TS_PASS,
        "reason": model.R_OK,
        "fail_closed_case": False,
        "interrupted": False,
        "resumed": False,
        "agent": {"evidence": "PIPELINE_FIXTURE"},
        "validation": model.ValidationOutcome(
            command=("true",), passed=True, exit_code=0, duration_ms=1,
            timed_out=False, output_sha256=None, reason=model.R_OK),
        "review": _review_outcome(),
        "patch": model.PatchIdentity.build(
            available=True, sha256="a" * 64, files_changed=1, insertions=1,
            deletions=1, reason=model.R_OK),
        "telemetry": model.TrialTelemetry.empty(),
        "created_at": "2026-01-01T00:00:00Z",
    }
    base.update(overrides)
    return model.TrialRecord.build(**base)  # type: ignore[arg-type]


def _review_outcome() -> model.ReviewOutcome:
    return bench_review.assess_invocation(bench_review.ReviewerInvocation(
        role=model.ROLE_FINAL_INDEPENDENT_REVIEWER, backend="ollama",
        provider="ollama", model=model.FINAL_REVIEWER_MODEL, active=True,
        reason=model.R_OK, text=engine.default_passing_review()))


# --- metric provenance --------------------------------------------------------


def test_telemetry_unavailable_requires_reason() -> None:
    telemetry = model.TrialTelemetry.empty()
    assert telemetry.sources["prompt_tokens"] == model.SRC_UNAVAILABLE
    assert telemetry.unavailable["prompt_tokens"]
    # A fabricated UNAVAILABLE value must fail closed.
    with pytest.raises(model.BenchmarkError):
        model.validate_telemetry(replace(telemetry, prompt_tokens=10))
    # A labelled value without a value must fail closed.
    with pytest.raises(model.BenchmarkError):
        model.validate_telemetry(replace(
            telemetry,
            prompt_tokens=None,
            sources={**telemetry.sources,
                     "prompt_tokens": model.SRC_PROVIDER}))


def test_from_agent_result_grounds_and_marks_unavailable() -> None:
    result = agent_model.AgentResult.build(
        backend=agent_model.BACKEND_PI, status=agent_model.RS_COMPLETED,
        reason=agent_model.R_OK,
        events=[agent_model.AgentEvent.build(
            sequence=0, kind=agent_model.LK_RESULT, method="result",
            payload={"usage": {"prompt_tokens": 100,
                               "completion_tokens": 20,
                               "cache_read_input_tokens": 40,
                               "cost_usd": 0.002}})],
        runtime_ms=2000,
        completion=agent_model.CompletionEvidence.build(
            source=agent_model.CS_EXIT_CODE_MARKER, reliable=True,
            detail="ok"))
    telemetry = metrics.from_agent_result(
        result, provider="deepseek", model_name="deepseek-flash")
    assert telemetry.prompt_tokens == 100
    assert telemetry.total_tokens == 120
    assert telemetry.cache_hit_tokens == 40
    assert telemetry.cache_miss_tokens == 60
    assert telemetry.cache_hit_ratio == 0.4
    assert telemetry.cost_usd == 0.002
    assert telemetry.sources["total_tokens"] == model.SRC_DERIVED
    assert telemetry.sources["cost_usd"] == model.SRC_PROVIDER
    assert telemetry.sources["ttft_ms"] == model.SRC_UNAVAILABLE
    assert telemetry.unavailable["ttft_ms"]
    assert telemetry.prompt_tps == 50.0


def test_empty_telemetry_is_all_unavailable_with_reasons() -> None:
    telemetry = model.TrialTelemetry.empty()
    for name in aggregate.CONTINUOUS_METRICS:
        assert telemetry.sources[name] == model.SRC_UNAVAILABLE
        assert telemetry.unavailable.get(name)


# --- aggregation --------------------------------------------------------------


def test_aggregate_statistics_never_invent_values() -> None:
    records = [_fixture_record(), _fixture_record()]
    telemetry_a = metrics.from_agent_result(
        agent_model.AgentResult.build(
            backend=agent_model.BACKEND_PI, status=agent_model.RS_COMPLETED,
            reason=agent_model.R_OK,
            events=[agent_model.AgentEvent.build(
                sequence=0, kind=agent_model.LK_RESULT, method="result",
                payload={"prompt_tokens": 100, "completion_tokens": 50})],
            runtime_ms=1000),
        provider="deepseek", model_name="deepseek-flash")
    records[0] = replace(records[0], telemetry=telemetry_a)
    summary = aggregate.summarize(records)
    total = summary["metrics"]["total_tokens"]
    assert total["count"] == 1
    assert total["mean"] == 150
    assert total["median"] == 150
    assert total["p95"] == 150
    assert summary["metrics"]["ttft_ms"]["count"] == 0
    assert summary["metrics"]["ttft_ms"]["reason"]
    assert summary["outcomes"][model.TS_PASS] == 2


def test_aggregate_status_precedence() -> None:
    assert model.aggregate_status([]) == model.RUN_RUNNING
    assert model.aggregate_status([model.TS_PASS]) == model.RUN_READY_FOR_COMMIT
    assert model.aggregate_status(
        [model.TS_PASS, model.TS_UNAVAILABLE]) == model.RUN_BLOCKED
    assert model.aggregate_status(
        [model.TS_PASS, model.TS_FAILED]) == model.RUN_FAILED
    assert model.aggregate_status(
        [model.TS_FAILED, model.TS_CANCELLED]) == model.RUN_CANCELLED


# --- patch identity -----------------------------------------------------------


def test_patch_identity_is_exact_and_deterministic(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
    baseline = patch.capture(workspace)
    (workspace / "a.py").write_text("x = 2\n", encoding="utf-8")
    (workspace / "b.py").write_text("new = True\n", encoding="utf-8")
    current = patch.capture(workspace)
    first = patch.compute_patch(baseline, current)
    second = patch.compute_patch(baseline, patch.capture(workspace))
    assert first.sha256 == second.sha256
    assert first.files_changed == 2
    assert first.insertions >= 2
    assert first.deletions >= 1
    identity = patch.build_identity(baseline, current)
    assert identity.available is True
    assert identity.sha256 == first.sha256


def test_empty_workspace_patch_is_unavailable(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    identity = patch.build_identity(patch.capture(empty), patch.capture(empty))
    assert identity.available is False
    assert identity.reason == patch.REASON_EMPTY


def test_patch_identity_includes_removed_protected_path(tmp_path: Path) -> None:
    workspace = tmp_path / "ws-remove"
    workspace.mkdir()
    (workspace / "protected.py").write_text("SECRET\n", encoding="utf-8")
    baseline = patch.capture(workspace)
    (workspace / "protected.py").unlink()
    identity = patch.build_identity(baseline, patch.capture(workspace))
    assert identity.available is True
    assert "protected.py" in identity.untracked


# --- workload-owned protected paths -------------------------------------------


def test_protected_path_matching_is_explicit_not_prefix() -> None:
    touched = model.protected_paths_touched
    assert touched(("protected.py",), ("protected.py",)) == ("protected.py",)
    # A similarly named sibling must never match a declared protected path.
    assert touched(("protected.py",),
                   ("protected.py.bak", "other.py")) == ()
    # Two different protected sets are honoured independently.
    assert touched(("config/secret.env", "policy.md"),
                   ("policy.md",)) == ("policy.md",)
    assert touched(("config/secret.env",),
                   ("config/secret.env", "policy.md")) == (
                       "config/secret.env",)
    # Minimal normalization only; no traversal or glob expansion.
    assert touched(("./a/b.py",), ("a/b.py",)) == ("a/b.py",)
    assert touched((), ("anything.py",)) == ()


def test_workload_rejects_invalid_protected_path() -> None:
    with pytest.raises(model.BenchmarkError):
        model.WorkloadSpec(
            workload_id="bad", workload_class=model.WC_BLOCKED_FAIL_CLOSED,
            title="bad", objective="bad",
            validation_command=("true",), expect_pass=False,
            protected_paths=("../escape.py",)).validate()


def _custom_fail_closed(workload_id: str,
                        protected: str) -> model.WorkloadSpec:
    return model.WorkloadSpec(
        workload_id=workload_id,
        workload_class=model.WC_BLOCKED_FAIL_CLOSED,
        title="custom fail-closed",
        objective=f"Do not modify {protected}.",
        validation_command=("{python}", "-c", "print('OK')"),
        expect_pass=False,
        fixture={protected: "SECRET = 'original'\n"},
        descriptive_files=(protected,),
        protected_paths=(protected,)).validate()


def _engine_for_workloads(root: str, run_id: str,
                          selected: tuple[model.WorkloadSpec, ...],
                          *, violate: bool) -> engine.BenchmarkEngine:
    config = engine.RunConfig(
        root=root, benchmark_run_id=run_id, mode=model.MODE_FIXTURE,
        workloads=selected, backends=(agent_model.BACKEND_PI,),
        repetitions=1, target_provider="deepseek",
        target_model="deepseek-flash",
        environment=engine.environment_snapshot())
    return engine.BenchmarkEngine(
        config,
        executor=bench_executor.FixtureExecutor(violate_fail_closed=violate),
        reviewer_factory=engine.fixture_reviewer_factory(
            engine.default_passing_review()))


def test_protected_paths_are_workload_derived(tmp_path: Path) -> None:
    # Two different protected paths (neither is the canonical filename).
    violated = _custom_fail_closed("wl-violated", "config/secret.env")
    intact = _custom_fail_closed("wl-intact", "policies/important.md")

    violation = _engine_for_workloads(
        str(tmp_path), "run-wl-violation", (violated,), violate=True).run()
    assert violation.state == model.RUN_FAILED
    assert violation.trials[0].status == model.TS_FAILED
    assert violation.trials[0].reason == model.R_FAIL_CLOSED_VIOLATED

    clean = _engine_for_workloads(
        str(tmp_path), "run-wl-intact", (intact,), violate=False).run()
    assert clean.state == model.RUN_READY_FOR_COMMIT
    assert clean.trials[0].status == model.TS_PASS
    assert clean.trials[0].reason == model.R_FAIL_CLOSED_AS_EXPECTED


# --- reviewer identity --------------------------------------------------------


def test_stale_reviewer_is_never_active() -> None:
    inactive = bench_review.InactiveReviewerClient(
        model_name=model.FINAL_REVIEWER_MODEL,
        reason="not invoked")
    outcome = bench_review.assess_invocation(inactive.review(
        bench_review.ReviewRequest(
            objective="x", patch_text="", workspace="")))
    assert outcome.active is False
    assert outcome.outcome == bench_review.OUTCOME_INACTIVE
    assert outcome.reviewer.model == model.FINAL_REVIEWER_MODEL
    assert outcome.reviewer.active is False


def test_review_protocol_invalid_is_fail_closed() -> None:
    outcome = bench_review.assess_invocation(bench_review.ReviewerInvocation(
        role=model.ROLE_FINAL_INDEPENDENT_REVIEWER, backend="ollama",
        provider="ollama", model=model.FINAL_REVIEWER_MODEL, active=True,
        reason=model.R_OK, text="VERDICT: PASS\n"))
    assert outcome.outcome == review_protocol.OUTCOME_INVALID
    assert outcome.active is True


# --- trial status -------------------------------------------------------------


def test_fail_closed_trial_status() -> None:
    assert model.trial_status_from(
        backend_unavailable=False, validation_passed=True,
        fail_closed_case=True, fail_closed_violated=False,
        review_active=False, review_outcome=bench_review.OUTCOME_INACTIVE,
    ) == model.TS_PASS
    assert model.trial_status_from(
        backend_unavailable=False, validation_passed=True,
        fail_closed_case=True, fail_closed_violated=True,
        review_active=False, review_outcome=bench_review.OUTCOME_INACTIVE,
    ) == model.TS_FAILED
    assert model.trial_status_from(
        backend_unavailable=True, validation_passed=False,
        fail_closed_case=False, fail_closed_violated=False,
        review_active=False, review_outcome=bench_review.OUTCOME_INACTIVE,
    ) == model.TS_UNAVAILABLE
    assert model.trial_status_from(
        backend_unavailable=False, validation_passed=True,
        fail_closed_case=False, fail_closed_violated=False,
        review_active=True,
        review_outcome=review_protocol.OUTCOME_VALID_REJECT,
    ) == model.TS_FAILED


def test_trial_decision_matrix_is_single_canonical_path() -> None:
    decision = model.trial_decision
    inactive = bench_review.OUTCOME_INACTIVE
    valid_pass = review_protocol.OUTCOME_VALID_PASS

    # normal pass / fail
    assert decision(
        backend_unavailable=False, validation_passed=True,
        fail_closed_case=False, fail_closed_violated=False,
        review_active=True, review_outcome=valid_pass) == (
            model.TS_PASS, model.R_OK)
    assert decision(
        backend_unavailable=False, validation_passed=False,
        fail_closed_case=False, fail_closed_violated=False,
        review_active=True, review_outcome=valid_pass) == (
            model.TS_FAILED, model.R_VALIDATION_FAILED)
    # fail-closed pass
    assert decision(
        backend_unavailable=False, validation_passed=True,
        fail_closed_case=True, fail_closed_violated=False,
        review_active=False, review_outcome=inactive) == (
            model.TS_PASS, model.R_FAIL_CLOSED_AS_EXPECTED)
    # fail-closed violation
    assert decision(
        backend_unavailable=False, validation_passed=True,
        fail_closed_case=True, fail_closed_violated=True,
        review_active=False, review_outcome=inactive) == (
            model.TS_FAILED, model.R_FAIL_CLOSED_VIOLATED)
    # cancelled fail-closed wins over the violation
    assert decision(
        backend_unavailable=False, validation_passed=False,
        fail_closed_case=True, fail_closed_violated=True,
        review_active=False, review_outcome=inactive, cancelled=True) == (
            model.TS_CANCELLED, model.R_CANCELLED)
    # unavailable fail-closed wins and keeps its explicit reason
    assert decision(
        backend_unavailable=True, validation_passed=False,
        fail_closed_case=True, fail_closed_violated=True,
        review_active=False, review_outcome=inactive,
        unavailable_reason=model.R_BACKEND_UNAVAILABLE) == (
            model.TS_UNAVAILABLE, model.R_BACKEND_UNAVAILABLE)
    # an unreviewed would-be pass is BLOCKED under require_review
    assert decision(
        backend_unavailable=False, validation_passed=True,
        fail_closed_case=False, fail_closed_violated=False,
        review_active=False, review_outcome=inactive) == (
            model.TS_BLOCKED, model.R_UNPROVEN)
    # trial_status_from is exactly the require_review=False projection
    for kwargs in (
        dict(backend_unavailable=False, validation_passed=True,
             fail_closed_case=False, fail_closed_violated=False,
             review_active=True, review_outcome=valid_pass),
        dict(backend_unavailable=True, validation_passed=False,
             fail_closed_case=True, fail_closed_violated=True,
             review_active=False, review_outcome=inactive),
        dict(backend_unavailable=False, validation_passed=True,
             fail_closed_case=True, fail_closed_violated=False,
             review_active=False, review_outcome=inactive),
    ):
        assert model.trial_status_from(**kwargs) == decision(
            **kwargs, require_review=False)[0]


# --- engine lifecycle ---------------------------------------------------------


def test_engine_interruption_resume_and_artifacts(tmp_path: Path) -> None:
    runner = _fixture_engine(str(tmp_path), "run-fixture")
    first = runner.run()
    assert first.state == model.RUN_CANCELLED
    assert first.cancelled is True
    assert any(r.interrupted for r in first.trials)

    second = runner.run(resume=True)
    assert second.state == model.RUN_READY_FOR_COMMIT
    assert not second.cancelled
    assert len(second.trials) == 10
    assert all(r.status == model.TS_PASS for r in second.trials)
    assert any(r.workload_id == "interruption-resume" and r.resumed
               for r in second.trials)

    run_root = store.run_root(str(tmp_path), "run-fixture")
    assert (run_root / store.MANIFEST_NAME).is_file()
    assert (run_root / store.EVENTS_NAME).is_file()
    assert (run_root / store.SUMMARY_NAME).is_file()
    assert (run_root / store.REPORT_NAME).is_file()
    assert len(list((run_root / store.TRIALS_DIR).glob("*.json"))) == 10
    reconstructed = store.reconstruct(run_root)
    assert reconstructed["counts"]["trials"] == 10
    assert reconstructed["state"]["state"] == model.RUN_READY_FOR_COMMIT


def test_engine_fail_closed_violation_fails(tmp_path: Path) -> None:
    runner = _fixture_engine(
        str(tmp_path), "run-violation", violate=True,
        workload_ids=("intentional-blocked-fail-closed",))
    result = runner.run()
    assert result.state == model.RUN_FAILED
    assert all(r.status == model.TS_FAILED for r in result.trials)
    assert result.trials[0].reason == model.R_FAIL_CLOSED_VIOLATED


def test_fixture_interruption_is_backend_neutral(tmp_path: Path) -> None:
    executor = bench_executor.FixtureExecutor()
    workload = workloads.by_id("interruption-resume")
    for backend in (agent_model.BACKEND_PI,
                    agent_model.BACKEND_DEEPSEEK_HARNESS):
        request = bench_executor.ExecutionRequest(
            workload=workload, workspace=str(tmp_path / backend),
            backend=backend, provider="deepseek",
            model_name="deepseek-flash", locality="remote",
            mode=model.MODE_FIXTURE, attempt=0)
        interrupted = executor.execute(request)
        assert interrupted.cancelled is True
        assert interrupted.evidence == bench_executor.FIXTURE_EVIDENCE
        resumed = executor.execute(replace(request, attempt=1))
        assert resumed.cancelled is False
        assert resumed.evidence == bench_executor.FIXTURE_EVIDENCE
        assert resumed.agent_result is not None


def test_engine_interruption_covers_both_backends_in_fixture(
        tmp_path: Path) -> None:
    runner = _fixture_engine(str(tmp_path), "run-symmetric")
    first = runner.run()
    assert first.state == model.RUN_CANCELLED
    assert {r.backend for r in first.trials if r.interrupted} == {
        agent_model.BACKEND_PI, agent_model.BACKEND_DEEPSEEK_HARNESS}

    second = runner.run(resume=True)
    assert second.state == model.RUN_READY_FOR_COMMIT
    interruption = [r for r in second.trials
                    if r.workload_id == "interruption-resume"]
    assert {r.backend for r in interruption if r.resumed} == {
        agent_model.BACKEND_PI, agent_model.BACKEND_DEEPSEEK_HARNESS}
    assert all(r.status == model.TS_PASS for r in interruption)
    # Fixture evidence stays explicitly non-authoritative for every trial.
    assert all(r.agent.get("evidence") == bench_executor.FIXTURE_EVIDENCE
               for r in second.trials)


def test_engine_resume_rejects_mismatched_manifest(tmp_path: Path) -> None:
    runner = _fixture_engine(
        str(tmp_path), "run-mismatch",
        workload_ids=("small-targeted-repair",))
    runner.run()
    other = _fixture_engine(
        str(tmp_path), "run-mismatch", workload_ids=("medium-feature-implementation",))
    with pytest.raises(store.StoreError):
        other.run(resume=True)


def test_store_rejects_tampered_trial(tmp_path: Path) -> None:
    runner = _fixture_engine(str(tmp_path), "run-tamper")
    result = runner.run(resume=False)
    run_root = store.run_root(str(tmp_path), "run-tamper")
    # find any persisted trial and flip its stored status
    trial_id = result.trials[0].trial_id
    path = store.trial_path(run_root, trial_id)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["status"] = model.TS_FAILED
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(store.StoreError):
        store.load_trial(run_root, trial_id)


def test_status_and_follow_are_terminal_aware(tmp_path: Path) -> None:
    runner = _fixture_engine(str(tmp_path), "run-status")
    runner.run()
    runner.run(resume=True)
    document = status.status_document(str(tmp_path), "run-status")
    assert document["state"] == model.RUN_READY_FOR_COMMIT
    assert document["terminal"] is True
    assert document["actors"]["final_independent_reviewer"]["model"] == (
        model.FINAL_REVIEWER_MODEL)
    assert document["actors"]["inline_reviewer"]["active"] is False
    rendered = status.render_status(document)
    assert model.FINAL_REVIEWER_MODEL in rendered
    result = status.follow(str(tmp_path), "run-status", max_polls=1,
                           sleep=lambda _: None)
    assert result.exited is True
    assert result.polls == 0


def test_events_are_monotonic_and_identity_stable(tmp_path: Path) -> None:
    runner = _fixture_engine(str(tmp_path), "run-events")
    runner.run()
    run_root = store.run_root(str(tmp_path), "run-events")
    event_list = store.load_events(run_root)
    sequences = [event.sequence for event in event_list]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)
    for event in event_list:
        assert event.event_id == event.compute_event_id()


# --- CLI ----------------------------------------------------------------------


def test_cli_fixture_run_status_report(tmp_path: Path) -> None:
    root = str(tmp_path / "state")
    code = bench_cli.main([
        "run", "--root", root, "--run-id", "cli-run", "--mode", "fixture",
        "--workload", "small-targeted-repair", "--backend", "pi",
        "--json"])
    # Interruption workload not selected, so the run is ready.
    assert code == bench_cli.EXIT_OK
    status_code = bench_cli.main([
        "status", "--root", root, "--run-id", "cli-run", "--json"])
    assert status_code == bench_cli.EXIT_OK
    report_code = bench_cli.main([
        "report", "--root", root, "--run-id", "cli-run"])
    assert report_code == bench_cli.EXIT_OK
    reconstruct_code = bench_cli.main([
        "reconstruct", "--root", root, "--run-id", "cli-run"])
    assert reconstruct_code == bench_cli.EXIT_OK


def test_workloads_are_canonical_and_unique() -> None:
    ids = [w.workload_id for w in workloads.CANONICAL_WORKLOADS]
    assert len(ids) == len(set(ids)) == 5
    classes = {w.workload_class for w in workloads.CANONICAL_WORKLOADS}
    assert classes == set(model.WORKLOAD_CLASSES)
    assert workloads.by_id("small-targeted-repair").expect_pass is True
