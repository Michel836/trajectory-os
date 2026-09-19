#!/usr/bin/env python3
"""M064–M071 — fresh independent final review on the exact final patch.

Builds the exact final bundle patch (tracked diffs + **untracked files via a
temporary Git index**), sends it verbatim to the mandated local independent
reviewer (``qwen3.8:27b-q4_K_M`` through Ollama) using the strict review
protocol, and records the raw response, the normalized assessment and the
exact patch identity (SHA-256) that was reviewed.

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
    "Review the M064-M071 real-world operating system and portfolio proof "
    "bundle change to TrajectoryOS (issue #246). It must deliver practical "
    "real-world value OVER the existing M017-M063 platform, without creating "
    "a competing lifecycle, readiness, mission/release identity, "
    "observability, scheduler, routing, RAG, dashboard or LifeOS truth model. "
    "Required capabilities: (M064) provenance-first real input adapters "
    "(local files/folders, Markdown/text, PDF-derived text, CSV/Excel "
    "exports, LifeOS notes, caller-supplied URL/research text) with a "
    "deterministic manifest (source_id/origin/location/timestamp/content "
    "hash/project/mission/sensitivity/ingestion status/duplicate/change "
    "status), explicit unsupported-format results, explicit conversion "
    "limitations, no silent OCR claim, no secret leakage and non-canonical "
    "context semantics; (M065) a complete career intelligence artifact set "
    "with FACT/EVIDENCE/INFERENCE/HYPOTHESIS typing, no invented personal or "
    "company fact, evidence map, gaps, problem hypotheses, AI/data "
    "opportunities, value proposition, interview brief, next actions and a "
    "LifeOS projection; (M066) a life-sciences/pharma workflow with "
    "evidence-linked observations, theme classification, entity/company "
    "linkage, recurring problems, cautious trends, opportunity hypotheses, "
    "mini business cases, OBSERVATION/EVIDENCE/INFERENCE/HYPOTHESIS "
    "distinction and no unsupported company claim; (M067) LifeOS operational "
    "intelligence answering what changed/blocked/requires attention/can wait/"
    "next best actions, every action with rationale, source, dependencies, "
    "uncertainty, urgency basis and alternatives, no opaque ranking and no "
    "competing truth model; (M068) outcome tracking linking prediction -> "
    "decision -> execution -> actual outcome with measured or explicitly "
    "entered actuals, UNKNOWN stays UNKNOWN, no success inferred from "
    "silence, persisted prediction error, provenance, schema versioning, "
    "idempotent delayed updates and an immutable append-only history; (M069) "
    "guarded champion/challenger model refresh with dataset snapshot "
    "identity, minimum sample thresholds, calibration comparison, "
    "data-quality checks, drift indicators, registry metadata, promotion "
    "rationale, rollback path, NO_PROMOTION and INSUFFICIENT_DATA, and no "
    "automatic promotion, policy mutation or hidden route/scheduler "
    "authority; (M070) a read-only non-developer decision cockpit over the "
    "existing control plane showing projects/next actions/attention/"
    "intelligence with why-this, uncertainty and evidence drill-down; (M071) "
    "portfolio/external proof with a 60-second executive overview, "
    "deterministic architecture representation, three case studies, a "
    "privacy-safe reproducible demo (scripts/trajectory demo portfolio), an "
    "external evidence pack, and build-in-public DRAFT material that is never "
    "auto-published and never claims a metric absent from the evidence pack; "
    "plus a >=60-case acceptance matrix and a real-world dogfood using real "
    "canonical runtime evidence where available, otherwise clearly labelled "
    "SAMPLE/FIXTURE. Implementation, review and realworld layers must NEVER "
    "commit, push, merge, reset, restore, clean, stash, rebase or switch "
    "branch, must never mutate policy, and must never fabricate a "
    "measurement or infer success from prose. The M017-M063 trust "
    "invariants (semantic patch identity, fresh final review, GO COMMIT, "
    "exact-head CI, GO MERGE) must be preserved."
)

EXCLUDE_PREFIXES = ("docs/missions/m064-m071/", ".artifacts/")


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
                       or REPO / "docs" / "missions" / "m064-m071"
                       / "m064-m071-final-review.json")
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
