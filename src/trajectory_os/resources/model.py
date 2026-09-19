"""M024 — local CPU/GPU/VRAM orchestration domain model (pure, fail closed).

This module owns the provider-agnostic, deterministic value objects that make
*local* resource arbitration operational on top of the proven
:mod:`trajectory_os.runs.resources` admission policy:

* an authoritative :class:`LocalResourceReport` describing the discovered
  CPU/RAM/NVIDIA resources (every dimension may be explicitly UNKNOWN);
* an explicit :class:`ResourcePolicy` with VRAM/CPU reservations for the
  local reviewer so agent workloads can never oversubscribe the GPU;
* :class:`Reservation` and :class:`AdmissionDecision` records that preserve
  locality (remote inference never consumes the local GPU) and role
  (``agent`` / ``reviewer`` / ``job``) provenance.

No I/O, no clocks and no hardware access happen here; discovery lives in
:mod:`trajectory_os.resources.probe` and runtime arbitration in
:mod:`trajectory_os.resources.arbiter`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trajectory_os.runs import resources as runs_resources

#: Schema version of every M024 resource document.
SCHEMA_VERSION = 1

#: Human/machine resource-orchestration version string (additive).
RESOURCE_VERSION = "m024.1"

# --- locality (closed set) ----------------------------------------------------

LOCAL = "local"
REMOTE = "remote"

LOCALITIES = frozenset({LOCAL, REMOTE})

# --- workload role (closed set) ----------------------------------------------

ROLE_AGENT = "agent"
ROLE_REVIEWER = "reviewer"
ROLE_JOB = "job"

ROLES = frozenset({ROLE_AGENT, ROLE_REVIEWER, ROLE_JOB})

# --- stable, machine-readable admission reason codes --------------------------

AR_OK = "RESOURCE_ADMITTED"
AR_UNKNOWN = "RESOURCE_UNKNOWN"
AR_EXHAUSTED = "RESOURCE_EXHAUSTED"
AR_CONCURRENCY = "RESOURCE_CONCURRENCY_LIMIT"
AR_LOCAL_CONCURRENCY = "RESOURCE_LOCAL_CONCURRENCY_LIMIT"
AR_DUPLICATE = "RESOURCE_DUPLICATE_RESERVATION"
AR_MALFORMED = "RESOURCE_MALFORMED"

#: Every known admission reason code (closed set, fail closed).
ADMISSION_REASONS = frozenset({
    AR_OK, AR_UNKNOWN, AR_EXHAUSTED, AR_CONCURRENCY, AR_LOCAL_CONCURRENCY,
    AR_DUPLICATE, AR_MALFORMED,
})

#: Explicit processor roles that own the reserved GPU pool.
REVIEWER_ROLES = frozenset({ROLE_REVIEWER})


class ResourceOrchestrationError(Exception):
    """Malformed resource orchestration input (fail closed)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class LocalResourceReport:
    """One authoritative, read-only local resource discovery report."""

    probed_at: str
    cpu_slots: int | None
    ram_bytes: int | None
    gpu_count: int | None
    gpu_mem_bytes: int | None
    gpu_mem_used_bytes: int | None
    gpu_utilization_pct: int | None
    gpu_name: str | None
    nvidia: bool
    cpu_known: bool
    ram_known: bool
    gpu_known: bool
    errors: tuple[str, ...] = ()
    report_id: str = ""

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "cpu_slots": self.cpu_slots,
            "ram_bytes": self.ram_bytes,
            "gpu_count": self.gpu_count,
            "gpu_mem_bytes": self.gpu_mem_bytes,
            "gpu_name": self.gpu_name,
            "nvidia": self.nvidia,
            "cpu_known": self.cpu_known,
            "ram_known": self.ram_known,
            "gpu_known": self.gpu_known,
        }

    def compute_report_id(self) -> str:
        import hashlib
        import json
        payload = json.dumps(self.identity_payload(), sort_keys=True,
                             separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(
            ("trajectory-os.local-resource-report.v1\x00" + payload)
            .encode("utf-8")).hexdigest()

    def capacity(self) -> runs_resources.ResourceCapacity:
        """Project discovered facts into the proven pure admission capacity."""
        gpu_slots: int | None = None
        gpu_mem: int | None = None
        if self.gpu_known:
            gpu_slots = self.gpu_count or 0
            gpu_mem = self.gpu_mem_bytes or 0
        return runs_resources.ResourceCapacity(
            cpu_slots=self.cpu_slots if self.cpu_known else None,
            ram_bytes=self.ram_bytes if self.ram_known else None,
            gpu_slots=gpu_slots,
            gpu_mem_bytes=gpu_mem,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "probed_at": self.probed_at,
            "gpu_mem_used_bytes": self.gpu_mem_used_bytes,
            "gpu_utilization_pct": self.gpu_utilization_pct,
            "errors": list(self.errors),
            "report_id": self.report_id,
        }

    @staticmethod
    def build(
        *,
        probed_at: str,
        cpu_slots: int | None = None,
        ram_bytes: int | None = None,
        gpu_count: int | None = None,
        gpu_mem_bytes: int | None = None,
        gpu_mem_used_bytes: int | None = None,
        gpu_utilization_pct: int | None = None,
        gpu_name: str | None = None,
        nvidia: bool = False,
        cpu_known: bool = False,
        ram_known: bool = False,
        gpu_known: bool = False,
        errors: tuple[str, ...] = (),
    ) -> LocalResourceReport:
        base = LocalResourceReport(
            probed_at=probed_at, cpu_slots=cpu_slots, ram_bytes=ram_bytes,
            gpu_count=gpu_count, gpu_mem_bytes=gpu_mem_bytes,
            gpu_mem_used_bytes=gpu_mem_used_bytes,
            gpu_utilization_pct=gpu_utilization_pct, gpu_name=gpu_name,
            nvidia=nvidia, cpu_known=cpu_known, ram_known=ram_known,
            gpu_known=gpu_known, errors=errors)
        return LocalResourceReport(
            probed_at=base.probed_at, cpu_slots=base.cpu_slots,
            ram_bytes=base.ram_bytes, gpu_count=base.gpu_count,
            gpu_mem_bytes=base.gpu_mem_bytes,
            gpu_mem_used_bytes=base.gpu_mem_used_bytes,
            gpu_utilization_pct=base.gpu_utilization_pct,
            gpu_name=base.gpu_name, nvidia=base.nvidia,
            cpu_known=base.cpu_known, ram_known=base.ram_known,
            gpu_known=base.gpu_known, errors=base.errors,
            report_id=base.compute_report_id())


@dataclass(frozen=True)
class ResourcePolicy:
    """Explicit local resource reservations and concurrency bounds."""

    reviewer_vram_reserve_bytes: int = 0
    reviewer_cpu_reserve_slots: int = 0
    max_local_concurrency: int = 1
    max_total_concurrency: int = 8

    def validate(self) -> ResourcePolicy:
        for name, value in (
            ("reviewer_vram_reserve_bytes", self.reviewer_vram_reserve_bytes),
            ("reviewer_cpu_reserve_slots", self.reviewer_cpu_reserve_slots),
            ("max_local_concurrency", self.max_local_concurrency),
            ("max_total_concurrency", self.max_total_concurrency),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ResourceOrchestrationError(
                    AR_MALFORMED, f"{name} must be an integer >= 0")
        if self.max_local_concurrency < 1:
            raise ResourceOrchestrationError(
                AR_MALFORMED, "max_local_concurrency must be >= 1")
        if self.max_total_concurrency < 1:
            raise ResourceOrchestrationError(
                AR_MALFORMED, "max_total_concurrency must be >= 1")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "reviewer_vram_reserve_bytes": self.reviewer_vram_reserve_bytes,
            "reviewer_cpu_reserve_slots": self.reviewer_cpu_reserve_slots,
            "max_local_concurrency": self.max_local_concurrency,
            "max_total_concurrency": self.max_total_concurrency,
        }

    @classmethod
    def from_dict(cls, data: object) -> ResourcePolicy:
        if not isinstance(data, dict):
            raise ResourceOrchestrationError(
                AR_MALFORMED, f"policy: {type(data).__name__}")
        known = {"schema_version", "reviewer_vram_reserve_bytes",
                 "reviewer_cpu_reserve_slots", "max_local_concurrency",
                 "max_total_concurrency"}
        extra = set(data) - known
        if extra:
            raise ResourceOrchestrationError(
                AR_MALFORMED, f"unknown fields: {sorted(extra)}")
        return cls(
            reviewer_vram_reserve_bytes=int(
                data.get("reviewer_vram_reserve_bytes", 0)),
            reviewer_cpu_reserve_slots=int(
                data.get("reviewer_cpu_reserve_slots", 0)),
            max_local_concurrency=int(data.get("max_local_concurrency", 1)),
            max_total_concurrency=int(data.get("max_total_concurrency", 8)),
        ).validate()


@dataclass(frozen=True)
class Reservation:
    """One active admission reservation (locality- and role-tagged)."""

    job_id: str
    locality: str
    role: str
    cpu_slots: int
    ram_bytes: int
    gpu_slots: int
    gpu_mem_bytes: int

    def usage(self) -> runs_resources.ResourceUsage:
        return runs_resources.ResourceUsage(
            cpu_slots=self.cpu_slots, ram_bytes=self.ram_bytes,
            gpu_slots=self.gpu_slots, gpu_mem_bytes=self.gpu_mem_bytes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "job_id": self.job_id,
            "locality": self.locality,
            "role": self.role,
            "cpu_slots": self.cpu_slots,
            "ram_bytes": self.ram_bytes,
            "gpu_slots": self.gpu_slots,
            "gpu_mem_bytes": self.gpu_mem_bytes,
        }

    @classmethod
    def from_dict(cls, data: object) -> Reservation:
        if not isinstance(data, dict):
            raise ResourceOrchestrationError(
                AR_MALFORMED, f"reservation: {type(data).__name__}")
        try:
            locality = str(data["locality"])
            role = str(data["role"])
        except KeyError as exc:
            raise ResourceOrchestrationError(
                AR_MALFORMED, f"missing field {exc}") from exc
        if locality not in LOCALITIES:
            raise ResourceOrchestrationError(
                AR_MALFORMED, f"invalid locality {locality!r}")
        if role not in ROLES:
            raise ResourceOrchestrationError(
                AR_MALFORMED, f"invalid role {role!r}")
        return cls(
            job_id=str(data.get("job_id", "")),
            locality=locality, role=role,
            cpu_slots=int(data.get("cpu_slots", 0)),
            ram_bytes=int(data.get("ram_bytes", 0)),
            gpu_slots=int(data.get("gpu_slots", 0)),
            gpu_mem_bytes=int(data.get("gpu_mem_bytes", 0)),
        )


@dataclass(frozen=True)
class AdmissionDecision:
    """One deterministic local resource admission decision."""

    allowed: bool
    reason: str
    reasons: tuple[str, ...]
    reservation: Reservation | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "allowed": self.allowed,
            "reason": self.reason,
            "reasons": list(self.reasons),
            "reservation": (None if self.reservation is None
                            else self.reservation.to_dict()),
        }


def with_locality_demand(
    requirement: runs_resources.ResourceRequirement,
    locality: str,
) -> runs_resources.ResourceRequirement:
    """Project a requirement onto local demand for a given locality.

    Remote inference performs no local GPU/VRAM work, so those dimensions are
    stripped; CPU/RAM demand is preserved (a remote client still uses host
    memory). Local workloads keep their full declared demand.
    """
    if locality == REMOTE:
        return runs_resources.ResourceRequirement(
            cpu_slots=requirement.cpu_slots,
            ram_bytes=requirement.ram_bytes,
            gpu=False, gpu_mem_bytes=None).validate()
    return requirement


def summarize_reasons(reasons: tuple[str, ...]) -> str:
    """Deterministic precedence: the first stable reason class wins."""
    for code in (AR_MALFORMED, AR_DUPLICATE, AR_UNKNOWN, AR_CONCURRENCY,
                 AR_LOCAL_CONCURRENCY, AR_EXHAUSTED):
        if any(reason == code or reason.startswith(code + ":")
               for reason in reasons):
            return code
    return reasons[0] if reasons else AR_MALFORMED
