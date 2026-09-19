"""Review-protocol semantic distinction: VALID_PASS / VALID_REJECT / INVALID.

Also binds the distinction to the shipped ``trajectory-pi`` reviewer parser so
a parser-level ``PASS``/``FAIL``/``REJECTED`` maps deterministically onto the
normalized outcome and the shipped behaviour can never be silently weakened.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

from trajectory_os.missions import review_protocol as rp

REPO = Path(__file__).resolve().parents[2]
WRAPPER = REPO / "scripts" / "trajectory-pi"
_PARSER_MARKER = "parse_review_verdict() {"

CANONICAL_PASS = (
    "VERDICT: PASS\n\nBLOCKERS:\n- None\n\nMAJORS:\n- None\n\n"
    "MINORS:\n- None\n\nFINAL RECOMMENDATION: GO COMMIT\n"
)
PASS_WITH_NOTES = CANONICAL_PASS.replace(
    "MINORS:\n- None", "MINORS:\n- naming nit\n- doc nit")
BRAND_REJECT = (
    "VERDICT: REJECT\n\nBLOCKERS:\n- untested behavior change\n\n"
    "MAJORS:\n- None\n\nMINORS:\n- None\n\nFINAL RECOMMENDATION: REPAIR\n"
)
LEGACY_FAIL_FIX = (
    "VERDICT: FAIL\n\nBLOCKERS:\n- real defect\n\nMAJORS:\n- None\n\n"
    "MINORS:\n- None\n\nFINAL RECOMMENDATION: FIX\n"
)
REJECT_NO_FINDINGS = (
    "VERDICT: REJECT\n\nBLOCKERS:\n- None\n\nMAJORS:\n- None\n\n"
    "MINORS:\n- None\n\nFINAL RECOMMENDATION: REPAIR\n"
)
PASS_WITH_BLOCKER = CANONICAL_PASS.replace(
    "BLOCKERS:\n- None", "BLOCKERS:\n- real blocker")
PASS_WITH_MAJOR = CANONICAL_PASS.replace(
    "MAJORS:\n- None", "MAJORS:\n- real major")
REJECT_WITH_MAJOR = BRAND_REJECT.replace(
    "MAJORS:\n- None", "MAJORS:\n- real major")
MALFORMED = (
    "VERDICT: PASS\n\nMAJORS:\n- None\n\nFINAL RECOMMENDATION: GO COMMIT\n"
)
OUT_OF_ORDER = (
    "VERDICT: PASS\n\nMAJORS:\n- None\n\nBLOCKERS:\n- None\n\n"
    "MINORS:\n- None\n\nFINAL RECOMMENDATION: GO COMMIT\n"
)
DUPLICATED = CANONICAL_PASS.replace(
    "BLOCKERS:\n- None", "BLOCKERS:\n- None\n\nBLOCKERS:\n- None")


def test_valid_pass_and_non_blocking_notes_are_permitted() -> None:
    for text in (CANONICAL_PASS, PASS_WITH_NOTES):
        assessment = rp.assess(text)
        assert assessment.outcome == rp.OUTCOME_VALID_PASS
        assert assessment.permits_commit is True
        assert assessment.blocking_count == 0


def test_valid_reject_requires_repair_and_blocking_finding() -> None:
    for text in (BRAND_REJECT, LEGACY_FAIL_FIX, REJECT_WITH_MAJOR):
        assessment = rp.assess(text)
        assert assessment.outcome == rp.OUTCOME_VALID_REJECT
        assert assessment.permits_commit is False
        assert assessment.blocking_count >= 1


@pytest.mark.parametrize("text", [
    REJECT_NO_FINDINGS,
    PASS_WITH_BLOCKER,
    PASS_WITH_MAJOR,
    MALFORMED,
    OUT_OF_ORDER,
    DUPLICATED,
    CANONICAL_PASS.replace("GO COMMIT", "FIX"),
    "not a reviewer response at all",
])
def test_invalid_protocol_is_never_a_pass(text: str) -> None:
    assessment = rp.assess(text)
    assert assessment.outcome == rp.OUTCOME_INVALID
    assert assessment.permits_commit is False
    assert assessment.valid is False


def test_assessment_identity_is_deterministic() -> None:
    first = rp.assess(CANONICAL_PASS)
    second = rp.assess(CANONICAL_PASS)
    assert first.classification_id == second.classification_id
    assert rp.assess(BRAND_REJECT).classification_id \
        != first.classification_id


def test_classify_parser_output_mapping() -> None:
    assert rp.classify_parser_output(
        {"RESULT": "PASS", "VERDICT": "PASS",
         "RECOMMENDATION": "GO COMMIT"}
    ).outcome == rp.OUTCOME_VALID_PASS
    assert rp.classify_parser_output(
        {"RESULT": "REJECTED", "VERDICT": "n/a",
         "RECOMMENDATION": "n/a"}
    ).outcome == rp.OUTCOME_INVALID
    # A parser FAIL is a reject only if concrete blocking findings exist; the
    # parser does not expose finding text, so it stays invalid (fail closed).
    assert rp.classify_parser_output(
        {"RESULT": "FAIL", "VERDICT": "FAIL", "RECOMMENDATION": "FIX"}
    ).outcome == rp.OUTCOME_INVALID


def _run_shipped_parser(tmp_path: Path, response: str) -> dict[str, str]:
    script = WRAPPER.read_text()
    start = script.index(_PARSER_MARKER)
    end = script.index("\n}", start)
    fn_text = script[start:end + 2]
    review_file = tmp_path / "review.txt"
    review_file.write_text(response, encoding="utf-8")
    driver = tmp_path / "driver.sh"
    driver.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n" + fn_text + "\n"
        + "parse_review_verdict " + shlex.quote(str(review_file)) + "\n",
        encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(driver)], capture_output=True, text=True,
        env={**os.environ, "PATH": os.environ["PATH"]})
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    return {
        key: value for key, sep, value in (
            line.partition("=") for line in proc.stdout.splitlines())
        if sep
    }


def test_shipped_parser_matches_normalized_distinction(tmp_path: Path) -> None:
    cases = [
        (CANONICAL_PASS, rp.OUTCOME_VALID_PASS),
        (PASS_WITH_MAJOR, rp.OUTCOME_INVALID),
        (PASS_WITH_BLOCKER, rp.OUTCOME_INVALID),
        (MALFORMED, rp.OUTCOME_INVALID),
        (DUPLICATED, rp.OUTCOME_INVALID),
    ]
    for text, expected in cases:
        parsed = _run_shipped_parser(tmp_path, text)
        assert rp.classify_parser_output(parsed).outcome == expected
        if expected == rp.OUTCOME_VALID_PASS:
            assert parsed["RESULT"] == "PASS"
        else:
            assert parsed["RESULT"] != "PASS"


_PROTOCOL_MARKER = "review_protocol_outcome() {"


def _run_shipped_protocol(tmp_path: Path, response: str) -> dict[str, str]:
    script = WRAPPER.read_text()
    start = script.index(_PROTOCOL_MARKER)
    end = script.index("\n}", start)
    fn_text = script[start:end + 2]
    review_file = tmp_path / "protocol-review.txt"
    review_file.write_text(response, encoding="utf-8")
    driver = tmp_path / "protocol-driver.sh"
    driver.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        f"REPO={shlex.quote(str(REPO))}\n"
        + fn_text + "\n"
        + "review_protocol_outcome " + shlex.quote(str(review_file)) + "\n",
        encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(driver)], capture_output=True, text=True,
        env={**os.environ, "PATH": os.environ["PATH"]})
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    return {
        key: value for key, sep, value in (
            line.partition("=") for line in proc.stdout.splitlines())
        if sep
    }


def test_shipped_wrapper_protocol_outcome(tmp_path: Path) -> None:
    assert _run_shipped_protocol(tmp_path, CANONICAL_PASS)["OUTCOME"] \
        == rp.OUTCOME_VALID_PASS
    assert _run_shipped_protocol(tmp_path, PASS_WITH_NOTES)["OUTCOME"] \
        == rp.OUTCOME_VALID_PASS
    assert _run_shipped_protocol(tmp_path, BRAND_REJECT)["OUTCOME"] \
        == rp.OUTCOME_VALID_REJECT
    assert _run_shipped_protocol(tmp_path, PASS_WITH_BLOCKER)["OUTCOME"] \
        == rp.OUTCOME_INVALID
    assert _run_shipped_protocol(tmp_path, MALFORMED)["OUTCOME"] \
        == rp.OUTCOME_INVALID
