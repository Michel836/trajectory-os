"""M030 — live follow loop (bounded, terminal-aware, notification-optional).

The loop polls the canonical status document on a 10–15 second heartbeat and
exits as soon as the run reaches *any* terminal lifecycle or readiness
outcome. Desktop notification (``notify-send``) is opportunistic: it is
attempted only when the binary is available and never affects correctness.

Observation is read-only: the loop never mutates run artifacts and never
performs a Git trust-boundary write.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from time import sleep as default_sleep
from typing import Any

from trajectory_os.observability import model, projection

#: Default heartbeat window (mission requires 10–15 seconds).
DEFAULT_INTERVAL_S = 12.0

#: Bounded number of polls (prevents an unbounded wait).
DEFAULT_MAX_POLLS = 240

#: Readiness outcomes that trigger an optional desktop notification.
NOTIFY_OUTCOMES = frozenset({
    model.RD_READY_FOR_COMMIT, model.RD_BLOCKED, model.RD_FAILED,
    model.RD_CANCELLED,
})


@dataclass(frozen=True)
class FollowOutcome:
    """The result of one bounded follow session."""

    document: dict[str, Any]
    polls: int
    exited: bool
    reason: str
    notifications: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "polls": self.polls,
            "exited": self.exited,
            "reason": self.reason,
            "notifications": list(self.notifications),
            "document": dict(self.document),
        }


def notify_send_available() -> bool:
    return shutil.which("notify-send") is not None


def default_notifier(title: str, body: str) -> bool:
    """Best-effort desktop notification; returns True when delivered."""
    if not notify_send_available():
        return False
    try:
        proc = subprocess.run(  # noqa: S603 - fixed, bounded argv
            ["notify-send", title, body],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _is_terminal(document: Mapping[str, Any]) -> bool:
    lifecycle = str(document.get("state", ""))
    readiness = str(document.get("readiness", ""))
    return (bool(document.get("terminal"))
            or lifecycle in model.TERMINAL_LIFECYCLE_STATES
            or readiness in model.TERMINAL_READINESS_STATES)


def follow(
    reader: Callable[[], dict[str, Any]],
    *,
    interval_s: float = DEFAULT_INTERVAL_S,
    max_polls: int = DEFAULT_MAX_POLLS,
    sleep: Callable[[float], None] = default_sleep,
    notifier: Callable[[str, str], bool] | None = default_notifier,
    on_frame: Callable[[str], None] | None = None,
    renderer: Callable[[Mapping[str, Any]], str] | None = None,
) -> FollowOutcome:
    """Follow one run until a terminal outcome (bounded).

    ``reader`` returns the canonical status document. ``renderer`` turns a
    document into a frame (default: the canonical CLI projection).
    ``on_frame`` receives the rendered frame for every poll. The returned
    outcome carries the final document and any delivered notification titles.
    """
    render = renderer or (lambda doc: projection.render_projection(
        doc, target=projection.PROJECTION_CLI))
    notifications: list[str] = []
    document = reader()
    polls = 1
    if on_frame is not None:
        on_frame(render(document))
    while not _is_terminal(document) and polls < max_polls:
        sleep(max(0.0, interval_s))
        document = reader()
        polls += 1
        if on_frame is not None:
            on_frame(render(document))
    exited = _is_terminal(document)
    readiness = str(document.get("readiness", model.RD_UNKNOWN))
    reason = (str(document.get("terminal_reason") or readiness)
              if exited else "FOLLOW_BOUND_REACHED")
    if exited and notifier is not None and readiness in NOTIFY_OUTCOMES:
        title = f"TrajectoryOS {readiness}"
        body = f"run {document.get('run_id')}: {reason}"
        if notifier(title, body):
            notifications.append(title)
    return FollowOutcome(document=document, polls=polls, exited=exited,
                         reason=reason, notifications=tuple(notifications))
