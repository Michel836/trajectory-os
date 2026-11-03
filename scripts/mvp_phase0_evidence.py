#!/usr/bin/env python3
"""Phase 0 — read-only evidence for the daily usability foundations.

Runs against the real local MVP portfolio without modifying it:

* reports the portfolio-health counts that the cockpit now surfaces;
* copies the portfolio to a temporary root and runs the explicit
  ``rederive_project_statuses`` repair there, so the effect of the
  executability rule can be measured honestly;
* verifies that the repair eliminates projects flagged BLOCKED while having a
  ready task, and that the real portfolio bytes are unchanged.

Exit codes: 0 = evidence produced; 2 = usage error; 3 = portfolio missing.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import sys
import tempfile
from datetime import UTC, datetime

REPO = pathlib.Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from trajectory_os.mvp import outcomes, visualization  # noqa: E402

DEFAULT_ROOT = REPO / "local_data" / "mvp"
EVIDENCE_PATH = REPO / ".artifacts" / "phase0" / "evidence.json"


def _health(root: pathlib.Path) -> dict[str, object]:
    views = visualization.build_views(root)
    health = views["portfolio_health"]
    return {
        "counts": health["counts"],
        "blocked_projects_with_ready_task": [
            item["ref_id"] for item in health[
                "blocked_projects_with_ready_task"]
        ],
    }


def main(argv: list[str]) -> int:
    root = pathlib.Path(argv[1]) if len(argv) > 1 else DEFAULT_ROOT
    if not (root / "portfolio.json").is_file():
        print(f"error: no portfolio at {root}/portfolio.json", file=sys.stderr)
        return 3

    portfolio_path = root / "portfolio.json"
    before_bytes = portfolio_path.read_bytes()
    before = _health(root)

    with tempfile.TemporaryDirectory(prefix="trajectory-phase0-") as temp:
        temp_root = pathlib.Path(temp)
        for name in ("portfolio.json", "outcomes.jsonl", "changes.jsonl"):
            source = root / name
            if source.is_file():
                shutil.copy2(source, temp_root / name)
        repair = outcomes.rederive_project_statuses(temp_root)
        after = _health(temp_root)

    if portfolio_path.read_bytes() != before_bytes:
        print("error: the real portfolio was modified", file=sys.stderr)
        return 3
    if after["counts"]["blocked_projects_with_ready_task"] != 0:
        print("error: repair did not clear inconsistent project statuses",
              file=sys.stderr)
        return 3

    payload = {
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "root": str(root),
        "before": before,
        "rederive": repair,
        "after": after,
        "real_portfolio_unchanged": True,
    }
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True),
                             encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    print(f"\nevidence written: {EVIDENCE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
