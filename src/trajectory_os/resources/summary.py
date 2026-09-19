"""M024 — operator-facing live resource status projection.

The document is derived only from one read-only discovery report plus the
persisted reservation ledger; no state is mutated and no admission is
performed. The human rendering is a compact, deterministic view suitable for
the CLI and the live TUI.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from trajectory_os.resources import arbiter as arbiter_module
from trajectory_os.resources import model, probe


def _fmt_bytes(value: object) -> str:
    if not isinstance(value, int):
        return "unknown"
    gib = value / (1024 ** 3)
    return f"{gib:.1f}GiB"


def _fmt_dim(value: object) -> str:
    if value is None:
        return "unknown"
    return str(value)


def status_document(
    *,
    root: str | Path | None = None,
    report: model.LocalResourceReport | None = None,
    policy: model.ResourcePolicy | None = None,
    discover: Callable[[], model.LocalResourceReport] = probe.discover,
) -> dict[str, Any]:
    """Complete machine-readable live resource status (read-only)."""
    live = report if report is not None else discover()
    arbiter = arbiter_module.ResourceArbiter(live, policy=policy, root=root)
    document = arbiter.snapshot()
    document["status"] = "OK"
    document["root"] = None if root is None else str(root)
    return document


def live_report() -> model.LocalResourceReport:
    """Canonical one-shot live discovery (used by the TUI driver)."""
    return probe.discover()


def render_status(document: Mapping[str, Any]) -> str:
    """Compact deterministic human rendering of a resource status document."""
    report = document.get("report", {})
    if not isinstance(report, Mapping):
        report = {}
    usage = document.get("usage", {})
    if not isinstance(usage, Mapping):
        usage = {}
    local_usage = document.get("local_usage", {})
    if not isinstance(local_usage, Mapping):
        local_usage = {}
    reviewer = document.get("reviewer_usage", {})
    if not isinstance(reviewer, Mapping):
        reviewer = {}
    policy = document.get("policy", {})
    if not isinstance(policy, Mapping):
        policy = {}
    gpu_name = report.get("gpu_name") or "unavailable"
    gpu_known = report.get("gpu_known")
    lines = [
        f"cpu       : {_fmt_dim(report.get('cpu_slots'))} slots "
        f"(known={report.get('cpu_known')})",
        f"ram       : {_fmt_bytes(report.get('ram_bytes'))}",
        f"gpu       : {gpu_name} "
        f"(nvidia={report.get('nvidia')}, known={gpu_known}, "
        f"count={_fmt_dim(report.get('gpu_count'))})",
        f"vram      : {_fmt_bytes(report.get('gpu_mem_bytes'))} total, "
        f"{_fmt_bytes(report.get('gpu_mem_used_bytes'))} used, "
        f"util={_fmt_dim(report.get('gpu_utilization_pct'))}%",
        f"reserve   : vram="
        f"{_fmt_bytes(policy.get('reviewer_vram_reserve_bytes'))} "
        f"cpu={policy.get('reviewer_cpu_reserve_slots')} "
        f"local-concurrency={policy.get('max_local_concurrency')} "
        f"total-concurrency={policy.get('max_total_concurrency')}",
        f"reserved  : count={document.get('reservation_count')} "
        f"local={document.get('local_count')} "
        f"cpu={usage.get('cpu_slots')} "
        f"vram={_fmt_bytes(usage.get('gpu_mem_bytes'))} "
        f"(local-vram={_fmt_bytes(local_usage.get('gpu_mem_bytes'))}, "
        f"reviewer-vram={_fmt_bytes(reviewer.get('gpu_mem_bytes'))})",
    ]
    errors = report.get("errors")
    if isinstance(errors, list) and errors:
        lines.append("warnings  : " + "; ".join(str(e) for e in errors))
    reservations = document.get("reservations")
    if isinstance(reservations, list) and reservations:
        lines.append("active    :")
        for entry in reservations:
            if not isinstance(entry, Mapping):
                continue
            lines.append(
                f"  - {entry.get('job_id')} [{entry.get('role')}/"
                f"{entry.get('locality')}] cpu={entry.get('cpu_slots')} "
                f"ram={_fmt_bytes(entry.get('ram_bytes'))} "
                f"gpu={entry.get('gpu_slots')} "
                f"vram={_fmt_bytes(entry.get('gpu_mem_bytes'))}")
    return "\n".join(lines)
