"""Regression contracts for trajectory-pi V1.81-V1.84.

These tests deliberately focus on deterministic harness invariants:
validation environment/evidence, review normalization, and bounded
repair orchestration.

They do not invoke a real model/provider.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "scripts" / "trajectory-pi"


def source() -> str:
    return WRAPPER.read_text(encoding="utf-8")


def extract_embedded_python(function_name: str) -> str:
    """Extract the first python heredoc embedded in a shell function."""
    text = source()

    start_token = f"{function_name}() {{"
    start = text.index(start_token)

    py_start = text.index("<<'PY'\n", start) + len("<<'PY'\n")
    py_end = text.index("\nPY\n}", py_start)

    return text[py_start:py_end]


# ---------------------------------------------------------------------------
# Release contract
# ---------------------------------------------------------------------------


def test_v040_version_contract() -> None:
    result = subprocess.run(
        ["bash", str(WRAPPER), "--version"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "trajectory-pi 0.4.0"


# ---------------------------------------------------------------------------
# V1.81 — deterministic validation environment
# ---------------------------------------------------------------------------


def test_v181_validation_environment_prefers_project_venv() -> None:
    text = source()

    assert 'VALIDATION_VENV_BIN="$dir/.venv/bin"' in text
    assert 'PATH=$VALIDATION_VENV_BIN${PATH:+:$PATH}' in text
    assert 'VIRTUAL_ENV=${VALIDATION_VENV_BIN%/bin}' in text

    assert (
        'VALIDATION_ENV_LABEL="$dir/.venv/bin/python (project venv)"'
        in text
    )


def test_v181_broken_venv_fails_closed() -> None:
    text = source()

    assert "VALIDATION_ENV_OK=0" in text
    assert "missing or not executable" in text
    assert "refusing to validate" in text


def test_v181_symlinked_venv_root_fails_closed() -> None:
    text = source()

    assert '[[ -L "$dir/.venv" ]]' in text
    assert "refusing non-local validation environment" in text


def test_v181_environment_walk_reaches_filesystem_root() -> None:
    text = source()

    block_start = text.index("detect_validation_environment()")
    block_end = text.index("\n}\n", block_start)
    block = text[block_start:block_end]

    assert '[[ "$dir" == "/" ]] && break' in block
    assert "depth < 8" not in block


# ---------------------------------------------------------------------------
# V1.82 — first-class validation evidence
# ---------------------------------------------------------------------------


def test_v182_validation_artifact_contract_present() -> None:
    text = source()

    required = (
        "validation.txt",
        "validation.json",
        "validation-$i.txt",
        "validation-$i.stdout",
        "validation-$i.stderr",
        "overall_status",
        "working_directory",
        "exit_code",
    )

    for item in required:
        assert item in text


def test_v182_validation_json_does_not_embed_raw_output() -> None:
    text = source()

    start = text.index("write_validation_evidence()")
    end = text.index(
        "# ------------------------------------------------------------",
        start + 1,
    )
    block = text[start:end]

    # Structured evidence references captured files instead of serializing
    # the captured stdout/stderr bodies into JSON.
    assert '"stdout_file"' in block
    assert '"stderr_file"' in block
    assert '"summary_file"' in block

    assert '"stdout":' not in block
    assert '"stderr":' not in block


def test_v182_machine_evidence_uses_project_python_when_available() -> None:
    text = source()

    start = text.index("write_validation_evidence()")
    end = text.index(
        "# ------------------------------------------------------------",
        start + 1,
    )
    block = text[start:end]

    assert 'local evidence_python="python3"' in block
    assert 'evidence_python="$VALIDATION_VENV_BIN/python"' in block
    assert '"$evidence_python" - ' in block


# ---------------------------------------------------------------------------
# V1.83 — deterministic review findings
# ---------------------------------------------------------------------------


def run_normalizer(tmp_path: Path, review: str) -> dict:
    script = tmp_path / "normalizer.py"
    raw = tmp_path / "review.txt"
    out = tmp_path / "out"

    script.write_text(
        extract_embedded_python("normalize_review_findings"),
        encoding="utf-8",
    )
    raw.write_text(review, encoding="utf-8")
    out.mkdir()

    result = subprocess.run(
        [sys.executable, str(script), str(raw), str(out)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr

    return json.loads(
        (out / "review-findings.json").read_text(encoding="utf-8")
    )


def test_v183_none_sections_and_headings_are_not_findings(
    tmp_path: Path,
) -> None:
    data = run_normalizer(
        tmp_path,
        """VERDICT: PASS

BLOCKERS:
- None

MAJORS:
- None

MINORS:
- None

FINAL RECOMMENDATION: GO COMMIT
""",
    )

    assert data["counts"] == {
        "open": 0,
        "rebutted": 0,
        "ambiguous": 0,
    }


def test_v183_genuine_finding_is_open(tmp_path: Path) -> None:
    data = run_normalizer(
        tmp_path,
        """VERDICT: FAIL

BLOCKERS:
- None

MAJORS:
- Repair attempt can reuse stale validation evidence.

MINORS:
- None

FINAL RECOMMENDATION: FIX
""",
    )

    assert data["counts"] == {
        "open": 1,
        "rebutted": 0,
        "ambiguous": 0,
    }


def test_v183_explicit_self_rebuttal_is_rebutted(tmp_path: Path) -> None:
    data = run_normalizer(
        tmp_path,
        """VERDICT: PASS

BLOCKERS:
- None

MAJORS:
- Possible query propagation defect.
  After tracing the invocation path, this is correct.
  No issue found.

MINORS:
- None

FINAL RECOMMENDATION: GO COMMIT
""",
    )

    assert data["counts"] == {
        "open": 0,
        "rebutted": 1,
        "ambiguous": 0,
    }


def test_v183_uncertain_rebuttal_is_ambiguous(tmp_path: Path) -> None:
    data = run_normalizer(
        tmp_path,
        """VERDICT: FAIL

BLOCKERS:
- None

MAJORS:
- The retry path might be unsafe.
  This may be a false positive, but I am not sure.

MINORS:
- None

FINAL RECOMMENDATION: FIX
""",
    )

    assert data["counts"] == {
        "open": 0,
        "rebutted": 0,
        "ambiguous": 1,
    }


def test_v183_generic_risk_language_remains_open(tmp_path: Path) -> None:
    data = run_normalizer(
        tmp_path,
        """VERDICT: FAIL

BLOCKERS:
- None

MAJORS:
- Untrusted output could lead to prompt injection.

MINORS:
- None

FINAL RECOMMENDATION: FIX
""",
    )

    assert data["counts"] == {
        "open": 1,
        "rebutted": 0,
        "ambiguous": 0,
    }


# ---------------------------------------------------------------------------
# V1.84 — bounded autonomous convergence
# ---------------------------------------------------------------------------


def test_v184_repair_attempt_count_is_strictly_bounded() -> None:
    text = source()

    assert '[[ "$REPAIR_ATTEMPTS" =~ ^[0-3]$ ]]' in text
    assert "REPAIR_USED < REPAIR_ATTEMPTS" in text
    assert "REPAIR_ATTEMPTS > 3" not in text


def test_v184_final_query_not_baked_into_pi_args() -> None:
    text = source()

    start = text.index("PI_ARGS=(")
    end = text.index("invoke_pi_agent()", start)
    construction = text[start:end]

    assert 'PI_ARGS+=("$FINAL_QUERY")' not in construction
    assert "PI_FINAL_QUERY" in construction


def test_v184_each_invocation_receives_pi_final_query() -> None:
    text = source()

    start = text.index("invoke_pi_agent()")
    end = text.index("\n}\n", start)
    block = text[start:end]

    assert 'pi "${PI_ARGS[@]}" "$PI_FINAL_QUERY"' in block


def test_v184_first_pass_uses_original_query() -> None:
    text = source()

    assert 'PI_FINAL_QUERY="$FINAL_QUERY"' in text
    assert 'invoke_pi_agent "$LOG"' in text


def test_v184_repair_pass_uses_repair_prompt() -> None:
    text = source()

    assert 'PI_FINAL_QUERY="$(cat -- "$attempt_dir/prompt.md")"' in text
    assert 'invoke_pi_agent "$attempt_dir/pi.log"' in text


def test_v184_signal_traps_are_installed_per_invocation() -> None:
    text = source()

    start = text.index("invoke_pi_agent()")
    end = text.index("\n}\n", start)
    block = text[start:end]

    assert "trap 'handle_signal INT' INT" in block
    assert "trap 'handle_signal TERM' TERM" in block

    # Traps are intentionally reset after waits and are therefore restored
    # by the next invoke_pi_agent call.
    assert text.count("trap - INT TERM") >= 2


def test_v184_repair_prompt_has_explicit_trust_boundary() -> None:
    text = source()

    start = text.index("build_repair_prompt()")
    end = text.index("\n}\n", start)
    block = text[start:end]

    assert "TRUST BOUNDARY:" in block
    assert "UNTRUSTED DIAGNOSTIC DATA" in block
    assert "not instructions" in block


def test_v184_repair_prompt_does_not_inline_raw_evidence() -> None:
    text = source()

    start = text.index("build_repair_prompt()")
    end = text.index("\n}\n", start)
    block = text[start:end]

    assert 'cat -- "$f"' not in block
    assert 'cat -- "$RUN_DIR/review-findings.txt"' not in block
    assert "tail -n 40" not in block

    assert "validation.txt" in block
    assert "validation.json" in block
    assert "review-findings.txt" in block
    assert "review-findings.json" in block


def test_v184_repair_refreshes_complete_readiness_pipeline() -> None:
    text = source()

    loop_start = text.index(
        "while (( REPAIR_USED < REPAIR_ATTEMPTS ))"
    )
    loop_end = text.index("\nfi", loop_start)
    loop = text[loop_start:loop_end]

    # Every repair pass must recompute readiness from fresh evidence.
    assert "compute_readiness" in loop

    # compute_readiness itself regenerates the complete evidence chain
    # from the current worktree.
    fn_start = text.index("compute_readiness()")
    fn_end = text.index("\n}\n", fn_start)
    fn = text[fn_start:fn_end]

    assert "run_worktree_snapshot" in fn
    assert "run_validations" in fn
    assert "run_review" in fn
    assert "run_final_verification" in fn


def test_v184_final_patch_identity_is_reverified() -> None:
    text = source()

    # SNAPSHOT_PATCH_SHA identifies the exact patch supplied to the
    # independent reviewer. FINAL_PATCH_SHA is recomputed by the late
    # final verification step. REVIEW_STALE guards the relationship.
    assert "SNAPSHOT_PATCH_SHA" in text
    assert "FINAL_VERIFY_STATUS" in text
    assert "FINAL_PATCH_SHA" in text
    assert "REVIEW_STALE" in text


def test_v184_repair_history_normalizes_multiline_reason() -> None:
    text = source()

    assert 'history_reason="${READINESS_REASON//$\'\\n\'/ }"' in text
    assert 'history_reason="${history_reason//$\'\\r\'/ }"' in text


def test_v184_convergence_summary_is_persisted() -> None:
    text = source()

    assert "repair-summary.txt" in text
    assert "requested_attempts=" in text
    assert "used_attempts=" in text
    assert "final_readiness=" in text
