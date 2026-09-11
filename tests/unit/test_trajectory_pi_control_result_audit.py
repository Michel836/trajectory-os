"""Unit tests for the V1.78 result envelope and V1.79 audit trail primitives.

Exercised directly against ``trajectory_os.runtime_control`` (no subprocess),
so schema stability, sanitisation, bounds and failure semantics are pinned
independently of the CLI surface.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from trajectory_os import runtime_control as rc

RESULT_FIELD_ORDER = (
    "schema",
    "command",
    "run_id",
    "outcome",
    "action_requested",
    "action_performed",
    "reasons",
    "target_state",
    "targetable",
    "stoppable",
    "target_pid",
    "signal_requested",
    "signal_sent",
    "lock",
    "grace_seconds",
    "identity",
    "lifecycle",
    "audit",
    "generated_at",
    "evidence_source",
)


# ------------------------------------------------------------------ V1.78


def test_result_envelope_has_a_fixed_ordered_field_set() -> None:
    result = rc.build_result(command="stop", outcome="REJECTED")
    list(result.keys())[0]
    assert tuple(result.keys()) == RESULT_FIELD_ORDER, "fixed, ordered field set"
    assert result["schema"] == "trajectory-pi-control-result/1"
    assert result["command"] == "stop"
    assert result["outcome"] == "REJECTED"
    # Unknown facts stay null — present, typed, never omitted or invented.
    assert result["run_id"] is None
    assert result["action_requested"] is None
    assert result["action_performed"] is None
    assert result["target_state"] is None
    assert result["targetable"] is None
    assert result["target_pid"] is None
    assert result["identity"] is None
    assert result["lifecycle"] is None
    assert result["signal_requested"] is None
    assert result["signal_sent"] is None
    assert result["lock"] is None
    assert result["grace_seconds"] is None
    assert result["audit"] is None
    assert result["evidence_source"] == "run-directory"  # core default
    assert result["generated_at"] == "unknown"
    assert result["reasons"] == []


def test_result_coerces_types_and_keeps_stability() -> None:
    result = rc.build_result(
        command="stop",
        outcome="GRACE_TIMEOUT",
        reasons=[123, "grace-timeout:5.0s"],
        action_requested="stop",
        action_performed="sigterm-sent",
        target_state="stale",
        targetable=False,
        target_pid=987654,
        identity={"state": "pid-and-workspace-match", "reasons": ["pid-live"]},
        lifecycle={"state": "stale", "reasons": ["stale"], "ended_at": None},
        signal_requested="SIGTERM",
        signal_sent=True,
        lock="acquired",
        grace_seconds=5,
        generated_at="2026-01-01T00:00:00",
        evidence_source="run-directory",
    )
    assert result["reasons"] == ["123", "grace-timeout:5.0s"]
    assert result["target_pid"] == 987654
    assert result["action_performed"] == "sigterm-sent"
    assert result["grace_seconds"] == 5
    assert result["identity"]["state"] == "pid-and-workspace-match"
    # Unknown stays unknown — JSON-serialisable, null preserved.
    text = json.dumps(result)
    assert json.loads(text) == result
    assert result["generated_at"] == "2026-01-01T00:00:00"


# ------------------------------------------------------------------ V1.79


def test_audit_sanitize_drops_secrets_and_unexpected_keys() -> None:
    record = rc.sanitize_audit_record(
        {
            "ts": "2026-01-01T00:00:00",
            "component": "trajectory-pi-control",
            "command": "stop",
            "runs_root": "/tmp/runs",
            "decision": "refused",
            "outcome": "REJECTED",
            "reasons": ["process-dead"],
            "run_id": "20260101-093000",
            "target_pid": 4294967295,
            "identity_state": "pid-and-workspace-match",
            # Must all be dropped — never allowed into the trail:
            "api_key": "sk-live-secret",
            "secret": "hunter2",
            "token": "tok_123",
            "authorization": "Bearer x",
            "password": "pw",
            "session_token": "s",
            "notes": {"arbitrary": {"blob": 1}},  # unexpected type
            "notes_raw": "free-form",
        }
    )
    assert tuple(record.keys()) == (
        "schema",
        "ts",
        "component",
        "runs_root",
        "command",
        "decision",
        "outcome",
        "reasons",
        "run_id",
        "target_pid",
        "identity_state",
    )
    assert record["decision"] == "refused"
    assert record["reasons"] == ["process-dead"]
    dumped = json.dumps(record)
    for secret in ("sk-live-secret", "hunter2", "tok_123", "Bearer x", "pw"):
        assert secret not in dumped


@pytest.mark.parametrize(
    ("key", "bad"),
    [("ts", 123), ("decision", ["refused"]), ("outcome", 4.5), ("grace_seconds", "x")],
)
def test_audit_sanitize_drops_invalid_values(key: str, bad: object) -> None:
    record = rc.sanitize_audit_record({key: bad})
    assert key not in record, f"{key}={bad!r} must be dropped, not guessed"


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("target_pid", "42", 42),
        ("grace_seconds", 5, 5),
        ("signal_sent", False, False),
        ("decision", "refused", "refused"),
        ("reasons", "single", ["single"]),
    ],
)
def test_audit_sanitize_coerces_to_stable_types(key: str, value: object, expected: object) -> None:
    record = rc.sanitize_audit_record({key: value})
    assert record[key] == expected


def test_append_audit_writes_single_line_with_schema_and_mode(tmp_path: Path) -> None:
    status = rc.append_audit_record(tmp_path, {"command": "stop", "run_id": "20260101-093000"})
    assert status == "ok"
    audit = tmp_path / "control-audit.jsonl"
    assert audit.stat().st_mode & 0o777 == 0o600
    lines = audit.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["schema"] == "control-audit/1"
    assert record["command"] == "stop"
    assert record["run_id"] == "20260101-093000"
    # Unknown facts are simply absent — never fabricated (no invented ts).
    assert "ts" not in record


def test_append_audit_is_bounded_to_newest_records(tmp_path: Path) -> None:
    for i in range(rc.AUDIT_MAX_RECORDS + 32):
        rc.append_audit_record(tmp_path, {"command": "stop", "ts": f"t{i:04d}"})
    lines = (tmp_path / "control-audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == rc.AUDIT_MAX_RECORDS
    first = json.loads(lines[0])
    assert first["ts"] == "t0032"  # oldest 32 dropped


def _lines_that_parse(path: Path) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def test_append_audit_tolerates_malformed_historical_lines(tmp_path: Path) -> None:
    audit = tmp_path / "control-audit.jsonl"
    audit.write_text(
        "garbage-line-not-json\n"
        + json.dumps({"schema": "control-audit/1", "ts": "t-keep", "run_id": "keep"})
        + "\n",
        encoding="utf-8",
    )
    status = rc.append_audit_record(tmp_path, {"command": "stop", "ts": "t-new"})
    assert status == "ok"
    parsed = _lines_that_parse(audit)
    assert len(parsed) == 2
    assert [r["ts"] for r in parsed] == ["t-keep", "t-new"]


def test_append_audit_unavailable_when_root_is_unreadable(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    os.chmod(root, 0o000)
    try:
        status = rc.append_audit_record(root, {"command": "stop"})
    finally:
        os.chmod(root, 0o755)
    if os.geteuid() == 0:
        pytest.skip("root bypasses permission bits; cannot simulate failure")
    assert status == "unavailable"
