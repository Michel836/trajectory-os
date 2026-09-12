"""V1.85 — deterministic read-only run registry.

Derives a canonical, schema-versioned, machine-readable view of existing
``.trajectory-pi/runs/<run-id>`` evidence directories.

Contracts:

* The run directory name (run ID) is the canonical identity — no new mutable
  identity is introduced and nothing is ever written here.
* State is derived ONLY from persisted evidence. Nothing is invented: an
  absent fact is ``None`` and is always listed in ``evidence_missing``.
* Incomplete or malformed evidence is represented explicitly
  (``incomplete`` / ``invalid``) and can never be classified ``ready``.
* Ordering is deterministic (newest run ID first; run IDs are timestamp
  shaped strings recorded by the producer).
* Inspection is strictly read-only: no file is created, modified, or
  deleted, and no Git command is executed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from trajectory_os.runs import model

RUN_ID_RE = re.compile(r"^\d{8}-\d{6}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# Small deterministic parsing helpers (all fail closed, never raise).
# ---------------------------------------------------------------------------


def parse_kv_file(text: str) -> tuple[dict[str, str], list[str]]:
    """Parse a producer ``key=value`` file.

    Last occurrence of a key wins (the producer appends lifecycle updates).
    Malformed lines (no ``=``) are collected as problems, never silently
    dropped and never fatal to the rest of the file.
    """
    values: dict[str, str] = {}
    problems: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            problems.append("malformed_line")
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            problems.append("malformed_line")
            continue
        values[key] = value.strip()
    return values, problems


def parse_iso_timestamp(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp; returns None when unusable (fail closed)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def is_valid_sha256(value: str | None) -> bool:
    if value is None:
        return False
    return bool(SHA256_RE.fullmatch(value))


def read_text_optional(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def first_token(text: str | None) -> str | None:
    if text is None:
        return None
    for line in text.splitlines():
        token = line.strip()
        if token:
            return token
    return None


def int_or_none(value: str | None) -> int | None:
    if not value:
        return None
    if re.fullmatch(r"-?\d+", value.strip()) is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Derived model (V1.85 + V1.86 share this canonical per-run model)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepairInfo:
    """Bounded repair evidence (V1.84 producer contract)."""

    requested: bool
    used_attempts: int | None
    convergence: str | None


@dataclass(frozen=True)
class OwnershipEvidence:
    """Live-ownership evidence for a started-not-ended run.

    ``live`` is a read-only /proc observation; it is never a PID-only
    decision: when a workspace was recorded it must also match the live
    process cwd, otherwise the code is unproven/stale (fail closed).
    """

    recorded_pid: int | None
    recorded_workspace: str | None
    live: bool | None
    code: str | None  # model.OWNERSHIP_* or None


@dataclass
class RunView:
    """Canonical per-run view derived from persisted evidence only."""

    run_id: str
    run_dir: str
    lifecycle: str
    state: str
    state_reasons: tuple[str, ...] = ()
    evidence_missing: tuple[str, ...] = ()
    evidence_problems: tuple[str, ...] = ()
    run_class: str | None = None
    branch: str | None = None
    workspace: str | None = None
    model: str | None = None
    head_before: str | None = None
    head_after: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_seconds: int | None = None
    pi_exit_code: int | None = None
    agent_classification: str | None = None
    readiness: str | None = None
    readiness_reason: str | None = None
    validation: str | None = None
    review_status: str | None = None
    final_verify_status: str | None = None
    patch_sha256: str | None = None
    patch_consistent: bool | None = None
    repaired: RepairInfo = field(default_factory=lambda: RepairInfo(False, None, None))
    ownership: OwnershipEvidence = field(
        default_factory=lambda: OwnershipEvidence(None, None, None, None)
    )

    @property
    def evidence_complete(self) -> bool:
        return self.evidence_missing == () and self.evidence_problems == ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_dir": self.run_dir,
            "lifecycle": self.lifecycle,
            "state": self.state,
            "state_reasons": list(self.state_reasons),
            "evidence_missing": list(self.evidence_missing),
            "evidence_problems": list(self.evidence_problems),
            "run_class": self.run_class,
            "branch": self.branch,
            "workspace": self.workspace,
            "model": self.model,
            "head_before": self.head_before,
            "head_after": self.head_after,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds,
            "pi_exit_code": self.pi_exit_code,
            "agent_classification": self.agent_classification,
            "readiness": self.readiness,
            "readiness_reason": self.readiness_reason,
            "validation": self.validation,
            "review_status": self.review_status,
            "final_verify_status": self.final_verify_status,
            "patch_sha256": self.patch_sha256,
            "patch_consistent": self.patch_consistent,
            "repaired": {
                "requested": self.repaired.requested,
                "used_attempts": self.repaired.used_attempts,
                "convergence": self.repaired.convergence,
            },
            "ownership": {
                "recorded_pid": self.ownership.recorded_pid,
                "recorded_workspace": self.ownership.recorded_workspace,
                "live": self.ownership.live,
                "code": self.ownership.code,
            },
        }


# ---------------------------------------------------------------------------
# Evidence extraction
# ---------------------------------------------------------------------------


def repair_info(run_dir: Path) -> RepairInfo:
    text = read_text_optional(run_dir / "repair-summary.txt")
    if text is not None:
        values, _ = parse_kv_file(text)
        convergence = values.get("convergence")
        used: int | None = None
        if convergence is not None:
            if "green on first pass" in convergence or "repaired on first pass" in convergence:
                used = 0
            else:
                match = re.search(r"(\d+) of \d+ attempt", convergence)
                if match is not None:
                    used = int(match.group(1))
        return RepairInfo(requested=True, used_attempts=used, convergence=convergence)
    repair_root = run_dir / "repair"
    if repair_root.is_dir():
        try:
            attempt_dirs = sorted(p for p in repair_root.iterdir() if p.is_dir())
        except OSError:
            attempt_dirs = []
        return RepairInfo(requested=True, used_attempts=len(attempt_dirs), convergence=None)
    return RepairInfo(requested=False, used_attempts=None, convergence=None)


def observe_live(pid: int | None) -> bool:
    """Read-only liveness observation for a recorded pid (False = unusable)."""
    if pid is None or pid <= 0:
        return False
    try:
        import trajectory_os.runtime_control as rc

        return rc.is_pid_alive(pid) and not rc.process_zombie(pid)
    except Exception:  # pragma: no cover - defensive: /proc unavailable
        return False


def ownership_evidence(pid: int | None, workspace: str | None) -> OwnershipEvidence:
    """Fail-closed ownership classification (never PID-only)."""
    live = observe_live(pid)
    if not live or pid is None:
        return OwnershipEvidence(pid, workspace, False, model.OWNERSHIP_STALE)
    if workspace is None:
        return OwnershipEvidence(pid, workspace, True, model.OWNERSHIP_UNPROVEN)
    try:
        import trajectory_os.runtime_control as rc

        snapshot = rc.snapshot_process(pid)
    except Exception:  # pragma: no cover - defensive
        return OwnershipEvidence(pid, workspace, True, model.OWNERSHIP_UNPROVEN)
    if snapshot.cwd is None:
        return OwnershipEvidence(pid, workspace, True, model.OWNERSHIP_UNPROVEN)
    if Path(snapshot.cwd).resolve() != Path(workspace).resolve():
        return OwnershipEvidence(pid, workspace, True, model.OWNERSHIP_STALE)
    return OwnershipEvidence(pid, workspace, True, model.OWNERSHIP_PROVEN)


# ---------------------------------------------------------------------------
# Gate evaluation (deterministic, canonical gate order).
# ---------------------------------------------------------------------------


def classify_gates(
    run_dir: Path, meta: dict[str, str], problems: list[str]
) -> tuple[list[str], list[str], list[str], dict[str, str | None], bool | None, str | None]:
    """Evaluate the deterministic gates.

    Returns ``(missing, fails, malformed, gate_values, patch_consistent,
    chosen_patch_sha)``.  Gate evaluation follows the canonical
    ``model.ALL_GATES`` order so every downstream output is deterministic.
    """
    missing: list[str] = []
    fails: list[str] = []
    malformed: list[str] = []
    values: dict[str, str | None] = {}

    def check_gate(
        name: str,
        value: str | None,
        *,
        waived: bool = False,
        recognized: frozenset[str] | None = None,
    ) -> None:
        allowed = recognized if recognized is not None else model.RECOGNIZED_GATE_VALUES
        if value is None:
            if not waived:
                missing.append(name)
            return
        if value not in allowed:
            malformed.append(name)
            return
        if not waived and value != model.GATE_PASS:
            fails.append(name)

    # --- validation ---------------------------------------------------------
    val = meta.get("validation") or None
    validation_malformed_doc = False
    if val is None:
        text = read_text_optional(run_dir / "validation.json")
        if text is not None:
            try:
                doc = json.loads(text)
                if isinstance(doc, dict) and isinstance(doc.get("overall_status"), str):
                    val = doc["overall_status"]
            except json.JSONDecodeError:
                # Document present but undecodable: malformed evidence,
                # not fabricatable absence.
                problems.append("validation.json_malformed")
                malformed.append("validation")
                validation_malformed_doc = True
    values["validation"] = val
    if not validation_malformed_doc:
        check_gate("validation", val)

    # --- review ---------------------------------------------------------------
    rev = meta.get("review_status") or None
    if rev is None:
        file_values, _file_problems = parse_kv_file(
            read_text_optional(run_dir / "review-meta.txt") or ""
        )
        rev = file_values.get("review_status")
    review_waived = meta.get("review_enabled") == "0"
    values["review"] = rev if rev is not None or not review_waived else "DISABLED"
    check_gate("review", values["review"], waived=review_waived)

    # --- final verification ---------------------------------------------------
    fv = meta.get("final_verify_status") or None
    if fv is None:
        file_values, _file_problems = parse_kv_file(
            read_text_optional(run_dir / "final-verify-meta.txt") or ""
        )
        fv = file_values.get("final_verify_status")
    values["final_verify"] = fv
    check_gate("final_verify", fv)

    # --- patch identity --------------------------------------------------------
    shas: list[str] = []
    sha_file = first_token(read_text_optional(run_dir / "worktree.patch.sha256"))
    if sha_file is not None:
        shas.append(sha_file)
    meta_values, _meta_problems = parse_kv_file(
        read_text_optional(run_dir / "worktree-metadata.txt") or ""
    )
    if meta_values.get("patch_sha256"):
        shas.append(meta_values["patch_sha256"])
    fv_values, _fv_problems = parse_kv_file(
        read_text_optional(run_dir / "final-verify-meta.txt") or ""
    )
    for key in ("canonical_patch_sha256", "final_patch_sha256"):
        if fv_values.get(key):
            shas.append(fv_values[key])
    if meta.get("final_patch_sha256"):
        shas.append(meta["final_patch_sha256"])

    valid_shas = [s for s in shas if is_valid_sha256(s)]
    invalid_shas = [s for s in shas if not is_valid_sha256(s)]
    consistency: bool | None
    if not shas:
        missing.append("patch_identity")
        consistency = None
    elif invalid_shas or len(set(valid_shas)) > 1:
        problems.append("patch_identity_mismatch")
        malformed.append("patch_identity")
        consistency = False
    else:
        consistency = len(valid_shas) >= 2
    values["patch_identity"] = None
    return missing, fails, malformed, values, consistency, (valid_shas[0] if valid_shas else None)


def snapshot_and_diff(
    run_dir: Path, meta: dict[str, str]
) -> tuple[list[str], list[str], list[str]]:
    """Snapshot completeness and diff-check gates (deterministic order)."""
    missing: list[str] = []
    fails: list[str] = []
    malformed: list[str] = []

    snap = meta.get("snapshot_status") or None
    if snap is None:
        values, _ = parse_kv_file(read_text_optional(run_dir / "worktree-metadata.txt") or "")
        snap = values.get("snapshot_status")
    if snap is None:
        missing.append("snapshot")
    elif snap not in model.RECOGNIZED_SNAPSHOT_VALUES:
        malformed.append("snapshot")
    elif snap != "COMPLETE":
        fails.append("snapshot")

    diff = meta.get("diff_check") or None
    if diff is None:
        text = read_text_optional(run_dir / "diff-check.txt")
        if text is not None:
            if "DIFF CHECK: PASS" in text:
                diff = "PASS"
            elif "DIFF CHECK: FAIL" in text:
                diff = "FAIL"
    if diff is None:
        missing.append("diff_check")
    elif diff not in model.RECOGNIZED_GATE_VALUES:
        malformed.append("diff_check")
    elif diff != "PASS":
        fails.append("diff_check")
    return missing, fails, malformed


# ---------------------------------------------------------------------------
# Run derivation
# ---------------------------------------------------------------------------


def derive_run(run_dir: Path) -> RunView:
    """Derive the canonical RunView for one run directory (read-only)."""
    problems: list[str] = []

    meta_text = read_text_optional(run_dir / "meta.txt")
    meta: dict[str, str] = {}
    if meta_text is None:
        problems.append("meta_missing")
    else:
        meta, meta_problems = parse_kv_file(meta_text)
        problems.extend(p for p in meta_problems if p not in problems)

    started_at = meta.get("started_at")
    ended_at = meta.get("ended_at")
    meta_run_id = meta.get("run_id")
    if meta_run_id is not None and meta_run_id != run_dir.name:
        # Report, never invent: the directory name remains authoritative and
        # the conflict is surfaced for explicit handling downstream.
        problems.append("meta_run_id_conflicts")
    if started_at is not None and started_at and parse_iso_timestamp(started_at) is None:
        problems.append("started_at_malformed")
    if ended_at is not None and ended_at and parse_iso_timestamp(ended_at) is None:
        problems.append("ended_at_malformed")

    raw_pid = meta.get("pid")
    recorded_pid = int_or_none(raw_pid)
    if raw_pid is not None and recorded_pid is None:
        problems.append("meta_pid_malformed")
    workspace = meta.get("workspace") or None

    if ended_at:
        lifecycle = model.LIFECYCLE_ENDED
        ownership = OwnershipEvidence(recorded_pid, workspace, None, None)
    elif started_at:
        ownership = ownership_evidence(recorded_pid, workspace)
        lifecycle = (
            model.LIFECYCLE_ACTIVE
            if ownership.code == model.OWNERSHIP_PROVEN
            else model.LIFECYCLE_UNKNOWN
        )
    else:
        lifecycle = model.LIFECYCLE_UNKNOWN
        ownership = OwnershipEvidence(recorded_pid, workspace, None, None)

    duration: int | None = None
    started_dt = parse_iso_timestamp(started_at)
    ended_dt = parse_iso_timestamp(ended_at)
    if started_dt is not None and ended_dt is not None:
        if started_dt.tzinfo is not None and ended_dt.tzinfo is None:
            ended_dt = ended_dt.replace(tzinfo=started_dt.tzinfo)
        if started_dt.tzinfo is None and ended_dt.tzinfo is not None:
            started_dt = started_dt.replace(tzinfo=ended_dt.tzinfo)
        if started_dt.tzinfo is not None and ended_dt.tzinfo is not None:
            duration = int((ended_dt - started_dt).total_seconds())

    gate_missing, gate_fails, gate_malformed, gate_values, consistent, chosen_sha = (
        classify_gates(run_dir, meta, problems)
    )
    missing: list[str] = list(gate_missing)
    if meta_text is None:
        missing.append("meta.txt")
    snap_missing, snap_fails, snap_malformed = snapshot_and_diff(run_dir, meta)
    missing.extend(snap_missing)
    fails = gate_fails + snap_fails
    malformed = gate_malformed + snap_malformed

    reasons: list[str] = []
    if lifecycle == model.LIFECYCLE_ACTIVE:
        state = model.STATE_IN_PROGRESS
    elif lifecycle == model.LIFECYCLE_UNKNOWN:
        # Started/not-ended but ownership cannot be proven either way:
        # explicit incomplete state, never ready.
        state = model.STATE_INCOMPLETE
        reasons.append("LIFECYCLE_UNPROVEN")
    elif malformed:
        state = model.STATE_INVALID
    elif missing:
        state = model.STATE_INCOMPLETE
    elif fails:
        state = model.STATE_FAILED
    else:
        state = model.STATE_READY

    for gate in sorted(set(malformed)):
        reasons.append(f"MALFORMED_{gate.upper()}")
    if meta_text is None:
        reasons.append("MISSING_META.TXT")
    for gate in sorted(set(gate_missing) | set(snap_missing)):
        reasons.append(f"MISSING_{gate.upper()}")
    for gate in sorted(set(fails)):
        reasons.append(f"GATE_FAIL_{gate.upper()}")
    for problem in sorted(set(problems)):
        reasons.append(f"PROBLEM_{problem.upper()}")

    return RunView(
        run_id=run_dir.name,
        run_dir=run_dir.name,
        lifecycle=lifecycle,
        state=state,
        state_reasons=tuple(sorted(set(reasons))),
        evidence_missing=tuple(sorted(set(missing))),
        evidence_problems=tuple(sorted(set(problems))),
        run_class=meta.get("run_class") or None,
        branch=meta.get("branch") or None,
        workspace=workspace,
        model=meta.get("model") or None,
        head_before=meta.get("head_before") or None,
        head_after=meta.get("head_after") or None,
        started_at=started_at or None,
        ended_at=ended_at or None,
        duration_seconds=duration,
        pi_exit_code=int_or_none(meta.get("pi_exit_code")),
        agent_classification=meta.get("agent_classification") or None,
        readiness=meta.get("repository_readiness") or None,
        readiness_reason=meta.get("readiness_reason") or None,
        validation=gate_values.get("validation"),
        review_status=gate_values.get("review"),
        final_verify_status=gate_values.get("final_verify"),
        patch_sha256=chosen_sha,
        patch_consistent=consistent,
        repaired=repair_info(run_dir),
        ownership=ownership,
    )


# ---------------------------------------------------------------------------
# Registry API (V1.85)
# ---------------------------------------------------------------------------


def list_run_dirs(runs_root: Path) -> list[Path]:
    """Deterministic list of run directories under ``runs_root`` (read-only).

    Directories whose name does not match the producer run-id shape are not
    runs and are skipped; non-directory entries are ignored entirely.
    """
    if not runs_root.is_dir():
        return []
    out: list[Path] = []
    try:
        for entry in runs_root.iterdir():
            if entry.is_dir() and RUN_ID_RE.fullmatch(entry.name):
                out.append(entry)
    except OSError:
        return []
    out.sort(key=lambda path: path.name)
    return out


def load_runs(runs_root: Path) -> list[RunView]:
    """Load the registry: deterministic newest-first ordering (read-only)."""
    runs = [derive_run(path) for path in list_run_dirs(runs_root)]
    runs.sort(key=lambda run: run.run_id, reverse=True)
    return runs


def registry_payload(runs_root: Path) -> dict[str, Any]:
    """Stable machine-readable registry envelope (schema-versioned)."""
    runs = load_runs(runs_root)
    return {
        "schema_version": model.SCHEMA_VERSION,
        "tool": model.CLI_NAME,
        "source": "trajectory_p_runs_evidence",
        "run_count": len(runs),
        "order": "newest_first",
        "runs": [run.to_dict() for run in runs],
    }
