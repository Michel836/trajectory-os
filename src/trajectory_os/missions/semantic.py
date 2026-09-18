"""Mission 007 — semantic sub-run outcome contract (producer/consumer layer).

Defines the smallest stable, machine-readable structured result that a
sub-run's implementing process (canonical trajectory-pi wrapper) must emit
to prove semantic completion beyond a bare exit-0.

Design invariants:

* deterministic — same inputs yield the same verdict; no I/O in the
  verification path, no clocks, no guessing;
* fail closed — missing, malformed, contradictory, stale, or mismatched
  semantic evidence never supports COMPLETED; it blocks (UNPROVEN) or
  preserves the process-failure classification;
* bound to the exact sub-run — the result MUST carry the exact
  ``subrun_id`` of the sub-run it was produced for, and the consumer
  verifies that binding explicitly; the runner also clears any stale
  file at that path before launch, so a previous attempt's evidence can
  never be mistaken for the current sub-run's proof;
* minimal — one JSON file with a small fixed schema (``schema_version=1``);
  no dependencies beyond the standard library;
* SUCCESS is the ONLY status that can support mission COMPLETED.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: Schema version of the semantic result file. Kept at 1 — forward
#: compatible: a consumer never guesses about versions it doesn't know.
SEMANTIC_SCHEMA_VERSION = 1

#: Environment variable name the runner sets so the subprocess wrapper
#: knows where to write the semantic result (the value is a path).
RESULT_FILE_ENV_VAR = "TRAJECTORY_SUBRUN_RESULT_FILE"

#: Environment variable name the runner sets carrying the EXACT sub-run
#: id this semantic result must be bound to.
SUBRUN_ID_ENV_VAR = "TRAJECTORY_SUBRUN_ID"

# --- canonical semantic statuses -------------------------------------------------

#: The sub-run semantically achieved its objective (the agent completed
#: the work, the wrapper confirmed it via deterministic gates).
STATUS_SUCCESS = "SUCCESS"

#: The sub-run process exited cleanly (0) but did NOT achieve the
#: objective (incomplete, missing completion marker, etc.).
STATUS_INCOMPLETE = "INCOMPLETE"

#: The sub-run failed due to an upstream provider / infrastructure
#: issue (not a deterministic logic failure of the sub-run itself).
STATUS_PROVIDER_FAILURE = "PROVIDER_FAILURE"

#: Deterministic failure (the work was attempted but definitively
#: failed the wrapper's own deterministic gates).
STATUS_FAILED = "FAILED"

#: The status is ambiguous or unknown (unrecognized terminal state).
STATUS_UNKNOWN = "UNKNOWN"

ALL_STATUSES = frozenset({
    STATUS_SUCCESS,
    STATUS_INCOMPLETE,
    STATUS_PROVIDER_FAILURE,
    STATUS_FAILED,
    STATUS_UNKNOWN,
})

#: Only this status is capable of supporting mission COMPLETED.
SUCCESS_STATUSES = frozenset({STATUS_SUCCESS})

#: All string fields are non-empty and bounded (fail closed on runaway).
_MAX_STR_LEN = 512

# ---------------------------------------------------------------------------
# Mission 010 — mandatory writable-mode semantic promotion contract
# ---------------------------------------------------------------------------
# The wrapper terminal classification for a successful writable run is
# ``AGENT_COMPLETED``. That classification alone proves only that the agent
# process exited cleanly; it does NOT prove the repository reached a green
# readiness state. For the writable modes below the producer and the consumer
# both require a deterministically proven readiness before SUCCESS:
#
#   AGENT_COMPLETED does NOT imply SUCCESS by itself
#   READY_FOR_COMMIT / READY_FOR_REVIEW -> SUCCESS
#   NEEDS_REVIEW / BLOCKED               -> FAILED
#   absent / unrecognized readiness      -> UNKNOWN
#
# No new semantic status is introduced and the schema/version stays frozen:
# the promotion only ever selects among the existing statuses.

#: Wrapper classification that signals a clean agent process (not success).
AGENT_COMPLETED = "AGENT_COMPLETED"

#: Modes whose success is gated on a proven green readiness.
WRITABLE_MODES = frozenset({"IMPLEMENT", "REPAIR", "RECOVERY", "SMOKE"})

#: Readiness states that support writable-mode SUCCESS.
READINESS_SUCCESS = frozenset({"READY_FOR_COMMIT", "READY_FOR_REVIEW"})

#: Readiness states that deterministically fail writable-mode promotion.
READINESS_FAILED = frozenset({"NEEDS_REVIEW", "BLOCKED"})

#: Optional Mission 010 provenance for the operator ``--require-changes``
#: contract. Absent on legacy evidence (readable as ``NOT_REQUIRED`` by
#: operators, never re-derived).
REQUIRE_CHANGES_NOT_REQUIRED = "NOT_REQUIRED"
REQUIRE_CHANGES_SATISFIED = "SATISFIED"
REQUIRE_CHANGES_UNSATISFIED = "UNSATISFIED"
REQUIRE_CHANGES_UNPROVEN = "UNPROVEN"

REQUIRE_CHANGES_VALUES = frozenset({
    REQUIRE_CHANGES_NOT_REQUIRED,
    REQUIRE_CHANGES_SATISFIED,
    REQUIRE_CHANGES_UNSATISFIED,
    REQUIRE_CHANGES_UNPROVEN,
})


def promote_writable_status(mode: str | None,
                            status: str | None,
                            agent_classification: str | None,
                            readiness: str | None) -> str | None:
    """Apply the writable-mode promotion contract to a raw status (pure).

    Non-writable modes and non-SUCCESS statuses are returned unchanged
    (fail closed on whatever the producer already claimed). For writable
    modes a raw SUCCESS is re-derived from the classification + readiness:

    * ``AGENT_COMPLETED`` + green readiness  -> ``SUCCESS``;
    * ``AGENT_COMPLETED`` + non-green readiness -> ``FAILED``;
    * ``AGENT_COMPLETED`` + absent/unrecognized readiness -> ``UNKNOWN``;
    * any other classification -> ``UNKNOWN`` (never promoted).

    ``None`` status stays ``None`` (no evidence, no guess).
    """
    if status is None or mode not in WRITABLE_MODES:
        return status
    if status not in SUCCESS_STATUSES:
        return status
    if agent_classification != AGENT_COMPLETED:
        return STATUS_UNKNOWN
    if readiness in READINESS_SUCCESS:
        return STATUS_SUCCESS
    if readiness in READINESS_FAILED:
        return STATUS_FAILED
    return STATUS_UNKNOWN


class SemanticError(Exception):
    """The semantic result file violates the contract (fail closed).

    ``code`` is a stable machine-readable reason (e.g. ``MALFORMED``,
    ``SUBRUN_MISMATCH``, ``STATUS_INVALID``) for durable audit.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"SEMANTIC_{code}: {detail}" if detail
                         else f"SEMANTIC_{code}")
        self.code = code
        self.detail = detail


def _require_bounded_str(doc: dict[str, Any], field: str) -> str:
    """Validate a bounded, non-empty string field (fail closed)."""
    val = doc[field]  # already checked for presence by the caller
    if not isinstance(val, str) or not 1 <= len(val) <= _MAX_STR_LEN:
        raise SemanticError(
            "FIELD_INVALID",
            f"{field} must be a non-empty string of at most {_MAX_STR_LEN} "
            f"chars, got {val!r}")
    return val


def validate_semantic(doc: Any) -> str:
    """Validate a parsed semantic result and return its status.

    Requires the exact contract (fail closed on ANY violation — missing,
    malformed, unknown, stale-version, or otherwise bad fields — it never
    guesses and never fabricates a status):

    * ``schema_version`` == 1 (required);
    * ``subrun_id``: required, non-empty, bounded string — the result is
      bound to the exact sub-run that produced it;
    * ``status``: required, one of :data:`ALL_STATUSES`;
    * optional provenance fields (``reason``, ``agent_classification``,
      ``readiness``, ``ts``): if present, MUST be bounded non-empty
      strings (``null``/oversized/non-string is a contract violation);
    * optional ``require_changes`` (Mission 010): if present, MUST be one
      of :data:`REQUIRE_CHANGES_VALUES` (a malformed value is a contract
      violation, never silently coerced).
    """
    if not isinstance(doc, dict):
        raise SemanticError("MALFORMED", "top-level must be an object")

    ver = doc.get("schema_version")
    if not (isinstance(ver, int) and not isinstance(ver, bool)
            and ver == SEMANTIC_SCHEMA_VERSION):
        raise SemanticError(
            "SCHEMA_VERSION_INVALID",
            f"schema_version={ver!r}, expected {SEMANTIC_SCHEMA_VERSION}")

    if "subrun_id" not in doc:
        raise SemanticError("SUBRUN_ID_MISSING",
                            "subrun_id is required (result must be bound "
                            "to the exact sub-run)")
    _ = _require_bounded_str(doc, "subrun_id")

    status = doc.get("status")
    if not isinstance(status, str) or status not in ALL_STATUSES:
        raise SemanticError("STATUS_INVALID",
                            f"status={status!r} not in {sorted(ALL_STATUSES)}")

    for field in ("reason", "agent_classification", "readiness", "ts"):
        if field in doc:
            _ = _require_bounded_str(doc, field)

    if "require_changes" in doc:
        require_changes = doc["require_changes"]
        if (not isinstance(require_changes, str)
                or require_changes not in REQUIRE_CHANGES_VALUES):
            raise SemanticError(
                "REQUIRE_CHANGES_INVALID",
                f"require_changes={require_changes!r} not in "
                f"{sorted(REQUIRE_CHANGES_VALUES)}")

    return status


def validate_subrun_binding(doc: Any, expected_subrun_id: str) -> None:
    """Explicitly verify the result is bound to the expected sub-run.

    Raises :class:`SemanticError` (``SUBRUN_MISMATCH`` when the ids
    differ; ``SUBRUN_ID_MISSING``/``FIELD_INVALID`` when the field is
    absent or malformed in ``doc``) — a mismatch is never tolerated,
    because only exact-sub-run evidence may support COMPLETED.
    """
    if not isinstance(expected_subrun_id, str) or not expected_subrun_id:
        raise SemanticError("EXPECTED_SUBRUN_INVALID",
                            f"expected_subrun_id={expected_subrun_id!r}")
    if not isinstance(doc, dict) or "subrun_id" not in doc:
        raise SemanticError(
            "SUBRUN_ID_MISSING",
            f"evidence is not bound to sub-run {expected_subrun_id!r}")
    actual = doc["subrun_id"]
    if not (isinstance(actual, str) and 1 <= len(actual) <= _MAX_STR_LEN):
        raise SemanticError("FIELD_INVALID", f"subrun_id={actual!r}")
    if actual != expected_subrun_id:
        raise SemanticError(
            "SUBRUN_MISMATCH",
            f"structural result is bound to sub-run {actual!r}, "
            f"expected {expected_subrun_id!r}")


def interpret_semantic(doc: Any | None,
                       expected_subrun_id: str) -> tuple[str | None, str | None]:
    """Fail-closed interpretation of semantic evidence.

    Returns ``(status, error)``:

    * valid + bound to ``expected_subrun_id`` -> ``(status, None)``;
    * anything else (missing, malformed, unknown, stale, mismatched)
      -> ``(None, error_code)`` — never a status, never a guess.
    """
    if doc is None:
        return (None, "MISSING")
    try:
        status = validate_semantic(doc)
        validate_subrun_binding(doc, expected_subrun_id)
    except SemanticError as exc:
        return (None, exc.code)
    return (status, None)


def semantic_supports_success(doc: Any | None,
                              expected_subrun_id: str) -> bool:
    """True ONLY if the evidence is valid, bound to the exact sub-run,
    AND says SUCCESS. Everything else is False (fail closed).
    """
    status, _err = interpret_semantic(doc, expected_subrun_id)
    return status in SUCCESS_STATUSES


def read_semantic_file(path: str) -> tuple[dict[str, Any] | None, str | None]:
    """Read and parse the semantic result file (fail closed, no raising).

    Returns ``(doc, error)``:

    * file absent/empty/unreadable            -> ``(None, reason)``;
    * valid JSON + valid contract            -> ``(doc, None)``;
    * malformed JSON / contract violation    -> ``(None, reason)``.

    The sub-run *binding* is NOT checked here (it is consumer-specific);
    use :func:`interpret_semantic` / :func:`validate_subrun_binding` to
    verify the evidence belongs to the exact expected sub-run.
    """
    p = Path(path)
    if not p.is_file():
        return (None, "MISSING")
    try:
        raw = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return (None, "UNREADABLE")
    if not raw.strip():
        return (None, "EMPTY")
    try:
        doc: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        return (None, f"MALFORMED:{exc.msg}")
    try:
        validate_semantic(doc)
    except SemanticError as exc:
        return (None, exc.code)
    return (doc, None)


# ---------------------------------------------------------------------------
# Mission 008 — versioned exact execution attestation
# ---------------------------------------------------------------------------
# The M007 core contract above stays frozen (schema_version=1). Mission 008
# adds one OPTIONAL, independently-versioned ``attestation`` object that binds
# a semantic result to the exact wrapper execution and the exact repository
# evidence that produced it:
#
#     "attestation": {
#       "schema_version":   1,
#       "subrun_id":        "<the exact sub-run>",
#       "run_id":           "<wrapper run identity / run dir name>",
#       "repo_head_before": "<40-or-64-hex repository HEAD before>",
#       "repo_head_after":  "<40-or-64-hex repository HEAD after>",
#       "patch_sha256":     "<64-hex SHA-256 of the exact patch bytes>"
#     }
#
# Only a valid M007 core PLUS a valid, independently-verified attestation can
# support COMPLETED. A legacy M007 record (no attestation) stays *readable*
# and its non-SUCCESS statuses keep their meaning, but its SUCCESS can never
# be silently promoted to attested completion (the runner gates SUCCESS on
# verification). All checks here are pure (no I/O, no clocks, no guessing).

#: Schema version of the nested attestation object. Independent of the
#: frozen top-level M007 ``schema_version`` so the legacy core never moves.
ATTESTATION_SCHEMA_VERSION = 1

#: The single accepted marker for an independently-verified attestation.
ATTESTATION_VERIFIED = "VERIFIED"

# --- stable fail-closed attestation reason codes -------------------------------
# One code per failure category the objective requires the runner to reject:
# missing / malformed / partial / contradictory / mismatched (stale is decided
# by the runner, which can observe the launch ordering and is therefore not a
# pure function of the parsed document).
ATT_MISSING = "ATTESTATION_MISSING"
ATT_MALFORMED = "ATTESTATION_MALFORMED"
ATT_PARTIAL = "ATTESTATION_PARTIAL"
ATT_MISMATCH = "ATTESTATION_MISMATCH"
ATT_STALE = "ATTESTATION_STALE"
ATT_CONTRADICTORY = "ATTESTATION_CONTRADICTORY"

ATTESTATION_REASON_CODES = frozenset({
    ATT_MISSING, ATT_MALFORMED, ATT_PARTIAL, ATT_MISMATCH, ATT_STALE,
    ATT_CONTRADICTORY,
})

#: Canonical attestation identity fields mapped to the accepted aliases. The
#: first alias is canonical; the alternatives keep the contract tolerant of
#: the equally-explicit ``wrapper_run_id`` / ``head_*`` / ``evidence_sha256``
#: spellings without inventing values.
_ATTESTATION_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "subrun_id": ("subrun_id",),
    "run_id": ("run_id", "wrapper_run_id"),
    "repo_head_before": ("repo_head_before", "head_before"),
    "repo_head_after": ("repo_head_after", "head_after"),
    "patch_sha256": ("patch_sha256", "evidence_sha256"),
}

#: Flat set of every accepted attestation key (nesting-agnostic detection).
#: ``subrun_id`` is deliberately excluded: the frozen M007 core always
#: carries it, so it cannot be used to detect a top-level attestation.
_ATTESTATION_MARKER_KEYS = frozenset(
    alias
    for field, aliases in _ATTESTATION_FIELD_ALIASES.items()
    if field != "subrun_id"
    for alias in aliases
)

#: Fields that MUST be present for an attestation to be usable (the sub-run
#: identity is also bound at the top level, so it is required separately).
ATTESTATION_REQUIRED_FIELDS = (
    "run_id", "repo_head_before", "repo_head_after", "patch_sha256",
)


def _is_lower_hex(value: str, length: int) -> bool:
    """True only for an exactly-``length`` lowercase hex digest string (pure)."""
    return len(value) == length and all(c in "0123456789abcdef" for c in value)


def extract_attestation(doc: Any) -> Any | None:
    """Return the attestation mapping carried by ``doc`` (or ``None``).

    Prefers the canonical nested ``attestation`` object; falls back to the
    top-level document only when it carries attestation marker keys (so a
    flat-shaped producer is still readable). A missing attestation yields
    ``None`` — the caller maps that to :data:`ATT_MISSING`.
    """
    if not isinstance(doc, dict):
        return None
    nested = doc.get("attestation")
    if nested is not None:
        return nested
    if any(key in doc for key in _ATTESTATION_MARKER_KEYS):
        return doc
    return None


def attestation_field(att: dict[str, Any], field: str) -> str | None:
    """Resolve a canonical attestation field through its accepted aliases."""
    for alias in _ATTESTATION_FIELD_ALIASES[field]:
        if alias in att:
            value = att[alias]
            return value if isinstance(value, str) else None
    return None


def validate_attestation(att: Any) -> str | None:
    """Pure, fail-closed validation of an attestation object.

    Returns ``None`` when ``att`` is a complete, well-formed, internally
    consistent attestation; otherwise a stable reason code (never raises and
    never guesses a value):

    * ``None``                             -> absent (``ATT_MISSING``);
    * non-object / bad version / wrong type / oversized / bad hex
      -> ``ATT_MALFORMED``;
    * a required field absent or blank      -> ``ATT_PARTIAL``;
    * ``repo_head_before != repo_head_after`` (the wrapper never moves HEAD)
      -> ``ATT_CONTRADICTORY``.
    """
    if att is None:
        return ATT_MISSING
    if not isinstance(att, dict):
        return ATT_MALFORMED

    version = att.get("schema_version")
    if not (isinstance(version, int) and not isinstance(version, bool)
            and version == ATTESTATION_SCHEMA_VERSION):
        return ATT_MALFORMED

    values: dict[str, str] = {}
    for field in ATTESTATION_REQUIRED_FIELDS:
        raw = att.get(field)
        if raw is None:
            # resolve aliases before declaring the field partial
            for alias in _ATTESTATION_FIELD_ALIASES[field]:
                if alias in att:
                    raw = att[alias]
                    break
        if raw is None:
            return ATT_PARTIAL
        if not isinstance(raw, str) or not 1 <= len(raw) <= _MAX_STR_LEN:
            return ATT_MALFORMED
        if not raw.strip():
            return ATT_PARTIAL
        values[field] = raw

    for field in ("repo_head_before", "repo_head_after"):
        if not (_is_lower_hex(values[field], 40)
                or _is_lower_hex(values[field], 64)):
            return ATT_MALFORMED
    if not _is_lower_hex(values["patch_sha256"], 64):
        return ATT_MALFORMED
    if values["repo_head_before"] != values["repo_head_after"]:
        return ATT_CONTRADICTORY
    return None


def validate_attestation_binding(att: Any, expected_subrun_id: str) -> None:
    """Verify an attestation's own ``subrun_id`` (when present) matches.

    Pure and fail-closed: a mismatched, malformed, or absent expected id is
    never tolerated. The attestation-level ``subrun_id`` is optional (the
    top-level M007 binding is authoritative), but when present it MUST agree
    with the expected sub-run.
    """
    if not isinstance(expected_subrun_id, str) or not expected_subrun_id:
        raise SemanticError("EXPECTED_SUBRUN_INVALID",
                            f"expected_subrun_id={expected_subrun_id!r}")
    if not isinstance(att, dict):
        raise SemanticError(ATT_MALFORMED, "attestation is not an object")
    actual = att.get("subrun_id")
    if actual is None:
        return
    if not (isinstance(actual, str) and 1 <= len(actual) <= _MAX_STR_LEN):
        raise SemanticError("FIELD_INVALID", f"attestation.subrun_id={actual!r}")
    if actual != expected_subrun_id:
        raise SemanticError(
            ATT_MISMATCH,
            f"attestation is bound to sub-run {actual!r}, "
            f"expected {expected_subrun_id!r}")
