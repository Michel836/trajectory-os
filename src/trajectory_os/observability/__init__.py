"""``trajectory_os.observability`` — M030 canonical live-run observability.

One canonical, backend-neutral observability and telemetry contract shared by
every operator surface (CLI / TUI / Web).

Canonical flow::

    runtime adapters
      -> authoritative events.jsonl
      -> authoritative status.json
      -> CLI / TUI / Web

Frontends consume the same canonical durable state. Current truth is never
reconstructed by grepping historical logs.

Design invariants:

* lifecycle completion and trust readiness are independent — a lifecycle
  ``COMPLETE`` state never implies ``READY_FOR_COMMIT``;
* reviewer identity is role-explicit — an inactive reviewer is never
  displayed with a phantom/default model;
* observation surfaces are read-only; runtime controls stay separate;
* every metric carries explicit provenance; an unavailable metric is ``null``
  with a stable reason and is never estimated or invented;
* collection, aggregation and presentation are separate layers.
"""

from trajectory_os.observability.model import (  # noqa: F401
    CANONICAL_SCHEMA_VERSION,
    LC_COMPLETE,
    LC_IMPLEMENTING,
    LC_PREFLIGHT,
    LC_REPAIRING,
    LC_REVIEWING,
    LC_RUNNING,
    LC_VALIDATING,
    OBSERVABILITY_VERSION,
    RD_BLOCKED,
    RD_CANCELLED,
    RD_FAILED,
    RD_INDETERMINATE,
    RD_READY_FOR_COMMIT,
    RD_UNKNOWN,
    TELEMETRY_BENCHMARK,
    TELEMETRY_MODES,
    TELEMETRY_OFF,
    TELEMETRY_STANDARD,
    CanonicalStatus,
    PreflightResult,
)
from trajectory_os.observability.projection import (  # noqa: F401
    PROJECTION_CLI,
    PROJECTION_TUI,
    PROJECTION_WEB,
    canonical_document,
    render_projection,
)
from trajectory_os.observability.store import (  # noqa: F401
    EVENTS_NAME,
    STATUS_NAME,
    SUMMARY_NAME,
    TELEMETRY_NAME,
)

__all__ = [
    "CANONICAL_SCHEMA_VERSION",
    "EVENTS_NAME",
    "LC_COMPLETE",
    "LC_IMPLEMENTING",
    "LC_PREFLIGHT",
    "LC_REPAIRING",
    "LC_REVIEWING",
    "LC_RUNNING",
    "LC_VALIDATING",
    "OBSERVABILITY_VERSION",
    "PROJECTION_CLI",
    "PROJECTION_TUI",
    "PROJECTION_WEB",
    "RD_BLOCKED",
    "RD_CANCELLED",
    "RD_FAILED",
    "RD_INDETERMINATE",
    "RD_READY_FOR_COMMIT",
    "RD_UNKNOWN",
    "STATUS_NAME",
    "SUMMARY_NAME",
    "TELEMETRY_BENCHMARK",
    "TELEMETRY_MODES",
    "TELEMETRY_NAME",
    "TELEMETRY_OFF",
    "TELEMETRY_STANDARD",
    "CanonicalStatus",
    "PreflightResult",
    "canonical_document",
    "render_projection",
]
