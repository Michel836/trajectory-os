"""``trajectory_os.platform`` — M048–M055 persistent autonomous operator.

An additive composition layer above the existing M017–M047 architecture. It
adds durable identity above missions (projects), a terminal-independent
supervisor, a deterministic multi-mission queue, a human-gate inbox, a local
API/dashboard, a LifeOS projection, evidence-based routing and production
hardening/disaster recovery — without introducing a competing mission,
release, readiness, event, semantic-patch or trust model.

Modules:

* :mod:`~trajectory_os.platform.projects`    — M048 project registry;
* :mod:`~trajectory_os.platform.supervisor`  — M049 persistent supervisor;
* :mod:`~trajectory_os.platform.queue`       — M050 portfolio queue/scheduler;
* :mod:`~trajectory_os.platform.inbox`       — M051 notifications/human gates;
* :mod:`~trajectory_os.platform.api`         — M052 local API + dashboard;
* :mod:`~trajectory_os.platform.lifeos`      — M053 LifeOS/Obsidian projection;
* :mod:`~trajectory_os.platform.efficiency`  — M054 evidence-based routing;
* :mod:`~trajectory_os.platform.hardening`   — M055 install/backup/recovery.
"""

from trajectory_os.platform.model import (  # noqa: F401
    PLATFORM_VERSION,
    SCHEMA_VERSION,
    PlatformError,
)

__all__ = [
    "PLATFORM_VERSION",
    "SCHEMA_VERSION",
    "PlatformError",
]
