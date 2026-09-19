#!/usr/bin/env python3
"""M030 — live-run observability and benchmark telemetry dogfood.

Drives the REAL canonical observability path end to end and records honest
evidence:

* S1 — canonical durable artifacts: events.jsonl, status.json, telemetry.json,
  summary.json produced by a real implementation/validation/review/repair
  lifecycle (fixture execution, explicitly non-authoritative);
* S2 — correct CURRENT/NEXT, stage/phase/attempt and reviewer identities;
* S3 — lifecycle vs readiness distinction (COMPLETE != READY_FOR_COMMIT);
* S4 — preflight fail-fast (invalid provider/model never reaches
  validation/review/repair);
* S5 — honest unavailable telemetry (null + stable reason, never invented);
* S6 — heartbeat/follow auto-exit on every terminal/readiness outcome;
* S7 — shared CLI/TUI/Web projection contract;
* S8 — standard vs benchmark telemetry overhead measured and reported;
* S9 — no Git trust-boundary write and no local polling loop in the reader.

Exit codes: 0 = all checks proven; 3 = proof blocked; 2 = usage error.
"""

# ruff: noqa: E402
# (the repository src/ layout is added to sys.path below)

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import shutil
import sys
import tempfile
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from trajectory_os.agents import model as agent_model
from trajectory_os.benchmark import workloads as bench_workloads
from trajectory_os.benchmark.executor import (
    ExecutionOutcome,
    ExecutionRequest,
    FixtureExecutor,
)
from trajectory_os.observability import follow as obs_follow
from trajectory_os.observability import model, preflight, projection, telemetry
from trajectory_os.observability import run as obs_run
from trajectory_os.observability import store as obs_store

PASS_REVIEW = (
    "VERDICT: PASS\nBLOCKERS:\n- none\nMAJORS:\n- none\nMINORS:\n- none\n"
    "FINAL RECOMMENDATION: GO COMMIT\n")
REJECT_REVIEW = (
    "VERDICT: REJECT\nBLOCKERS:\n- missing edge-case handling\n"
    "MAJORS:\n- none\nMINORS:\n- none\nFINAL RECOMMENDATION: REPAIR\n")

CHECK_LOG: list[dict[str, Any]] = []


def _check(scenario: str, description: str, ok: bool) -> bool:
    CHECK_LOG.append({"scenario": scenario, "description": description,
                      "ok": bool(ok)})
    return bool(ok)


def _config(root: str, run_id: str, **overrides: Any) -> obs_run.LiveRunConfig:
    workspace = pathlib.Path(root) / run_id / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    base: dict[str, Any] = {
        "run_id": run_id, "root": root, "workspace": str(workspace),
        "backend": agent_model.BACKEND_PI, "provider": "deepseek",
        "model": "deepseek-flash",
        "workload": bench_workloads.by_id("small-targeted-repair"),
        "telemetry_mode": model.TELEMETRY_BENCHMARK,
        "max_repairs": 2, "mode": "fixture",
    }
    base.update(overrides)
    return obs_run.LiveRunConfig(**base)


def _scenario_canonical(root: str) -> dict[str, Any]:
    run_id = "m030-dogfood-repair"
    coordinator = obs_run.RunCoordinator(
        _config(root, run_id, telemetry_mode=model.TELEMETRY_BENCHMARK),
        executor=FixtureExecutor(interrupt_once=False),
        reviewer_factory=obs_run.scripted_reviewer_factory(
            [REJECT_REVIEW, PASS_REVIEW]))
    result = coordinator.run()
    run_root = obs_store.run_root(root, run_id)
    artifacts = {
        name: (run_root / name).is_file()
        for name in (obs_store.EVENTS_NAME, obs_store.STATUS_NAME,
                     obs_store.TELEMETRY_NAME, obs_store.SUMMARY_NAME)}
    _check("S1", "canonical durable artifacts are persisted",
           all(artifacts.values()))
    status = result.status
    _check("S1", "lifecycle completes and readiness is explicitly ready",
           status.state == model.LC_COMPLETE
           and status.readiness == model.RD_READY_FOR_COMMIT
           and status.success is True)
    events = obs_store.load_events(run_root)
    review_results = [event["result"] for event in events
                      if event["kind"] == "REVIEW_COMPLETED"]
    _check("S1", "review REJECT -> repair -> final PASS is preserved",
           review_results == [model.RESULT_REJECT, model.RESULT_PASS]
           and result.attempts == 2)
    _check("S1", "historical and current patch identity are preserved",
           status.reviewed_patch == status.current_patch
           and bool(status.reviewed_patch)
           and any(event.get("patch") for event in events
                   if event["result"] == model.RESULT_REJECT))
    return {
        "run_id": run_id, "state": status.state,
        "readiness": status.readiness, "attempts": result.attempts,
        "artifacts": artifacts, "events": len(events),
    }


def _scenario_status_shape(root: str) -> dict[str, Any]:
    document = obs_store.load_status(
        obs_store.run_root(root, "m030-dogfood-repair"))
    required = (
        "run_id", "state", "stage", "phase", "attempt", "current_backend",
        "current_provider", "current_model", "inline_review_enabled",
        "inline_reviewer", "final_review_enabled", "final_reviewer",
        "previous_gate", "previous_result", "reviewed_patch", "current_patch",
        "next_action", "last_meaningful_event_at", "heartbeat_at",
        "terminal_reason", "readiness")
    missing = [field for field in required if field not in document]
    _check("S2", "canonical status exposes every required field",
           not missing)
    _check("S2", "correct CURRENT/NEXT and stage/phase/attempt are present",
           document["current"] is not None
           and document["stage"] == model.STAGE_DONE
           and document["phase"] == "DONE"
           and document["attempt"] == 1)
    final = document["final_reviewer"]
    inline = document["inline_reviewer"]
    _check("S2", "final reviewer is shown in the correct role",
           final["role"] == model.ROLE_FINAL_INDEPENDENT_REVIEWER
           and final["active"] is True
           and final["display_model"] == model.FINAL_REVIEWER_MODEL)
    _check("S2", "inline reviewer is never displayed as active",
           inline["role"] == model.ROLE_INLINE_REVIEWER
           and inline["display_model"] is None)
    return {"missing": missing, "final": final, "inline": inline}


def _scenario_no_review(root: str) -> dict[str, Any]:
    run_id = "m030-dogfood-no-review"
    coordinator = obs_run.RunCoordinator(
        _config(root, run_id, require_review=False,
                final_review_enabled=False,
                telemetry_mode=model.TELEMETRY_STANDARD),
        executor=FixtureExecutor(interrupt_once=False))
    result = coordinator.run()
    document = result.status.to_dict()
    rendered = projection.render_projection(
        document, target=projection.PROJECTION_CLI)
    _check("S2", "--no-review never displays a phantom qwen reviewer",
           "qwen3.6" not in rendered and "qwen3.8" not in rendered
           and document["final_reviewer"]["display_model"] is None)
    _check("S2", "--no-review still reaches an explicit readiness",
           result.status.readiness == model.RD_READY_FOR_COMMIT)
    return {"rendered_has_qwen": "qwen" in rendered.lower()}


def _scenario_lifecycle_readiness() -> dict[str, Any]:
    blocked = model.CanonicalStatus.build(
        run_id="blocked", state=model.LC_COMPLETE, stage=model.STAGE_DONE,
        phase="DONE", attempt=0, current_backend="pi",
        current_provider="deepseek", current_model="deepseek-flash",
        inline_review_enabled=False,
        inline_reviewer=model.ReviewerStatus.disabled(
            model.ROLE_INLINE_REVIEWER),
        final_review_enabled=True,
        final_reviewer=model.ReviewerStatus(
            role=model.ROLE_FINAL_INDEPENDENT_REVIEWER, enabled=True,
            active=True, backend="ollama", provider="ollama",
            model=model.FINAL_REVIEWER_MODEL, reason=model.R_OK),
        previous_gate=model.GATE_REVIEW, previous_result=model.RESULT_REJECT,
        reviewed_patch="a" * 64, current_patch="a" * 64,
        next_action="operator: resolve blocking findings",
        last_meaningful_event_at=None, heartbeat_at=None,
        terminal_reason="blocking findings", readiness=model.RD_BLOCKED)
    document = blocked.to_dict()
    web = projection.render_projection(
        document, target=projection.PROJECTION_WEB)
    _check("S3", "COMPLETE + BLOCKED is never rendered as ready/success",
           document["state"] == model.LC_COMPLETE
           and document["success"] is False
           and 'data-ready-for-commit="false"' in web)
    return {"success": document["success"],
            "readiness": document["readiness"]}


class _CountingExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        self.calls += 1
        raise AssertionError("downstream execution must not run")


def _scenario_preflight(root: str) -> dict[str, Any]:
    executor = _CountingExecutor()
    coordinator = obs_run.RunCoordinator(
        _config(root, "m030-dogfood-preflight", provider="ollama",
                model="deepseek-flash"),
        executor=executor,
        reviewer_factory=obs_run.scripted_reviewer_factory([PASS_REVIEW]))
    result = coordinator.run()
    events = obs_store.load_events(
        obs_store.run_root(root, "m030-dogfood-preflight"))
    kinds = [event["kind"] for event in events]
    _check("S4", "invalid provider/model stops at preflight",
           executor.calls == 0
           and result.status.readiness == model.RD_BLOCKED
           and result.status.terminal_reason_code
           == model.R_INVALID_MODEL_PROVIDER)
    _check("S4", "no validation/review/repair cycle runs after rejection",
           kinds == ["PREFLIGHT_COMPLETED"])
    _check("S4", "preflight rejection shows no phantom reviewer",
           result.status.final_reviewer.display_model is None
           and result.status.final_reviewer.active is False)
    return {"calls": executor.calls, "events": kinds,
            "preflight": preflight.preflight(preflight.PreflightRequest(
                run_id="p", backend=agent_model.BACKEND_PI,
                provider="ollama", model="deepseek-flash",
                workspace=root)).to_dict()}


def _scenario_honest_telemetry(root: str) -> dict[str, Any]:
    document = obs_store.load_telemetry(
        obs_store.run_root(root, "m030-dogfood-repair"))
    ttft = document["metrics"]["ttft_ms"]
    cpu = document["metrics"]["cpu_percent"]
    _check("S5", "unavailable metrics are null with a stable reason",
           ttft["value"] is None and ttft["source"] == model.SRC_UNAVAILABLE
           and bool(ttft["reason"])
           and cpu["value"] is None and bool(cpu["reason"]))
    derived = document["derived"]
    _check("S5", "deterministic derived metrics are present",
           derived["review_reject_rate"]["value"] == 0.5
           and derived["repair_rate"]["value"] == 0.5
           and derived["cost_per_successful_task"]["value"] is not None)
    _check("S5", "benchmark mode adds percentile series",
           "p95" in document["series"]["total_duration_ms"])
    return {"ttft": ttft, "cpu": cpu, "derived_keys": sorted(derived)}


def _scenario_follow(root: str) -> dict[str, Any]:
    run_root = obs_store.run_root(root, "m030-dogfood-repair")
    terminal_doc = obs_store.load_status(run_root)
    outcome = obs_follow.follow(
        lambda: terminal_doc, interval_s=0.0, max_polls=3,
        sleep=lambda _: None, notifier=None)
    _check("S6", "follow auto-exits on a terminal/readiness outcome",
           outcome.exited is True and outcome.polls == 1)
    frames = [
        {"run_id": "hb", "state": model.LC_RUNNING,
         "readiness": model.RD_INDETERMINATE, "terminal": False},
        {"run_id": "hb", "state": model.LC_COMPLETE,
         "readiness": model.RD_BLOCKED, "terminal": True,
         "terminal_reason": "blocked"},
    ]
    index = {"i": 0}

    def reader() -> dict[str, Any]:
        value = frames[min(index["i"], len(frames) - 1)]
        index["i"] += 1
        return value

    polled = obs_follow.follow(reader, interval_s=0.0, max_polls=5,
                               sleep=lambda _: None, notifier=None)
    _check("S6", "follow polls a live run then exits on terminal readiness",
           polled.exited is True and polled.polls == 2)
    return {"immediate_polls": outcome.polls,
            "polled_polls": polled.polls}


def _scenario_projections(root: str) -> dict[str, Any]:
    document = obs_store.load_status(
        obs_store.run_root(root, "m030-dogfood-repair"))
    view = projection.view_model(document)
    renders = {
        target: projection.render_projection(document, target=target)
        for target in (projection.PROJECTION_CLI, projection.PROJECTION_TUI,
                       projection.PROJECTION_WEB)}
    _check("S7", "CLI/TUI/Web consume the same canonical view model",
           set(renders) == {projection.PROJECTION_CLI,
                            projection.PROJECTION_TUI,
                            projection.PROJECTION_WEB}
           and view.banner in renders[projection.PROJECTION_CLI]
           and view.banner in renders[projection.PROJECTION_TUI]
           and 'data-ready-for-commit="true"'
           in renders[projection.PROJECTION_WEB])
    return {"readiness": view.readiness, "success": view.success}


def _scenario_overhead() -> dict[str, Any]:
    standard = telemetry.measure_overhead(
        model.TELEMETRY_STANDARD, lambda: [1, 2, 3])
    benchmark = telemetry.measure_overhead(
        model.TELEMETRY_BENCHMARK, lambda: list(range(100)))
    _check("S8", "standard vs benchmark overhead is measured and reported",
           standard.mode == model.TELEMETRY_STANDARD
           and benchmark.mode == model.TELEMETRY_BENCHMARK
           and standard.sample_count == 3
           and benchmark.sample_count == 100
           and standard.collection_ms >= 0
           and benchmark.collection_ms >= 0)
    return {"standard": standard.to_dict(),
            "benchmark": benchmark.to_dict()}


def _scenario_trust_boundary() -> dict[str, Any]:
    import trajectory_os.observability.adapter as ad
    import trajectory_os.observability.follow as fl
    import trajectory_os.observability.preflight as pf
    import trajectory_os.observability.projection as pj
    import trajectory_os.observability.run as rn
    import trajectory_os.observability.store as st
    import trajectory_os.observability.telemetry as tm

    verbs = ("commit", "push", "merge", "reset", "restore", "clean", "stash",
             "rebase", "switch", "checkout", "pull", "fetch", "cherry-pick")
    offenders: list[str] = []
    for module in (ad, fl, pf, pj, rn, st, tm):
        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        for verb in verbs:
            if f'"git", "{verb}"' in source or f"'git', '{verb}'" in source:
                offenders.append(f"{module.__name__}:{verb}")
    _check("S9", "no observability module performs a Git trust-boundary write",
           not offenders)
    reader = (REPO / "scripts" / "trajectory-pi-status").read_text(
        encoding="utf-8")
    _check("S9", "reader keeps no local polling loop or direct sleep",
           "while True" not in reader and "sleep(" not in reader)
    return {"offenders": offenders}


def _scenario_result(name: str) -> dict[str, Any]:
    checks = [c for c in CHECK_LOG if c["scenario"] == name]
    return {"scenario": name, "checks": len(checks),
            "passed": sum(1 for c in checks if c["ok"]),
            "status": "PASS" if checks and all(c["ok"] for c in checks)
            else "FAIL"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mission030_dogfood",
        description="M030 live-run observability and telemetry dogfood.")
    parser.add_argument("--root", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--keep", action="store_true", default=False)
    args = parser.parse_args(argv)

    temporary = args.root is None
    root = args.root or tempfile.mkdtemp(prefix="trajectory-m030-")
    pathlib.Path(root).mkdir(parents=True, exist_ok=True)
    try:
        canonical = _scenario_canonical(root)
        shape = _scenario_status_shape(root)
        no_review = _scenario_no_review(root)
        lifecycle = _scenario_lifecycle_readiness()
        preflight_result = _scenario_preflight(root)
        honest = _scenario_honest_telemetry(root)
        follow_result = _scenario_follow(root)
        projections = _scenario_projections(root)
        overhead = _scenario_overhead()
        trust = _scenario_trust_boundary()
        scenarios = sorted({c["scenario"] for c in CHECK_LOG})
        report = {
            "mission": "M030",
            "issue": 232,
            "generated_at": datetime.datetime.now(datetime.UTC)
            .replace(microsecond=0, tzinfo=None).isoformat() + "Z",
            "root": root,
            "canonical": canonical,
            "status_shape": shape,
            "no_review": no_review,
            "lifecycle_readiness": lifecycle,
            "preflight": preflight_result,
            "honest_telemetry": honest,
            "follow": follow_result,
            "projections": projections,
            "overhead": overhead,
            "trust_boundary": trust,
            "scenarios": [_scenario_result(code) for code in scenarios],
            "checks": CHECK_LOG,
            "status": ("PASS" if all(c["ok"] for c in CHECK_LOG)
                       else "FAIL"),
        }
        out = pathlib.Path(
            args.out or pathlib.Path(root) / "m030-dogfood.json")
        out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        print(f"M030 dogfood {report['status']}: {out}")
        for code in scenarios:
            print(f"  {code}: {_scenario_result(code)['status']}")
        return 0 if report["status"] == "PASS" else 3
    finally:
        if temporary and not args.keep:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
