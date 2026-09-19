#!/usr/bin/env python3
"""M040–M047 — fresh independent final review on the exact final patch.

Builds the exact final bundle patch (tracked diffs + **untracked files via a
temporary Git index**), sends it verbatim to the mandated local independent
reviewer (``qwen3.8:27b-q4_K_M`` through Ollama) using the strict M026/M028
review protocol, and records the raw response, the normalized assessment and
the exact patch identity (SHA-256) that was reviewed.

The temporary-index approach guarantees that newly added (untracked) source,
test and script files are part of the reviewed bytes — not silently omitted.

Exit codes: 0 = VALID_PASS; 3 = any non-pass / protocol-invalid; 2 = the
reviewer transport was unavailable (fail closed).
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from typing import Any
from urllib import request as urlrequest

REPO = pathlib.Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from trajectory_os.benchmark.review import build_review_prompt
from trajectory_os.missions import review_protocol

REVIEWER_MODEL = "qwen3.8:27b-q4_K_M"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"

OBJECTIVE = (
    "Review the M040-M047 self-hosting operator platform bundle change to "
    "TrajectoryOS. It must consolidate M017-M039 into one coherent operator "
    "product WITHOUT a competing lifecycle/readiness/mission/release/"
    "observability/trust model (mission_id == run_id remains authoritative). "
    "It must add: (M040) true self-release dogfood over the production path "
    "with separated fixture proof vs live dogfood evidence, durable "
    "mission-release linkage and end-to-end semantic patch identity; (M041) "
    "one unified operator control plane (scripts/trajectory) reusing "
    "assembly/observability/runtime-control/release with read-only "
    "observation and explicit mutations; (M042) full-lifecycle deterministic "
    "recovery that never duplicates commit/push/PR/merge, fails closed on "
    "local/remote contradiction and is idempotent; (M043) an additive, "
    "append-only, replayable unified event envelope with deterministic "
    "ordering, duplicate handling and M030-M039 migration compatibility that "
    "does NOT replace canonical status.json/closure.json/release artifacts; "
    "(M044) a small deterministic policy layer with safe/fast-local/"
    "benchmark/release profiles where release ALWAYS retains explicit human "
    "GO COMMIT and GO MERGE and cannot silently disable exact-head CI, with "
    "persisted deterministic resolution and no environment mutation; (M045) "
    "consolidated backend/reviewer routing with explicit persisted "
    "implementation/inline/final identities, no phantom reviewer, fail-closed "
    "unavailable backends, deterministic persisted fallback, and the final "
    "independent reviewer remaining qwen3.8:27b-q4_K_M; (M046) one "
    "read-only one-screen operator product state projection; (M047) a "
    "production acceptance matrix proving the full happy path, trust "
    "boundaries, crash/recovery idempotence, exact-head CI, human merge "
    "authorization, event replay, semantic identity and no prose-derived "
    "success. Implementation/review/runtime-control components must never "
    "commit/push/merge/reset/restore/clean/stash/rebase/switch; release Git "
    "writes must remain behind the existing explicit operator authorization "
    "gates; and no existing acceptance evidence or M030-M039 compatibility "
    "may be broken."
)

EXCLUDE_PREFIXES = ("docs/missions/m040-m047/", ".artifacts/")


def _git(args: list[str], *, env: dict[str, str] | None = None,
         check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=str(REPO), capture_output=True,
                          text=True, check=False, env=env)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


def build_patch(*, include_evidence: bool) -> str:
    with tempfile.NamedTemporaryFile(delete=False) as handle:
        index_path = handle.name
    try:
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = index_path
        _git(["read-tree", "HEAD"], env=env)
        _git(["add", "-A"], env=env)
        if not include_evidence:
            for prefix in EXCLUDE_PREFIXES:
                _git(["rm", "--cached", "-r", "--ignore-unmatch", "--quiet",
                      prefix], env=env, check=False)
        return _git(["diff", "--cached", "--binary"], env=env)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(index_path)


def call_reviewer(prompt: str, *, timeout_s: int) -> str:
    payload = json.dumps({
        "model": REVIEWER_MODEL,
        "stream": False,
        "options": {"num_ctx": 131072, "temperature": 0},
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urlrequest.Request(
        OLLAMA_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    with urlrequest.urlopen(req, timeout=timeout_s) as response:  # noqa: S310
        body = response.read()
    document: Any = json.loads(body.decode("utf-8"))
    message = document.get("message")
    if not isinstance(message, dict) or not isinstance(
            message.get("content"), str):
        raise RuntimeError("malformed Ollama response")
    return str(message["content"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=None)
    parser.add_argument("--raw-out", default=None)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--max-chars", type=int, default=600000)
    parser.add_argument("--include-evidence", action="store_true",
                        default=False)
    args = parser.parse_args(argv)

    patch = build_patch(include_evidence=args.include_evidence)
    patch_sha = hashlib.sha256(patch.encode("utf-8")).hexdigest()
    prompt = build_review_prompt(OBJECTIVE, patch, max_chars=args.max_chars)
    try:
        text = call_reviewer(prompt, timeout_s=args.timeout)
    except Exception as exc:  # noqa: BLE001 - transport failure fails closed
        print(f"reviewer unavailable: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2
    assessment = review_protocol.assess(text)
    current_patch = build_patch(include_evidence=args.include_evidence)
    current_sha = hashlib.sha256(current_patch.encode("utf-8")).hexdigest()
    document = {
        "reviewer_model": REVIEWER_MODEL,
        "reviewed_patch_sha256": patch_sha,
        "post_review_patch_sha256": current_sha,
        "reviewed_patch_still_current": patch_sha == current_sha,
        "reviewed_patch_bytes": len(patch.encode("utf-8")),
        "prompt_bytes": len(prompt.encode("utf-8")),
        "generated_at": (datetime.datetime.now(datetime.UTC)
                         .replace(microsecond=0, tzinfo=None).isoformat()
                         + "Z"),
        "assessment": assessment.to_dict(),
        "raw_response": text,
    }
    out = pathlib.Path(args.out
                       or REPO / "docs" / "missions" / "m040-m047"
                       / "m040-m047-final-review.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    raw = pathlib.Path(args.raw_out or out.with_suffix(".txt"))
    raw.write_text(text + "\n", encoding="utf-8")
    print(f"final review: {assessment.outcome} ({assessment.reason}) "
          f"patch={patch_sha}")
    print(f"evidence: {out}")
    return 0 if assessment.outcome == review_protocol.OUTCOME_VALID_PASS else 3


if __name__ == "__main__":
    raise SystemExit(main())
