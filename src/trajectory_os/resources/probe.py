"""M024 — read-only local CPU/GPU/VRAM discovery.

Discovery is bounded, deterministic and strictly read-only:

* CPU slots come from the process CPU affinity (falling back to
  ``os.cpu_count``);
* RAM comes from ``/proc/meminfo`` ``MemTotal``;
* NVIDIA/RTX resources come from one bounded ``nvidia-smi --query-gpu`` call
  (name, total/used VRAM, utilization). No other GPU API is used and no
  vendor library is imported.

Every dimension may be explicitly UNKNOWN (missing nvidia-smi, unreadable
meminfo, malformed output). UNKNOWN evidence is honest: admission against an
unknown dimension defers rather than guessing. Tests inject
``TRAJECTORY_NVIDIA_SMI`` / ``TRAJECTORY_MEMINFO_FILE`` or call
:func:`discover` with explicit dependencies.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trajectory_os.resources import model

#: Standard read-only NVIDIA telemetry query (nounits keeps parsing simple).
NVIDIA_SMI_QUERY = (
    "--query-gpu=name,memory.total,memory.used,utilization.gpu",
    "--format=csv,noheader,nounits",
)

#: Bounded discovery time budget (seconds).
DEFAULT_TIMEOUT_S = 30

#: Environment overrides (dependency injection for tests/operators).
NVIDIA_SMI_ENV = "TRAJECTORY_NVIDIA_SMI"
MEMINFO_ENV = "TRAJECTORY_MEMINFO_FILE"

_MIB = 1024 * 1024


def utc_now_iso() -> str:
    return (datetime.now(UTC).replace(microsecond=0).isoformat() + "Z")


@dataclass(frozen=True)
class GpuSample:
    """One parsed GPU line (name, total MiB, used MiB, utilization %)."""

    name: str
    total_mib: int
    used_mib: int
    utilization_pct: int


def parse_nvidia_smi(stdout: str) -> tuple[GpuSample, ...]:
    """Parse every valid ``nvidia-smi`` line deterministically.

    Malformed lines are skipped rather than failing the whole probe; the
    result is empty when no valid GPU line exists.
    """
    samples: list[GpuSample] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 4:
            continue
        name = parts[0]
        if not name:
            continue
        try:
            total = int(parts[1])
            used = int(parts[2])
            util = int(parts[3])
        except ValueError:
            continue
        if total <= 0 or used < 0:
            continue
        samples.append(GpuSample(
            name=name, total_mib=total, used_mib=used,
            utilization_pct=max(0, min(100, util))))
    return tuple(samples)


def _detect_nvidia(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in
               ("nvidia", "geforce", "rtx", "quadro", "tesla"))


def resolve_nvidia_smi(environment: Mapping[str, str] | None = None) -> str | None:
    env = dict(os.environ if environment is None else environment)
    configured = env.get(NVIDIA_SMI_ENV, "nvidia-smi")
    if "/" in configured:
        path = Path(configured).expanduser()
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which(configured)


def _default_runner(
    argv: Sequence[str], timeout_s: int,
) -> tuple[int, str, str]:
    proc = subprocess.run(  # noqa: S603 - bounded argv, no shell
        list(argv), capture_output=True, text=True, timeout=timeout_s,
        check=False)
    return proc.returncode, proc.stdout, proc.stderr


def detect_cpu_slots(
    cpu_count: Callable[[], int | None] | None = None,
) -> int | None:
    """Detect usable CPU slots from affinity, then ``os.cpu_count``.

    An explicitly injected ``cpu_count`` wins so discovery is deterministic
    under test; the default path always prefers the real CPU affinity.
    """
    if cpu_count is not None:
        injected = cpu_count()
        return injected if isinstance(injected, int) and injected > 0 else None
    try:
        affinity = os.sched_getaffinity(0)
        if affinity:
            return len(affinity)
    except (AttributeError, OSError):
        pass
    value = os.cpu_count()
    return value if isinstance(value, int) and value > 0 else None


def detect_ram_bytes(meminfo_path: Path) -> int | None:
    """Read ``MemTotal`` from the kernel memory summary (read-only)."""
    try:
        text = meminfo_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"^MemTotal:\s+(\d+)\s+kB", text, re.MULTILINE)
    if match is None:
        return None
    try:
        total_kb = int(match.group(1))
    except ValueError:
        return None
    if total_kb <= 0:
        return None
    return total_kb * 1024


@dataclass(frozen=True)
class GpuDetection:
    available: bool
    samples: tuple[GpuSample, ...]
    error: str | None


def detect_nvidia_gpus(
    binary: str | None,
    *,
    runner: Callable[[Sequence[str], int], tuple[int, str, str]] | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> GpuDetection:
    """Run one bounded read-only ``nvidia-smi`` query."""
    if binary is None:
        return GpuDetection(available=False, samples=(), error="unavailable")
    execute = runner or _default_runner
    try:
        code, stdout, stderr = execute(
            [binary, *NVIDIA_SMI_QUERY], max(1, timeout_s))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GpuDetection(available=False, samples=(),
                            error=f"probe failed: {type(exc).__name__}")
    if code != 0:
        detail = (stderr or stdout).strip()
        return GpuDetection(
            available=False, samples=(),
            error=(detail[:200] or f"nvidia-smi exit {code}"))
    samples = parse_nvidia_smi(stdout)
    if not samples:
        return GpuDetection(available=False, samples=(),
                            error="no parsable GPU line")
    return GpuDetection(available=True, samples=samples, error=None)


def discover(
    *,
    environment: Mapping[str, str] | None = None,
    cpu_count: Callable[[], int | None] | None = None,
    meminfo: Path | None = None,
    nvidia_smi: str | None = None,
    which: Callable[[str], str | None] | None = None,
    runner: Callable[[Sequence[str], int], tuple[int, str, str]] | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    clock: Callable[[], str] = utc_now_iso,
) -> model.LocalResourceReport:
    """Discover local resources once (bounded, fail-soft, never raises)."""
    env = dict(os.environ if environment is None else environment)
    cpu_slots = detect_cpu_slots(cpu_count)
    mem_path = meminfo or Path(env.get(MEMINFO_ENV, "/proc/meminfo"))
    ram_bytes = detect_ram_bytes(mem_path)
    binary = nvidia_smi
    if binary is None and which is not None:
        binary = which(env.get(NVIDIA_SMI_ENV, "nvidia-smi"))
    elif binary is None:
        binary = resolve_nvidia_smi(env)
    gpu = detect_nvidia_gpus(binary, runner=runner, timeout_s=timeout_s)

    errors: list[str] = []
    if cpu_slots is None:
        errors.append("cpu: unknown")
    if ram_bytes is None:
        errors.append("ram: unknown")
    if not gpu.available:
        errors.append(f"gpu: {gpu.error or 'unknown'}")

    if gpu.available:
        total_bytes = sum(s.total_mib for s in gpu.samples) * _MIB
        used_bytes = sum(s.used_mib for s in gpu.samples) * _MIB
        utilization = max(s.utilization_pct for s in gpu.samples)
        name = ", ".join(dict.fromkeys(s.name for s in gpu.samples))
        return model.LocalResourceReport.build(
            probed_at=clock(), cpu_slots=cpu_slots, ram_bytes=ram_bytes,
            gpu_count=len(gpu.samples), gpu_mem_bytes=total_bytes,
            gpu_mem_used_bytes=used_bytes,
            gpu_utilization_pct=utilization, gpu_name=name,
            nvidia=any(_detect_nvidia(s.name) for s in gpu.samples),
            cpu_known=cpu_slots is not None, ram_known=ram_bytes is not None,
            gpu_known=True, errors=tuple(errors))
    return model.LocalResourceReport.build(
        probed_at=clock(), cpu_slots=cpu_slots, ram_bytes=ram_bytes,
        cpu_known=cpu_slots is not None, ram_known=ram_bytes is not None,
        gpu_known=False, errors=tuple(errors))
