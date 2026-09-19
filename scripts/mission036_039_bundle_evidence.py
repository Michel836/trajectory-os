#!/usr/bin/env python3
"""M036–M039 — consolidate the human-gated release bundle evidence.

Reads the authoritative dogfood and final-review artifacts, runs the
deterministic release acceptance matrix, performs the repository trust-
boundary scan, and writes one machine-readable bundle evidence document plus
a compact human summary.

The bundle evidence is *derived* evidence: it never invents a fact; it points
at the authoritative artifact paths and records the exact baseline, mission
identity, patch identity, commit/PR/CI/merge chain and terminal release
closure.
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

from trajectory_os.release import acceptance as release_acceptance

BASELINE = "f98113ec5264e67289e75d098d26c77aff10ead7"
MARKER = "M036_M039_HUMAN_GATED_RELEASE_BUNDLE_COMPLETE"

NON_RELEASE_MODULES = (
    "src/trajectory_os/agents",
    "src/trajectory_os/assembly",
    "src/trajectory_os/benchmark",
    "src/trajectory_os/missions",
    "src/trajectory_os/observability",
    "src/trajectory_os/runtime_control.py",
)
GIT_VERBS = ("commit", "push", "merge", "reset", "restore", "clean", "stash",
             "rebase", "switch", "checkout")


def trust_boundary_scan() -> dict[str, Any]:
    """Prove non-release modules never perform a Git trust-boundary write."""
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
    runtime_control = (REPO / "src/trajectory_os/runtime_control.py").read_text(
        encoding="utf-8")
    return {
        "offenders": offenders,
        "clean": not offenders,
        "scanned_files": len(files),
        "runtime_control_subprocess_free": "subprocess" not in runtime_control,
    }


def load(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build(docs: pathlib.Path, runtime: pathlib.Path,
          *, baseline: str = BASELINE) -> dict[str, Any]:
    dogfood = load(docs / "m036-m039-dogfood-evidence.json")
    final_review = load(docs / "m036-m039-final-review.json")

    report = release_acceptance.run_acceptance(runtime / "acceptance")
    acceptance_json = docs / "m036-m039-acceptance-report.json"
    acceptance_txt = docs / "m036-m039-acceptance-report.txt"
    acceptance_json.write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    acceptance_txt.write_text(report.render() + "\n", encoding="utf-8")

    return {
        "program": "M036-M039",
        "issue": 238,
        "marker": MARKER,
        "baseline": baseline,
        "generated_at": (datetime.datetime.now(datetime.UTC)
                         .replace(microsecond=0, tzinfo=None).isoformat()
                         + "Z"),
        "mission_identity": {
            "mission_id": dogfood["mission_id"],
            "run_id": dogfood["mission_id"],
            "objective": "release dogfood: fix add(a, b)",
        },
        "repository": {
            "branch": dogfood["remote_branch"],
            "base_branch": dogfood["base_branch"],
            "handoff_baseline_head": dogfood["handoff"]["baseline_head"],
            "handoff_current_head": dogfood["handoff"]["current_head"],
            "authorized_commit_sha": dogfood["commit"]["commit_sha"],
            "remote_branch_head": dogfood["remote_branch_head"],
            "remote_push_verified": dogfood["remote_push_verified"],
        },
        "patch_identity": {
            "reviewed_patch_sha256": dogfood["handoff"]["patch_sha256"],
            "closure_reviewed_patch_sha256":
                dogfood["closure"]["reviewed_patch_sha256"],
            "review_gate": dogfood["review_gate"],
        },
        "commit_handoff": dogfood["handoff"],
        "commit": dogfood["commit"],
        "pr_binding": dogfood["pr"],
        "exact_head_ci": dogfood["ci"],
        "go_merge_gate": {
            "gate": "GO_MERGE",
            "merge_method": dogfood["merge"]["method"],
            "verified": dogfood["merge"]["verified"],
        },
        "merge_result": dogfood["merge"],
        "release_closure": dogfood["closure"],
        "release_state": dogfood["release_state"],
        "release_events": dogfood["release_events"],
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
        "github_read_only_dogfood": dogfood.get("github_read_only_probe"),
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
            "dogfood": str(docs / "m036-m039-dogfood-evidence.json"),
            "acceptance_json": str(acceptance_json),
            "acceptance_txt": str(acceptance_txt),
            "final_review": str(docs / "m036-m039-final-review.json"),
        },
    }


def render(evidence: dict[str, Any]) -> str:
    repo = evidence["repository"]
    ci = evidence["exact_head_ci"]
    merge = evidence["merge_result"]
    closure = evidence["release_closure"]
    acceptance = evidence["acceptance"]
    lines = [
        "# M036–M039 Human-Gated Release Bundle — Evidence",
        "",
        f"Baseline: `{evidence['baseline']}`  ",
        f"Marker: `{evidence['marker']}`",
        "",
        "## Mission identity",
        f"- mission / run: `{evidence['mission_identity']['mission_id']}`",
        f"- objective: {evidence['mission_identity']['objective']}",
        "",
        "## Git trust boundary (operator-authorized)",
        f"- branch: `{repo['branch']}` -> `{repo['base_branch']}`",
        f"- baseline HEAD: `{repo['handoff_baseline_head']}`",
        f"- authorized commit: `{repo['authorized_commit_sha']}`",
        f"- remote branch head: `{repo['remote_branch_head']}` "
        f"(push verified: {repo['remote_push_verified']})",
        "",
        "## Semantic patch identity",
        f"- reviewed patch: "
        f"`{evidence['patch_identity']['reviewed_patch_sha256']}`",
        f"- closure reviewed patch: "
        f"`{evidence['patch_identity']['closure_reviewed_patch_sha256']}`",
        "",
        "## PR + exact-head CI",
        f"- PR #{evidence['pr_binding']['number']} head "
        f"`{evidence['pr_binding']['head_sha']}`",
        f"- CI: {ci['state']} on `{ci['head_sha']}` (green: {ci['green']})",
        "",
        "## GO MERGE + merge result",
        f"- method: {merge['method']} (verified: {merge['verified']})",
        f"- merge SHA: `{merge['merge_sha']}` -> {merge['target_branch']}",
        f"- target branch verified: {closure['target_branch_verified']}",
        "",
        "## Release closure",
        f"- status: **{closure['status']}**",
        f"- commit `{closure['commit_sha']}`, PR #{closure['pr_number']}, "
        f"CI {closure['ci_status']}, merge `{closure['merge_sha']}`",
        f"- issue closure: {closure['issue_closure']}",
        "",
        "## Acceptance",
        f"- status: **{acceptance['status']}** "
        f"({acceptance['summary']['passed_cases']}/"
        f"{acceptance['summary']['cases']} cases, "
        f"{acceptance['summary']['passed_checks']}/"
        f"{acceptance['summary']['checks']} checks)",
        "",
        "## Trust boundary",
        f"- non-release Git-write offenders: "
        f"{evidence['trust_boundary']['offenders']}",
        f"- runtime_control subprocess-free: "
        f"{evidence['trust_boundary']['runtime_control_subprocess_free']}",
        "",
        "## Real GitHub read-only dogfood",
        f"- {evidence['github_read_only_dogfood']}",
        "",
        "## Final independent review",
        f"- reviewer: "
        f"`{evidence['final_independent_review']['reviewer_model']}`",
        f"- outcome: **{evidence['final_independent_review']['outcome']}** "
        f"({evidence['final_independent_review']['reason']})",
        f"- reviewed patch still current: "
        f"{evidence['final_independent_review']['reviewed_patch_still_current']}",
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
    parser.add_argument("--docs", default="docs/missions/m036-m039")
    parser.add_argument("--runtime", default=".artifacts/m036-m039/bundle")
    parser.add_argument("--out", default=None)
    parser.add_argument("--markdown", default=None)
    parser.add_argument("--baseline", default=BASELINE)
    args = parser.parse_args(argv)
    docs = pathlib.Path(args.docs)
    runtime = pathlib.Path(args.runtime)
    runtime.mkdir(parents=True, exist_ok=True)
    evidence = build(docs, runtime, baseline=args.baseline)
    out = pathlib.Path(args.out or docs / "m036-m039-bundle-evidence.json")
    out.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    markdown = pathlib.Path(args.markdown
                            or docs / "m036-m039-bundle-evidence.md")
    markdown.write_text(render(evidence) + "\n", encoding="utf-8")
    print(f"bundle evidence written: {out}")
    print(f"bundle summary written:  {markdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
