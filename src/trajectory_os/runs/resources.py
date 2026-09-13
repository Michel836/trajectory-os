"""V2.04 — explicit local resource requirements and capacity policy.

The admission layer gains an OPTIONAL, deterministic resource gate:

* a job spec MAY declare explicit local resource requirements
  (``cpu_slots``, ``ram_bytes``, ``gpu`` presence, ``gpu_mem_bytes``);
* the operator supplies a **capacity policy** built from *authoritative*
  resource evidence (a plain JSON document);
* the decision is fully deterministic and pure — no hardware probing, no
  provider coupling, no side effects in read-only paths, and no inference
  of capacity from PIDs;
* UNKNOWN evidence (``null`` / ``"unknown"``) for a dimension a job
  requires DEFERS the job (``RESOURCE_UNKNOWN:<dimension>``) — the policy
  never over-commits by guessing;
* exhausted capacity rejects/defers with ``RESOURCE_EXHAUSTED:<dim>``.

Separation of concerns (deliberate):

* **Declaration validity** (``ResourceRequirement``) validates type and
  domain only — positive, JSON-serializable integers of *unbounded*
  magnitude.  A very large but well-formed requirement is a *valid
  declaration*; no local parsing bound may silently reject it;
* **Capacity admissibility** (``evaluate`` against authoritative capacity
  evidence minus usage) decides whether the requirement actually fits:
  a requirement larger than any real evidence is naturally DEFERRED or
  REJECTED (``RESOURCE_UNKNOWN`` / ``RESOURCE_EXHAUSTED``).  Admissibility
  is therefore decided by evidence and policy, never by a hidden parser
  cap on the declaration itself.

All structures are frozen dataclasses with strict, fail-closed parsing:
malformed evidence is rejected with a stable code, never coerced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trajectory_os.runs import model

MAX_CPU_SLOTS = 512
# Bounded slot domain (scheduling units): a JSON-serializable, honest upper
# bound for slot counts only.  Byte dimensions (``ram_bytes``,
# ``gpu_mem_bytes``) are validated as non-negative integers of UNBOUNDED
# magnitude — declaration validity is type/domain only, and actual
# admissibility is decided later by capacity evidence via ``evaluate``.


class ResourcePolicyError(Exception):
    """Malformed resource evidence / requirement (fail closed)."""

    def __init__(
        self, code: str = model.REASON_RESOURCE_POLICY_MALFORMED, message: str = ""
    ) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ResourceRequirement:
    """Explicit per-job local resource requirements (all optional)."""

    cpu_slots: int | None = None
    ram_bytes: int | None = None
    gpu: bool | None = None
    gpu_mem_bytes: int | None = None

    # -- validation ---------------------------------------------------------
    def validate(self) -> ResourceRequirement:
        def _positive_int(value: object, dim: str) -> int | None:
            if value is None:
                return None
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ResourcePolicyError(message=f"{dim} must be a positive integer")
            # No magnitude cap: a very large positive integer is still a
            # valid declaration; admissibility is decided by capacity evidence.
            return value

        if self.cpu_slots is not None:
            cpu = self.cpu_slots
            if isinstance(cpu, bool) or not isinstance(cpu, int) or cpu < 1:
                raise ResourcePolicyError(message="cpu_slots must be a positive integer")
            if cpu > MAX_CPU_SLOTS:
                raise ResourcePolicyError(message="cpu_slots exceeds bounded limit")
        if self.ram_bytes is not None:
            _positive_int(self.ram_bytes, "ram_bytes")
        if self.gpu is not None and not isinstance(self.gpu, bool):
            raise ResourcePolicyError(message="gpu must be a boolean")
        if self.gpu_mem_bytes is not None:
            _positive_int(self.gpu_mem_bytes, "gpu_mem_bytes")
        if self.gpu_mem_bytes is not None and self.gpu is False:
            raise ResourcePolicyError(
                message="gpu_mem_bytes requires gpu=true (inconsistent)"
            )
        return self

    @property
    def declared(self) -> bool:
        return any(
            value is not None
            for value in (self.cpu_slots, self.ram_bytes, self.gpu, self.gpu_mem_bytes)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "cpu_slots": self.cpu_slots,
            "ram_bytes": self.ram_bytes,
            "gpu": self.gpu,
            "gpu_mem_bytes": self.gpu_mem_bytes,
        }

    @classmethod
    def from_dict(cls, data: object) -> ResourceRequirement:
        if not isinstance(data, dict):
            raise ResourcePolicyError(message=f"requirement: {type(data).__name__}")
        known = {"cpu_slots", "ram_bytes", "gpu", "gpu_mem_bytes"}
        extra = set(data) - known
        if extra:
            raise ResourcePolicyError(message=f"unknown fields: {sorted(extra)}")
        req = cls(
            cpu_slots=data.get("cpu_slots"),
            ram_bytes=data.get("ram_bytes"),
            gpu=data.get("gpu"),
            gpu_mem_bytes=data.get("gpu_mem_bytes"),
        )
        return req.validate()

    def dimensions(self) -> dict[str, int]:
        """Declared dimensions sized as integer units (gpu -> 1 slot)."""
        out: dict[str, int] = {}
        if self.cpu_slots is not None:
            out["cpu_slots"] = self.cpu_slots
        if self.ram_bytes is not None:
            out["ram_bytes"] = self.ram_bytes
        if self.gpu is True:
            out["gpu_slots"] = 1
        if self.gpu_mem_bytes is not None:
            out["gpu_mem_bytes"] = self.gpu_mem_bytes
        return out


@dataclass(frozen=True)
class ResourceCapacity:
    """Authoritative per-dimension capacity. ``None`` = UNKNOWN evidence."""

    cpu_slots: int | None = None
    ram_bytes: int | None = None
    gpu_slots: int | None = None
    gpu_mem_bytes: int | None = None

    def __getitem__(self, dim: str) -> int | None:
        if dim == "cpu_slots":
            return self.cpu_slots
        if dim == "ram_bytes":
            return self.ram_bytes
        if dim == "gpu_slots":
            return self.gpu_slots
        if dim == "gpu_mem_bytes":
            return self.gpu_mem_bytes
        raise ResourcePolicyError(message=f"unknown dimension: {dim}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "cpu_slots": self.cpu_slots,
            "ram_bytes": self.ram_bytes,
            "gpu_slots": self.gpu_slots,
            "gpu_mem_bytes": self.gpu_mem_bytes,
        }

    @classmethod
    def from_dict(cls, data: object) -> ResourceCapacity:
        if not isinstance(data, dict):
            raise ResourcePolicyError(message=f"capacity: {type(data).__name__}")
        known = {"cpu_slots", "ram_bytes", "gpu_slots", "gpu_mem_bytes"}
        extra = set(data) - known
        if extra:
            raise ResourcePolicyError(message=f"unknown fields: {sorted(extra)}")

        def _dim(key: str) -> int | None:
            value = data.get(key)
            if value is None or value == "unknown":
                return None  # explicit unknown evidence: honest, not an error
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ResourcePolicyError(message=f"{key} must be an integer >= 0")
            # Byte dimensions carry no magnitude cap: real capacity evidence
            # (even well past any single node's RAM) must be accepted verbatim;
            # admitting or deferring a requirement against it is ``evaluate``'s
            # job.  Only slot domains (scheduling units) are bounded.
            return value

        cpu_slots = _dim("cpu_slots")
        if cpu_slots is not None and cpu_slots > MAX_CPU_SLOTS:
            raise ResourcePolicyError(message="cpu_slots exceeds bounded limit")
        gpu_slots = _dim("gpu_slots")
        if gpu_slots is not None and gpu_slots > MAX_CPU_SLOTS:
            raise ResourcePolicyError(message="gpu_slots exceeds bounded limit")
        return cls(
            cpu_slots=cpu_slots,
            ram_bytes=_dim("ram_bytes"),
            gpu_slots=gpu_slots,
            gpu_mem_bytes=_dim("gpu_mem_bytes"),
        )


@dataclass(frozen=True)
class ResourceUsage:
    """Currently consumed units per dimension (active admissions)."""

    cpu_slots: int = 0
    ram_bytes: int = 0
    gpu_slots: int = 0
    gpu_mem_bytes: int = 0

    def __getitem__(self, dim: str) -> int:
        if dim == "cpu_slots":
            return self.cpu_slots
        if dim == "ram_bytes":
            return self.ram_bytes
        if dim == "gpu_slots":
            return self.gpu_slots
        if dim == "gpu_mem_bytes":
            return self.gpu_mem_bytes
        raise ResourcePolicyError(message=f"unknown dimension: {dim}")

    def add(self, other: ResourceUsage) -> ResourceUsage:
        return ResourceUsage(
            self.cpu_slots + other.cpu_slots,
            self.ram_bytes + other.ram_bytes,
            self.gpu_slots + other.gpu_slots,
            self.gpu_mem_bytes + other.gpu_mem_bytes,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "cpu_slots": self.cpu_slots,
            "ram_bytes": self.ram_bytes,
            "gpu_slots": self.gpu_slots,
            "gpu_mem_bytes": self.gpu_mem_bytes,
        }

    @classmethod
    def from_dict(cls, data: object) -> ResourceUsage:
        if not isinstance(data, dict):
            raise ResourcePolicyError(message=f"usage: {type(data).__name__}")
        known = {"cpu_slots", "ram_bytes", "gpu_slots", "gpu_mem_bytes"}
        extra = set(data) - known
        if extra:
            raise ResourcePolicyError(message=f"unknown fields: {sorted(extra)}")

        def _key(key: str) -> int:
            value = data.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ResourcePolicyError(message=f"{key} must be an integer >= 0")
            return value

        return cls(
            cpu_slots=_key("cpu_slots"),
            ram_bytes=_key("ram_bytes"),
            gpu_slots=_key("gpu_slots"),
            gpu_mem_bytes=_key("gpu_mem_bytes"),
        )


@dataclass(frozen=True)
class ResourceDecision:
    """Deterministic resource admission decision for one requirement."""

    decision: str  # RES_DECISION_ALLOWED | RES_DECISION_DEFERRED
    reasons: tuple[str, ...]

    @property
    def allowed(self) -> bool:
        return self.decision == model.RES_DECISION_ALLOWED

    def to_dict(self) -> dict[str, Any]:
        # Resource decisions are operations evidence (V1.98 ops-view family),
        # not canonical state documents: use the canonical ops schema version.
        return {
            "schema_version": model.OPS_SCHEMA_VERSION,
            "decision": self.decision,
            "reasons": list(self.reasons),
        }


def evaluate(
    requirement: ResourceRequirement | None,
    capacity: ResourceCapacity,
    usage: ResourceUsage | None = None,
) -> ResourceDecision:
    """Pure, deterministic decision. Unknown evidence defers (no overcommit).

    A requirement of ``None`` (or with no declared dimensions) is allowed
    against any valid capacity — resource policy is opt-in.
    """
    if requirement is None or not requirement.declared:
        return ResourceDecision(model.RES_DECISION_ALLOWED, ())
    current = usage or ResourceUsage()
    reasons: list[str] = []
    for dim in model.RESOURCE_DIMENSIONS:
        needed = requirement.dimensions().get(dim)
        if needed is None:
            continue
        cap = capacity[dim]
        if cap is None:
            reasons.append(f"{model.REASON_RESOURCE_UNKNOWN}:{dim}")
            continue
        if current[dim] + needed > cap:
            reasons.append(f"{model.REASON_RESOURCE_EXHAUSTED}:{dim}")
    if reasons:
        return ResourceDecision(model.RES_DECISION_DEFERRED, tuple(reasons))
    return ResourceDecision(model.RES_DECISION_ALLOWED, ())


def policy_from_evidence(doc: object) -> ResourceCapacity:
    """Build a capacity policy from an authoritative evidence document.

    Strict and deterministic: any malformed value raises
    ``ResourcePolicyError`` (fail closed). No hardware probing is performed.
    """
    return ResourceCapacity.from_dict(doc)
