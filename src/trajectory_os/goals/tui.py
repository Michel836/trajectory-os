"""M019 — live terminal dashboard over the canonical goal snapshot.

The renderer is a *pure function* of one snapshot document: the same snapshot
always renders the same frame, so the TUI can never invent or mutate state.
The driver only decides when to re-read the snapshot and how to refresh the
screen (ANSI clear + home), which keeps it usable on one terminal and
directly unit-testable without a real TTY.

No third-party TUI dependency is introduced.
"""

from __future__ import annotations

import math
import shutil
import sys
import time
from collections.abc import Callable, Mapping
from typing import Any, TextIO

from trajectory_os.goals.snapshot import build_snapshot, utc_now_iso

#: ANSI: clear screen + move cursor home (a full, flicker-free redraw).
CLEAR_SCREEN = "\x1b[2J\x1b[H"

DEFAULT_WIDTH = 100
DEFAULT_HEIGHT = 34
MAX_BLOCKERS = 6
MAX_CRITERIA = 8
MAX_WHY = 4
MAX_MISSIONS = 6


def _clip(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def _elapsed_text(seconds: object) -> str:
    """Format an elapsed duration, failing safe on anything non-duration.

    Elapsed values legitimately arrive as ``int`` or ``float`` (for example a
    ``time.time()`` difference). Only a finite, non-negative number is a
    duration; everything else renders ``-`` rather than a bogus clock.
    """
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return "-"
    if isinstance(seconds, float) and not math.isfinite(seconds):
        return "-"
    total = int(seconds)
    if total < 0:
        return "-"
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _active_lines(activity: Mapping[str, Any]) -> list[str]:
    agent = activity.get("agent")
    label = "running"
    if not isinstance(agent, Mapping):
        agent = activity.get("last")
        label = "last"
    if not isinstance(agent, Mapping):
        return ["  (no mission activity)"]
    elapsed = _elapsed_text(agent.get("elapsed_seconds"))
    state = "RUNNING" if agent.get("running") else "idle"
    return [
        f"  node     : {agent.get('node_id')}  [{label} {state}]",
        f"  mission  : {agent.get('mission_id')} "
        f"state={agent.get('mission_state')} "
        f"({agent.get('mission_reason')})",
        f"  phase    : {agent.get('phase_id')} "
        f"kind={agent.get('phase_kind')}",
        f"  agent    : {agent.get('agent_backend')} "
        f"provider={agent.get('provider')} "
        f"model={agent.get('model')} "
        f"locality={agent.get('locality')}",
        f"  verify   : validate={agent.get('validate_state')} "
        f"review={agent.get('review_state')}",
        f"  elapsed  : {elapsed}  started={agent.get('started_at')}",
    ]


def _local_resource_lines(document: Any) -> list[str]:
    """Render the optional M024 live local resource state (pure)."""
    if not isinstance(document, Mapping):
        return []
    report = document.get("report")
    report = report if isinstance(report, Mapping) else {}
    usage = document.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    gpu = report.get("gpu_name") or "unavailable"
    nvidia = "nvidia" if report.get("nvidia") else "no-nvidia"
    known = "known" if report.get("gpu_known") else "unknown"
    return [
        f"local     : cpu={report.get('cpu_slots', '?')} "
        f"ram={_gib(report.get('ram_bytes'))} "
        f"gpu={gpu} ({nvidia}/{known})",
        f"vram      : total={_gib(report.get('gpu_mem_bytes'))} "
        f"used={_gib(report.get('gpu_mem_used_bytes'))} "
        f"util={report.get('gpu_utilization_pct', '?')}% "
        f"reserved={_gib(usage.get('gpu_mem_bytes'))} "
        f"slots={document.get('reservation_count', 0)}",
    ]


def _gib(value: object) -> str:
    if not isinstance(value, int):
        return "?"
    return f"{value / (1024 ** 3):.1f}GiB"


def render_frame(snapshot: Mapping[str, Any], *,
                 width: int = DEFAULT_WIDTH,
                 height: int = DEFAULT_HEIGHT,
                 now_iso: str | None = None) -> str:
    """Render one complete live frame (pure; deterministic)."""
    now_iso = now_iso or snapshot.get("generated_at") or utc_now_iso()
    goal = snapshot["goal"]
    final = snapshot["final"]
    proof = snapshot["proof"]
    generation = snapshot.get("generation") or {}
    counts = snapshot["counts"]
    readiness = snapshot["readiness"]
    resources = snapshot.get("resources", {})
    gate = snapshot["gate"]
    activity = snapshot.get("activity", {})
    stop = snapshot.get("stop")

    lines = [
        f"TrajectoryOS goal dashboard   {now_iso}",
        "=" * max(0, min(width, 78)),
        f"goal      : {goal['goal_id']}   "
        f"{'STOP REQUESTED' if stop else ''}".rstrip(),
        f"objective : {goal['objective']}",
        f"final     : {final['state']}/{final['reason']} "
        f"complete={final['complete']}",
        f"generation: {generation.get('generation_number', '-')} "
        f"{generation.get('generation_id', '-')}",
        f"proof     : {proof.get('proof_id')} "
        f"persisted={proof.get('persisted_proof_id')} "
        f"stale={proof.get('stale')}",
        "",
        "missions  : "
        f"total={counts['missions_total']} proven={counts['missions_proven']} "
        f"incomplete={counts['missions_incomplete']} "
        f"missing={counts['missions_missing']}",
    ]
    for node in snapshot.get("decomposition", [])[:MAX_MISSIONS]:
        lines.append(
            f"  - {node.get('node_id')} [{node.get('state')}] "
            f"mission={node.get('mission_id')} "
            f"state={node.get('mission_state')}")
    remaining = len(snapshot.get("decomposition", [])) - MAX_MISSIONS
    if remaining > 0:
        lines.append(f"  ... {remaining} more")
    lines.extend([
        "nodes     : "
        f"complete={len(readiness['complete'])} "
        f"ready={len(readiness['ready'])} "
        f"inflight={len(readiness['in_progress'])} "
        f"blocked={len(readiness['blocked'])}",
        f"criteria  : proven={counts['criteria_proven']}/"
        f"{counts['criteria_total']} "
        f"unproven={counts['criteria_unproven']}",
        f"path      : {' '.join(snapshot['critical_path']) or '-'}",
        f"gate      : {gate['state']} human={gate['human_action_required']} "
        f"({gate['reason']})",
        f"resources : "
        f"cpu={resources.get('cpu_reserved')}/{resources.get('cpu_limit')} "
        f"gpu={resources.get('gpu_reserved')}/{resources.get('gpu_limit')} "
        f"vram={resources.get('vram_reserved')}/{resources.get('vram_limit')} "
        f"slots={resources.get('slots_reserved')}/"
        f"{resources.get('concurrency_limit')}",
        "",
        "active:",
    ])
    lines.extend(_local_resource_lines(snapshot.get("local_resources")))
    portfolio = snapshot.get("portfolio")
    if isinstance(portfolio, Mapping) and portfolio.get("present"):
        entry = portfolio.get("entry")
        entry_map = entry if isinstance(entry, Mapping) else {}
        lines.append(
            f"portfolio : member={portfolio.get('member')} "
            f"outcome={entry_map.get('outcome', '-')} "
            f"reason={entry_map.get('reason', '-')}"
        )
    daemon = snapshot.get("daemon")
    if isinstance(daemon, Mapping) and daemon.get("present"):
        lines.append(
            f"daemon    : status={daemon.get('daemon_status')} "
            f"cycles={daemon.get('cycles_executed')} "
            f"selected={daemon.get('selected_in_cycles', 0)}"
        )
    lines.extend(_active_lines(activity))

    replans = snapshot.get("replans", {})
    projection = (replans.get("projection") or {}) if isinstance(
        replans, Mapping) else {}
    lines.append(
        f"replans   : accepted={projection.get('accepted', 0)} "
        f"supersessions={projection.get('supersessions', 0)} "
        f"rejected={projection.get('rejected', 0)} "
        f"generations={projection.get('generation_count', 0)}")

    artifacts = snapshot.get("artifacts")
    if isinstance(artifacts, Mapping) and artifacts.get("present"):
        artifact_counts = artifacts.get("counts")
        artifact_counts = (artifact_counts
                           if isinstance(artifact_counts, Mapping) else {})
        lines.append(
            f"artifacts : total={artifact_counts.get('artifacts_total', 0)} "
            f"missions={artifact_counts.get('missions_total', 0)} "
            f"lineage={str(artifacts.get('lineage_id'))[:12]}")

    blockers = snapshot.get("blockers", [])
    lines.append(f"blockers  : {len(blockers)}")
    for blocker in blockers[:MAX_BLOCKERS]:
        lines.append(
            f"  ! {blocker.get('kind')}:{blocker.get('subject')} "
            f"{blocker.get('reason')}")
    if len(blockers) > MAX_BLOCKERS:
        lines.append(f"  ... {len(blockers) - MAX_BLOCKERS} more")

    lines.append("evidence:")
    criteria = snapshot.get("evidence", {}).get("criteria", [])
    for criterion in criteria[:MAX_CRITERIA]:
        mark = "+" if criterion.get("status") == "PROVEN" else "!"
        lines.append(
            f"  {mark} {criterion.get('node_id')}/"
            f"{criterion.get('criterion_id')} "
            f"{criterion.get('status')} trust={criterion.get('trust')}")
    if len(criteria) > MAX_CRITERIA:
        lines.append(f"  ... {len(criteria) - MAX_CRITERIA} more")

    why = snapshot.get("why", [])
    if why:
        lines.append("why:")
        for item in why[:MAX_WHY]:
            lines.append(f"  - {item}")

    clipped = [_clip(line, width) for line in lines]
    visible = clipped[: max(1, height)]
    return CLEAR_SCREEN + "\n".join(visible) + "\n"


def render_error_frame(message: str, *, width: int = DEFAULT_WIDTH,
                       height: int = DEFAULT_HEIGHT) -> str:
    """Render a bounded error frame (never leaks a traceback to the TTY)."""
    lines = [
        "TrajectoryOS goal dashboard   (read error)",
        "=" * max(0, min(width, 78)),
        f"error     : {message}",
    ]
    clipped = [_clip(line, width) for line in lines]
    return CLEAR_SCREEN + "\n".join(clipped[: max(1, height)]) + "\n"


def run_tui(
    root: str,
    goal_id: str,
    *,
    interval_s: float = 2.0,
    iterations: int | None = None,
    stream: TextIO | None = None,
    width: int | None = None,
    height: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    snapshot_fn: Callable[..., dict[str, Any]] = build_snapshot,
    clock: Callable[[], str] = utc_now_iso,
    resource_probe: Callable[[], Mapping[str, Any]] | None = None,
) -> int:
    """Live-refresh the goal dashboard until interrupted / bounded.

    ``iterations`` bounds the loop (tests use a small bound); when ``None``
    the loop runs until ``KeyboardInterrupt``. ``resource_probe`` is an
    optional M024 live local-resource provider; when supplied its document is
    attached to each snapshot for display.
    """
    stream = stream if stream is not None else sys.stdout
    if width is None or height is None:
        terminal = shutil.get_terminal_size(fallback=(DEFAULT_WIDTH,
                                                      DEFAULT_HEIGHT))
        width = width or terminal.columns
        height = height or terminal.lines
    count = 0
    while iterations is None or count < iterations:
        count += 1
        try:
            snapshot = snapshot_fn(root, goal_id, now_iso=clock())
            if resource_probe is not None:
                snapshot = dict(snapshot)
                snapshot["local_resources"] = dict(resource_probe())
            frame = render_frame(snapshot, width=width, height=height)
        except Exception as exc:  # noqa: BLE001 - a read error must not crash
            frame = render_error_frame(
                f"{type(exc).__name__}: {exc}", width=width, height=height)
        stream.write(frame)
        stream.flush()
        if iterations is not None and count >= iterations:
            break
        sleep(interval_s)
    return 0
