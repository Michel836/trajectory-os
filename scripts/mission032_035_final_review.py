#!/usr/bin/env python3
"""M032–M035 — fresh independent final review on the exact final patch.

Builds the exact final bundle patch (tracked diffs + untracked files), sends
it verbatim to the mandated local independent reviewer
(``qwen3.8:27b-q4_K_M`` through Ollama) using the strict M026/M028 review
protocol, and records the raw response, the normalized assessment and the
exact patch identity (sha256) that was reviewed.

Exit codes: 0 = VALID_PASS; 3 = any non-pass / protocol-invalid; 2 = the
reviewer transport was unavailable (fail closed).
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import pathlib
import subprocess
import sys
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
    "Review the M032-M035 real-operator-bundle change to TrajectoryOS. It "
    "must: run a real non-fixture mission through the assembled path; make "
    "recovery deterministic and idempotent with an explicit safe resume "
    "point; add mission-scoped operator control (request-stop/pause/cancel) "
    "that is separate from observation, targets explicit mission ids, fails "
    "closed, produces canonical CANCELLED and preserves evidence; and add a "
    "deterministic production acceptance matrix. It must not introduce a "
    "second status/lifecycle model, must not weaken the human GO COMMIT gate, "
    "and must never perform a Git trust-boundary write."
)

EXCLUDE_PREFIXES = (
    "docs/missions/m032-m035/",
)


def build_patch(*, include_evidence: bool) -> str:
    chunks: list[str] = []
    chunks.append(subprocess.run(
        ["git", "diff", "--", "README.md", "src", "tests", "scripts",
         "docs/adr"],
        cwd=str(REPO), capture_output=True, text=True, check=True).stdout)
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=str(REPO), capture_output=True, text=True, check=True
    ).stdout.splitlines()
    for relative in untracked:
        if not include_evidence and any(
                relative.startswith(prefix) for prefix in EXCLUDE_PREFIXES):
            continue
        diff = subprocess.run(
            ["git", "diff", "--no-index", "/dev/null", relative],
            cwd=str(REPO), capture_output=True, text=True, check=False)
        chunks.append(diff.stdout)
    return "".join(chunks)


def call_reviewer(prompt: str, *, timeout_s: int) -> str:
    payload = json.dumps({
        "model": REVIEWER_MODEL,
        "stream": False,
        "options": {"num_ctx": 65536, "temperature": 0},
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
    return message["content"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=None)
    parser.add_argument("--raw-out", default=None)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--max-chars", type=int, default=220000)
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
    # Post-review exact patch identity verification: the implementation patch
    # that was reviewed must still be the current implementation patch (the
    # review-evidence artifact itself is excluded, by construction).
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
                       or REPO / "docs" / "missions" / "m032-m035"
                       / "m032-m035-final-review.json")
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
