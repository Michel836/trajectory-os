"""M024 — operational local CPU/GPU/VRAM arbitration.

:class:`ResourceArbiter` makes the proven pure admission policy operational:
it discovers the real local capacity, keeps an explicit reservation ledger,
performs deterministic admission control with bounded concurrency, prevents
oversubscription, distinguishes remote inference (no local GPU use) from local
GPU workloads, and isolates the local reviewer's reserved CPU/VRAM pool.

The arbiter is fail-closed: a missing dimension defers (never guesses a
capacity), a duplicate reservation is rejected, and concurrency bounds are
hard. Reservations may optionally be persisted so a restarted process
reconstructs the exact active set.
"""

from __future__ import annotations

from pathlib import Path

from trajectory_os.resources import model, probe, store
from trajectory_os.runs import model as runs_model
from trajectory_os.runs import resources as runs_resources


class ResourceArbiter:
    """Deterministic, operational local resource arbiter."""

    def __init__(
        self,
        report: model.LocalResourceReport,
        *,
        policy: model.ResourcePolicy | None = None,
        root: str | Path | None = None,
    ) -> None:
        self._report = report
        self._policy = (policy or model.ResourcePolicy()).validate()
        self._root = Path(root) if root is not None else None
        self._reservations: dict[str, model.Reservation] = {}
        if self._root is not None:
            self._reservations = store.load_reservations(self._root)

    # -- introspection ---------------------------------------------------------

    @property
    def report(self) -> model.LocalResourceReport:
        return self._report

    @property
    def policy(self) -> model.ResourcePolicy:
        return self._policy

    def active(self) -> tuple[model.Reservation, ...]:
        return tuple(self._reservations[key]
                     for key in sorted(self._reservations))

    def capacity(self) -> runs_resources.ResourceCapacity:
        return self._report.capacity()

    def usage(
        self,
        *,
        role: str | None = None,
        locality: str | None = None,
    ) -> runs_resources.ResourceUsage:
        total = runs_resources.ResourceUsage()
        for reservation in self._reservations.values():
            if role is not None and reservation.role != role:
                continue
            if locality is not None and reservation.locality != locality:
                continue
            total = total.add(reservation.usage())
        return total

    def workload_usage(self) -> runs_resources.ResourceUsage:
        """Usage attributable to non-reviewer (agent/job) workloads."""
        total = runs_resources.ResourceUsage()
        for reservation in self._reservations.values():
            if reservation.role in model.REVIEWER_ROLES:
                continue
            total = total.add(reservation.usage())
        return total

    def effective_capacity(
        self, *, role: str, locality: str,
    ) -> runs_resources.ResourceCapacity:
        """Capacity available to a workload class (reviewer pool excluded).

        The reviewer owns the reserved pool, so its effective capacity is the
        full machine. Every other workload can only use the machine minus the
        reviewer's explicit reserves.
        """
        total = self._report.capacity()
        if role in model.REVIEWER_ROLES:
            return total
        return runs_resources.ResourceCapacity(
            cpu_slots=_subtract(total.cpu_slots,
                                self._policy.reviewer_cpu_reserve_slots),
            ram_bytes=_subtract(total.ram_bytes, 0),
            gpu_slots=_subtract(total.gpu_slots, 0),
            gpu_mem_bytes=_subtract(
                total.gpu_mem_bytes,
                self._policy.reviewer_vram_reserve_bytes),
        )

    # -- admission -------------------------------------------------------------

    def admit(
        self,
        job_id: str,
        requirement: runs_resources.ResourceRequirement | None,
        *,
        locality: str = model.LOCAL,
        role: str = model.ROLE_JOB,
    ) -> model.AdmissionDecision:
        """Deterministically admit (and reserve) or defer one workload."""
        if not isinstance(job_id, str) or not job_id.strip():
            return model.AdmissionDecision(
                allowed=False, reason=model.AR_MALFORMED,
                reasons=(model.AR_MALFORMED,))
        if locality not in model.LOCALITIES:
            return model.AdmissionDecision(
                allowed=False, reason=model.AR_MALFORMED,
                reasons=(model.AR_MALFORMED,))
        if role not in model.ROLES:
            return model.AdmissionDecision(
                allowed=False, reason=model.AR_MALFORMED,
                reasons=(model.AR_MALFORMED,))
        if job_id in self._reservations:
            return model.AdmissionDecision(
                allowed=False, reason=model.AR_DUPLICATE,
                reasons=(model.AR_DUPLICATE,))
        try:
            validated = (requirement.validate()
                         if requirement is not None else None)
        except runs_resources.ResourcePolicyError:
            return model.AdmissionDecision(
                allowed=False, reason=model.AR_MALFORMED,
                reasons=(model.AR_MALFORMED,))

        declared = False
        dimensions: dict[str, int] = {}
        if validated is not None and validated.declared:
            declared = True
            dimensions = model.with_locality_demand(
                validated, locality).dimensions()
        reasons: list[str] = []

        # Bounded concurrency (hard, no oversubscription by count). This gate
        # applies to EVERY admitted workload, including a zero-resource
        # (undeclared) job: such a job still occupies a slot in the live
        # reservation set and is counted by every later admission, so letting
        # it skip the bound would make ``max_total_concurrency`` /
        # ``max_local_concurrency`` trivially bypassable.
        if len(self._reservations) >= self._policy.max_total_concurrency:
            reasons.append(model.AR_CONCURRENCY)
        local_active = sum(
            1 for r in self._reservations.values()
            if r.locality == model.LOCAL)
        if locality == model.LOCAL \
                and local_active >= self._policy.max_local_concurrency:
            reasons.append(model.AR_LOCAL_CONCURRENCY)

        if declared:
            effective = self.effective_capacity(role=role, locality=locality)
            if role not in model.REVIEWER_ROLES:
                # Non-reviewer workloads cannot consume the reviewer reserves.
                current = self.workload_usage()
                # The reviewer pool and the workload pool are both carved out
                # of the SAME physical capacity, so a role-local check is not
                # sufficient: a reviewer that legitimately reserves more than
                # the static reserve would otherwise let the two pools sum
                # past the machine. Enforce the global machine invariant too.
                total_usage = self.usage()
                machine = self._report.capacity()
                for dimension in runs_model.RESOURCE_DIMENSIONS:
                    needed = dimensions.get(dimension)
                    if needed is None:
                        continue
                    capacity = getattr(effective, dimension, None)
                    if capacity is None:
                        reasons.append(f"{model.AR_UNKNOWN}:{dimension}")
                    elif getattr(current, dimension, 0) + needed > capacity:
                        reasons.append(f"{model.AR_EXHAUSTED}:{dimension}")
                    machine_capacity = getattr(machine, dimension, None)
                    if machine_capacity is not None and \
                            getattr(total_usage, dimension, 0) + needed \
                            > machine_capacity:
                        reasons.append(f"{model.AR_EXHAUSTED}:{dimension}")
            else:
                # The reviewer owns the reserved pool but may never
                # oversubscribe the machine as a whole.
                total_usage = self.usage()
                total = self._report.capacity()
                for dimension in runs_model.RESOURCE_DIMENSIONS:
                    needed = dimensions.get(dimension)
                    if needed is None:
                        continue
                    capacity = getattr(total, dimension, None)
                    if capacity is None:
                        reasons.append(f"{model.AR_UNKNOWN}:{dimension}")
                    elif getattr(total_usage, dimension, 0) + needed > capacity:
                        reasons.append(f"{model.AR_EXHAUSTED}:{dimension}")

        if reasons:
            return model.AdmissionDecision(
                allowed=False,
                reason=model.summarize_reasons(tuple(reasons)),
                reasons=tuple(sorted(set(reasons))))
        reservation = _reservation(
            job_id, locality, role,
            cpu_slots=dimensions.get("cpu_slots", 0),
            ram_bytes=dimensions.get("ram_bytes", 0),
            gpu_slots=dimensions.get("gpu_slots", 0),
            gpu_mem_bytes=dimensions.get("gpu_mem_bytes", 0))
        self._commit(reservation)
        return model.AdmissionDecision(
            allowed=True, reason=model.AR_OK, reasons=(),
            reservation=reservation)

    def release(self, job_id: str) -> bool:
        """Release one reservation (idempotent; ``False`` when absent)."""
        if job_id not in self._reservations:
            return False
        del self._reservations[job_id]
        self._persist()
        return True

    def clear(self) -> None:
        self._reservations.clear()
        self._persist()

    # -- persistence -----------------------------------------------------------

    def _commit(self, reservation: model.Reservation) -> None:
        self._reservations[reservation.job_id] = reservation
        self._persist()

    def _persist(self) -> None:
        if self._root is not None:
            store.save_reservations(self._root, self._reservations)

    # -- snapshot --------------------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        """Deterministic, machine-readable live arbitration state."""
        capacity = self._report.capacity()
        policy = self._policy.to_dict()
        usage = self.usage()
        local_usage = self.usage(locality=model.LOCAL)
        reviewer_usage = self.usage(role=model.ROLE_REVIEWER)
        return {
            "schema_version": model.SCHEMA_VERSION,
            "resource_version": model.RESOURCE_VERSION,
            "report": self._report.to_dict(),
            "capacity": capacity.to_dict(),
            "policy": policy,
            "reservations": [r.to_dict() for r in self.active()],
            "reservation_count": len(self._reservations),
            "local_count": sum(
                1 for r in self._reservations.values()
                if r.locality == model.LOCAL),
            "usage": usage.to_dict(),
            "local_usage": local_usage.to_dict(),
            "reviewer_usage": reviewer_usage.to_dict(),
            "effective": {
                "reviewer": self.effective_capacity(
                    role=model.ROLE_REVIEWER, locality=model.LOCAL).to_dict(),
                "workload": self.effective_capacity(
                    role=model.ROLE_JOB, locality=model.LOCAL).to_dict(),
            },
        }

    @classmethod
    def discover(
        cls,
        *,
        root: str | Path | None = None,
        policy: model.ResourcePolicy | None = None,
        environment: dict[str, str] | None = None,
    ) -> ResourceArbiter:
        """Discover local resources and construct an arbiter."""
        report = probe.discover(environment=environment)
        return cls(report, policy=policy, root=root)


def _reservation(
    job_id: str,
    locality: str,
    role: str,
    *,
    cpu_slots: int = 0,
    ram_bytes: int = 0,
    gpu_slots: int = 0,
    gpu_mem_bytes: int = 0,
) -> model.Reservation:
    return model.Reservation(
        job_id=job_id, locality=locality, role=role,
        cpu_slots=cpu_slots, ram_bytes=ram_bytes, gpu_slots=gpu_slots,
        gpu_mem_bytes=gpu_mem_bytes)


def _subtract(total: int | None, reserved: int) -> int | None:
    if total is None:
        return None
    return max(0, total - max(0, reserved))
