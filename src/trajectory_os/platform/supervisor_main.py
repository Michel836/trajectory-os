"""M049 — runnable supervisor entrypoint (terminal-independent).

Invoked by ``scripts/trajectory daemon start`` in a detached session. It holds
no interactive terminal: standard input/output are not required, the loop only
delegates to the canonical scheduler, and it never performs a Git write.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from trajectory_os.platform import supervisor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trajectory-os-supervisor")
    parser.add_argument("--root", required=True)
    parser.add_argument("--max-cycles", dest="max_cycles", type=int, default=1)
    parser.add_argument("--project-id", dest="project_ids", action="append",
                        default=[])
    parser.add_argument("--cycle-interval", dest="cycle_interval", type=float,
                        default=0.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = supervisor.run_supervisor(
        Path(args.root),
        project_ids=tuple(args.project_ids) if args.project_ids else None,
        max_cycles=args.max_cycles,
        cycle_interval_s=args.cycle_interval,
    )
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
