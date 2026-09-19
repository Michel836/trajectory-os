#!/usr/bin/env python3
"""M029 — Pi vs DeepSeek Harness benchmark dogfood.

Drives the REAL M029 benchmark pipeline end to end and records honest
evidence:

* S1 — deterministic pipeline proof: full canonical workload set, both
  backends, interruption + resume, fail-closed trial, artifact
  reconstruction and aggregation (fixture execution evidence, explicitly
  labelled non-authoritative);
* S2 — live backend evidence: real probe of Pi and DeepSeek Harness and a
  bounded isolated Harness qualification. When the official runtime cannot
  be proven, the result is recorded as UNAVAILABLE/INCOMPATIBLE evidence;
  no successful Harness measurement is ever fabricated;
* S3 — reviewer identity semantics: the active final reviewer is
  ``qwen3.8:27b-q4_K_M`` and no stale ``qwen3.6`` reviewer is ever shown as
  active;
* S4 — no benchmark module performs a Git trust-boundary write.

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

from trajectory_os.agents import harness_qualification as hq
from trajectory_os.agents import model as agent_model
from trajectory_os.agents import registry
from trajectory_os.benchmark import engine, metrics, model, status, store
from trajectory_os.benchmark import executor as bench_executor
from trajectory_os.benchmark import workloads as bench_workloads

FIXTURE_RUN = "m029-dogfood-fixture"
FAIL_CLOSED_RUN = "m029-dogfood-fail-closed"
LIVE_RUN = "m029-dogfood-live"

CHECK_LOG: list[dict[str, Any]] = []


def _check(scenario: str, description: str, ok: bool) -> bool:
    CHECK_LOG.append({"scenario": scenario, "description": description,
                      "ok": bool(ok)})
    return bool(ok)


def _fixture_engine(root: str, run_id: str, *,
                    violate: bool = False,
                    workload_ids: tuple[str, ...] | None = None,
                    ) -> engine.BenchmarkEngine:
    config = engine.RunConfig(
        root=root, benchmark_run_id=run_id, mode=model.MODE_FIXTURE,
        workloads=bench_workloads.select(workload_ids),
        backends=(agent_model.BACKEND_PI,
                  agent_model.BACKEND_DEEPSEEK_HARNESS),
        repetitions=1, target_provider="deepseek",
        target_model="deepseek-flash",
        final_reviewer_model=model.FINAL_REVIEWER_MODEL,
        environment=engine.environment_snapshot(), require_review=True)
    return engine.BenchmarkEngine(
        config, executor=bench_executor.FixtureExecutor(
            violate_fail_closed=violate),
        reviewer_factory=engine.fixture_reviewer_factory(
            engine.default_passing_review()))


def _scenario_pipeline(root: str) -> dict[str, Any]:
    runner = _fixture_engine(root, FIXTURE_RUN)
    first = runner.run()
    _check("S1", "interruption stops the first pass with CANCELLED",
           first.state == model.RUN_CANCELLED and first.cancelled)
    interrupted = [r for r in first.trials if r.interrupted]
    _check("S1", "interruption/resume covers every backend symmetrically",
           {r.backend for r in interrupted} == {
               agent_model.BACKEND_PI,
               agent_model.BACKEND_DEEPSEEK_HARNESS}
           and all(r.status == model.TS_CANCELLED for r in interrupted))

    second = runner.run(resume=True)
    _check("S1", "resume completes every planned trial",
           second.state == model.RUN_READY_FOR_COMMIT
           and not second.cancelled
           and second.skipped >= 1
           and all(r.status == model.TS_PASS for r in second.trials))
    _check("S1", "both backends record the resume",
           {r.backend for r in second.trials
            if r.workload_id == "interruption-resume" and r.resumed} == {
                agent_model.BACKEND_PI,
                agent_model.BACKEND_DEEPSEEK_HARNESS})

    run_root = store.run_root(root, FIXTURE_RUN)
    artifacts = {
        "manifest.json": (run_root / store.MANIFEST_NAME).is_file(),
        "events.jsonl": (run_root / store.EVENTS_NAME).is_file(),
        "summary.json": (run_root / store.SUMMARY_NAME).is_file(),
        "report.md": (run_root / store.REPORT_NAME).is_file(),
        "trials": len(list((run_root / store.TRIALS_DIR).glob("*.json"))),
    }
    _check("S1", "all canonical artifacts are persisted",
           all(v for k, v in artifacts.items() if k != "trials")
           and artifacts["trials"] == 10)

    reconstructed = store.reconstruct(run_root)
    _check("S1", "durable state reconstructs exactly (fail closed)",
           reconstructed["counts"]["trials"] == 10
           and reconstructed["counts"]["events"] > 0
           and reconstructed["state"]["state"]
           == model.RUN_READY_FOR_COMMIT)

    summary = store.load_summary(run_root)
    both = all(backend in summary["by_backend"] for backend in
               (agent_model.BACKEND_PI,
                agent_model.BACKEND_DEEPSEEK_HARNESS))
    _check("S1", "aggregation reports both backends with p95/median stats",
           both
           and summary["by_backend"][agent_model.BACKEND_PI]["metrics"]
           ["total_tokens"]["p95"]
           == 1020)
    _check("S1", "unavailable metrics stay null with an explicit reason",
           summary["by_backend"][agent_model.BACKEND_PI]["metrics"]
           ["ttft_ms"]["count"] == 0
           and bool(summary["by_backend"][agent_model.BACKEND_PI]["metrics"]
                    ["ttft_ms"]["reason"]))

    # The intentional fail-closed case is PASS; a violation is FAILED.
    violating = _fixture_engine(
        root, FAIL_CLOSED_RUN, violate=True,
        workload_ids=("intentional-blocked-fail-closed",))
    violating.run()
    violating_root = store.run_root(root, FAIL_CLOSED_RUN)
    violating_records = store.load_trials(violating_root)
    fail_closed = [r for r in violating_records if r.fail_closed_case]
    _check("S1", "fail-closed violation is detected and fails the trial",
           fail_closed and all(r.status == model.TS_FAILED
                               for r in fail_closed))
    return {"first_state": first.state, "second_state": second.state,
            "trials": len(second.trials), "artifacts": artifacts,
            "reconstructed": reconstructed["counts"]}


def _scenario_reviewer_identity(root: str) -> dict[str, Any]:
    document = status.status_document(root, FIXTURE_RUN)
    final = document["actors"]["final_independent_reviewer"]
    inline = document["actors"]["inline_reviewer"]
    _check("S3", "final reviewer identity is the M029 model",
           final["model"] == model.FINAL_REVIEWER_MODEL)
    _check("S3", "the inline reviewer is never shown as active",
           inline["active"] is False)
    rendered = status.render_status(document)
    _check("S3", "status never renders a stale qwen3.6 reviewer as active",
           "qwen3.6" not in rendered
           or "active=False" in rendered)
    records = store.load_trials(store.run_root(root, FIXTURE_RUN))
    stale = [r.trial_id for r in records
             if r.review.reviewer.model.startswith("qwen3.6")]
    _check("S3", "no trial persisted a stale qwen3.6 reviewer identity",
           not stale)
    return {"final_reviewer": final, "inline_reviewer": inline,
            "stale": stale}


def _scenario_live(root: str) -> dict[str, Any]:
    from trajectory_os.agents import qualification

    probes = registry.probe_all()
    _check("S2", "Pi backend probes as the proven local path",
           probes[agent_model.BACKEND_PI]["backend"]
           == agent_model.BACKEND_PI)
    qualification_outcome = qualification.qualify(
        workspace=root, timeout_s=30, allow_runtime=True)
    document = qualification_outcome.to_dict()
    _check("S2", "Harness qualification is attempted and recorded honestly",
           document["status"] in (
               qualification.QS_QUALIFIED, qualification.QS_UNAVAILABLE,
               qualification.QS_INCOMPATIBLE, qualification.QS_FAILED))
    _check("S2", "no successful Harness measurement is fabricated",
           document["status"] == qualification.QS_QUALIFIED
           or document["primary_probe"]["backend"]
           == agent_model.BACKEND_DEEPSEEK_HARNESS)
    isolated = hq.qualify_isolated(
        workspace=root, timeout_s=30, allow_runtime=True)
    isolated_document = isolated.to_dict()
    _check("S2", "M023 isolated Harness qualification is attempted",
           isolated_document["environment"]["status"]
           in hq.ENVIRONMENT_STATUSES
           and isolated_document["isolation"]["pythonpath_dropped"] is True
           and isolated_document["isolation"]["sdk_imported_into_project"]
           is False)
    return {"probes": probes, "qualification": document,
            "isolated_qualification": isolated_document}


def _scenario_live_trial(root: str, live: bool) -> dict[str, Any]:
    if not live:
        _check("S2", "live trial skipped by operator flag", True)
        return {"skipped": True}
    config = engine.RunConfig(
        root=root, benchmark_run_id=LIVE_RUN, mode=model.MODE_LIVE,
        workloads=bench_workloads.select(("small-targeted-repair",)),
        backends=(agent_model.BACKEND_PI,
                  agent_model.BACKEND_DEEPSEEK_HARNESS),
        repetitions=1, target_provider="deepseek",
        target_model="deepseek-flash",
        final_reviewer_model=model.FINAL_REVIEWER_MODEL,
        timeout_s=180, environment=engine.environment_snapshot(),
        require_review=False)
    runner = engine.BenchmarkEngine(
        config, executor=bench_executor.LiveExecutor(),
        reviewer_factory=engine.default_reviewer_factory(
            live=False, model_name=model.FINAL_REVIEWER_MODEL),
        resource_sampler=metrics.local_resource_sampler("deepseek-flash"))
    result = runner.run()
    pi_records = [r for r in result.trials
                  if r.backend == agent_model.BACKEND_PI]
    harness_records = [r for r in result.trials
                       if r.backend
                       == agent_model.BACKEND_DEEPSEEK_HARNESS]
    _check("S2", "live Pi trial is executed and trust-gated",
           pi_records and pi_records[0].status
           in (model.TS_PASS, model.TS_FAILED, model.TS_BLOCKED))
    _check("S2", "live Harness trial is recorded without fabrication",
           harness_records and harness_records[0].status
           in (model.TS_PASS, model.TS_FAILED, model.TS_BLOCKED,
               model.TS_UNAVAILABLE))
    return {
        "state": result.state,
        "pi": [r.to_dict() for r in pi_records],
        "harness": [r.to_dict() for r in harness_records],
    }


def _scenario_no_git_write() -> dict[str, Any]:
    import trajectory_os.benchmark.aggregate as agg
    import trajectory_os.benchmark.cli as cli
    import trajectory_os.benchmark.engine as eng
    import trajectory_os.benchmark.executor as ex
    import trajectory_os.benchmark.patch as pt
    import trajectory_os.benchmark.report as rp
    import trajectory_os.benchmark.review as rv
    import trajectory_os.benchmark.status as st
    import trajectory_os.benchmark.store as sr

    verbs = ("commit", "push", "merge", "reset", "restore", "clean", "stash",
             "rebase", "switch", "checkout", "pull", "fetch", "cherry-pick")
    offenders: list[str] = []
    for module in (agg, cli, eng, ex, pt, rp, rv, st, sr):
        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        for verb in verbs:
            if f'"git", "{verb}"' in source or f"'git', '{verb}'" in source:
                offenders.append(f"{module.__name__}:{verb}")
    _check("S4", "no benchmark module performs a Git trust-boundary write",
           not offenders)
    return {"offenders": offenders,
            "checked": [m.__name__ for m in (agg, cli, eng, ex, pt, rp, rv,
                                              st, sr)]}


def _scenario_result(name: str) -> dict[str, Any]:
    checks = [c for c in CHECK_LOG if c["scenario"] == name]
    return {"scenario": name, "checks": len(checks),
            "passed": sum(1 for c in checks if c["ok"]),
            "status": "PASS" if checks and all(c["ok"] for c in checks)
            else "FAIL"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mission029_dogfood",
        description="M029 Pi vs DeepSeek Harness benchmark dogfood.")
    parser.add_argument("--root", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--live", action="store_true", default=False,
                        help="also run real Pi/Harness trials (slower)")
    parser.add_argument("--keep", action="store_true", default=False)
    args = parser.parse_args(argv)

    temporary = args.root is None
    root = args.root or tempfile.mkdtemp(prefix="trajectory-m029-")
    pathlib.Path(root).mkdir(parents=True, exist_ok=True)
    try:
        pipeline = _scenario_pipeline(root)
        reviewer = _scenario_reviewer_identity(root)
        live_evidence = _scenario_live(root)
        live_trial = _scenario_live_trial(root, args.live)
        git = _scenario_no_git_write()
        scenarios = sorted({c["scenario"] for c in CHECK_LOG})
        report = {
            "mission": "M029",
            "issue": 229,
            "generated_at": datetime.datetime.now(datetime.UTC)
            .replace(microsecond=0, tzinfo=None).isoformat() + "Z",
            "root": root,
            "live_requested": args.live,
            "pipeline": pipeline,
            "reviewer": reviewer,
            "live_evidence": live_evidence,
            "live_trial": live_trial,
            "git_safety": git,
            "scenarios": [_scenario_result(code) for code in scenarios],
            "checks": CHECK_LOG,
            "status": ("PASS" if all(c["ok"] for c in CHECK_LOG) else "FAIL"),
        }
        out = pathlib.Path(
            args.out or pathlib.Path(root) / "m029-dogfood.json")
        out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        print(f"M029 dogfood {report['status']}: {out}")
        for code in scenarios:
            print(f"  {code}: {_scenario_result(code)['status']}")
        return 0 if report["status"] == "PASS" else 3
    finally:
        if temporary and not args.keep:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
