"""M036–M039 — human-gated release bundle: canonical vocabulary (pure).

This is a *thin release/handoff layer* laid on top of the assembled mission
flow (M030–M035). It never introduces a competing lifecycle, readiness, trust
or control model: the canonical mission truth stays in the M030
``status.json`` and the M031 ``closure.json``; the release layer only records
the *release progression* from ``READY_FOR_COMMIT`` to a closed release.

The release progression is deliberately expressed as an ordered set of
**stages** (not lifecycle states), each of which is derived from durable
operator-authorized evidence:

    READY_FOR_COMMIT
      -> COMMIT_HANDOFF      (deterministic GO COMMIT handoff emitted)
      -> COMMITTED           (operator-authorized commit/push)
      -> PR_BOUND            (exactly one PR bound to the exact commit SHA)
      -> CI_PENDING          (exact-head CI queued/in_progress)
      -> CI_SUCCESS / CI_FAILED
      -> MERGE_HANDOFF       (GO MERGE gate emitted after green exact-head CI)
      -> MERGED              (operator-authorized merge)
      -> RELEASED            (release closure emitted)
      -> BLOCKED             (fail-closed guard fired)

Design invariants:

* deterministic and pure — no I/O, no clocks, no randomness in this module;
* fail closed — a missing/malformed/stale/moved fact never advances a stage;
* semantic patch identity remains authoritative: the exact reviewed patch
  SHA-256 is carried unchanged from the M031 closure into the commit handoff,
  the PR binding, the merge handoff and the release closure;
* never trust a branch name, a stale review, stale CI, a prose claim or a
  moved PR head.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn

#: Schema version of every durable release document.
SCHEMA_VERSION = 1

#: Human/machine release-layer version string (additive).
RELEASE_VERSION = "m039.1"

# --- release gates (closed set) ----------------------------------------------

GATE_GO_COMMIT = "GO_COMMIT"
GATE_GO_MERGE = "GO_MERGE"

RELEASE_GATES = frozenset({GATE_GO_COMMIT, GATE_GO_MERGE})

# --- release stages (closed, ordered set) ------------------------------------

RST_READY_FOR_COMMIT = "READY_FOR_COMMIT"
RST_COMMIT_HANDOFF = "COMMIT_HANDOFF"
RST_COMMITTED = "COMMITTED"
RST_PR_BOUND = "PR_BOUND"
RST_CI_PENDING = "CI_PENDING"
RST_CI_SUCCESS = "CI_SUCCESS"
RST_CI_FAILED = "CI_FAILED"
RST_MERGE_HANDOFF = "MERGE_HANDOFF"
RST_MERGED = "MERGED"
RST_RELEASED = "RELEASED"
RST_BLOCKED = "BLOCKED"

RELEASE_STAGES = frozenset({
    RST_READY_FOR_COMMIT, RST_COMMIT_HANDOFF, RST_COMMITTED, RST_PR_BOUND,
    RST_CI_PENDING, RST_CI_SUCCESS, RST_CI_FAILED, RST_MERGE_HANDOFF,
    RST_MERGED, RST_RELEASED, RST_BLOCKED,
})

#: The canonical release progression (``BLOCKED`` is terminal but off-axis).
RELEASE_STAGE_SEQUENCE = (
    RST_READY_FOR_COMMIT, RST_COMMIT_HANDOFF, RST_COMMITTED, RST_PR_BOUND,
    RST_CI_PENDING, RST_CI_SUCCESS, RST_MERGE_HANDOFF, RST_MERGED,
    RST_RELEASED,
)

#: Release stages that finalize the release (absorbing).
TERMINAL_RELEASE_STAGES = frozenset({RST_RELEASED, RST_BLOCKED})

# --- exact-head CI states (closed set) ---------------------------------------

CI_QUEUED = "queued"
CI_IN_PROGRESS = "in_progress"
CI_SUCCESS = "success"
CI_FAILURE = "failure"
CI_CANCELLED = "cancelled"
CI_MISSING = "missing"
CI_UNKNOWN = "unknown"

CI_STATES = frozenset({
    CI_QUEUED, CI_IN_PROGRESS, CI_SUCCESS, CI_FAILURE, CI_CANCELLED,
    CI_MISSING, CI_UNKNOWN,
})

#: The ONLY CI state that may permit a merge.
CI_GREEN_STATES = frozenset({CI_SUCCESS})

#: CI states that block a merge.
CI_BLOCKING_STATES = frozenset({
    CI_QUEUED, CI_IN_PROGRESS, CI_FAILURE, CI_CANCELLED, CI_MISSING, CI_UNKNOWN,
})

# --- merge methods (closed set) ----------------------------------------------

MERGE_SQUASH = "squash"
MERGE_MERGE = "merge"
MERGE_REBASE = "rebase"

MERGE_METHODS = frozenset({MERGE_SQUASH, MERGE_MERGE, MERGE_REBASE})

#: Trajectory_OS default merge method (explicit, never implicit).
DEFAULT_MERGE_METHOD = MERGE_SQUASH

# --- pull request states (closed set) ----------------------------------------

PR_OPEN = "OPEN"
PR_CLOSED = "CLOSED"
PR_MERGED = "MERGED"

PULL_REQUEST_STATES = frozenset({PR_OPEN, PR_CLOSED, PR_MERGED})

# --- stable release reason codes ---------------------------------------------

R_OK = "OK"
R_NOT_READY = "MISSION_NOT_READY_FOR_COMMIT"
R_LIFECYCLE_NOT_COMPLETE = "LIFECYCLE_NOT_COMPLETE"
R_READINESS_NOT_READY = "READINESS_NOT_READY_FOR_COMMIT"
R_NO_ACTIVE_REVIEWER = "NO_ACTIVE_FINAL_REVIEWER"
R_STALE_REVIEW = "STALE_REVIEW"
R_PATCH_MISMATCH = "REVIEWED_PATCH_MISMATCH"
R_PATCH_INVALID = "PATCH_IDENTITY_INVALID"
R_REVIEW_NOT_PASS = "REVIEW_NOT_VALID_PASS"
R_BRANCH_MOVED = "BRANCH_MOVED"
R_HEAD_MOVED = "HEAD_MOVED"
R_PATCH_CHANGED = "PATCH_CHANGED"
R_READINESS_CHANGED = "READINESS_CHANGED"
R_HANDOFF_MISSING = "COMMIT_HANDOFF_MISSING"
R_UNAUTHORIZED = "EXPLICIT_OPERATOR_AUTHORIZATION_REQUIRED"
R_ALREADY_COMMITTED = "ALREADY_COMMITTED"
R_COMMIT_FAILED = "COMMIT_FAILED"
R_PUSH_FAILED = "PUSH_FAILED"
R_PR_AMBIGUOUS = "AMBIGUOUS_PULL_REQUEST"
R_PR_MISSING = "PULL_REQUEST_MISSING"
R_PR_HEAD_MOVED = "PULL_REQUEST_HEAD_MOVED"
R_PR_NOT_OPEN = "PULL_REQUEST_NOT_OPEN"
R_PR_NOT_MERGEABLE = "PULL_REQUEST_NOT_MERGEABLE"
R_CI_NOT_GREEN = "EXACT_HEAD_CI_NOT_GREEN"
R_CI_MISSING = "EXACT_HEAD_CI_MISSING"
R_MERGE_HANDOFF_MISSING = "MERGE_HANDOFF_MISSING"
R_MERGE_HANDOFF_STALE = "MERGE_HANDOFF_STALE"
R_MERGE_METHOD_INVALID = "MERGE_METHOD_INVALID"
R_MERGE_FAILED = "MERGE_FAILED"
R_NOT_MERGED = "MERGE_NOT_CONFIRMED"
R_TARGET_MISMATCH = "TARGET_BRANCH_MISMATCH"
R_TARGET_UNVERIFIED = "TARGET_BRANCH_UNVERIFIED"
R_MALFORMED = "MALFORMED_RELEASE_DOCUMENT"
R_IDENTITY_MISMATCH = "RELEASE_IDENTITY_MISMATCH"
R_ADAPTER_UNAVAILABLE = "ADAPTER_UNAVAILABLE"

RELEASE_REASON_CODES = frozenset({
    R_OK, R_NOT_READY, R_LIFECYCLE_NOT_COMPLETE, R_READINESS_NOT_READY,
    R_NO_ACTIVE_REVIEWER, R_STALE_REVIEW, R_PATCH_MISMATCH, R_PATCH_INVALID,
    R_REVIEW_NOT_PASS, R_BRANCH_MOVED, R_HEAD_MOVED, R_PATCH_CHANGED,
    R_READINESS_CHANGED, R_HANDOFF_MISSING, R_UNAUTHORIZED, R_ALREADY_COMMITTED,
    R_COMMIT_FAILED, R_PUSH_FAILED, R_PR_AMBIGUOUS, R_PR_MISSING,
    R_PR_HEAD_MOVED, R_PR_NOT_OPEN, R_PR_NOT_MERGEABLE, R_CI_NOT_GREEN,
    R_CI_MISSING, R_MERGE_HANDOFF_MISSING, R_MERGE_HANDOFF_STALE,
    R_MERGE_METHOD_INVALID, R_MERGE_FAILED, R_NOT_MERGED, R_TARGET_MISMATCH,
    R_TARGET_UNVERIFIED, R_MALFORMED, R_IDENTITY_MISMATCH,
    R_ADAPTER_UNAVAILABLE,
})

# --- bounds ------------------------------------------------------------------

MAX_MESSAGE_LEN = 4096
MAX_REASON_LEN = 1024
MAX_SCOPE_ITEMS = 64
MAX_SUMMARY_VALUE_LEN = 1024
MAX_CI_RUNS = 128
MAX_CI_JOBS = 256
MAX_BRANCH_LEN = 256
MAX_URL_LEN = 2048
MAX_LOG_REF_LEN = 2048

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
#: Git object ids are SHA-1 (40 hex) or SHA-256 (64 hex).
_GIT_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

UNKNOWN = "UNKNOWN"


class ReleaseError(Exception):
    """A fail-closed release-layer refusal (stable ``code``)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _fail(code: str, detail: str = "") -> NoReturn:
    raise ReleaseError(code, detail)


def is_sha256(value: object) -> bool:
    """True only for an exact 64-char lowercase hex SHA-256 digest."""
    return isinstance(value, str) and bool(_SHA256_RE.fullmatch(value))


def is_git_sha(value: object) -> bool:
    """True only for an exact lowercase 40/64-char hex Git object id."""
    return isinstance(value, str) and bool(_GIT_SHA_RE.fullmatch(value))


def _require_bounded_str(value: object, code: str, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_SUMMARY_VALUE_LEN:
        _fail(code, f"{field} must be a bounded non-empty string")
    return value


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def utc_now() -> str:
    """Canonical UTC timestamp (seconds precision, ``Z`` suffix)."""
    from datetime import UTC, datetime

    return (datetime.now(UTC).replace(microsecond=0).isoformat()
            .replace("+00:00", "Z"))


# --- CI classification (pure) -------------------------------------------------


def classify_check_run(status: object, conclusion: object) -> str:
    """Map one GitHub check-run/status object to a canonical CI state (pure).

    * ``status != completed`` -> ``queued`` / ``in_progress`` (never success);
    * ``completed`` + ``success`` -> ``success`` (including non-blocking
      ``neutral`` / ``skipped``);
    * ``completed`` + ``failure`` / ``timed_out`` / ``action_required`` /
      ``stale`` -> ``failure``;
    * ``completed`` + ``cancelled`` -> ``cancelled``;
    * anything else (unknown status/conclusion, or a completed run without a
      recognized conclusion) -> ``unknown`` (fail closed).
    """
    status_text = status if isinstance(status, str) else ""
    conclusion_text = conclusion if isinstance(conclusion, str) else ""
    if status_text != "completed":
        if status_text == "queued":
            return CI_QUEUED
        if status_text in ("in_progress", "pending", "requested", "waiting"):
            return CI_IN_PROGRESS
        if status_text == "":
            # Older commit-status API: a bare state with no check-run status.
            if conclusion_text == "pending":
                return CI_IN_PROGRESS
            if conclusion_text == "success":
                return CI_SUCCESS
            if conclusion_text == "failure":
                return CI_FAILURE
            return CI_UNKNOWN
        return CI_UNKNOWN
    if conclusion_text in ("success", "neutral", "skipped"):
        return CI_SUCCESS
    if conclusion_text in ("failure", "timed_out", "action_required", "stale"):
        return CI_FAILURE
    if conclusion_text == "cancelled":
        return CI_CANCELLED
    return CI_UNKNOWN


def combine_ci_states(states: Sequence[str]) -> str:
    """Fold per-run CI states into one exact-head decision (pure, fail closed).

    No runs at all is ``missing`` (never a silent success). A failure or a
    cancellation dominates; an unknown blocks; queued/in_progress blocks and
    is reported with the most informative pending state; only every-green
    yields ``success``.
    """
    if not states:
        return CI_MISSING
    seen = set(states)
    if CI_FAILURE in seen:
        return CI_FAILURE
    if CI_CANCELLED in seen:
        return CI_CANCELLED
    if CI_UNKNOWN in seen:
        return CI_UNKNOWN
    pending = seen & {CI_QUEUED, CI_IN_PROGRESS}
    if pending:
        return CI_QUEUED if pending == {CI_QUEUED} else CI_IN_PROGRESS
    if seen == {CI_SUCCESS}:
        return CI_SUCCESS
    return CI_UNKNOWN


def ci_allows_merge(state: str) -> bool:
    """Only a completed, successful exact-head CI may permit a merge."""
    return state in CI_GREEN_STATES


# --- review / commit-handoff gate evidence ------------------------------------


@dataclass(frozen=True)
class ReviewGateEvidence:
    """The M036 review/readiness evidence derived from canonical artifacts."""

    mission_id: str
    run_id: str
    lifecycle: str
    readiness: str
    require_review: bool
    final_review_enabled: bool
    final_reviewer_active: bool
    final_reviewer_model: str | None
    reviewed_patch: str | None
    current_patch: str | None
    semantic_patch_identity: str | None
    fresh_review: bool
    review_outcome: str | None
    review_reason: str | None
    review_at: str | None
    attempts: int
    repairs: int
    ready: bool
    reason: str

    def validate(self) -> ReviewGateEvidence:
        if not self.mission_id:
            _fail(R_MALFORMED, "review evidence mission_id required")
        if self.ready and self.reason != R_OK:
            _fail(R_MALFORMED, "ready review evidence must carry reason OK")
        if self.semantic_patch_identity is not None and not is_sha256(
                self.semantic_patch_identity):
            _fail(R_PATCH_INVALID, str(self.semantic_patch_identity))
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "run_id": self.run_id,
            "lifecycle": self.lifecycle,
            "readiness": self.readiness,
            "require_review": self.require_review,
            "final_review_enabled": self.final_review_enabled,
            "final_reviewer_active": self.final_reviewer_active,
            "final_reviewer_model": self.final_reviewer_model,
            "reviewed_patch": self.reviewed_patch,
            "current_patch": self.current_patch,
            "semantic_patch_identity": self.semantic_patch_identity,
            "fresh_review": self.fresh_review,
            "review_outcome": self.review_outcome,
            "review_reason": self.review_reason,
            "review_at": self.review_at,
            "attempts": self.attempts,
            "repairs": self.repairs,
            "ready": self.ready,
            "reason": self.reason,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> ReviewGateEvidence:
        return ReviewGateEvidence(
            mission_id=str(data.get("mission_id", "")),
            run_id=str(data.get("run_id", "")),
            lifecycle=str(data.get("lifecycle", "")),
            readiness=str(data.get("readiness", "")),
            require_review=bool(data.get("require_review", True)),
            final_review_enabled=bool(data.get("final_review_enabled", False)),
            final_reviewer_active=bool(data.get("final_reviewer_active",
                                                False)),
            final_reviewer_model=_opt_str(data.get("final_reviewer_model")),
            reviewed_patch=_opt_str(data.get("reviewed_patch")),
            current_patch=_opt_str(data.get("current_patch")),
            semantic_patch_identity=_opt_str(
                data.get("semantic_patch_identity")),
            fresh_review=bool(data.get("fresh_review", False)),
            review_outcome=_opt_str(data.get("review_outcome")),
            review_reason=_opt_str(data.get("review_reason")),
            review_at=_opt_str(data.get("review_at")),
            attempts=int(data.get("attempts", 0)),
            repairs=int(data.get("repairs", 0)),
            ready=bool(data.get("ready", False)),
            reason=str(data.get("reason", R_NOT_READY)),
        ).validate()


# --- commit handoff -----------------------------------------------------------


@dataclass(frozen=True)
class CommitHandoff:
    """The deterministic GO COMMIT handoff emitted after READY_FOR_COMMIT."""

    mission_id: str
    run_id: str
    objective: str
    branch: str
    base_branch: str
    baseline_head: str
    current_head: str
    patch_sha256: str
    reviewed_patch_sha256: str
    semantic_patch_identity: str
    proposed_commit_message: str
    scope_summary: Mapping[str, Any]
    review_evidence: Mapping[str, Any]
    gate: str
    gate_reason: str
    created_at: str
    schema_version: int = SCHEMA_VERSION
    release_version: str = RELEASE_VERSION

    def validate(self) -> CommitHandoff:
        if not self.mission_id:
            _fail(R_MALFORMED, "commit handoff mission_id required")
        if self.gate != GATE_GO_COMMIT:
            _fail(R_MALFORMED, f"handoff gate {self.gate!r}")
        if not is_sha256(self.patch_sha256):
            _fail(R_PATCH_INVALID, str(self.patch_sha256))
        if not is_sha256(self.reviewed_patch_sha256):
            _fail(R_PATCH_INVALID, str(self.reviewed_patch_sha256))
        if not is_sha256(self.semantic_patch_identity):
            _fail(R_PATCH_INVALID, str(self.semantic_patch_identity))
        if self.patch_sha256 != self.reviewed_patch_sha256:
            _fail(R_PATCH_MISMATCH,
                  "handoff patch does not equal the reviewed patch")
        if not self.branch or not self.base_branch:
            _fail(R_MALFORMED, "handoff branch/base_branch required")
        if len(self.proposed_commit_message) > MAX_MESSAGE_LEN:
            _fail(R_MALFORMED, "commit message exceeds bound")
        return self

    @property
    def human_summary(self) -> str:
        """Compact, deterministic operator summary (no prose logs)."""
        message = self.proposed_commit_message.splitlines()[0]
        return (
            f"GO COMMIT ready: mission {self.mission_id} on {self.branch} "
            f"(base {self.base_branch})\n"
            f"  baseline HEAD : {self.baseline_head}\n"
            f"  current HEAD  : {self.current_head}\n"
            f"  reviewed patch: {self.reviewed_patch_sha256}\n"
            f"  commit message: {message}\n"
            f"  authorize with: trajectory-release go-commit "
            f"--mission-id {self.mission_id} --authorize-commit <token>")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "release_version": self.release_version,
            "mission_id": self.mission_id,
            "run_id": self.run_id,
            "objective": self.objective,
            "branch": self.branch,
            "base_branch": self.base_branch,
            "baseline_head": self.baseline_head,
            "current_head": self.current_head,
            "patch_sha256": self.patch_sha256,
            "reviewed_patch_sha256": self.reviewed_patch_sha256,
            "semantic_patch_identity": self.semantic_patch_identity,
            "proposed_commit_message": self.proposed_commit_message,
            "scope_summary": dict(self.scope_summary),
            "review_evidence": dict(self.review_evidence),
            "gate": self.gate,
            "gate_reason": self.gate_reason,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> CommitHandoff:
        if not isinstance(data, Mapping):
            _fail(R_MALFORMED, "commit handoff must be an object")
        scope = data.get("scope_summary")
        review = data.get("review_evidence")
        return CommitHandoff(
            mission_id=str(data.get("mission_id", "")),
            run_id=str(data.get("run_id", "")),
            objective=str(data.get("objective", "")),
            branch=str(data.get("branch", "")),
            base_branch=str(data.get("base_branch", "")),
            baseline_head=str(data.get("baseline_head", "")),
            current_head=str(data.get("current_head", "")),
            patch_sha256=str(data.get("patch_sha256", "")),
            reviewed_patch_sha256=str(data.get("reviewed_patch_sha256", "")),
            semantic_patch_identity=str(data.get("semantic_patch_identity", "")),
            proposed_commit_message=str(
                data.get("proposed_commit_message", "")),
            scope_summary=(dict(scope) if isinstance(scope, Mapping) else {}),
            review_evidence=(dict(review) if isinstance(review, Mapping)
                             else {}),
            gate=str(data.get("gate", GATE_GO_COMMIT)),
            gate_reason=str(data.get("gate_reason", R_OK)),
            created_at=str(data.get("created_at", "")),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            release_version=str(data.get("release_version",
                                        RELEASE_VERSION)),
        ).validate()


@dataclass(frozen=True)
class CommitResult:
    """The authoritative result of an operator-authorized commit + push."""

    mission_id: str
    commit_sha: str
    branch: str
    remote: str
    pushed_sha: str
    parent_head: str
    baseline_head: str
    reviewed_patch_sha256: str
    message: str
    authorization: Mapping[str, Any]
    created_at: str
    schema_version: int = SCHEMA_VERSION
    release_version: str = RELEASE_VERSION

    def validate(self) -> CommitResult:
        if not is_git_sha(self.commit_sha):
            _fail(R_MALFORMED, "commit result requires a Git object id")
        if self.pushed_sha != self.commit_sha:
            _fail(R_PUSH_FAILED,
                  "pushed SHA does not equal the committed SHA")
        if not is_sha256(self.reviewed_patch_sha256):
            _fail(R_PATCH_INVALID, str(self.reviewed_patch_sha256))
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "release_version": self.release_version,
            "mission_id": self.mission_id,
            "commit_sha": self.commit_sha,
            "branch": self.branch,
            "remote": self.remote,
            "pushed_sha": self.pushed_sha,
            "parent_head": self.parent_head,
            "baseline_head": self.baseline_head,
            "reviewed_patch_sha256": self.reviewed_patch_sha256,
            "message": self.message,
            "authorization": dict(self.authorization),
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> CommitResult:
        auth = data.get("authorization")
        return CommitResult(
            mission_id=str(data.get("mission_id", "")),
            commit_sha=str(data.get("commit_sha", "")),
            branch=str(data.get("branch", "")),
            remote=str(data.get("remote", "")),
            pushed_sha=str(data.get("pushed_sha", "")),
            parent_head=str(data.get("parent_head", "")),
            baseline_head=str(data.get("baseline_head", "")),
            reviewed_patch_sha256=str(data.get("reviewed_patch_sha256", "")),
            message=str(data.get("message", "")),
            authorization=(dict(auth) if isinstance(auth, Mapping) else {}),
            created_at=str(data.get("created_at", "")),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            release_version=str(data.get("release_version",
                                        RELEASE_VERSION)),
        ).validate()


# --- CI / pull request --------------------------------------------------------


@dataclass(frozen=True)
class CiJob:
    """One failed-job reference exposed for operator inspection."""

    name: str
    status: str
    conclusion: str | None
    url: str | None
    log_ref: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "conclusion": self.conclusion,
            "url": self.url,
            "log_ref": self.log_ref,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> CiJob:
        return CiJob(
            name=str(data.get("name", "")),
            status=str(data.get("status", "")),
            conclusion=_opt_str(data.get("conclusion")),
            url=_opt_str(data.get("url")),
            log_ref=_opt_str(data.get("log_ref")),
        )


@dataclass(frozen=True)
class CiRun:
    """One exact-head CI workflow run."""

    workflow: str
    run_id: str
    name: str
    head_sha: str
    status: str
    conclusion: str | None
    url: str | None
    jobs: tuple[CiJob, ...] = ()

    def validate(self) -> CiRun:
        if not self.workflow:
            _fail(R_MALFORMED, "CI run workflow required")
        if not self.head_sha:
            _fail(R_MALFORMED, "CI run head_sha required")
        if len(self.jobs) > MAX_CI_JOBS:
            _fail(R_MALFORMED, "CI run job list exceeds bound")
        return self

    @property
    def state(self) -> str:
        return classify_check_run(self.status, self.conclusion)

    @property
    def failed(self) -> bool:
        return self.state in (CI_FAILURE, CI_CANCELLED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow": self.workflow,
            "run_id": self.run_id,
            "name": self.name,
            "head_sha": self.head_sha,
            "status": self.status,
            "conclusion": self.conclusion,
            "state": self.state,
            "url": self.url,
            "jobs": [job.to_dict() for job in self.jobs],
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> CiRun:
        jobs = data.get("jobs")
        return CiRun(
            workflow=str(data.get("workflow", "")),
            run_id=str(data.get("run_id", "")),
            name=str(data.get("name", "")),
            head_sha=str(data.get("head_sha", "")),
            status=str(data.get("status", "")),
            conclusion=_opt_str(data.get("conclusion")),
            url=_opt_str(data.get("url")),
            jobs=tuple(CiJob.from_dict(job) for job in jobs
                       if isinstance(job, Mapping))
            if isinstance(jobs, Sequence) and not isinstance(jobs, (str, bytes))
            else (),
        ).validate()


@dataclass(frozen=True)
class CiStatus:
    """The exact-head CI lookup result (never a branch-name-only view)."""

    state: str
    head_sha: str
    source: str
    runs: tuple[CiRun, ...]
    checked_at: str
    missing_reason: str | None = None

    def validate(self) -> CiStatus:
        if self.state not in CI_STATES:
            _fail(R_MALFORMED, f"CI state {self.state!r}")
        if not self.head_sha:
            _fail(R_MALFORMED, "CI status head_sha required")
        if len(self.runs) > MAX_CI_RUNS:
            _fail(R_MALFORMED, "CI status run list exceeds bound")
        return self

    @property
    def green(self) -> bool:
        return ci_allows_merge(self.state)

    @property
    def failed_jobs(self) -> tuple[str, ...]:
        names: list[str] = []
        for run in self.runs:
            if not run.failed:
                continue
            for job in run.jobs:
                if classify_check_run(job.status, job.conclusion) in (
                        CI_FAILURE, CI_CANCELLED):
                    names.append(job.name)
            if not run.jobs and run.name:
                names.append(run.name)
        return tuple(names)

    @property
    def log_refs(self) -> tuple[str, ...]:
        refs: list[str] = []
        for run in self.runs:
            if not run.failed:
                continue
            if run.url:
                refs.append(run.url)
            for job in run.jobs:
                if classify_check_run(job.status, job.conclusion) in (
                        CI_FAILURE, CI_CANCELLED) and job.log_ref:
                    refs.append(job.log_ref)
        return tuple(refs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "head_sha": self.head_sha,
            "source": self.source,
            "green": self.green,
            "missing_reason": self.missing_reason,
            "failed_jobs": list(self.failed_jobs),
            "log_refs": list(self.log_refs),
            "runs": [run.to_dict() for run in self.runs],
            "checked_at": self.checked_at,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> CiStatus:
        runs = data.get("runs")
        return CiStatus(
            state=str(data.get("state", CI_UNKNOWN)),
            head_sha=str(data.get("head_sha", "")),
            source=str(data.get("source", "")),
            runs=tuple(CiRun.from_dict(run) for run in runs
                       if isinstance(run, Mapping))
            if isinstance(runs, Sequence) and not isinstance(runs, (str, bytes))
            else (),
            checked_at=str(data.get("checked_at", "")),
            missing_reason=_opt_str(data.get("missing_reason")),
        ).validate()


@dataclass(frozen=True)
class PullRequestBinding:
    """The single PR bound to the exact release commit SHA."""

    mission_id: str
    number: int
    url: str | None
    base_branch: str
    base_sha: str | None
    head_branch: str
    head_sha: str
    state: str
    mergeable: bool | None
    mergeable_state: str | None
    created: bool
    bound_commit_sha: str
    bound_at: str
    schema_version: int = SCHEMA_VERSION
    release_version: str = RELEASE_VERSION

    def validate(self) -> PullRequestBinding:
        if not self.mission_id:
            _fail(R_MALFORMED, "PR binding mission_id required")
        if self.number <= 0:
            _fail(R_MALFORMED, "PR number must be positive")
        if self.state not in PULL_REQUEST_STATES:
            _fail(R_MALFORMED, f"PR state {self.state!r}")
        if not is_git_sha(self.bound_commit_sha):
            _fail(R_MALFORMED, "PR bound commit must be a Git object id")
        if self.head_sha != self.bound_commit_sha:
            _fail(R_PR_HEAD_MOVED,
                  f"PR head {self.head_sha} != bound commit "
                  f"{self.bound_commit_sha}")
        if not self.base_branch or not self.head_branch:
            _fail(R_MALFORMED, "PR branches required")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "release_version": self.release_version,
            "mission_id": self.mission_id,
            "number": self.number,
            "url": self.url,
            "base_branch": self.base_branch,
            "base_sha": self.base_sha,
            "head_branch": self.head_branch,
            "head_sha": self.head_sha,
            "state": self.state,
            "mergeable": self.mergeable,
            "mergeable_state": self.mergeable_state,
            "created": self.created,
            "bound_commit_sha": self.bound_commit_sha,
            "bound_at": self.bound_at,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> PullRequestBinding:
        return PullRequestBinding(
            mission_id=str(data.get("mission_id", "")),
            number=int(data.get("number", 0)),
            url=_opt_str(data.get("url")),
            base_branch=str(data.get("base_branch", "")),
            base_sha=_opt_str(data.get("base_sha")),
            head_branch=str(data.get("head_branch", "")),
            head_sha=str(data.get("head_sha", "")),
            state=str(data.get("state", "")),
            mergeable=(data.get("mergeable")
                       if isinstance(data.get("mergeable"), bool) else None),
            mergeable_state=_opt_str(data.get("mergeable_state")),
            created=bool(data.get("created", False)),
            bound_commit_sha=str(data.get("bound_commit_sha", "")),
            bound_at=str(data.get("bound_at", "")),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            release_version=str(data.get("release_version",
                                        RELEASE_VERSION)),
        ).validate()


# --- merge handoff / result ---------------------------------------------------


@dataclass(frozen=True)
class MergeHandoff:
    """The operator-authorized GO MERGE handoff (emitted before the gate)."""

    mission_id: str
    release_commit_sha: str
    pr_number: int
    pr_url: str | None
    pr_head_sha: str
    base_branch: str
    base_sha: str | None
    ci_state: str
    ci_head_sha: str
    ci_checked_at: str
    merge_method: str
    gate: str
    gate_reason: str
    created_at: str
    schema_version: int = SCHEMA_VERSION
    release_version: str = RELEASE_VERSION

    def validate(self) -> MergeHandoff:
        if self.gate != GATE_GO_MERGE:
            _fail(R_MALFORMED, f"merge handoff gate {self.gate!r}")
        if not is_git_sha(self.release_commit_sha):
            _fail(R_MALFORMED, "release commit must be a Git object id")
        if self.pr_head_sha != self.release_commit_sha:
            _fail(R_PR_HEAD_MOVED,
                  f"merge handoff PR head {self.pr_head_sha} != "
                  f"release commit {self.release_commit_sha}")
        if self.ci_head_sha != self.release_commit_sha:
            _fail(R_CI_NOT_GREEN,
                  "merge handoff CI was not looked up for the exact release "
                  "commit")
        if not ci_allows_merge(self.ci_state):
            _fail(R_CI_NOT_GREEN, f"merge handoff CI state {self.ci_state!r}")
        if self.merge_method not in MERGE_METHODS:
            _fail(R_MERGE_METHOD_INVALID, str(self.merge_method))
        return self

    @property
    def human_summary(self) -> str:
        return (
            f"GO MERGE ready: PR #{self.pr_number} -> {self.base_branch} "
            f"squash={self.merge_method == MERGE_SQUASH}\n"
            f"  release commit: {self.release_commit_sha}\n"
            f"  PR head       : {self.pr_head_sha}\n"
            f"  exact-head CI : {self.ci_state} (checked {self.ci_checked_at})\n"
            f"  authorize with: trajectory-release go-merge "
            f"--mission-id {self.mission_id} --authorize-merge <token>")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "release_version": self.release_version,
            "mission_id": self.mission_id,
            "release_commit_sha": self.release_commit_sha,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "pr_head_sha": self.pr_head_sha,
            "base_branch": self.base_branch,
            "base_sha": self.base_sha,
            "ci_state": self.ci_state,
            "ci_head_sha": self.ci_head_sha,
            "ci_checked_at": self.ci_checked_at,
            "merge_method": self.merge_method,
            "gate": self.gate,
            "gate_reason": self.gate_reason,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> MergeHandoff:
        return MergeHandoff(
            mission_id=str(data.get("mission_id", "")),
            release_commit_sha=str(data.get("release_commit_sha", "")),
            pr_number=int(data.get("pr_number", 0)),
            pr_url=_opt_str(data.get("pr_url")),
            pr_head_sha=str(data.get("pr_head_sha", "")),
            base_branch=str(data.get("base_branch", "")),
            base_sha=_opt_str(data.get("base_sha")),
            ci_state=str(data.get("ci_state", CI_UNKNOWN)),
            ci_head_sha=str(data.get("ci_head_sha", "")),
            ci_checked_at=str(data.get("ci_checked_at", "")),
            merge_method=str(data.get("merge_method", DEFAULT_MERGE_METHOD)),
            gate=str(data.get("gate", GATE_GO_MERGE)),
            gate_reason=str(data.get("gate_reason", R_OK)),
            created_at=str(data.get("created_at", "")),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            release_version=str(data.get("release_version",
                                        RELEASE_VERSION)),
        ).validate()


@dataclass(frozen=True)
class MergeResult:
    """The authoritative merge result recorded after the GO MERGE gate."""

    mission_id: str
    merged: bool
    merge_method: str
    merge_sha: str | None
    target_branch: str
    target_head_sha: str | None
    pr_number: int
    merged_at: str
    verified: bool
    reason: str
    schema_version: int = SCHEMA_VERSION
    release_version: str = RELEASE_VERSION

    def validate(self) -> MergeResult:
        if self.merge_method not in MERGE_METHODS:
            _fail(R_MERGE_METHOD_INVALID, str(self.merge_method))
        if self.merged and self.merge_sha is not None and not is_git_sha(
                self.merge_sha):
            _fail(R_MALFORMED, "merge_sha must be a Git object id")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "release_version": self.release_version,
            "mission_id": self.mission_id,
            "merged": self.merged,
            "merge_method": self.merge_method,
            "merge_sha": self.merge_sha,
            "target_branch": self.target_branch,
            "target_head_sha": self.target_head_sha,
            "pr_number": self.pr_number,
            "merged_at": self.merged_at,
            "verified": self.verified,
            "reason": self.reason,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> MergeResult:
        return MergeResult(
            mission_id=str(data.get("mission_id", "")),
            merged=bool(data.get("merged", False)),
            merge_method=str(data.get("merge_method", DEFAULT_MERGE_METHOD)),
            merge_sha=_opt_str(data.get("merge_sha")),
            target_branch=str(data.get("target_branch", "")),
            target_head_sha=_opt_str(data.get("target_head_sha")),
            pr_number=int(data.get("pr_number", 0)),
            merged_at=str(data.get("merged_at", "")),
            verified=bool(data.get("verified", False)),
            reason=str(data.get("reason", "")),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            release_version=str(data.get("release_version",
                                        RELEASE_VERSION)),
        ).validate()


# --- release state / closure --------------------------------------------------


@dataclass(frozen=True)
class ReleaseState:
    """The durable pointer to the release progression (never mission truth)."""

    mission_id: str
    stage: str
    reason: str
    updated_at: str
    commit_sha: str | None = None
    branch: str | None = None
    base_branch: str | None = None
    pr_number: int | None = None
    pr_head_sha: str | None = None
    ci_state: str | None = None
    merge_sha: str | None = None
    schema_version: int = SCHEMA_VERSION
    release_version: str = RELEASE_VERSION

    def validate(self) -> ReleaseState:
        if self.stage not in RELEASE_STAGES:
            _fail(R_MALFORMED, f"release stage {self.stage!r}")
        if not self.mission_id:
            _fail(R_MALFORMED, "release state mission_id required")
        if self.ci_state is not None and self.ci_state not in CI_STATES:
            _fail(R_MALFORMED, f"release state CI {self.ci_state!r}")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "release_version": self.release_version,
            "mission_id": self.mission_id,
            "stage": self.stage,
            "reason": self.reason,
            "commit_sha": self.commit_sha,
            "branch": self.branch,
            "base_branch": self.base_branch,
            "pr_number": self.pr_number,
            "pr_head_sha": self.pr_head_sha,
            "ci_state": self.ci_state,
            "merge_sha": self.merge_sha,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> ReleaseState:
        return ReleaseState(
            mission_id=str(data.get("mission_id", "")),
            stage=str(data.get("stage", "")),
            reason=str(data.get("reason", "")),
            updated_at=str(data.get("updated_at", "")),
            commit_sha=_opt_str(data.get("commit_sha")),
            branch=_opt_str(data.get("branch")),
            base_branch=_opt_str(data.get("base_branch")),
            pr_number=(int(data["pr_number"])
                       if isinstance(data.get("pr_number"), int)
                       else None),
            pr_head_sha=_opt_str(data.get("pr_head_sha")),
            ci_state=_opt_str(data.get("ci_state")),
            merge_sha=_opt_str(data.get("merge_sha")),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            release_version=str(data.get("release_version",
                                        RELEASE_VERSION)),
        ).validate()


@dataclass(frozen=True)
class ReleaseClosure:
    """M039 — the durable release closure (reconstruction without prose)."""

    mission_id: str
    run_id: str
    objective: str
    reviewed_patch_sha256: str
    final_patch_sha256: str
    commit_sha: str
    remote_branch: str
    pr_number: int
    pr_url: str | None
    base_branch: str
    base_sha: str | None
    pr_head_sha: str
    ci_workflow: str | None
    ci_run_id: str | None
    ci_status: str
    ci_conclusion: str | None
    ci_head_sha: str
    go_commit_evidence: Mapping[str, Any]
    go_merge_evidence: Mapping[str, Any]
    merge_sha: str
    target_branch: str
    target_branch_verified: bool
    issue_closure: Mapping[str, Any]
    timestamps: Mapping[str, Any]
    artifacts: Mapping[str, str]
    status: str
    created_at: str
    schema_version: int = SCHEMA_VERSION
    release_version: str = RELEASE_VERSION

    def validate(self) -> ReleaseClosure:
        if not self.mission_id:
            _fail(R_MALFORMED, "release closure mission_id required")
        if self.status not in ("CLOSED", "BLOCKED"):
            _fail(R_MALFORMED, f"release closure status {self.status!r}")
        if not is_sha256(self.reviewed_patch_sha256):
            _fail(R_PATCH_INVALID, str(self.reviewed_patch_sha256))
        if not is_sha256(self.final_patch_sha256):
            _fail(R_PATCH_INVALID, str(self.final_patch_sha256))
        if self.status == "CLOSED":
            if not is_git_sha(self.commit_sha):
                _fail(R_MALFORMED, "closed release requires a commit SHA")
            if self.merge_sha and not is_git_sha(self.merge_sha):
                _fail(R_MALFORMED, "merge_sha must be a Git object id")
            if self.pr_head_sha != self.commit_sha:
                _fail(R_PR_HEAD_MOVED,
                      "release closure PR head does not equal the commit")
        return self

    @property
    def human_summary(self) -> str:
        return (
            f"RELEASE {self.status}: mission {self.mission_id}\n"
            f"  reviewed patch : {self.reviewed_patch_sha256}\n"
            f"  commit SHA     : {self.commit_sha}\n"
            f"  remote branch  : {self.remote_branch}\n"
            f"  PR             : #{self.pr_number} ({self.pr_url})\n"
            f"  base           : {self.base_branch} @ {self.base_sha}\n"
            f"  exact PR head  : {self.pr_head_sha}\n"
            f"  CI             : {self.ci_status} / {self.ci_conclusion} "
            f"({self.ci_workflow} run {self.ci_run_id})\n"
            f"  merge SHA      : {self.merge_sha} -> {self.target_branch} "
            f"(verified={self.target_branch_verified})\n")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "release_version": self.release_version,
            "mission_id": self.mission_id,
            "run_id": self.run_id,
            "objective": self.objective,
            "reviewed_patch_sha256": self.reviewed_patch_sha256,
            "final_patch_sha256": self.final_patch_sha256,
            "commit_sha": self.commit_sha,
            "remote_branch": self.remote_branch,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "base_branch": self.base_branch,
            "base_sha": self.base_sha,
            "pr_head_sha": self.pr_head_sha,
            "ci_workflow": self.ci_workflow,
            "ci_run_id": self.ci_run_id,
            "ci_status": self.ci_status,
            "ci_conclusion": self.ci_conclusion,
            "ci_head_sha": self.ci_head_sha,
            "go_commit_evidence": dict(self.go_commit_evidence),
            "go_merge_evidence": dict(self.go_merge_evidence),
            "merge_sha": self.merge_sha,
            "target_branch": self.target_branch,
            "target_branch_verified": self.target_branch_verified,
            "issue_closure": dict(self.issue_closure),
            "timestamps": dict(self.timestamps),
            "artifacts": dict(sorted(self.artifacts.items())),
            "status": self.status,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> ReleaseClosure:
        if not isinstance(data, Mapping):
            _fail(R_MALFORMED, "release closure must be an object")
        return ReleaseClosure(
            mission_id=str(data.get("mission_id", "")),
            run_id=str(data.get("run_id", "")),
            objective=str(data.get("objective", "")),
            reviewed_patch_sha256=str(data.get("reviewed_patch_sha256", "")),
            final_patch_sha256=str(data.get("final_patch_sha256", "")),
            commit_sha=str(data.get("commit_sha", "")),
            remote_branch=str(data.get("remote_branch", "")),
            pr_number=int(data.get("pr_number", 0)),
            pr_url=_opt_str(data.get("pr_url")),
            base_branch=str(data.get("base_branch", "")),
            base_sha=_opt_str(data.get("base_sha")),
            pr_head_sha=str(data.get("pr_head_sha", "")),
            ci_workflow=_opt_str(data.get("ci_workflow")),
            ci_run_id=_opt_str(data.get("ci_run_id")),
            ci_status=str(data.get("ci_status", CI_UNKNOWN)),
            ci_conclusion=_opt_str(data.get("ci_conclusion")),
            ci_head_sha=str(data.get("ci_head_sha", "")),
            go_commit_evidence=_mapping(data.get("go_commit_evidence")),
            go_merge_evidence=_mapping(data.get("go_merge_evidence")),
            merge_sha=str(data.get("merge_sha", "")),
            target_branch=str(data.get("target_branch", "")),
            target_branch_verified=bool(
                data.get("target_branch_verified", False)),
            issue_closure=_mapping(data.get("issue_closure")),
            timestamps=_mapping(data.get("timestamps")),
            artifacts={str(k): str(v)
                       for k, v in data.get("artifacts", {}).items()}
            if isinstance(data.get("artifacts"), Mapping) else {},
            status=str(data.get("status", "")),
            created_at=str(data.get("created_at", "")),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            release_version=str(data.get("release_version",
                                        RELEASE_VERSION)),
        ).validate()


def _mapping(value: object) -> Mapping[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


__all__ = [
    "CI_BLOCKING_STATES",
    "CI_CANCELLED",
    "CI_FAILURE",
    "CI_GREEN_STATES",
    "CI_IN_PROGRESS",
    "CI_MISSING",
    "CI_QUEUED",
    "CI_STATES",
    "CI_SUCCESS",
    "CI_UNKNOWN",
    "DEFAULT_MERGE_METHOD",
    "GATE_GO_COMMIT",
    "GATE_GO_MERGE",
    "MERGE_MERGE",
    "MERGE_METHODS",
    "MERGE_REBASE",
    "MERGE_SQUASH",
    "PR_CLOSED",
    "PR_MERGED",
    "PR_OPEN",
    "PULL_REQUEST_STATES",
    "RELEASE_GATES",
    "RELEASE_REASON_CODES",
    "RELEASE_STAGES",
    "RELEASE_STAGE_SEQUENCE",
    "RELEASE_VERSION",
    "RST_BLOCKED",
    "RST_CI_FAILED",
    "RST_CI_PENDING",
    "RST_CI_SUCCESS",
    "RST_COMMITTED",
    "RST_COMMIT_HANDOFF",
    "RST_MERGE_HANDOFF",
    "RST_MERGED",
    "RST_PR_BOUND",
    "RST_READY_FOR_COMMIT",
    "RST_RELEASED",
    "R_ALREADY_COMMITTED",
    "R_BRANCH_MOVED",
    "R_CI_MISSING",
    "R_CI_NOT_GREEN",
    "R_MALFORMED",
    "R_OK",
    "R_STALE_REVIEW",
    "R_UNAUTHORIZED",
    "SCHEMA_VERSION",
    "TERMINAL_RELEASE_STAGES",
    "CiJob",
    "CiRun",
    "CiStatus",
    "CommitHandoff",
    "CommitResult",
    "MergeHandoff",
    "MergeResult",
    "PullRequestBinding",
    "ReleaseClosure",
    "ReleaseError",
    "ReleaseState",
    "ReviewGateEvidence",
    "ci_allows_merge",
    "classify_check_run",
    "combine_ci_states",
    "is_git_sha",
    "is_sha256",
    "utc_now",
]
