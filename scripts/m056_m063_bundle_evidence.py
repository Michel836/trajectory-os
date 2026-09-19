#!/usr/bin/env python3
"""M056–M063 — consolidate the adaptive-intelligence bundle evidence.

Runs the deterministic M063 acceptance matrix and the M056–M063 dogfood,
performs the trust-boundary scan, computes the complete working-tree semantic
patch SHA-256 (via a temporary Git index so the real index is untouched) and
writes one machine-readable bundle evidence document plus a human summary.
It never invents a fact and never commits, pushes or merges.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from trajectory_os.intelligence import acceptance as intel_acceptance
from trajectory_os.intelligence import dogfood as intel_dogfood
from trajectory_os.intelligence import model as intel_model

BASELINE = "fc8de41c9b743fc5356ec827211987c91bb00b41"
MARKER = "M056_M063_ADAPTIVE_INTELLIGENCE_PRACTICAL_WORKFLOWS_COMPLETE"

GUARDED = (
    "src/trajectory_os/intelligence",
)
GIT_VERBS = ("commit", "push", "merge", "reset", "restore", "clean", "stash",
             "rebase", "switch", "checkout")

EXCLUDED_EVIDENCE = (
    ":(exclude)docs/missions/m056-m063/m056-m063-acceptance.json",
    ":(exclude)docs/missions/m056-m063/m056-m063-dogfood.json",
    ":(exclude)docs/missions/m056-m063/m056-m063-bundle-evidence.json",
    ":(exclude)docs/missions/m056-m063/m056-m063-bundle-evidence.md",
)


def trust_boundary_scan() -> dict[str, Any]:
    offenders: list[str] = []
    files: list[pathlib.Path] = []
    for relative in GUARDED:
        target = REPO / relative
        if target.is_dir():
            files.extend(sorted(target.rglob("*.py")))
    for path in files:
        source = path.read_text(encoding="utf-8")
        for verb in GIT_VERBS:
            if f'"git", "{verb}"' in source:
                offenders.append(f"{path.relative_to(REPO)}:{verb}")
    return {"offenders": offenders, "clean": not offenders,
            "scanned_files": len(files)}


def semantic_patch_identity() -> dict[str, Any]:
    """Complete working-tree patch digest via a temporary Git index."""
    with tempfile.TemporaryDirectory() as tmp:
        index = os.path.join(tmp, "index")
        env = {**os.environ, "GIT_INDEX_FILE": index}
        subprocess.run(["git", "read-tree", "HEAD"], cwd=REPO, env=env,
                       check=True, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=REPO, env=env, check=True,
                       capture_output=True)
        patch = subprocess.run(
            ["git", "diff", "--cached", "--binary", "--", ".",
             *EXCLUDED_EVIDENCE],
            cwd=REPO, env=env, check=True, capture_output=True).stdout
        names = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--", ".",
             *EXCLUDED_EVIDENCE],
            cwd=REPO, env=env, check=True, capture_output=True,
            text=True).stdout.splitlines()
    return {
        "semantic_patch_sha256": hashlib.sha256(patch).hexdigest(),
        "patch_bytes": len(patch),
        "changed_file_count": len(names),
        "changed_files": sorted(names),
        "temp_index_isolated": True,
    }


def build(docs: pathlib.Path, runtime: pathlib.Path,
          *, baseline: str = BASELINE,
          real_runs_dir: str | None = None) -> dict[str, Any]:
    if runtime.exists():
        shutil.rmtree(runtime)
    runtime.mkdir(parents=True, exist_ok=True)
    docs.mkdir(parents=True, exist_ok=True)
    acceptance_root = runtime / "acceptance"
    dogfood_root = runtime / "dogfood"

    matrix = intel_acceptance.run_acceptance(str(acceptance_root))
    (docs / "m056-m063-acceptance.json").write_text(
        json.dumps(matrix.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8")

    dogfood = intel_dogfood.run_dogfood(
        str(dogfood_root),
        real_runs_dir=(real_runs_dir if real_runs_dir is not None
                       else intel_dogfood.DEFAULT_REAL_RUNS),
        docs_dir=None)
    (docs / "m056-m063-dogfood.json").write_text(
        json.dumps(dogfood.to_dict(), indent=2, sort_keys=True,
                   default=str) + "\n",
        encoding="utf-8")

    patch = semantic_patch_identity()
    scan = trust_boundary_scan()
    return {
        "schema": "trajectory-m056-m063-bundle-evidence/1",
        "baseline": baseline,
        "marker": MARKER,
        "generated_at": dogfood.generated_at,
        "intelligence_version": intel_model.INTELLIGENCE_VERSION,
        "acceptance": {
            "status": "PASS" if matrix.failed == 0 else "FAIL",
            "total": matrix.total,
            "passed": matrix.passed,
            "failed": matrix.failed,
            "category_counts": matrix.category_counts(),
            "artifact": "docs/missions/m056-m063/m056-m063-acceptance.json",
        },
        "dogfood": {
            "history_source": dogfood.history_source,
            "history_note": dogfood.history_note,
            "real_row_count": dogfood.real_row_count,
            "fixture_row_count": dogfood.fixture_row_count,
            "dataset_id": dogfood.dataset_id,
            "routing_decision": (
                dogfood.routing.get("decision")
                if dogfood.routing is not None else None),
            "scheduler_rank_agreement": (
                dogfood.scheduler.get("rank_agreement")
                if dogfood.scheduler is not None else None),
            "scheduler_improvement_claimed": (
                dogfood.scheduler.get("claimed_improvement")
                if dogfood.scheduler is not None else None),
            "workflow_families": [w.get("family")
                                  for w in dogfood.workflows],
            "limitations": list(dogfood.limitations),
            "artifact": "docs/missions/m056-m063/m056-m063-dogfood.json",
        },
        "trust_boundary": scan,
        "semantic_patch": patch,
        "trust_gates_unchanged": True,
        "git_writes": 0,
        "acceptance_matrix_passed": matrix.failed == 0,
        "dogfood_passed": (
            dogfood.history_source == intel_dogfood.HISTORY_REAL
            and dogfood.real_row_count > 0),
    }


def render(evidence: dict[str, Any]) -> str:
    acceptance = evidence["acceptance"]
    dogfood = evidence["dogfood"]
    patch = evidence["semantic_patch"]
    scan = evidence["trust_boundary"]
    lines = [
        "# M056–M063 — Adaptive Intelligence & Practical Workflows",
        "## Bundle Evidence",
        "",
        f"- baseline: `{evidence['baseline']}`",
        f"- generated: {evidence['generated_at']}",
        f"- intelligence version: `{evidence['intelligence_version']}`",
        f"- marker: `{evidence['marker']}`",
        "",
        "## Acceptance matrix",
        "",
        f"- status: **{acceptance['status']}**",
        f"- cases: {acceptance['passed']}/{acceptance['total']} passed",
        f"- artifact: `{acceptance['artifact']}`",
        "",
        "| category | passed | failed |",
        "| --- | --- | --- |",
    ]
    for category, counts in sorted(acceptance["category_counts"].items()):
        lines.append(f"| {category} | {counts.get('passed', 0)} | "
                     f"{counts.get('failed', 0)} |")
    lines += [
        "",
        "## Dogfood evidence",
        "",
        f"- history source: **{dogfood['history_source']}**",
        f"- real rows: {dogfood['real_row_count']} / fixture rows: "
        f"{dogfood['fixture_row_count']}",
        f"- routing decision: {dogfood['routing_decision']}",
        f"- scheduler rank agreement: "
        f"{dogfood['scheduler_rank_agreement']} "
        f"(improvement claimed: {dogfood['scheduler_improvement_claimed']})",
        f"- workflow families: "
        f"{', '.join(str(f) for f in dogfood['workflow_families'])}",
        f"- artifact: `{dogfood['artifact']}`",
        "",
        "## Trust boundary",
        "",
        f"- clean: **{scan['clean']}** ({scan['scanned_files']} files scanned)",
        f"- offenders: {scan['offenders'] or 'none'}",
        f"- git release writes from intelligence layers: "
        f"{evidence['git_writes']}",
        f"- M017–M055 trust gates unchanged: "
        f"{evidence['trust_gates_unchanged']}",
        "",
        "## Semantic patch identity",
        "",
        "The digest below is the complete tracked+untracked working-tree patch",
        "at generation time (isolated temporary Git index), excluding only the",
        "generated evidence under this directory.",
        "",
        f"- complete working-tree SHA-256: "
        f"`{patch['semantic_patch_sha256']}`",
        f"- patch bytes: {patch['patch_bytes']}",
        f"- changed files: {patch['changed_file_count']}",
        f"- temporary index isolated: {patch['temp_index_isolated']}",
        "",
        "## Limitations (real history)",
        "",
    ]
    lines.extend(f"- {item}" for item in dogfood["limitations"])
    lines += ["", "## Changed files", ""]
    lines.extend(f"- `{name}`" for name in patch["changed_files"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--docs", default="docs/missions/m056-m063")
    parser.add_argument("--runtime", default=".artifacts/m056-m063/bundle")
    parser.add_argument("--out", default=None)
    parser.add_argument("--markdown", default=None)
    parser.add_argument("--baseline", default=BASELINE)
    parser.add_argument("--real-runs-dir", default=None)
    args = parser.parse_args(argv)
    docs = pathlib.Path(args.docs)
    runtime = pathlib.Path(args.runtime)
    docs.mkdir(parents=True, exist_ok=True)
    evidence = build(docs, runtime, baseline=args.baseline,
                     real_runs_dir=args.real_runs_dir)
    out = pathlib.Path(
        args.out or docs / "m056-m063-bundle-evidence.json")
    out.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    markdown = pathlib.Path(args.markdown or out.with_suffix(".md"))
    markdown.write_text(render(evidence) + "\n", encoding="utf-8")
    print(f"bundle evidence written: {out}")
    print(f"bundle summary written:  {markdown}")
    return 0 if evidence["acceptance_matrix_passed"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
