#!/usr/bin/env python3
"""M040–M047 — consolidate the self-hosting operator platform evidence.

Runs the deterministic M040–M047 acceptance matrix, reads the authoritative
dogfood and final-review artifacts, performs the repository trust-boundary
scan and writes one machine-readable bundle evidence document plus a compact
human summary. It never invents a fact; it points at the authoritative
artifact paths and records the exact platform identities.
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

from trajectory_os.operator import acceptance as operator_acceptance
from trajectory_os.operator import model as operator_model

BASELINE = "d39f42aade4c28340f6e2814b99ea82ec7af9451"
MARKER = "M040_M047_SELF_HOSTING_OPERATOR_PLATFORM_COMPLETE"

NON_RELEASE_MODULES = (
    "src/trajectory_os/agents",
    "src/trajectory_os/assembly",
    "src/trajectory_os/benchmark",
    "src/trajectory_os/missions",
    "src/trajectory_os/observability",
    "src/trajectory_os/operator",
    "src/trajectory_os/runtime_control.py",
)
GIT_VERBS = ("commit", "push", "merge", "reset", "restore", "clean", "stash",
             "rebase", "switch", "checkout")


def trust_boundary_scan() -> dict[str, Any]:
    offenders: list[str] = []
    files: list[pathlib.Path] = []
    for relative in NON_RELEASE_MODULES:
        target = REPO / relative
        if target.is_file():
            files.append(target)
        elif target.is_dir():
            files.extend(sorted(target.rglob("*.py")))
    for path in files:
        source = path.read_text(encoding="utf-8")
        for verb in GIT_VERBS:
            if f'"git", "{verb}"' in source:
                offenders.append(f"{path.relative_to(REPO)}:{verb}")
    runtime_control = (
        REPO / "src/trajectory_os/runtime_control.py"
    ).read_text(encoding="utf-8")
    return {
        "offenders": offenders,
        "clean": not offenders,
        "scanned_files": len(files),
        "runtime_control_subprocess_free": "subprocess" not in runtime_control,
    }


def load(path: pathlib.Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def build(docs: pathlib.Path, runtime: pathlib.Path,
          *, baseline: str = BASELINE) -> dict[str, Any]:
    dogfood = load(docs / "m040-m047-dogfood-evidence.json")
    final_review = load(docs / "m040-m047-final-review.json")
    report = operator_acceptance.run_acceptance(runtime / "acceptance")
    acceptance_json = docs / "m040-m047-acceptance-report.json"
    acceptance_txt = docs / "m040-m047-acceptance-report.txt"
    acceptance_json.write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    acceptance_txt.write_text(report.render() + "\n", encoding="utf-8")

    final_block: dict[str, Any] = {"present": final_review is not None}
    if final_review is not None:
        final_block.update({
            "reviewer_model": final_review["reviewer_model"],
            "outcome": final_review["assessment"]["outcome"],
            "reason": final_review["assessment"]["reason"],
            "reviewed_patch_sha256": final_review["reviewed_patch_sha256"],
            "reviewed_patch_still_current": final_review[
                "reviewed_patch_still_current"],
            "blocking_count": final_review["assessment"]["blocking_count"],
            "minors": final_review["assessment"]["minors"],
            "generated_at": final_review["generated_at"],
        })
    return {
        "program": "M040-M047",
        "issue": 240,
        "marker": MARKER,
        "baseline": baseline,
        "operator_version": operator_model.OPERATOR_VERSION,
        "generated_at": (datetime.datetime.now(datetime.UTC)
                         .replace(microsecond=0, tzinfo=None).isoformat()
                         + "Z"),
        "dogfood": dogfood,
        "acceptance": {
            "status": report.status,
            "summary": report.to_dict()["summary"],
            "cases": [
                {"case": case.case, "title": case.title, "ok": case.ok,
                 "checks": [check.description for check in case.checks]}
                for case in report.cases
            ],
        },
        "trust_boundary": trust_boundary_scan(),
        "final_independent_review": final_block,
        "authoritative_artifacts": {
            "dogfood": str(docs / "m040-m047-dogfood-evidence.json"),
            "acceptance_json": str(acceptance_json),
            "acceptance_txt": str(acceptance_txt),
            "final_review": str(docs / "m040-m047-final-review.json"),
        },
    }


def render(evidence: dict[str, Any]) -> str:
    acceptance = evidence["acceptance"]
    dogfood = evidence["dogfood"] or {}
    final = evidence["final_independent_review"]
    lines = [
        "# M040–M047 Self-Hosting Operator Platform — Evidence",
        "",
        f"Baseline: `{evidence['baseline']}`  ",
        f"Marker: `{evidence['marker']}`",
        "",
        "## Acceptance",
        f"- status: **{acceptance['status']}** "
        f"({acceptance['summary']['passed_cases']}/"
        f"{acceptance['summary']['cases']} cases, "
        f"{acceptance['summary']['passed_checks']}/"
        f"{acceptance['summary']['checks']} checks)",
        "",
        "## Self-hosting dogfood",
        f"- status: {dogfood.get('status')}",
        f"- fixture proof ok: {(dogfood.get('fixture_proof') or {}).get('ok')}",
        f"- live ready-for-commit: "
        f"{(dogfood.get('live_dogfood') or {}).get('ready_for_commit')}",
        f"- crossed human gate: "
        f"{(dogfood.get('guardrails') or {}).get('crossed_human_gate')}",
        "",
        "## Trust boundary",
        f"- non-release Git-write offenders: "
        f"{evidence['trust_boundary']['offenders']}",
        f"- runtime_control subprocess-free: "
        f"{evidence['trust_boundary']['runtime_control_subprocess_free']}",
        "",
        "## Final independent review",
        f"- present: {final.get('present')}",
        f"- reviewer: `{final.get('reviewer_model')}`",
        f"- outcome: **{final.get('outcome')}** ({final.get('reason')})",
        f"- reviewed patch still current: "
        f"{final.get('reviewed_patch_still_current')}",
        f"- blocking findings: {final.get('blocking_count')}",
        "",
        "## Authoritative artifacts",
    ]
    for name, path in evidence["authoritative_artifacts"].items():
        lines.append(f"- {name}: `{path}`")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--docs", default="docs/missions/m040-m047")
    parser.add_argument("--runtime", default=".artifacts/m040-m047/bundle")
    parser.add_argument("--out", default=None)
    parser.add_argument("--markdown", default=None)
    parser.add_argument("--baseline", default=BASELINE)
    args = parser.parse_args(argv)
    docs = pathlib.Path(args.docs)
    runtime = pathlib.Path(args.runtime)
    docs.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    evidence = build(docs, runtime, baseline=args.baseline)
    out = pathlib.Path(args.out or docs / "m040-m047-bundle-evidence.json")
    out.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    markdown = pathlib.Path(args.markdown or out.with_suffix(".md"))
    markdown.write_text(render(evidence) + "\n", encoding="utf-8")
    print(f"bundle evidence written: {out}")
    print(f"bundle summary written:  {markdown}")
    return 0 if evidence["acceptance"]["status"] == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
