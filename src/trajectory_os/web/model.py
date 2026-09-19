"""M027 — local/private operator web dashboard (projection + bounded control).

The dashboard is a **projection and control** surface over the canonical
M012-M026 state. It introduces no second source of truth: every GET is a
render of the existing snapshot/event/portfolio/artifact projections, and
every POST is a bounded, non-Git operator control that delegates to the
existing canonical engines.

Security is structural and fail closed:

* the server binds a loopback address only; a non-loopback host is rejected;
* requests whose ``Host`` header is not loopback are rejected;
* an optional bearer token (``TRAJECTORY_WEB_TOKEN``) is required when set;
* request bodies are bounded, methods are restricted, and the only permitted
  controls are safe stop / clear stop / refresh events / daemon safe stop;
* no control ever performs a Git trust-boundary write.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import NoReturn

#: Schema version of every web dashboard document.
SCHEMA_VERSION = 1

#: Human/machine web version string (additive).
WEB_VERSION = "m027.1"

#: Loopback hosts the dashboard may bind (closed set).
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

#: Hard bounded limits.
MAX_BODY_BYTES = 65536
MIN_PORT = 1024
MAX_PORT = 65535
DEFAULT_PORT = 8765
MAX_REASON_LEN = 256
MAX_GOAL_ID_LEN = 256

#: Permitted operator controls (closed set). None performs a Git write.
CONTROL_STOP = "stop"
CONTROL_CLEAR_STOP = "clear-stop"
CONTROL_REFRESH_EVENTS = "refresh-events"
CONTROL_DAEMON_STOP = "daemon-stop"

CONTROLS = frozenset({
    CONTROL_STOP, CONTROL_CLEAR_STOP, CONTROL_REFRESH_EVENTS,
    CONTROL_DAEMON_STOP,
})

# --- stable fail-closed error codes ------------------------------------------

E_INVALID_CONFIG = "INVALID_WEB_CONFIG"
E_FORBIDDEN = "WEB_FORBIDDEN"
E_UNKNOWN_CONTROL = "WEB_UNKNOWN_CONTROL"
E_MALFORMED = "MALFORMED_WEB_REQUEST"


class WebError(Exception):
    """A web-dashboard contract violation (fail closed)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _fail(code: str, detail: str = "") -> NoReturn:
    raise WebError(code, detail)


@dataclass(frozen=True)
class WebConfig:
    """Bounded local-dashboard configuration (never inferred)."""

    root: str
    default_goal_id: str | None = None
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    token: str | None = None
    max_body_bytes: int = MAX_BODY_BYTES

    def validate(self) -> WebConfig:
        if self.host not in LOOPBACK_HOSTS:
            _fail(E_INVALID_CONFIG,
                  f"host {self.host!r} is not a loopback address")
        if isinstance(self.port, bool) or not isinstance(self.port, int) \
                or not (self.port == 0
                        or MIN_PORT <= self.port <= MAX_PORT):
            _fail(E_INVALID_CONFIG, f"port out of bounds: {self.port!r}")
        if not isinstance(self.root, str) or not self.root:
            _fail(E_INVALID_CONFIG, "root is required")
        if self.default_goal_id is not None and (
                not isinstance(self.default_goal_id, str)
                or not self.default_goal_id
                or len(self.default_goal_id) > MAX_GOAL_ID_LEN):
            _fail(E_INVALID_CONFIG, "default_goal_id is invalid")
        if not (1 <= self.max_body_bytes <= MAX_BODY_BYTES):
            _fail(E_INVALID_CONFIG, "max_body_bytes out of bounds")
        if self.token is not None and (
                not isinstance(self.token, str) or not self.token):
            _fail(E_INVALID_CONFIG, "token must be a non-empty string")
        return self

    @property
    def loopback(self) -> bool:
        return self.host in LOOPBACK_HOSTS

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "web_version": WEB_VERSION,
            "host": self.host,
            "port": self.port,
            "default_goal_id": self.default_goal_id,
            "auth_required": self.token is not None,
            "controls": sorted(CONTROLS),
        }


def config_from_env(root: str, *,
                    default_goal_id: str | None = None,
                    host: str | None = None,
                    port: int | None = None,
                    token: str | None = None) -> WebConfig:
    """Build a validated config from explicit values + environment.

    ``TRAJECTORY_WEB_TOKEN`` (when non-empty) enables bearer auth. ``host``
    and ``port`` default to loopback/8765 and can never be raised beyond the
    closed loopback set by the environment.
    """
    env_host = os.environ.get("TRAJECTORY_WEB_HOST") or "127.0.0.1"
    env_port = os.environ.get("TRAJECTORY_WEB_PORT")
    resolved_token = token
    if resolved_token is None:
        env_token = os.environ.get("TRAJECTORY_WEB_TOKEN")
        resolved_token = env_token if env_token else None
    chosen_port = port
    if chosen_port is None:
        if env_port:
            try:
                chosen_port = int(env_port)
            except ValueError:
                chosen_port = DEFAULT_PORT
        else:
            chosen_port = DEFAULT_PORT
    return WebConfig(
        root=root,
        default_goal_id=default_goal_id,
        host=host or env_host,
        port=chosen_port,
        token=resolved_token,
    ).validate()
