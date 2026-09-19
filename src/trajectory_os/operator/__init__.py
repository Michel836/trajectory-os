"""``trajectory_os.operator`` — M040–M047 self-hosting operator platform.

One coherent operator product over the existing M017–M039 architecture. It
reuses the canonical mission/release/observability layers and introduces no
competing lifecycle, readiness, identity or trust model.

* **M040** :mod:`~trajectory_os.operator.dogfood` — true self-release dogfood;
* **M041** :mod:`~trajectory_os.operator.control_plane` — unified control;
* **M042** :mod:`~trajectory_os.operator.recovery` — full-lifecycle recovery;
* **M043** :mod:`~trajectory_os.operator.events` — unified durable events;
* **M044** :mod:`~trajectory_os.operator.policy` — deterministic policy layer;
* **M045** :mod:`~trajectory_os.operator.routing` — backend/reviewer routing;
* **M046** :mod:`~trajectory_os.operator.state` — one-screen product state;
* **M047** :mod:`~trajectory_os.operator.acceptance` — product acceptance.
"""

from trajectory_os.operator.model import (  # noqa: F401
    OPERATOR_VERSION,
    SCHEMA_VERSION,
    OperatorError,
)

__all__ = [
    "OPERATOR_VERSION",
    "SCHEMA_VERSION",
    "OperatorError",
]
