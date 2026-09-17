"""Focused tests for the PLAN semantic status guard in ``scripts/trajectory-pi``.

Only ``semantic_status_for_classification()`` is exercised. The function is
extracted from the wrapper source and executed in a lightweight Bash harness
per case — the full wrapper is never invoked, no real agent runs, and no
run history (e.g. M008) is read.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "scripts" / "trajectory-pi"


def _extract_guard() -> str:
    text = WRAPPER.read_text(encoding="utf-8")
    start = text.index("semantic_status_for_classification() {")
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


GUARD = _extract_guard()

# All three PLAN invariants proven unchanged: HEAD stable, semantic index
# unchanged, worktree unchanged.
PROVEN: dict[str, str] = {
    "HEAD_BEFORE": "head-sha-1",
    "HEAD_AFTER": "head-sha-1",
    "SEMANTIC_INDEX_AFTER_AGENT_STATE": "UNCHANGED",
    "PLAN_WORKTREE_STATE": "UNCHANGED",
}


def _classify(classification: str, env: dict[str, str], tmp_path: Path) -> str:
    """Run the extracted guard in an isolated Bash process and return its verdict."""
    script = tmp_path / "guard-case.sh"
    assignments = "\n".join(f"{key}={value}" for key, value in env.items())
    script.write_text(
        "set -euo pipefail\n"
        f"{GUARD}\n"
        "RUN_MODE=PLAN\n"
        f"{assignments}\n"
        f'out="$(semantic_status_for_classification {classification})"\n'
        'printf "%s\\n" "$out"\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"guard harness failed: {result.stderr}"
    return result.stdout.strip()


def test_plan_success_when_all_invariants_proven(tmp_path: Path) -> None:
    assert _classify("AGENT_COMPLETED", PROVEN, tmp_path) == "SUCCESS"


def test_incomplete_agent_run_is_incomplete(tmp_path: Path) -> None:
    assert _classify("INCOMPLETE_AGENT_RUN", PROVEN, tmp_path) == "INCOMPLETE"


def test_provider_missing_query_is_provider_failure(tmp_path: Path) -> None:
    assert (
        _classify("UPSTREAM_PROVIDER_MISSING_QUERY", PROVEN, tmp_path)
        == "PROVIDER_FAILURE"
    )


def test_plan_failed_when_worktree_changed(tmp_path: Path) -> None:
    env = dict(PROVEN, PLAN_WORKTREE_STATE="CHANGED")
    assert _classify("AGENT_COMPLETED", env, tmp_path) == "FAILED"


def test_plan_failed_when_semantic_index_changed(tmp_path: Path) -> None:
    env = dict(PROVEN, SEMANTIC_INDEX_AFTER_AGENT_STATE="CHANGED")
    assert _classify("AGENT_COMPLETED", env, tmp_path) == "FAILED"


def test_plan_failed_when_head_changed(tmp_path: Path) -> None:
    env = dict(PROVEN, HEAD_AFTER="head-sha-2")
    assert _classify("AGENT_COMPLETED", env, tmp_path) == "FAILED"


def test_plan_unknown_when_any_invariant_unproven(tmp_path: Path) -> None:
    unproven_variants = (
        # HEAD snapshot unproven (empty before/after).
        {"HEAD_BEFORE": "", "HEAD_AFTER": ""},
        # Semantic index evidence unproven (empty state).
        {"SEMANTIC_INDEX_AFTER_AGENT_STATE": ""},
        # Worktree state unproven (not captured as a plan state).
        {"PLAN_WORKTREE_STATE": "NOT_PLAN"},
    )

    for index, variant in enumerate(unproven_variants):
        env = dict(PROVEN, **variant)
        assert _classify("AGENT_COMPLETED", env, tmp_path) == "UNKNOWN", (
            f"unproven invariant variant {index} ({sorted(variant)}) must be UNKNOWN"
        )
