#!/usr/bin/env python3
"""Mission 016 — record bounded DeepSeek Harness qualification evidence.

This script runs **one** bounded, read-only DeepSeek Harness canary through
the M015 provider-neutral contract and writes deterministic comparison
evidence to ``docs/missions/m016-canary-evidence.json``. It never launches a
full mission, never performs a Git trust-boundary write and never logs or
persists a credential value.

The result is deterministic and fail-closed: when the official SDK, runtime
or credentials are unavailable or incompatible, the evidence records the
exact status/reason and continues through the proven Pi path.

Usage::

    PYTHONPATH=src python3 scripts/mission016_canary_evidence.py \
        --workspace /tmp/m016-canary-ws \
        --out docs/missions/m016-canary-evidence.json
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from trajectory_os.agents import qualification

DEFAULT_OUT = (
    Path(__file__).resolve().parents[1]
    / "docs" / "missions" / "m016-canary-evidence.json"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=None,
                        help="isolated canary workspace (default: temp dir)")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--allow-runtime", action="store_true",
                        help="allow the runtime transport without the SDK")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)

    workspace = args.workspace or tempfile.mkdtemp(prefix="m016-canary-")
    Path(workspace).mkdir(parents=True, exist_ok=True)
    outcome = qualification.qualify(
        workspace=workspace, timeout_s=args.timeout,
        allow_runtime=args.allow_runtime)
    document = {
        "note": (
            "Mission 016 DeepSeek Harness qualification over the M015 "
            "provider-neutral agent-backend contract. One bounded, read-only "
            "canary was attempted with an isolated DSH_HOME/workspace. "
            "Structured JSON-RPC lifecycle evidence is authoritative: an "
            "errored turn is never reinterpreted as success. When credentials "
            "are unavailable or the developer-preview runtime is "
            "incompatible, the result is a deterministic CANARY_UNAVAILABLE "
            "or CANARY_INCOMPATIBLE and the proven Pi path remains the "
            "fallback. No secret is logged. No Git trust-boundary write is "
            "performed."
        ),
        "workspace": workspace,
        "dsh_home": os.environ.get("DSH_HOME"),
        "outcome": outcome.to_dict(),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    print(f"wrote {out} status={outcome.status} reason={outcome.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
