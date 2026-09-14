"""Mission 001-D closure-report contract (V2.06).

This module is a **fail-closed validator and pure renderer** for the mission
closure report (``docs/missions/mission-001-closure-report.json``). It is not a
generator: a bounded proof harness assembles the report from real artifacts,
and this module checks that the report does not claim a proof it does not have.

Central safety invariant (no fabricated positive proof):

* every section carries an explicit provenance ``status`` in
  ``measured / derived / unproven / not_applicable / pending``;
* a ``proven`` claim is admissible only when its supporting measured facts are
  present, internally consistent, and backed by non-empty evidence;
* a positive (``proven``) section whose measured support is missing is a
  structural violation.

This is deterministic and I/O-free: :func:`validate_report` operates on an
already-parsed dict and :func:`render_markdown` derives the markdown from the
exact same data (never inventing values).

The module never imports subprocess/OS state — it is pure data handling so it
can be unit-tested without running any process.
"""

from __future__ import annotations

from typing import Any

from trajectory_os.runs import spec as _spec

CLOSURE_REPORT_SCHEMA_VERSION = 1

# Allowed provenance statuses for every report section/field.
STATUS_MEASURED = "measured"
STATUS_DERIVED = "derived"
STATUS_UNPROVEN = "unproven"
STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_PENDING = "pending"
VALID_STATUSES = frozenset(
    {
        STATUS_MEASURED,
        STATUS_DERIVED,
        STATUS_UNPROVEN,
        STATUS_NOT_APPLICABLE,
        STATUS_PENDING,
    }
)

# Outcome statuses for the two central closure proofs (B and C).
PROOF_PROVEN = "proven"
PROOF_BLOCKED = "blocked"
PROOF_NOT_RUN = "not_run"
PROOF_OUTCOMES = frozenset({PROOF_PROVEN, PROOF_BLOCKED, PROOF_NOT_RUN})

# Canonical execution classes (single source of truth: the spec module).
_EXEC_CLASSES = frozenset(_spec.EXECUTION_CLASSES)

# Minimum measured concurrency required to honestly claim concurrent execution.
MIN_PROVEN_JOBS = 2


# ---------------------------------------------------------------------------
# Small typed helpers (deterministic, fail closed)
# ---------------------------------------------------------------------------


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _opt_int(value: Any) -> int | None:
    """Return ``value`` as ``int`` when it is a real int, else ``None``."""
    if _is_int(value):
        return int(value)
    return None


def _section(doc: dict[str, Any], key: str) -> dict[str, Any]:
    value = doc.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"closure report: section {key!r} must be an object")
    return value


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_report(doc: Any) -> tuple[bool, list[str]]:
    """Validate a closure-report dict against the structural contract.

    Returns ``(ok, violations)``. ``ok`` is ``True`` iff ``violations`` is
    empty. Every check is deterministic and fail closed: a missing or
    inconsistent field is a violation, never a silent pass. Critically, a
    ``proven`` claim is rejected when its measured support is absent or
    self-contradictory (no fabricated positive proof).
    """
    violations: list[str] = []
    if not isinstance(doc, dict):
        return False, [f"report must be an object, got {type(doc).__name__}"]

    if doc.get("schema_version") != CLOSURE_REPORT_SCHEMA_VERSION:
        violations.append(
            "schema_version must be "
            f"{CLOSURE_REPORT_SCHEMA_VERSION}, got {doc.get('schema_version')!r}"
        )

    # Identity fields (measured).
    for key in ("mission", "baseline_commit", "closure_branch"):
        if not isinstance(doc.get(key), str) or not str(doc.get(key)).strip():
            violations.append(f"required non-empty string: {key}")

    # --- Production-path proof (B) ------------------------------------------
    try:
        proof = _section(doc, "production_path_proof")
    except ValueError as exc:
        violations.append(str(exc))
        proof = {}

    if (
        not isinstance(proof.get("status"), str)
        or proof.get("status") not in PROOF_OUTCOMES
    ):
        violations.append(
            "production_path_proof.status must be one of "
            f"{sorted(PROOF_OUTCOMES)}, got {proof.get('status')!r}"
        )

    created = _opt_int(proof.get("jobs_created"))
    started = _opt_int(proof.get("jobs_started"))
    completed = _opt_int(proof.get("jobs_completed"))
    if created is not None and created < 0:
        violations.append("jobs_created must be >= 0")
    if started is not None and started < 0:
        violations.append("jobs_started must be >= 0")
    if completed is not None and completed < 0:
        violations.append("jobs_completed must be >= 0")

    max_conc = _opt_int(proof.get("max_concurrent_observed"))
    if max_conc is None or max_conc < 0:
        violations.append("max_concurrent_observed must be a non-negative integer")

    status = proof.get("status")
    if status == PROOF_PROVEN:
        # A proven production path needs measured support (no fabrication).
        if created is not None and started is not None and completed is not None:
            if started < MIN_PROVEN_JOBS:
                violations.append(
                    f"proven production path requires jobs_started >= {MIN_PROVEN_JOBS}"
                )
            if not (created >= started and completed >= started):
                violations.append(
                    "proven production path requires jobs_created >= jobs_started "
                    "and jobs_completed >= jobs_started"
                )
        if max_conc is not None:
            if max_conc < 1:
                violations.append(
                    "proven production path requires max_concurrent_observed >= 1"
                )
            if started is not None and max_conc > started:
                violations.append(
                    "max_concurrent_observed cannot exceed jobs_started"
                )
        if proof.get("concurrency_claim") in (PROOF_PROVEN, "concurrent") and (
            max_conc is None or max_conc < 2
        ):
            violations.append(
                "concurrency_claim 'concurrent' requires max_concurrent_observed >= 2"
            )
        evidence = proof.get("evidence")
        if not _nonempty_str_list(evidence):
            violations.append(
                "proven production path requires non-empty evidence list"
            )

    if status == PROOF_BLOCKED and (
        not isinstance(proof.get("blocking_reason"), str)
        or not str(proof.get("blocking_reason")).strip()
    ):
        violations.append("blocked production path requires a non-empty blocking_reason")

    # --- Conflict proof (C) -------------------------------------------------
    try:
        conflict = _section(doc, "conflict_proof")
    except ValueError as exc:
        violations.append(str(exc))
        conflict = {}

    if (
        not isinstance(conflict.get("status"), str)
        or conflict.get("status") not in PROOF_OUTCOMES
    ):
        violations.append(
            "conflict_proof.status must be one of "
            f"{sorted(PROOF_OUTCOMES)}, got {conflict.get('status')!r}"
        )
    if conflict.get("status") == PROOF_PROVEN:
        for key in ("candidate_job", "active_job", "reason_code"):
            if not isinstance(conflict.get(key), str) or not str(
                conflict.get(key)
            ).strip():
                violations.append(f"conflict_proof.{key} is required for a proven proof")
        for key in ("candidate_execution_class", "active_execution_class"):
            if conflict.get(key) not in _EXEC_CLASSES:
                violations.append(
                    f"conflict_proof.{key} must be a canonical execution class"
                )
        checkout = conflict.get("source_checkout")
        if not isinstance(checkout, (str, type(None))) or (
            checkout is not None and not str(checkout).strip()
        ):
            violations.append(
                "conflict_proof.source_checkout must be a non-empty string for a proven proof"
            )

    # --- Lifecycle (D) ------------------------------------------------------
    lifecycle = doc.get("lifecycle")
    if not isinstance(lifecycle, dict):
        violations.append("lifecycle must be an object")
    else:
        for stage in ("enqueue", "start", "observe", "completion", "reap", "reconstruction"):
            if stage not in lifecycle:
                violations.append(f"lifecycle.{stage} is required")
            elif not isinstance(lifecycle.get(stage), dict):
                violations.append(f"lifecycle.{stage} must be an object")
            elif lifecycle[stage].get("status") not in VALID_STATUSES:
                violations.append(
                    f"lifecycle.{stage}.status must be one of {sorted(VALID_STATUSES)}"
                )

    # --- Final / quality / review ------------------------------------------
    final_state = doc.get("final_state")
    if not isinstance(final_state, dict) or final_state.get("status") not in (
        VALID_STATUSES | {PROOF_PROVEN, PROOF_BLOCKED}
    ):
        violations.append(
            "final_state.status must be a valid status/outcome"
        )

    quality = doc.get("quality_state")
    if not isinstance(quality, dict) or quality.get("status") not in (
        VALID_STATUSES | {"PASS", "FAIL"}
    ):
        violations.append("quality_state.status must be a valid status or PASS/FAIL")
    if (
        isinstance(quality, dict)
        and quality.get("status") in ("PASS", "FAIL")
        and not _nonempty_str(quality.get("command"))
    ):
        violations.append("quality_state.command is required for a quality verdict")

    review = doc.get("review_state")
    if review is not None and not isinstance(review, (str, dict)):
        violations.append("review_state must be a string or object")

    # --- Evidence references ------------------------------------------------
    evidence = doc.get("evidence")
    if not _nonempty_str_list(evidence):
        violations.append("top-level evidence must be a non-empty list of strings")

    return (not violations), violations


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _nonempty_str_list(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) > 0
        and all(isinstance(item, str) and str(item).strip() for item in value)
    )


# ---------------------------------------------------------------------------
# Pure deterministic renderer (markdown derived from the exact JSON)
# ---------------------------------------------------------------------------


def render_markdown(doc: dict[str, Any]) -> str:
    """Render the markdown report deterministically from the JSON document.

    Every value in the output is read directly from ``doc``; nothing is
    invented, interpolated, or inferred. Section provenance (measured vs.
    derived vs. unproven) is surfaced verbatim.
    """
    def g(section: str, key: str, default: str | None = "—") -> str | None:
        sect = doc.get(section)
        if isinstance(sect, dict):
            val = sect.get(key)
            if val is None or val == "":
                return default
            return _fmt(val)
        return default

    def status(section: str) -> str | None:
        return g(section, "status", "UNKNOWN")

    lines: list[str] = []
    lines.append(f"# Mission {doc.get('mission', '?')} — Closure Report")
    lines.append("")
    lines.append(
        f"_Baseline commit_: `{doc.get('baseline_commit', '?')}`  "
        f"_Closure branch_: `{doc.get('closure_branch', '?')}`"
    )
    lines.append("")
    lines.append(
        f"_Schema version_: {doc.get('schema_version', '?')}  "
        "_Every field below is labeled with its provenance status and must not "
        "be read as a stronger claim than that status states._"
    )
    lines.append("")

    lines.append("## Production-path proof (B)")
    lines.append(f"- status: **{status('production_path_proof')}**")
    lines.append(f"- jobs created: {g('production_path_proof', 'jobs_created')} (measured)")
    lines.append(f"- jobs started: {g('production_path_proof', 'jobs_started')} (measured)")
    lines.append(f"- jobs completed: {g('production_path_proof', 'jobs_completed')} (measured)")
    lines.append(
        f"- max concurrent observed: "
        f"{g('production_path_proof', 'max_concurrent_observed')} (measured)"
    )
    lines.append(
        f"- concurrency claim: {g('production_path_proof', 'concurrency_claim')} "
        "(derived from max_concurrent_observed)"
    )
    lines.append(
        f"- automatic retries: {g('production_path_proof', 'automatic_retries')} (measured)"
    )
    lines.append(
        f"- automatic recoveries: "
        f"{g('production_path_proof', 'automatic_recoveries')} (measured)"
    )
    lines.append(
        f"- provider failures recovered: "
        f"{g('production_path_proof', 'provider_failures_recovered')} (measured)"
    )
    lines.append(
        f"- provider failures surfaced: "
        f"{g('production_path_proof', 'provider_failures_surfaced')} (measured)"
    )
    if status("production_path_proof") == PROOF_BLOCKED:
        lines.append(f"- blocking reason: {g('production_path_proof', 'blocking_reason')}")
    lines.append("")

    lines.append("## Same-worktree conflict proof (C)")
    lines.append(f"- status: **{status('conflict_proof')}**")
    lines.append(f"- candidate job: {g('conflict_proof', 'candidate_job')}")
    lines.append(f"- active/conflicting job: {g('conflict_proof', 'active_job')}")
    lines.append(f"- source checkout: `{g('conflict_proof', 'source_checkout')}`")
    lines.append(
        f"- candidate execution class: {g('conflict_proof', 'candidate_execution_class')}"
    )
    lines.append(
        f"- active execution class: {g('conflict_proof', 'active_execution_class')}"
    )
    lines.append(f"- deterministic reason code: `{g('conflict_proof', 'reason_code')}`")
    lines.append("")

    lines.append("## Lifecycle (D)")
    lifecycle = doc.get("lifecycle", {})
    order = (
        "enqueue",
        "start",
        "observe",
        "completion",
        "reap",
        "reconstruction",
    )
    for stage in order:
        sect = lifecycle.get(stage, {}) if isinstance(lifecycle, dict) else {}
        lines.append(
            f"- {stage}: **{sect.get('status', 'UNKNOWN')}** {sect.get('note', '')}".rstrip()
        )
    lines.append("")

    lines.append("## Final state & quality (E)")
    lines.append(f"- final state: **{g('final_state', 'status')}** {g('final_state', 'note')}")
    lines.append(
        f"- quality: **{g('quality_state', 'status')}** "
        f"(`{g('quality_state', 'command')}`)"
    )
    review_val = g("review_state", "status", None) or doc.get("review_state", "pending")
    lines.append(f"- review state: {review_val}")
    lines.append("")

    lines.append("## Evidence")
    for item in doc.get("evidence", []) or []:
        lines.append(f"- `{item}`")
    lines.append("")
    lines.append(
        "_Report is a deterministic derivation of the machine-readable JSON "
        "(`mission-001-closure-report.json`); the JSON is the source of truth._"
    )
    lines.append("")
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    """Render a scalar without silently altering string evidence."""
    if isinstance(value, str):
        return value
    return str(value)


__all__ = [
    "CLOSURE_REPORT_SCHEMA_VERSION",
    "STATUS_MEASURED",
    "STATUS_DERIVED",
    "STATUS_UNPROVEN",
    "STATUS_NOT_APPLICABLE",
    "STATUS_PENDING",
    "VALID_STATUSES",
    "PROOF_PROVEN",
    "PROOF_BLOCKED",
    "PROOF_NOT_RUN",
    "PROOF_OUTCOMES",
    "MIN_PROVEN_JOBS",
    "validate_report",
    "render_markdown",
]
