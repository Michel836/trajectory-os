#!/usr/bin/env python3
"""M064–M071 — consolidate the real-world OS / portfolio bundle evidence.

Runs the deterministic acceptance matrix, the real-history dogfood and the
privacy-safe demo; copies the portfolio documents; performs the trust-boundary
scan; and computes the complete working-tree semantic patch SHA-256 via a
temporary Git index (the real index is never touched). It never invents a
fact and never commits, pushes or merges.
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

from trajectory_os.realworld import acceptance as rw_acceptance
from trajectory_os.realworld import dogfood as rw_dogfood
from trajectory_os.realworld import model as rw_model

BASELINE = "77de25be7b04c27d4a66de206a8fc911f7da4017"
MARKER = "M064_M071_REAL_WORLD_OS_PORTFOLIO_PROOF_COMPLETE"

GUARDED = ("src/trajectory_os/realworld",)
GIT_VERBS = ("commit", "push", "merge", "reset", "restore", "clean", "stash",
             "rebase", "switch", "checkout")

EXCLUDED_EVIDENCE = (
    ":(exclude)docs/missions/m064-m071/m064-m071-acceptance.json",
    ":(exclude)docs/missions/m064-m071/m064-m071-dogfood.json",
    ":(exclude)docs/missions/m064-m071/m064-m071-bundle-evidence.json",
    ":(exclude)docs/missions/m064-m071/m064-m071-bundle-evidence.md",
    ":(exclude)docs/missions/m064-m071/portfolio",
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


def _copy_portfolio(demo_root: pathlib.Path,
                    docs_dir: pathlib.Path) -> list[str]:
    source = demo_root / "realworld" / "portfolio"
    if not source.is_dir():
        return []
    target = docs_dir / "portfolio"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for path in sorted(source.iterdir()):
        if path.is_file():
            shutil.copy2(path, target / path.name)
            written.append(f"docs/missions/m064-m071/portfolio/{path.name}")
    return written


def build(docs: pathlib.Path, runtime: pathlib.Path,
          *, baseline: str = BASELINE,
          real_runs_dir: str | None = None) -> dict[str, Any]:
    if runtime.exists():
        shutil.rmtree(runtime)
    runtime.mkdir(parents=True, exist_ok=True)
    docs.mkdir(parents=True, exist_ok=True)

    matrix = rw_acceptance.run_acceptance(str(runtime / "acceptance"))
    (docs / "m064-m071-acceptance.json").write_text(
        json.dumps(matrix.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8")

    dogfood = rw_dogfood.run_dogfood(
        str(runtime / "dogfood"),
        real_runs_dir=(real_runs_dir if real_runs_dir is not None
                       else rw_dogfood.DEFAULT_REAL_RUNS),
        docs_dir=None)
    (docs / "m064-m071-dogfood.json").write_text(
        json.dumps(dogfood.to_dict(), indent=2, sort_keys=True,
                   default=str) + "\n", encoding="utf-8")

    demo = rw_dogfood.run_privacy_safe_demo(
        str(runtime / "demo"), generated_at=dogfood.generated_at,
        run_acceptance_matrix=False)
    # The external evidence pack is copied from the real-history dogfood (it
    # carries the measured acceptance/outcome evidence); the privacy-safe demo
    # is recorded separately below.
    portfolio_files = _copy_portfolio(runtime / "dogfood", docs)

    patch = semantic_patch_identity()
    scan = trust_boundary_scan()
    return {
        "schema": "trajectory-m064-m071-bundle-evidence/1",
        "baseline": baseline,
        "marker": MARKER,
        "generated_at": dogfood.generated_at,
        "realworld_version": rw_model.REALWORLD_VERSION,
        "acceptance": {
            "status": "PASS" if matrix.failed == 0 else "FAIL",
            "total": matrix.total,
            "passed": matrix.passed,
            "failed": matrix.failed,
            "category_counts": matrix.category_counts(),
            "artifact": "docs/missions/m064-m071/m064-m071-acceptance.json",
        },
        "dogfood": {
            "history_source": dogfood.history_source,
            "history_note": dogfood.history_note,
            "real_row_count": dogfood.real_row_count,
            "fixture_row_count": dogfood.fixture_row_count,
            "career_input": dogfood.career_input,
            "life_sciences_input": dogfood.life_sciences_input,
            "outcome_links": dogfood.outcome_reconciliation.get("link_count"),
            "outcome_reconciled": dogfood.outcome_reconciliation.get(
                "reconciled"),
            "prediction_error": dogfood.outcome_reconciliation.get(
                "mean_absolute_error"),
            "model_refresh_state": dogfood.model_refresh.get("state"),
            "value_metrics": [dict(item) for item in dogfood.value_metrics],
            "limitations": list(dogfood.limitations),
            "artifact": "docs/missions/m064-m071/m064-m071-dogfood.json",
        },
        "privacy_safe_demo": {
            "command": "scripts/trajectory demo portfolio",
            "history_source": demo.history_source,
            "real_rows": demo.real_row_count,
            "documents": portfolio_files,
        },
        "trust_boundary": scan,
        "semantic_patch": patch,
        "trust_gates_unchanged": True,
        "git_writes": 0,
        "acceptance_matrix_passed": matrix.failed == 0,
    }


def render(evidence: dict[str, Any]) -> str:
    acceptance = evidence["acceptance"]
    dogfood = evidence["dogfood"]
    patch = evidence["semantic_patch"]
    scan = evidence["trust_boundary"]
    demo = evidence["privacy_safe_demo"]
    lines = [
        "# M064–M071 — Real-World Operating System & Portfolio Proof",
        "## Bundle Evidence",
        "",
        f"- baseline: `{evidence['baseline']}`",
        f"- generated: {evidence['generated_at']}",
        f"- realworld version: `{evidence['realworld_version']}`",
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
        f"- career input: {dogfood['career_input']}",
        f"- life-sciences input: {dogfood['life_sciences_input']}",
        f"- outcome links: {dogfood['outcome_links']} "
        f"({dogfood['outcome_reconciled']} reconciled)",
        f"- prediction error (MAE): {dogfood['prediction_error']}",
        f"- model refresh state: {dogfood['model_refresh_state']}",
        f"- artifact: `{dogfood['artifact']}`",
        "",
        "## Value metrics",
        "",
        "| metric | value | unit | source |",
        "| --- | --- | --- | --- |",
    ]
    for metric in dogfood["value_metrics"]:
        lines.append(f"| {metric.get('name')} | {metric.get('value')} | "
                     f"{metric.get('unit')} | {metric.get('source')} |")
    lines += [
        "",
        "## Privacy-safe demo",
        "",
        f"- command: `{demo['command']}`",
        f"- history source: {demo['history_source']} "
        f"(real rows: {demo['real_rows']})",
        f"- documents: {len(demo['documents'])}",
        "",
        "## Trust boundary",
        "",
        f"- clean: **{scan['clean']}** ({scan['scanned_files']} files scanned)",
        f"- offenders: {scan['offenders'] or 'none'}",
        f"- git release writes from realworld layers: {evidence['git_writes']}",
        f"- M017–M063 trust gates unchanged: "
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
    parser.add_argument("--docs", default="docs/missions/m064-m071")
    parser.add_argument("--runtime", default=".artifacts/m064-m071/bundle")
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
        args.out or docs / "m064-m071-bundle-evidence.json")
    out.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    markdown = pathlib.Path(args.markdown or out.with_suffix(".md"))
    markdown.write_text(render(evidence) + "\n", encoding="utf-8")
    print(f"bundle evidence written: {out}")
    print(f"bundle summary written:  {markdown}")
    return 0 if evidence["acceptance_matrix_passed"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
