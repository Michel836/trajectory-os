"""M029 — benchmark domain model (pure, fail closed, never invented).

The benchmark compares the proven ``Pi`` runtime with the qualified
``DeepSeek Harness`` runtime on the same task definitions, repository
baseline, validation gates and review semantics. This module owns the
bounded value objects; no I/O, no clocks, no randomness.

Metric provenance is explicit and closed:

* ``PROVIDER``    read verbatim from a structured backend/provider payload;
* ``DERIVED``     computed from at least one provider/local value
                  (for example ``total = prompt + completion``);
* ``LOCAL``       measured by the benchmark runtime itself
                  (for example wall-clock duration);
* ``UNAVAILABLE`` not exposed by the backend/provider. The value is ``None``
                  and an explicit reason is preserved. Nothing is guessed.

A metric that is ``UNAVAILABLE`` MUST carry a non-empty reason. A metric with
any other source MUST carry a value. That invariant is enforced here so no
downstream report can silently display a fabricated number.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import Any, NoReturn

from trajectory_os.agents import model as agent_model
from trajectory_os.benchmark import identity
from trajectory_os.missions import review_protocol

#: Schema version of every durable benchmark document.
SCHEMA_VERSION = 1

#: Human/machine benchmark version string (additive).
BENCHMARK_VERSION = "m029.1"

#: Final independent reviewer model for M029 (never the legacy default).
FINAL_REVIEWER_MODEL = "qwen3.8:27b-q4_K_M"

#: Inline reviewer model (implementation-adjacent, may be absent).
INLINE_REVIEWER_MODEL = "qwen3.8:27b-q4_K_M"

# --- metric provenance (closed set) ------------------------------------------

SRC_PROVIDER = "PROVIDER"
SRC_DERIVED = "DERIVED"
SRC_LOCAL = "LOCAL"
SRC_UNAVAILABLE = "UNAVAILABLE"

SOURCES = frozenset({SRC_PROVIDER, SRC_DERIVED, SRC_LOCAL, SRC_UNAVAILABLE})

# --- execution modes (closed set) --------------------------------------------

MODE_LIVE = "LIVE"
MODE_FIXTURE = "FIXTURE"

EXECUTION_MODES = frozenset({MODE_LIVE, MODE_FIXTURE})

# --- trial statuses (closed set) ---------------------------------------------

TS_PASS = "PASS"
TS_BLOCKED = "BLOCKED"
TS_FAILED = "FAILED"
TS_CANCELLED = "CANCELLED"
TS_UNAVAILABLE = "UNAVAILABLE"

TRIAL_STATUSES = frozenset({
    TS_PASS, TS_BLOCKED, TS_FAILED, TS_CANCELLED, TS_UNAVAILABLE,
})

#: Statuses that count as a trust-gated success.
SUCCESS_STATUSES = frozenset({TS_PASS})

# --- workload classes (closed set) -------------------------------------------

WC_SMALL_REPAIR = "SMALL_TARGETED_REPAIR"
WC_MEDIUM_FEATURE = "MEDIUM_FEATURE_IMPLEMENTATION"
WC_MULTI_FILE = "MULTI_FILE_INTEGRATION_CHANGE"
WC_INTERRUPT_RESUME = "INTERRUPTION_RESUME"
WC_BLOCKED_FAIL_CLOSED = "INTENTIONAL_BLOCKED_FAIL_CLOSED"

WORKLOAD_CLASSES = frozenset({
    WC_SMALL_REPAIR, WC_MEDIUM_FEATURE, WC_MULTI_FILE, WC_INTERRUPT_RESUME,
    WC_BLOCKED_FAIL_CLOSED,
})

# --- benchmark run states (closed set) ---------------------------------------

RUN_RUNNING = "RUNNING"
RUN_READY_FOR_COMMIT = "READY_FOR_COMMIT"
RUN_BLOCKED = "BLOCKED"
RUN_FAILED = "FAILED"
RUN_CANCELLED = "CANCELLED"
RUN_COMPLETE = "COMPLETE"

RUN_STATES = frozenset({
    RUN_RUNNING, RUN_READY_FOR_COMMIT, RUN_BLOCKED, RUN_FAILED,
    RUN_CANCELLED, RUN_COMPLETE,
})

#: Terminal run states (follow mode exits on these).
TERMINAL_RUN_STATES = frozenset({
    RUN_READY_FOR_COMMIT, RUN_BLOCKED, RUN_FAILED, RUN_CANCELLED,
    RUN_COMPLETE,
})

# --- actor roles (closed set) -------------------------------------------------

ROLE_IMPLEMENTATION_AGENT = "IMPLEMENTATION_AGENT"
ROLE_INLINE_REVIEWER = "INLINE_REVIEWER"
ROLE_FINAL_INDEPENDENT_REVIEWER = "FINAL_INDEPENDENT_REVIEWER"

ACTOR_ROLES = frozenset({
    ROLE_IMPLEMENTATION_AGENT, ROLE_INLINE_REVIEWER,
    ROLE_FINAL_INDEPENDENT_REVIEWER,
})

# --- stable reason codes ------------------------------------------------------

R_OK = "OK"
R_TIMEOUT = "TIMEOUT"
R_VALIDATION_FAILED = "VALIDATION_FAILED"
R_REVIEW_REJECTED = "REVIEW_REJECTED"
R_REVIEW_PROTOCOL_INVALID = "REVIEW_PROTOCOL_INVALID"
R_BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
R_BACKEND_FAILED = "BACKEND_FAILED"
R_CANCELLED = "CANCELLED"
R_FAIL_CLOSED_AS_EXPECTED = "FAIL_CLOSED_AS_EXPECTED"
R_FAIL_CLOSED_VIOLATED = "FAIL_CLOSED_VIOLATED"
R_INTERRUPTED = "INTERRUPTED"
R_RESUMED = "RESUMED"
R_NO_PATCH = "NO_PATCH"
R_UNPROVEN = "UNPROVEN"


class BenchmarkError(Exception):
    """Malformed or untrusted benchmark state (fail closed)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _fail(code: str, detail: str = "") -> NoReturn:
    raise BenchmarkError(code, detail)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# --- workload specification ---------------------------------------------------

#: Hard bound on the number of protected paths one workload may declare.
MAX_PROTECTED_PATHS = 64

#: Hard bound on the length of one declared protected path.
MAX_PROTECTED_PATH_LEN = 256


def normalize_workspace_path(value: str) -> str:
    """Normalize a workspace-relative path for explicit equality matching.

    Only backslashes are unified and redundant ``./`` prefixes are removed.
    Absolute paths and any ``..`` traversal component are preserved verbatim
    so a malformed declaration fails closed rather than silently matching a
    different path.
    """
    path = value.replace("\\", "/").strip()
    while path.startswith("./"):
        path = path[2:]
    return path


def protected_paths_touched(protected: Sequence[str],
                            changed: Sequence[str]) -> tuple[str, ...]:
    """Return the declared protected paths present in a change set (pure).

    Matching is exact workspace-relative path equality after minimal
    normalization — never a glob, prefix or substring match — so a declared
    protected set can only match exactly what it names. The result is sorted
    and deduplicated so it is deterministic evidence.
    """
    declared = {
        normalize_workspace_path(path)
        for path in protected
        if path and len(path) <= MAX_PROTECTED_PATH_LEN
    }
    if not declared:
        return ()
    observed = {normalize_workspace_path(path) for path in changed}
    return tuple(sorted(declared & observed))


@dataclass(frozen=True)
class WorkloadSpec:
    """One canonical benchmark workload definition (deterministic input).

    ``protected_paths`` is the workload-owned fail-closed contract: the exact
    workspace-relative paths that must remain untouched for the intentional
    blocked/fail-closed workload to count as a correct refusal. It is derived
    from the workload definition (never hardcoded in the engine), so any
    workload can express its own protected file/path set.
    """

    workload_id: str
    workload_class: str
    title: str
    objective: str
    validation_command: tuple[str, ...]
    expect_pass: bool
    interrupt: bool = False
    fixture: Mapping[str, str] = field(default_factory=dict)
    descriptive_files: tuple[str, ...] = ()
    protected_paths: tuple[str, ...] = ()

    def validate(self) -> WorkloadSpec:
        if not self.workload_id or len(self.workload_id) > 64:
            _fail("MALFORMED_WORKLOAD", "workload_id")
        if self.workload_class not in WORKLOAD_CLASSES:
            _fail("MALFORMED_WORKLOAD", f"class {self.workload_class!r}")
        if not self.objective.strip():
            _fail("MALFORMED_WORKLOAD", "objective required")
        if not self.validation_command:
            _fail("MALFORMED_WORKLOAD", "validation_command required")
        if len(self.protected_paths) > MAX_PROTECTED_PATHS:
            _fail("MALFORMED_WORKLOAD", "too many protected paths")
        for path in self.protected_paths:
            normalized = normalize_workspace_path(path)
            if not normalized or len(normalized) > MAX_PROTECTED_PATH_LEN:
                _fail("MALFORMED_WORKLOAD", f"invalid protected path {path!r}")
            parts = PurePosixPath(normalized).parts
            if normalized.startswith("/") or ".." in parts:
                _fail("MALFORMED_WORKLOAD", f"invalid protected path {path!r}")
        return self

    @property
    def fail_closed_case(self) -> bool:
        """True for the intentional blocked/fail-closed workload."""
        return self.workload_class == WC_BLOCKED_FAIL_CLOSED

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "workload_id": self.workload_id,
            "workload_class": self.workload_class,
            "title": self.title,
            "objective": self.objective,
            "validation_command": list(self.validation_command),
            "expect_pass": self.expect_pass,
            "interrupt": self.interrupt,
            "fixture": dict(sorted(self.fixture.items())),
            "descriptive_files": list(self.descriptive_files),
            "protected_paths": list(self.protected_paths),
        }


# --- reviewer identity --------------------------------------------------------


@dataclass(frozen=True)
class ReviewerObservation:
    """One reviewer identity observation (pure; active means really invoked).

    ``active`` is the *only* thing that authorises a reviewer identity to be
    displayed as the reviewer that produced the outcome. A configured but
    inactive reviewer is recorded as inactive with its own model, so a stale
    or default identity can never be presented as active.
    """

    role: str
    backend: str
    provider: str | None
    model: str
    active: bool
    reason: str
    observation_id: str = ""

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "role": self.role,
            "backend": self.backend,
            "provider": self.provider,
            "model": self.model,
            "active": self.active,
            "reason": self.reason,
        }

    def compute_observation_id(self) -> str:
        return identity.review_id(self.identity_payload())

    @staticmethod
    def build(*, role: str, backend: str, provider: str | None,
              model: str, active: bool, reason: str) -> ReviewerObservation:
        if role not in ACTOR_ROLES:
            _fail("MALFORMED_REVIEWER", f"role {role!r}")
        base = ReviewerObservation(
            role=role, backend=backend, provider=provider, model=model,
            active=active, reason=reason)
        return replace(base, observation_id=base.compute_observation_id())

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "observation_id": self.observation_id}


# --- validation / review / patch outcomes ------------------------------------


@dataclass(frozen=True)
class ValidationOutcome:
    command: tuple[str, ...]
    passed: bool
    exit_code: int | None
    duration_ms: int | None
    timed_out: bool
    output_sha256: str | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "passed": self.passed,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "output_sha256": self.output_sha256,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ReviewOutcome:
    reviewer: ReviewerObservation
    active: bool
    outcome: str
    reason: str
    blocking_count: int
    assessment: Mapping[str, Any] | None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reviewer": self.reviewer.to_dict(),
            "active": self.active,
            "outcome": self.outcome,
            "reason": self.reason,
            "blocking_count": self.blocking_count,
            "assessment": (None if self.assessment is None
                           else dict(self.assessment)),
            "error": self.error,
        }


@dataclass(frozen=True)
class PatchIdentity:
    """Exact semantic patch identity for one trial (or an honest absence).

    ``available`` is False when the workspace is not a Git repository or no
    change could be observed; the explicit ``reason`` is preserved. The
    digest is over the exact unified-diff bytes, never a guessed value.
    """

    available: bool
    sha256: str | None
    files_changed: int | None
    insertions: int | None
    deletions: int | None
    reason: str
    untracked: tuple[str, ...] = ()
    patch_identity: str = ""

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "available": self.available,
            "sha256": self.sha256,
            "files_changed": self.files_changed,
            "insertions": self.insertions,
            "deletions": self.deletions,
            "untracked": list(self.untracked),
        }

    def compute_patch_identity(self) -> str:
        return identity.patch_id(self.identity_payload())

    @staticmethod
    def build(*, available: bool, sha256: str | None,
              files_changed: int | None, insertions: int | None,
              deletions: int | None, reason: str,
              untracked: Sequence[str] = ()) -> PatchIdentity:
        base = PatchIdentity(
            available=available, sha256=sha256, files_changed=files_changed,
            insertions=insertions, deletions=deletions, reason=reason,
            untracked=tuple(untracked))
        return replace(base,
                       patch_identity=base.compute_patch_identity())

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "reason": self.reason,
                "patch_identity": self.patch_identity}


@dataclass(frozen=True)
class UnavailableMetric:
    """One explicitly unavailable metric with its stable reason."""

    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"value": None, "source": SRC_UNAVAILABLE,
                "reason": self.reason}


# --- telemetry ----------------------------------------------------------------


@dataclass(frozen=True)
class PhaseDuration:
    phase: str
    duration_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {"phase": self.phase, "duration_ms": self.duration_ms}


@dataclass(frozen=True)
class TrialTelemetry:
    """Authoritative per-trial telemetry (provider-grounded or explicit NA).

    Every field is optional; ``sources`` names the provenance of every
    recorded field and ``unavailable`` carries an explicit reason for every
    metric the provider/backend does not expose. No value is ever invented.
    """

    request_count: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    cache_read_tokens: int | None
    cache_hit_tokens: int | None
    cache_miss_tokens: int | None
    cache_hit_ratio: float | None
    cost_usd: float | None
    total_duration_ms: int | None
    request_latency_ms: int | None
    ttft_ms: int | None
    first_useful_patch_ms: int | None
    resume_duration_ms: int | None
    prompt_tps: float | None
    generation_tps: float | None
    retries: int
    repairs: int
    review_rejects: int
    protocol_errors: int
    validation_failures: int
    phase_durations: tuple[PhaseDuration, ...]
    cpu_percent: float | None
    ram_bytes: int | None
    gpu_utilization_pct: int | None
    vram_bytes: int | None
    gpu_power_w: float | None
    gpu_temp_c: float | None
    resource_scope: str
    sources: Mapping[str, str] = field(default_factory=dict)
    unavailable: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "request_count": self.request_count,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "cache_hit_ratio": self.cache_hit_ratio,
            "cost_usd": self.cost_usd,
            "total_duration_ms": self.total_duration_ms,
            "request_latency_ms": self.request_latency_ms,
            "ttft_ms": self.ttft_ms,
            "first_useful_patch_ms": self.first_useful_patch_ms,
            "resume_duration_ms": self.resume_duration_ms,
            "prompt_tps": self.prompt_tps,
            "generation_tps": self.generation_tps,
            "retries": self.retries,
            "repairs": self.repairs,
            "review_rejects": self.review_rejects,
            "protocol_errors": self.protocol_errors,
            "validation_failures": self.validation_failures,
            "phase_durations": [p.to_dict() for p in self.phase_durations],
            "cpu_percent": self.cpu_percent,
            "ram_bytes": self.ram_bytes,
            "gpu_utilization_pct": self.gpu_utilization_pct,
            "vram_bytes": self.vram_bytes,
            "gpu_power_w": self.gpu_power_w,
            "gpu_temp_c": self.gpu_temp_c,
            "resource_scope": self.resource_scope,
            "sources": dict(sorted(self.sources.items())),
            "unavailable": dict(sorted(self.unavailable.items())),
        }

    @staticmethod
    def empty() -> TrialTelemetry:
        """All-unavailable telemetry (never a fabricated zero)."""
        fields = (
            "prompt_tokens", "completion_tokens", "total_tokens",
            "cache_read_tokens", "cache_hit_tokens", "cache_miss_tokens",
            "cache_hit_ratio", "cost_usd", "total_duration_ms",
            "request_latency_ms", "ttft_ms", "first_useful_patch_ms",
            "resume_duration_ms", "prompt_tps", "generation_tps",
            "cpu_percent", "ram_bytes", "gpu_utilization_pct", "vram_bytes",
            "gpu_power_w", "gpu_temp_c",
        )
        telemetry = TrialTelemetry(
            request_count=0, prompt_tokens=None, completion_tokens=None,
            total_tokens=None, cache_read_tokens=None, cache_hit_tokens=None,
            cache_miss_tokens=None, cache_hit_ratio=None, cost_usd=None,
            total_duration_ms=None, request_latency_ms=None, ttft_ms=None,
            first_useful_patch_ms=None, resume_duration_ms=None,
            prompt_tps=None, generation_tps=None, retries=0, repairs=0,
            review_rejects=0, protocol_errors=0, validation_failures=0,
            phase_durations=(), cpu_percent=None, ram_bytes=None,
            gpu_utilization_pct=None, vram_bytes=None, gpu_power_w=None,
            gpu_temp_c=None, resource_scope="unavailable",
            sources={"request_count": SRC_LOCAL})
        telemetry = replace(telemetry, sources=_sources_for(telemetry))
        telemetry = replace(
            telemetry,
            unavailable={name: "backend/provider does not expose this metric"
                         for name in fields})
        validate_telemetry(telemetry)
        return telemetry


def _sources_for(telemetry: TrialTelemetry) -> dict[str, str]:
    """Derive the provenance of every recorded field (never a guess).

    Values that are ``None`` are explicitly ``UNAVAILABLE``. The caller is
    responsible for overriding provider/derived/local where it has stronger
    evidence (see :func:`with_sources`).
    """
    fields = TELEMETRY_FIELDS
    out: dict[str, str] = {}
    for name in fields:
        value = getattr(telemetry, name)
        out[name] = SRC_UNAVAILABLE if value is None else SRC_LOCAL
    return out


#: Every metric field whose provenance is tracked.
TELEMETRY_FIELDS = (
    "request_count", "prompt_tokens", "completion_tokens", "total_tokens",
    "cache_read_tokens", "cache_hit_tokens", "cache_miss_tokens",
    "cache_hit_ratio", "cost_usd", "total_duration_ms", "request_latency_ms",
    "ttft_ms", "first_useful_patch_ms", "resume_duration_ms", "prompt_tps",
    "generation_tps", "retries", "repairs", "review_rejects",
    "protocol_errors", "validation_failures", "cpu_percent", "ram_bytes",
    "gpu_utilization_pct", "vram_bytes", "gpu_power_w", "gpu_temp_c",
)


def with_sources(telemetry: TrialTelemetry,
                 overrides: Mapping[str, str]) -> TrialTelemetry:
    """Return ``telemetry`` with explicit per-field provenance overrides."""
    sources = _sources_for(telemetry)
    for name, source in overrides.items():
        if name not in TELEMETRY_FIELDS:
            _fail("MALFORMED_TELEMETRY", f"unknown metric {name!r}")
        if source not in SOURCES:
            _fail("MALFORMED_TELEMETRY", f"unknown source {source!r}")
        sources[name] = source
    # Reasons for UNAVAILABLE metrics are supplied by the caller (a metric
    # may be temporarily unlabelled while being assembled); the final
    # ``validate_telemetry`` at the persistence boundary is authoritative.
    return replace(telemetry, sources=sources)


def validate_telemetry(telemetry: TrialTelemetry) -> None:
    """Fail closed on any invented/unlabelled metric."""
    for name in TELEMETRY_FIELDS:
        value = getattr(telemetry, name)
        source = telemetry.sources.get(name)
        if source is None:
            _fail("MALFORMED_TELEMETRY", f"{name} has no source")
        if source not in SOURCES:
            _fail("MALFORMED_TELEMETRY", f"{name} source {source!r}")
        if source == SRC_UNAVAILABLE:
            if value is not None:
                _fail("MALFORMED_TELEMETRY",
                      f"{name} is UNAVAILABLE but has a value")
            reason = telemetry.unavailable.get(name)
            if not reason:
                _fail("MALFORMED_TELEMETRY",
                      f"{name} is UNAVAILABLE without a reason")
        else:
            if value is None:
                _fail("MALFORMED_TELEMETRY",
                      f"{name} has source {source} but no value")


# --- trial record -------------------------------------------------------------


@dataclass(frozen=True)
class TrialRecord:
    """One fully persisted trial (authoritative, reproducible evidence)."""

    benchmark_run_id: str
    trial_id: str
    workload_id: str
    workload_class: str
    repetition: int
    backend: str
    provider: str | None
    model: str | None
    locality: str
    runtime_version: str | None
    sdk_version: str | None
    mode: str
    status: str
    reason: str
    fail_closed_case: bool
    interrupted: bool
    resumed: bool
    agent: Mapping[str, Any]
    validation: ValidationOutcome
    review: ReviewOutcome
    patch: PatchIdentity
    telemetry: TrialTelemetry
    created_at: str
    record_id: str = ""

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "benchmark_version": BENCHMARK_VERSION,
            "benchmark_run_id": self.benchmark_run_id,
            "trial_id": self.trial_id,
            "workload_id": self.workload_id,
            "workload_class": self.workload_class,
            "repetition": self.repetition,
            "backend": self.backend,
            "provider": self.provider,
            "model": self.model,
            "locality": self.locality,
            "runtime_version": self.runtime_version,
            "sdk_version": self.sdk_version,
            "mode": self.mode,
            "status": self.status,
            "reason": self.reason,
            "fail_closed_case": self.fail_closed_case,
            "interrupted": self.interrupted,
            "resumed": self.resumed,
            "agent": dict(self.agent),
            "validation": self.validation.to_dict(),
            "review": self.review.to_dict(),
            "patch": self.patch.to_dict(),
            "telemetry": self.telemetry.to_dict(),
        }

    def compute_record_id(self) -> str:
        return identity.trial_id(self.identity_payload())

    @staticmethod
    def build(**kwargs: Any) -> TrialRecord:
        base = TrialRecord(record_id="", **kwargs)
        if base.status not in TRIAL_STATUSES:
            _fail("MALFORMED_TRIAL", f"status {base.status!r}")
        if base.mode not in EXECUTION_MODES:
            _fail("MALFORMED_TRIAL", f"mode {base.mode!r}")
        validate_telemetry(base.telemetry)
        return replace(base, record_id=base.compute_record_id())

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "record_id": self.record_id,
                "created_at": self.created_at}


# --- run manifest & summary ---------------------------------------------------


@dataclass(frozen=True)
class RunManifest:
    """The exact inputs and configuration of one benchmark run."""

    benchmark_run_id: str
    created_at: str
    baseline_revision: str | None
    baseline_patch: str | None
    mode: str
    repetitions: int
    backends: tuple[str, ...]
    target_provider: str
    target_model: str
    final_reviewer_model: str
    workload_ids: tuple[str, ...]
    workloads: tuple[Mapping[str, Any], ...]
    environment: Mapping[str, Any]
    manifest_id: str = ""

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "benchmark_version": BENCHMARK_VERSION,
            "benchmark_run_id": self.benchmark_run_id,
            "baseline_revision": self.baseline_revision,
            "baseline_patch": self.baseline_patch,
            "mode": self.mode,
            "repetitions": self.repetitions,
            "backends": list(self.backends),
            "target_provider": self.target_provider,
            "target_model": self.target_model,
            "final_reviewer_model": self.final_reviewer_model,
            "workload_ids": list(self.workload_ids),
            "workloads": [dict(w) for w in self.workloads],
            "environment": dict(self.environment),
        }

    def compute_manifest_id(self) -> str:
        return identity.run_id(self.identity_payload())

    @staticmethod
    def build(**kwargs: Any) -> RunManifest:
        base = RunManifest(manifest_id="", **kwargs)
        return replace(base, manifest_id=base.compute_manifest_id())

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "created_at": self.created_at,
                "manifest_id": self.manifest_id}


# --- outcome helpers ----------------------------------------------------------


def aggregate_status(statuses: Sequence[str]) -> str:
    """Deterministic run-level status from trial statuses (fail closed).

    Precedence: CANCELLED > FAILED > BLOCKED > UNAVAILABLE > PASS. An empty
    trial set is RUNNING (nothing to conclude).
    """
    if not statuses:
        return RUN_RUNNING
    unique = set(statuses)
    if TS_CANCELLED in unique:
        return RUN_CANCELLED
    if TS_FAILED in unique:
        return RUN_FAILED
    if TS_BLOCKED in unique:
        return RUN_BLOCKED
    if TS_UNAVAILABLE in unique:
        return RUN_BLOCKED
    return RUN_READY_FOR_COMMIT


def trial_decision(
    *,
    backend_unavailable: bool,
    validation_passed: bool,
    fail_closed_case: bool,
    fail_closed_violated: bool,
    review_active: bool,
    review_outcome: str,
    cancelled: bool = False,
    unavailable_reason: str | None = None,
    require_review: bool = True,
) -> tuple[str, str]:
    """The single canonical trial ``(status, reason)`` decision (pure).

    Every status/reason decision in the benchmark goes through this function;
    the engine only assembles its inputs. Precedence is explicit and fail
    closed:

    * ``CANCELLED`` wins over everything (an interrupted trial);
    * a backend that could not execute is ``UNAVAILABLE`` with its own reason;
    * the intentional fail-closed workload is ``PASS`` (``FAIL_CLOSED_AS_EXPECTED``)
      only when the backend actually refused to produce a promotable change;
      otherwise it is ``FAILED`` (``FAIL_CLOSED_VIOLATED``);
    * a failing validation is ``FAILED``;
    * an active rejecting/invalid review is ``FAILED``;
    * with ``require_review`` (the default) a would-be pass whose reviewer was
      never invoked is ``BLOCKED`` (``UNPROVEN``) rather than silently passed;
    * only a validated, reviewed/refused trial is ``PASS``.
    """
    if cancelled:
        return TS_CANCELLED, R_CANCELLED
    if backend_unavailable:
        return TS_UNAVAILABLE, (unavailable_reason or R_BACKEND_UNAVAILABLE)
    if fail_closed_case:
        if fail_closed_violated:
            return TS_FAILED, R_FAIL_CLOSED_VIOLATED
        return TS_PASS, R_FAIL_CLOSED_AS_EXPECTED
    if not validation_passed:
        return TS_FAILED, R_VALIDATION_FAILED
    if review_active and review_outcome != review_protocol.OUTCOME_VALID_PASS:
        return TS_FAILED, R_REVIEW_REJECTED
    if require_review and not review_active:
        return TS_BLOCKED, R_UNPROVEN
    return TS_PASS, R_OK


def trial_status_from(
    *,
    backend_unavailable: bool,
    validation_passed: bool,
    fail_closed_case: bool,
    fail_closed_violated: bool,
    review_active: bool,
    review_outcome: str,
    cancelled: bool = False,
) -> str:
    """Canonical trial status only (pure, fail closed).

    Thin projection of :func:`trial_decision` with the trust-gate
    ``require_review`` policy disabled; kept for callers that need the status
    in isolation. The decision itself is never duplicated.
    """
    return trial_decision(
        backend_unavailable=backend_unavailable,
        validation_passed=validation_passed,
        fail_closed_case=fail_closed_case,
        fail_closed_violated=fail_closed_violated,
        review_active=review_active,
        review_outcome=review_outcome,
        cancelled=cancelled,
        require_review=False,
    )[0]


def reviewer_is_reject(review_outcome: str) -> bool:
    return review_outcome in (review_protocol.OUTCOME_VALID_REJECT,
                              review_protocol.OUTCOME_INVALID)


def backend_locality(backend: str, model: str | None) -> str:
    """Best-effort locality classification mirroring ``live_status``."""
    if model and ("deepseek" in model.lower() or "-flash" in model.lower()):
        return "remote"
    if backend == agent_model.BACKEND_DEEPSEEK_HARNESS:
        return "remote"
    if model and any(marker in model.lower() for marker in
                     ("qwen", "ollama", "llama", "local")):
        return "local"
    return "unknown"
