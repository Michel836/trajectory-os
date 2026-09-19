"""M024 — local resource discovery and arbitration unit tests (no hardware)."""

from __future__ import annotations

from pathlib import Path

from trajectory_os.resources import arbiter as arbiter_module
from trajectory_os.resources import model, probe
from trajectory_os.resources import store as resource_store
from trajectory_os.runs import resources as runs_resources

GIB = 1024 ** 3


def _report(**overrides: object) -> model.LocalResourceReport:
    base: dict[str, object] = {
        "probed_at": "2026-01-01T00:00:00Z",
        "cpu_slots": 16, "ram_bytes": 64 * GIB, "gpu_count": 1,
        "gpu_mem_bytes": 24 * GIB, "gpu_mem_used_bytes": 2 * GIB,
        "gpu_utilization_pct": 5, "gpu_name": "NVIDIA GeForce RTX 3090",
        "nvidia": True, "cpu_known": True, "ram_known": True,
        "gpu_known": True, "errors": (),
    }
    base.update(overrides)
    return model.LocalResourceReport.build(**base)  # type: ignore[arg-type]


# --- probe --------------------------------------------------------------------


def test_parse_nvidia_smi_skips_malformed_lines() -> None:
    out = (
        "NVIDIA GeForce RTX 3090, 24576, 1024, 7\n"
        "garbage line\n"
        ", 100, 0, 1\n"
        "NVIDIA GeForce RTX 3090, 24576, 2048, 10\n"
    )
    samples = probe.parse_nvidia_smi(out)
    assert [s.used_mib for s in samples] == [1024, 2048]
    assert samples[0].name == "NVIDIA GeForce RTX 3090"


def test_detect_ram_bytes(tmp_path: Path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       65661664 kB\nMemFree: 1 kB\n",
                       encoding="utf-8")
    assert probe.detect_ram_bytes(meminfo) == 65661664 * 1024
    assert probe.detect_ram_bytes(tmp_path / "absent") is None


def test_detect_cpu_slots_injected() -> None:
    assert probe.detect_cpu_slots(lambda: 12) == 12
    assert probe.detect_cpu_slots(lambda: None) is None
    assert probe.detect_cpu_slots() is not None  # real affinity/count


def test_discover_with_gpu_runner(tmp_path: Path) -> None:
    def runner(argv: list[str], timeout_s: int) -> tuple[int, str, str]:
        return 0, "NVIDIA GeForce RTX 3090, 24576, 4096, 11\n", ""

    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 65536000 kB\n", encoding="utf-8")
    report = probe.discover(
        meminfo=meminfo, nvidia_smi="/usr/bin/nvidia-smi", runner=runner,
        cpu_count=lambda: 8, clock=lambda: "2026-01-01T00:00:00Z")
    assert report.gpu_known is True
    assert report.nvidia is True
    assert report.gpu_count == 1
    assert report.gpu_mem_bytes == 24576 * 1024 * 1024
    assert report.gpu_mem_used_bytes == 4096 * 1024 * 1024
    assert report.gpu_utilization_pct == 11
    assert report.cpu_slots == 8
    assert report.ram_bytes == 65536000 * 1024
    assert report.report_id == report.compute_report_id()


def test_discover_without_gpu_is_unknown_not_zero(tmp_path: Path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 65536000 kB\n", encoding="utf-8")
    report = probe.discover(
        meminfo=meminfo, nvidia_smi=None, which=lambda _: None,
        cpu_count=lambda: 8, clock=lambda: "2026-01-01T00:00:00Z")
    assert report.gpu_known is False
    capacity = report.capacity()
    assert capacity.gpu_slots is None
    assert capacity.gpu_mem_bytes is None
    assert any("gpu" in error for error in report.errors)


def test_discover_malformed_gpu_output_is_unknown(tmp_path: Path) -> None:
    def runner(argv: list[str], timeout_s: int) -> tuple[int, str, str]:
        return 0, "not a gpu line\n", ""

    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 1 kB\n", encoding="utf-8")
    report = probe.discover(
        meminfo=meminfo, nvidia_smi="/usr/bin/nvidia-smi", runner=runner,
        cpu_count=lambda: 1, clock=lambda: "2026-01-01T00:00:00Z")
    assert report.gpu_known is False


# --- admission ----------------------------------------------------------------


def _requirement(*, gpu: bool = False, vram: int = 0,
                 cpu: int | None = None) -> runs_resources.ResourceRequirement:
    return runs_resources.ResourceRequirement(
        cpu_slots=cpu, gpu=gpu, gpu_mem_bytes=vram or None).validate()


def test_remote_inference_does_not_consume_local_gpu() -> None:
    arbiter = arbiter_module.ResourceArbiter(_report())
    decision = arbiter.admit(
        "remote-1", _requirement(gpu=True, vram=20 * GIB),
        locality=model.REMOTE, role=model.ROLE_AGENT)
    assert decision.allowed is True
    assert decision.reservation is not None
    assert decision.reservation.gpu_mem_bytes == 0
    assert decision.reservation.gpu_slots == 0


def test_reviewer_reserve_is_isolated_from_agent_workloads() -> None:
    policy = model.ResourcePolicy(
        reviewer_vram_reserve_bytes=8 * GIB, max_local_concurrency=2)
    arbiter = arbiter_module.ResourceArbiter(_report(), policy=policy)
    # Reviewer may use its reserved 8 GiB.
    reviewer = arbiter.admit(
        "rev-1", _requirement(gpu=True, vram=8 * GIB),
        role=model.ROLE_REVIEWER)
    assert reviewer.allowed is True
    # An agent needing 20 GiB exceeds the 24 - 8 = 16 GiB workload pool.
    agent = arbiter.admit(
        "agent-1", _requirement(gpu=True, vram=20 * GIB),
        role=model.ROLE_AGENT)
    assert agent.allowed is False
    assert agent.reason == model.AR_EXHAUSTED
    assert not arbiter.release("nope")
    assert arbiter.release("rev-1") is True
    agent2 = arbiter.admit(
        "agent-2", _requirement(gpu=True, vram=16 * GIB),
        role=model.ROLE_AGENT)
    assert agent2.allowed is True


def test_reviewer_cannot_oversubscribe_total_vram_with_agent() -> None:
    """The reviewer pool and agent pool share one physical GPU.

    A reviewer that reserves MORE than its static reserve must not let the
    agent pool (carved from ``total - reserve``) push the SUM of all admitted
    reservations past the discovered machine capacity. This is the
    oversubscription invariant the M024 acceptance criteria require.
    """
    policy = model.ResourcePolicy(
        reviewer_vram_reserve_bytes=8 * GIB, max_local_concurrency=4)
    arbiter = arbiter_module.ResourceArbiter(_report(), policy=policy)
    reviewer = arbiter.admit(
        "rev-1", _requirement(gpu=True, vram=24 * GIB),
        role=model.ROLE_REVIEWER)
    assert reviewer.allowed is True
    agent = arbiter.admit(
        "agent-1", _requirement(gpu=True, vram=16 * GIB),
        role=model.ROLE_AGENT)
    assert agent.allowed is False
    assert agent.reason == model.AR_EXHAUSTED
    usage = arbiter.usage()
    capacity = _report().capacity()
    assert usage.gpu_mem_bytes <= (capacity.gpu_mem_bytes or 0)


def test_reviewer_cannot_oversubscribe_total_cpu_with_agent() -> None:
    policy = model.ResourcePolicy(
        reviewer_cpu_reserve_slots=2, max_local_concurrency=4)
    arbiter = arbiter_module.ResourceArbiter(_report(), policy=policy)
    reviewer = arbiter.admit(
        "rev-1", _requirement(cpu=16), role=model.ROLE_REVIEWER)
    assert reviewer.allowed is True
    agent = arbiter.admit(
        "agent-1", _requirement(cpu=8), role=model.ROLE_AGENT)
    assert agent.allowed is False
    assert agent.reason == model.AR_EXHAUSTED
    usage = arbiter.usage()
    capacity = _report().capacity()
    assert usage.cpu_slots <= (capacity.cpu_slots or 0)


def test_bounded_local_concurrency_blocks_second_local_job() -> None:
    policy = model.ResourcePolicy(max_local_concurrency=1,
                                  max_total_concurrency=8)
    arbiter = arbiter_module.ResourceArbiter(_report(), policy=policy)
    assert arbiter.admit("a", _requirement(gpu=True, vram=GIB),
                         role=model.ROLE_AGENT).allowed
    second = arbiter.admit("b", _requirement(gpu=True, vram=GIB),
                           role=model.ROLE_AGENT)
    assert second.allowed is False
    assert second.reason == model.AR_LOCAL_CONCURRENCY
    # A remote job is still admissible (remote does not use the local GPU).
    assert arbiter.admit("c", _requirement(), locality=model.REMOTE,
                         role=model.ROLE_AGENT).allowed


def test_undeclared_workloads_respect_total_concurrency_bound() -> None:
    """Zero-resource (undeclared) jobs must not bypass the hard bound.

    An undeclared job is still stored in the live reservation set and is
    counted by every later admission, so it must obey
    ``max_total_concurrency`` exactly like a declared job.
    """
    policy = model.ResourcePolicy(max_total_concurrency=1,
                                  max_local_concurrency=4)
    arbiter = arbiter_module.ResourceArbiter(_report(), policy=policy)
    undeclared = runs_resources.ResourceRequirement().validate()
    assert undeclared.declared is False
    assert arbiter.admit("a", undeclared, role=model.ROLE_JOB).allowed is True
    second = arbiter.admit("b", undeclared, role=model.ROLE_JOB)
    assert second.allowed is False
    assert second.reason == model.AR_CONCURRENCY
    assert len(arbiter.active()) == 1


def test_unknown_capacity_defers_local_gpu_workload() -> None:
    report = _report(gpu_known=False, gpu_count=None, gpu_mem_bytes=None,
                     gpu_mem_used_bytes=None, gpu_utilization_pct=None,
                     gpu_name=None, nvidia=False)
    arbiter = arbiter_module.ResourceArbiter(report)
    decision = arbiter.admit(
        "a", _requirement(gpu=True, vram=GIB), role=model.ROLE_AGENT)
    assert decision.allowed is False
    assert decision.reason == model.AR_UNKNOWN


def test_duplicate_reservation_rejected() -> None:
    arbiter = arbiter_module.ResourceArbiter(_report())
    assert arbiter.admit("a", _requirement(), role=model.ROLE_JOB).allowed
    duplicate = arbiter.admit("a", _requirement(), role=model.ROLE_JOB)
    assert duplicate.allowed is False
    assert duplicate.reason == model.AR_DUPLICATE


def test_invalid_locality_and_role_rejected() -> None:
    arbiter = arbiter_module.ResourceArbiter(_report())
    assert not arbiter.admit("a", _requirement(), locality="mars").allowed
    assert not arbiter.admit("b", _requirement(), role="robot").allowed
    assert not arbiter.admit("", _requirement()).allowed


def test_reservation_persistence_round_trip(tmp_path: Path) -> None:
    policy = model.ResourcePolicy(reviewer_vram_reserve_bytes=8 * GIB)
    first = arbiter_module.ResourceArbiter(
        _report(), policy=policy, root=tmp_path)
    assert first.admit("rev-1", _requirement(gpu=True, vram=4 * GIB),
                       role=model.ROLE_REVIEWER).allowed
    loaded = resource_store.load_reservations(tmp_path)
    assert set(loaded) == {"rev-1"}
    reloaded = arbiter_module.ResourceArbiter(
        _report(), policy=policy, root=tmp_path)
    assert [r.job_id for r in reloaded.active()] == ["rev-1"]
    assert reloaded.release("rev-1") is True
    assert resource_store.load_reservations(tmp_path) == {}


def test_malformed_persisted_reservations_fail_closed(tmp_path: Path) -> None:
    import pytest

    path = resource_store.reservations_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(resource_store.MalformedReservationsError):
        resource_store.load_reservations(tmp_path)
