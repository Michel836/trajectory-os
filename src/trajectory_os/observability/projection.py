"""M030 — shared canonical projection for CLI / TUI / Web.

Every operator surface consumes the *same* canonical status document. This
module is the only place that turns that document into a rendered view, so
the three surfaces cannot disagree about current truth.

The projection never reconstructs state from logs and never invents a
reviewer: an inactive role is rendered as disabled with no model. Success is
rendered from ``readiness == READY_FOR_COMMIT`` alone; a lifecycle
``COMPLETE`` with a blocked readiness renders as blocked, never as success.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from html import escape
from typing import Any

from trajectory_os.observability import model

PROJECTION_CLI = "cli"
PROJECTION_TUI = "tui"
PROJECTION_WEB = "web"

PROJECTIONS = frozenset({PROJECTION_CLI, PROJECTION_TUI, PROJECTION_WEB})

_UNAVAILABLE = "unavailable"


def _fmt(value: object) -> str:
    if value is None or value == "":
        return _UNAVAILABLE
    return str(value)


@dataclass(frozen=True)
class StatusViewModel:
    """Frontend-neutral view of one canonical status document."""

    run_id: str
    lifecycle: str
    readiness: str
    stage: str
    phase: str
    attempt: int
    current: str
    next: str
    current_backend: str
    current_provider: str
    current_model: str
    implementation_agent: str
    inline_reviewer: str
    final_reviewer: str
    previous_gate: str
    previous_result: str
    reviewed_patch: str
    current_patch: str
    next_action: str
    last_meaningful_event_at: str
    heartbeat_at: str
    terminal_reason: str
    telemetry: Mapping[str, Any]
    telemetry_mode: str
    terminal: bool
    success: bool
    banner: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "lifecycle": self.lifecycle,
            "readiness": self.readiness,
            "stage": self.stage,
            "phase": self.phase,
            "attempt": self.attempt,
            "current": self.current,
            "next": self.next,
            "current_backend": self.current_backend,
            "current_provider": self.current_provider,
            "current_model": self.current_model,
            "implementation_agent": self.implementation_agent,
            "inline_reviewer": self.inline_reviewer,
            "final_reviewer": self.final_reviewer,
            "previous_gate": self.previous_gate,
            "previous_result": self.previous_result,
            "reviewed_patch": self.reviewed_patch,
            "current_patch": self.current_patch,
            "next_action": self.next_action,
            "last_meaningful_event_at": self.last_meaningful_event_at,
            "heartbeat_at": self.heartbeat_at,
            "terminal_reason": self.terminal_reason,
            "telemetry": dict(self.telemetry),
            "telemetry_mode": self.telemetry_mode,
            "terminal": self.terminal,
            "success": self.success,
            "banner": self.banner,
        }


def _reviewer_text(reviewer: Mapping[str, Any]) -> str:
    if not reviewer.get("enabled"):
        return "disabled"
    if not reviewer.get("active"):
        return f"enabled (inactive: {_fmt(reviewer.get('reason'))})"
    return (f"{_fmt(reviewer.get('backend'))} / "
            f"{_fmt(reviewer.get('provider'))}:"
            f"{_fmt(reviewer.get('display_model'))} (active)")


def _compact_telemetry(document: Mapping[str, Any]) -> Mapping[str, Any]:
    telemetry = document.get("telemetry")
    if not isinstance(telemetry, Mapping):
        return {}
    metrics = telemetry.get("metrics")
    if not isinstance(metrics, Mapping):
        return {}
    compact: dict[str, Any] = {}
    for name in ("trial_count", "total_tokens", "cost_usd", "generation_tps",
                 "total_duration_ms"):
        metric = metrics.get(name)
        if isinstance(metric, Mapping) and metric.get("value") is not None:
            compact[name] = metric["value"]
    return compact


def view_model(document: Mapping[str, Any]) -> StatusViewModel:
    """Build the single frontend-neutral view from a canonical document."""
    inline = document.get("inline_reviewer")
    inline = inline if isinstance(inline, Mapping) else {}
    final = document.get("final_reviewer")
    final = final if isinstance(final, Mapping) else {}
    readiness = str(document.get("readiness", model.RD_UNKNOWN))
    lifecycle = str(document.get("state", model.LC_RUNNING))
    success = readiness == model.RD_READY_FOR_COMMIT
    terminal = bool(document.get("terminal")) or (
        lifecycle in model.TERMINAL_LIFECYCLE_STATES
        or readiness in model.TERMINAL_READINESS_STATES)
    if success:
        banner = f"READY_FOR_COMMIT — run {document.get('run_id')}"
    elif readiness in model.TERMINAL_READINESS_STATES:
        banner = f"{readiness} — run {document.get('run_id')}"
    elif lifecycle in model.TERMINAL_LIFECYCLE_STATES:
        # Lifecycle complete but no terminal readiness: never success.
        banner = (f"TRAITEMENT TERMINÉ ({lifecycle}) — "
                  f"readiness {readiness}")
    else:
        banner = f"{lifecycle} — run {document.get('run_id')}"
    implementation = (f"{_fmt(document.get('current_backend'))} / "
                      f"{_fmt(document.get('current_provider'))}:"
                      f"{_fmt(document.get('current_model'))}")
    return StatusViewModel(
        run_id=str(document.get("run_id", _UNAVAILABLE)),
        lifecycle=lifecycle, readiness=readiness,
        stage=str(document.get("stage", _UNAVAILABLE)),
        phase=str(document.get("phase", _UNAVAILABLE)),
        attempt=int(document.get("attempt", 0) or 0),
        current=_fmt(document.get("current")),
        next=_fmt(document.get("next")),
        current_backend=_fmt(document.get("current_backend")),
        current_provider=_fmt(document.get("current_provider")),
        current_model=_fmt(document.get("current_model")),
        implementation_agent=implementation,
        inline_reviewer=_reviewer_text(inline),
        final_reviewer=_reviewer_text(final),
        previous_gate=_fmt(document.get("previous_gate")),
        previous_result=_fmt(document.get("previous_result")),
        reviewed_patch=_fmt(document.get("reviewed_patch")),
        current_patch=_fmt(document.get("current_patch")),
        next_action=_fmt(document.get("next_action")),
        last_meaningful_event_at=_fmt(
            document.get("last_meaningful_event_at")),
        heartbeat_at=_fmt(document.get("heartbeat_at")),
        terminal_reason=_fmt(document.get("terminal_reason")),
        telemetry=_compact_telemetry(document),
        telemetry_mode=_fmt(document.get("telemetry_mode")),
        terminal=terminal, success=success, banner=banner,
    )


def canonical_document(status: model.CanonicalStatus) -> dict[str, Any]:
    """The canonical dict every surface consumes (status.to_dict)."""
    return status.validate().to_dict()


def render_cli(view: StatusViewModel) -> str:
    lines = [
        "=" * 68,
        f"RUN ID   : {view.run_id}",
        f"LIFECYCLE: {view.lifecycle}   READINESS: {view.readiness}",
        f"STAGE    : {view.stage}   PHASE: {view.phase}   "
        f"ATTEMPT: {view.attempt}",
        f"CURRENT  : {view.current}",
        f"NEXT     : {view.next}",
        f"ACTION   : {view.next_action}",
        "-" * 68,
        f"backend  : {view.current_backend}   "
        f"provider: {view.current_provider}   model: {view.current_model}",
        f"agent    : {view.implementation_agent}",
        f"inline   : {view.inline_reviewer}",
        f"final    : {view.final_reviewer}",
        "-" * 68,
        f"gate     : {view.previous_gate}   result: {view.previous_result}",
        f"reviewed : {view.reviewed_patch}",
        f"current  : {view.current_patch}",
        f"event at : {view.last_meaningful_event_at}   "
        f"heartbeat: {view.heartbeat_at}",
        f"reason   : {view.terminal_reason}",
        f"telemetry: mode={view.telemetry_mode} {dict(view.telemetry)}",
        "=" * 68,
        view.banner,
        "=" * 68,
    ]
    return "\n".join(lines)


def render_tui(view: StatusViewModel,
               *, width: int = 100) -> str:
    """A single deterministic TUI frame (ANSI clear + the canonical view)."""
    body = render_cli(view)
    clipped = "\n".join(line[:width] for line in body.splitlines())
    return "\x1b[2J\x1b[H" + clipped + "\n"


def render_web(view: StatusViewModel) -> str:
    """A minimal HTML projection of the same canonical view model."""
    rows = {
        "run_id": view.run_id,
        "lifecycle": view.lifecycle,
        "readiness": view.readiness,
        "stage": view.stage,
        "phase": view.phase,
        "attempt": view.attempt,
        "current": view.current,
        "next": view.next,
        "next_action": view.next_action,
        "backend": view.current_backend,
        "provider": view.current_provider,
        "model": view.current_model,
        "implementation_agent": view.implementation_agent,
        "inline_reviewer": view.inline_reviewer,
        "final_reviewer": view.final_reviewer,
        "previous_gate": view.previous_gate,
        "previous_result": view.previous_result,
        "reviewed_patch": view.reviewed_patch,
        "current_patch": view.current_patch,
        "heartbeat_at": view.heartbeat_at,
        "terminal_reason": view.terminal_reason,
        "telemetry_mode": view.telemetry_mode,
        "telemetry": str(dict(view.telemetry)),
        "banner": view.banner,
    }
    cells = "".join(
        f"<tr><th>{escape(str(key))}</th><td>{escape(str(value))}</td></tr>"
        for key, value in rows.items())
    return (f"<table class=\"trajectory-status\" "
            f"data-ready-for-commit=\"{str(view.success).lower()}\" "
            f"data-terminal=\"{str(view.terminal).lower()}\">"
            f"{cells}</table>")


def render_projection(document: Mapping[str, Any], *,
                      target: str = PROJECTION_CLI) -> str:
    """Render one canonical document for a named frontend surface."""
    if target not in PROJECTIONS:
        raise model.ObservabilityError(
            "UNKNOWN_PROJECTION", target)
    view = view_model(document)
    if target == PROJECTION_CLI:
        return render_cli(view)
    if target == PROJECTION_TUI:
        return render_tui(view)
    return render_web(view)
