#!/usr/bin/env python3
"""M032–M035 — consolidate the durable operator-bundle evidence.

Reads the authoritative per-milestone evidence artifacts produced by the
M032/M033/M035 dogfoods, adds a deterministic mission-level control-action
proof and a repository trust-boundary scan, and writes one machine-readable
bundle evidence document plus a compact human summary.

The bundle evidence is *derived* evidence: it never invents a fact, it points
at the authoritative artifact paths, and it records the exact baseline,
mission ids, provider/backend/model, patch identities, review outcomes and
terminal readiness.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from trajectory_os.assembly import control, model
from trajectory_os.assembly import orchestrator as assembly_run
from trajectory_os.benchmark.executor import FixtureExecutor
from trajectory_os.observability import run as obs_run

BASELINE = "39fc9b0029142abdf6d8e42759bd4f4679a3ac96"

ASSEMBLY_SOURCES = (
    "src/trajectory_os/assembly/__init__.py",
    "src/trajectory_os/assembly/baseline.py",
    "src/trajectory_os/assembly/cli.py",
    "src/trajectory_os/assembly/closure.py",
    "src/trajectory_os/assembly/control.py",
    "src/trajectory_os/assembly/model.py",
    "src/trajectory_os/assembly/orchestrator.py",
    "src/trajectory_os/assembly/recovery.py",
    "src/trajectory_os/assembly/store.py",
)

GIT_VERBS = ("commit", "push", "merge", "reset", "restore", "clean", "stash",
             "rebase", "switch", "checkout", "pull", "fetch", "cherry-pick")


def control_proof(root: pathlib.Path) -> dict[str, Any]:
    """Deterministically prove mission-scoped control on a real mission root."""
    import tempfile

    root = pathlib.Path(tempfile.mkdtemp(prefix="m034-control-",
                                         dir=str(root)))
    workspace = root / "m034-control-proof" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    pass_review = (
        "VERDICT: PASS\nBLOCKERS:\n- none\nMAJORS:\n- none\nMINORS:\n- none\n"
        "FINAL RECOMMENDATION: GO COMMIT\n")
    orchestrator = assembly_run.MissionOrchestrator(
        str(root), executor=FixtureExecutor(interrupt_once=False),
        reviewer_factory=obs_run.scripted_reviewer_factory([pass_review]))
    orchestrator.start(assembly_run.MissionRequest(
        objective="control-action proof (interrupted before execution)",
        workspace=str(workspace), mission_id="m034-control-proof",
        workload_id="small-targeted-repair"),
        interrupt_after_phase=model.MP_PLAN)
    stop = control.request_stop(str(root), "m034-control-proof",
                                reason="bundle control proof")
    cleared = control.clear_stop_request(str(root), "m034-control-proof")
    cancelled = control.cancel(str(root), "m034-control-proof",
                               reason="bundle control proof")
    unknown_rejected = False
    try:
        control.cancel(str(root), "does-not-exist")
    except model.AssemblyError as exc:
        unknown_rejected = exc.code == model.R_MISSION_MISSING
    status = {
        "request_stop": stop.result,
        "clear_stop": cleared.result,
        "cancel": cancelled.result,
        "cancel_readiness": cancelled.readiness,
        "unknown_mission_fails_closed": unknown_rejected,
        "control_log": control.control_log(str(root), "m034-control-proof"),
        "stop_latch_after_cancel": control.stop_requested(
            str(root), "m034-control-proof"),
    }
    return status


def trust_boundary_scan() -> dict[str, Any]:
    offenders: list[str] = []
    for relative in ASSEMBLY_SOURCES:
        path = REPO / relative
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        for verb in GIT_VERBS:
            if f'"git", "{verb}"' in source or f"'git', '{verb}'" in source:
                offenders.append(f"{relative}:{verb}")
        if "subprocess" in source and relative.endswith("control.py"):
            offenders.append(f"{relative}:subprocess")
    return {"offenders": offenders, "clean": not offenders}


def load(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build(docs: pathlib.Path, runtime: pathlib.Path,
          *, baseline: str = BASELINE) -> dict[str, Any]:
    m032 = load(docs / "m032-live-mission-evidence.json")
    m032_blocked = load(docs / "m032-live-mission-blocked-evidence.json")
    m033 = load(docs / "m033-interruption-evidence.json")
    m035 = load(docs / "m035-acceptance-report.json")
    final_review = load(docs / "m032-m035-final-review.json")
    control_result = control_proof(runtime)
    trust = trust_boundary_scan()
    return {
        "program": "M032-M035",
        "issue": 236,
        "marker": "M032_M035_REAL_OPERATOR_BUNDLE_COMPLETE",
        "baseline": baseline,
        "generated_at": (datetime.datetime.now(datetime.UTC)
                         .replace(microsecond=0, tzinfo=None).isoformat()
                         + "Z"),
        "m032_real_mission": {
            "selected_objective": m032["selected_objective"],
            "why_chosen": m032["why_chosen"],
            "mission_id": m032["mission_id"],
            "run_id": m032["run_id"],
            "backend": m032["backend"],
            "provider": m032["provider"],
            "model": m032["model"],
            "reviewer_model": m032["reviewer_model"],
            "mode": m032["mode"],
            "readiness": m032["readiness"],
            "lifecycle": m032["lifecycle"],
            "terminal_reason": m032["terminal_reason"],
            "terminal_reason_code": m032["terminal_reason_code"],
            "current_patch": m032["current_patch"],
            "reviewed_patch": m032["reviewed_patch"],
            "attempts": m032["attempts"],
            "repairs": m032["repairs"],
            "phase_transitions": m032["phase_transitions"],
            "validation_results": m032["validation_results"],
            "review_results": m032["review_results"],
            "telemetry_summary": m032["telemetry_summary"],
            "objective_implemented_in_workspace": m032["objective_implemented"],
            "artifacts": m032["artifacts"],
        },
        "m033_interruption_resume": m033,
        "m032_blocked_attempt": {
            "mission_id": m032_blocked["mission_id"],
            "readiness": m032_blocked["readiness"],
            "terminal_reason": m032_blocked["terminal_reason"],
            "terminal_reason_code": m032_blocked["terminal_reason_code"],
            "attempts": m032_blocked["attempts"],
            "repairs": m032_blocked["repairs"],
            "review_outcomes": [
                {"attempt": entry["attempt"], "outcome": entry["outcome"],
                 "result": entry["result"], "reason": entry["reason"],
                 "patch": entry["patch"]}
                for entry in m032_blocked["review_results"]],
            "note": ("Earlier real run: the independent reviewer produced a "
                     "protocol-invalid PASS-with-blocking-findings verdict "
                     "on a correct, validation-passing patch; the mission "
                     "failed closed to BLOCKED after two bounded repairs."),
        },
        "m034_operator_control": control_result,
        "m035_acceptance": {
            "status": m035["status"],
            "summary": m035["summary"],
            "cases": [
                {"case": case["case"], "title": case["title"],
                 "ok": case["ok"],
                 "checks": [check["description"] for check in case["checks"]]}
                for case in m035["cases"]
            ],
        },
        "trust_boundary": trust,
        "final_independent_review": {
            "reviewer_model": final_review["reviewer_model"],
            "outcome": final_review["assessment"]["outcome"],
            "reason": final_review["assessment"]["reason"],
            "reviewed_patch_sha256": final_review[
                "reviewed_patch_sha256"],
            "reviewed_patch_still_current": final_review[
                "reviewed_patch_still_current"],
            "blocking_count": final_review["assessment"]["blocking_count"],
            "minors": final_review["assessment"]["minors"],
            "generated_at": final_review["generated_at"],
        },
        "authoritative_artifacts": {
            "m032_live_mission": str(
                docs / "m032-live-mission-evidence.json"),
            "m032_blocked_attempt": str(
                docs / "m032-live-mission-blocked-evidence.json"),
            "m033_interruption": str(
                docs / "m033-interruption-evidence.json"),
            "m035_acceptance_json": str(
                docs / "m035-acceptance-report.json"),
            "m035_acceptance_txt": str(
                docs / "m035-acceptance-report.txt"),
            "final_review": str(
                docs / "m032-m035-final-review.json"),
        },
    }


def render(evidence: dict[str, Any]) -> str:
    m032 = evidence["m032_real_mission"]
    m033 = evidence["m033_interruption_resume"]
    m035 = evidence["m035_acceptance"]
    lines = [
        "# M032–M035 Real Operator Bundle — Evidence",
        "",
        f"Baseline: `{evidence['baseline']}`  ",
        f"Marker: `{evidence['marker']}`",
        "",
        "## M032 — real non-fixture mission",
        f"- objective: {m032['selected_objective']}",
        f"- mission_id / run_id: `{m032['mission_id']}` / `{m032['run_id']}`",
        f"- backend: `{m032['backend']}` / `{m032['provider']}` / "
        f"`{m032['model']}`; reviewer `{m032['reviewer_model']}`",
        f"- terminal readiness: **{m032['readiness']}** "
        f"({m032['lifecycle']}) — {m032['terminal_reason']}",
        f"- current patch: `{m032['current_patch']}`",
        f"- reviewed patch: `{m032['reviewed_patch']}`",
        f"- attempts / repairs: {m032['attempts']} / {m032['repairs']}",
        f"- phase transitions: {', '.join(m032['phase_transitions'])}",
        f"- earlier fail-closed attempt: "
        f"`{evidence['m032_blocked_attempt']['mission_id']}` -> "
        f"{evidence['m032_blocked_attempt']['readiness']} "
        f"(reviewer protocol invalid after "
        f"{evidence['m032_blocked_attempt']['repairs']} repairs)",
        "",
        "## M033 — real interruption / recovery",
        f"- status: **{m033['status']}** "
        f"(SIGKILL and SIGTERM scenarios)",
    ]
    for label, result in m033["results"].items():
        lines.append(
            f"- {label}: identity preserved="
            f"{result['same_mission_id']}, evidence preserved="
            f"{result['prior_evidence_preserved']}, readiness="
            f"{result['readiness_after_resume']}, idempotent repeated resume="
            f"{result['repeated_resume_byte_idempotent']}")
    lines += [
        "",
        "## M034 — operator control",
        f"- request-stop={evidence['m034_operator_control']['request_stop']}, "
        f"clear-stop={evidence['m034_operator_control']['clear_stop']}, "
        f"cancel={evidence['m034_operator_control']['cancel']} "
        f"({evidence['m034_operator_control']['cancel_readiness']})",
        f"- unknown mission fails closed: "
        f"{evidence['m034_operator_control']['unknown_mission_fails_closed']}",
        "",
        "## M035 — production acceptance",
        f"- status: **{m035['status']}** "
        f"({m035['summary']['passed_cases']}/{m035['summary']['cases']} cases, "
        f"{m035['summary']['passed_checks']}/{m035['summary']['checks']} checks)",
        "",
        "## Trust boundary",
        f"- Git trust-boundary offenders: {evidence['trust_boundary']['offenders']}",
        "",
        "## Final independent review",
        f"- reviewer: `{evidence['final_independent_review']['reviewer_model']}`",
        f"- outcome: **{evidence['final_independent_review']['outcome']}** "
        f"({evidence['final_independent_review']['reason']})",
        f"- reviewed patch: "
        f"`{evidence['final_independent_review']['reviewed_patch_sha256']}` "
        f"(still current: "
        f"{evidence['final_independent_review']['reviewed_patch_still_current']})",
        f"- blocking findings: "
        f"{evidence['final_independent_review']['blocking_count']}",
        "",
        "## Authoritative artifacts",
    ]
    for name, path in evidence["authoritative_artifacts"].items():
        lines.append(f"- {name}: `{path}`")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--docs", default="docs/missions/m032-m035")
    parser.add_argument("--runtime", default=".artifacts/m032-m035/bundle")
    parser.add_argument("--out", default=None)
    parser.add_argument("--markdown", default=None)
    parser.add_argument("--baseline", default=BASELINE,
                        help="recorded baseline commit for the bundle")
    args = parser.parse_args(argv)
    docs = pathlib.Path(args.docs)
    runtime = pathlib.Path(args.runtime)
    runtime.mkdir(parents=True, exist_ok=True)
    evidence = build(docs, runtime, baseline=args.baseline)
    out = pathlib.Path(args.out or docs / "m032-m035-bundle-evidence.json")
    out.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    markdown = pathlib.Path(args.markdown
                            or docs / "m032-m035-bundle-evidence.md")
    markdown.write_text(render(evidence) + "\n", encoding="utf-8")
    print(f"bundle evidence written: {out}")
    print(f"bundle summary written:  {markdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
