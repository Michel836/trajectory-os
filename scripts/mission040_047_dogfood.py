#!/usr/bin/env python3
"""M040 — true self-release dogfood evidence over the production code path.

Runs the canonical operator control plane (M041) with the release policy
(M044), routing (M045), unified events (M043), full-lifecycle recovery (M042)
and one-screen state (M046) over the real repository, stopping at the human
GO COMMIT gate and performing zero Git writes.

The evidence separates the deterministic fixture proof (full path) from the
live dogfood (real repo, read-only handoff) and is written as durable JSON
plus a compact human summary.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import shutil
import sys
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from trajectory_os.operator import dogfood as operator_dogfood

MARKER = "M040_M047_SELF_HOSTING_OPERATOR_PLATFORM_COMPLETE"


def render(evidence: dict[str, Any]) -> str:
    fixture = evidence["fixture_proof"]
    live = evidence["live_dogfood"]
    handoff = live.get("commit_handoff") or {}
    lines = [
        "# M040 — True Self-Release Dogfood — Evidence",
        "",
        f"Marker: `{MARKER}`  ",
        f"Status: **{evidence['status']}**",
        "",
        "## Fixture proof (deterministic, full path)",
        f"- ok: {fixture.get('ok')}",
        f"- mission: `{fixture.get('mission_id')}`",
        f"- reviewed patch: `{fixture.get('reviewed_patch')}`",
        f"- commit: `{fixture.get('commit_sha')}`",
        f"- PR: #{fixture.get('pr_number')} head "
        f"`{fixture.get('pr_head_sha')}`",
        f"- exact-head CI: {fixture.get('ci_state')}",
        f"- merge: `{fixture.get('merge_sha')}` "
        f"(verified: {fixture.get('target_branch_verified')})",
        f"- release closure: **{fixture.get('release_status')}**",
        f"- recovery action: {fixture.get('recovery_action')}",
        "",
        "## Live dogfood (real repository, stopped at the human gate)",
        f"- mission: `{live.get('mission_id')}`",
        f"- ready for commit: {live.get('ready_for_commit')}",
        f"- lifecycle / readiness: {live.get('lifecycle')} / "
        f"{live.get('readiness')}",
        f"- policy profile: {live.get('policy_profile')}",
        f"- reviewed patch: `{live.get('reviewed_patch')}`",
        f"- commit handoff patch: `{handoff.get('reviewed_patch_sha256')}`",
        f"- stopped at: {live.get('stopped_at')}",
        f"- Git writes: {live.get('git_writes')}",
        "",
        "## Guardrails",
    ]
    for name, value in evidence["guardrails"].items():
        lines.append(f"- {name}: {value}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".artifacts/m040-m047/dogfood")
    parser.add_argument("--repo", default=str(REPO))
    parser.add_argument("--docs", default="docs/missions/m040-m047")
    parser.add_argument("--out", default=None)
    parser.add_argument("--markdown", default=None)
    parser.add_argument("--reset", action="store_true", default=True)
    args = parser.parse_args(argv)

    root = pathlib.Path(args.root)
    if args.reset:
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    evidence = operator_dogfood.run_self_hosting_dogfood(
        root, repo=args.repo, issue="240", write_evidence=True)
    evidence["marker"] = MARKER
    evidence["generated_at"] = (
        datetime.datetime.now(datetime.UTC)
        .replace(microsecond=0, tzinfo=None).isoformat() + "Z")

    docs = pathlib.Path(args.docs)
    docs.mkdir(parents=True, exist_ok=True)
    out = pathlib.Path(args.out or docs / "m040-m047-dogfood-evidence.json")
    out.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    markdown = pathlib.Path(args.markdown or out.with_suffix(".md"))
    markdown.write_text(render(evidence) + "\n", encoding="utf-8")
    print(f"dogfood evidence written: {out}")
    print(f"dogfood summary written:  {markdown}")
    print(f"status: {evidence['status']}")
    return 0 if evidence["status"] == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
