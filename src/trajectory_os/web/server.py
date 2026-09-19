"""M027 — local/private HTTP dashboard server (stdlib, loopback-only).

The server exposes authoritative read projections and a small closed set of
bounded operator controls. It is deliberately dependency-free (standard
library only), binds a loopback address only, rejects non-loopback ``Host``
headers, enforces an optional bearer token and a bounded body, and never
performs a Git trust-boundary write.

Controls delegate to the existing canonical engines:

* ``POST /api/control/stop``          -> goal safe-stop request
* ``POST /api/control/clear-stop``    -> clear safe-stop request
* ``POST /api/control/refresh-events``-> durable event projection refresh
* ``POST /api/control/daemon-stop``   -> daemon safe-stop request

Read routes:

* ``GET /``            -> HTML goal overview
* ``GET /goal/<id>``   -> HTML goal dashboard
* ``GET /healthz``     -> liveness
* ``GET /api/overview``-> JSON overview
* ``GET /api/goals/<id>`` -> JSON combined goal dashboard
* ``GET /api/goals/<id>/events`` -> JSON event projection
* ``GET /api/controls``-> JSON permitted control list
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from trajectory_os.web import model, projection, render

_GOAL_ROUTE = re.compile(r"^/api/goals/(?P<goal>[^/]+)(?:/(?P<sub>events))?$")
_GOAL_HTML_ROUTE = re.compile(r"^/goal/(?P<goal>[^/]+)$")


class ControlError(Exception):
    """A rejected control request (fail closed)."""

    def __init__(self, code: str, detail: str = "",
                 status: int = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.status = status


class WebApp:
    """Bounded application object shared by every request handler."""

    def __init__(self, config: model.WebConfig) -> None:
        self.config = config.validate()

    # --- projections ----------------------------------------------------------

    def overview(self) -> dict[str, Any]:
        return projection.overview(self.config.root)

    def goal_document(self, goal_id: str) -> dict[str, Any]:
        return projection.dashboard(self.config.root, goal_id)

    def events(self, goal_id: str, *, refresh: bool = False) -> dict[str, Any]:
        return projection.events(self.config.root, goal_id, refresh=refresh)

    # --- controls -------------------------------------------------------------

    def resolve_goal(self, payload: Mapping[str, Any]) -> str:
        raw = payload.get("goal_id")
        if raw is None:
            raw = self.config.default_goal_id
        if not isinstance(raw, str) or not raw \
                or len(raw) > model.MAX_GOAL_ID_LEN:
            raise ControlError(model.E_MALFORMED, "goal_id is required")
        return raw

    def control(self, name: str,
                payload: Mapping[str, Any]) -> dict[str, Any]:
        """Execute one permitted control (never a Git write)."""
        if name not in model.CONTROLS:
            raise ControlError(model.E_UNKNOWN_CONTROL, name,
                               HTTPStatus.NOT_FOUND)
        reason = payload.get("reason")
        if reason is not None and (
                not isinstance(reason, str)
                or len(reason) > model.MAX_REASON_LEN):
            raise ControlError(model.E_MALFORMED, "reason is invalid")
        if name == model.CONTROL_STOP:
            from trajectory_os.goals import runner as goal_runner

            goal_id = self.resolve_goal(payload)
            document = goal_runner.request_stop(
                self.config.root, goal_id, reason=reason)
            return {"control": name, "goal_id": goal_id,
                    "result": document}
        if name == model.CONTROL_CLEAR_STOP:
            from trajectory_os.goals import runner as goal_runner

            goal_id = self.resolve_goal(payload)
            goal_runner.clear_stop(self.config.root, goal_id)
            return {"control": name, "goal_id": goal_id,
                    "result": {"cleared": True}}
        if name == model.CONTROL_REFRESH_EVENTS:
            from trajectory_os.events import engine as event_engine

            goal_id = self.resolve_goal(payload)
            result = event_engine.refresh(self.config.root, goal_id)
            return {
                "control": name,
                "goal_id": goal_id,
                "result": {
                    "count": result.count,
                    "projection_id": result.document.get("projection_id"),
                    "added": [r.event_id for r in result.added],
                    "removed": [r.event_id for r in result.removed],
                },
            }
        if name == model.CONTROL_DAEMON_STOP:
            from trajectory_os.daemon import engine as daemon_engine

            document = daemon_engine.request_stop(
                self.config.root, reason=reason)
            return {"control": name, "result": document}
        raise ControlError(model.E_UNKNOWN_CONTROL, name,
                           HTTPStatus.NOT_FOUND)  # pragma: no cover


def _host_is_loopback(header: str | None) -> bool:
    if not header:
        return True  # HTTP/1.0 clients without Host; loopback bind protects
    host = header.strip()
    if host.startswith("["):  # IPv6 literal [::1]:port
        host = host[1:host.find("]")] if "]" in host else host
    else:
        host = host.rsplit(":", 1)[0]
    return host in model.LOOPBACK_HOSTS


def make_handler(app: WebApp) -> type[BaseHTTPRequestHandler]:
    """Build the request handler bound to one :class:`WebApp`."""

    class _Handler(BaseHTTPRequestHandler):
        server_version = "TrajectoryOS/" + model.WEB_VERSION
        protocol_version = "HTTP/1.1"

        # --- helpers ------------------------------------------------------

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            return  # keep the local dashboard quiet

        def _authorized(self) -> bool:
            if app.config.token is None:
                return True
            header = self.headers.get("Authorization") or ""
            presented = ""
            if header.startswith("Bearer "):
                presented = header[len("Bearer "):].strip()
            elif self.headers.get("X-Trajectory-Token"):
                presented = self.headers["X-Trajectory-Token"].strip()
            if not presented:
                query = parse_qs(urlparse(self.path).query)
                values = query.get("token")
                presented = values[0].strip() if values else ""
            return presented == app.config.token

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
            body = (json.dumps(payload, indent=2, sort_keys=True)
                    + "\n").encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def _send_html(self, status: int, text: str) -> None:
            self._send(status, text.encode("utf-8"),
                       "text/html; charset=utf-8")

        def _guard(self) -> bool:
            if not _host_is_loopback(self.headers.get("Host")):
                self._send_json(HTTPStatus.FORBIDDEN,
                                {"status": "FORBIDDEN",
                                 "error": model.E_FORBIDDEN,
                                 "detail": "non-loopback Host"})
                return False
            if not self._authorized():
                self.send_response(HTTPStatus.UNAUTHORIZED)
                self.send_header("WWW-Authenticate", "Bearer")
                body = b'{"status": "UNAUTHORIZED"}\n'
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return False
            return True

        # --- method entry points -----------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            if not self._guard():
                return
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if path in ("/", "/index.html"):
                document = app.overview()
                self._send_html(HTTPStatus.OK,
                                render.render_index(app.config.root,
                                                    document))
                return
            if path == "/healthz":
                self._send_json(HTTPStatus.OK, {"status": "OK",
                                                "web_version":
                                                    model.WEB_VERSION})
                return
            if path == "/api/overview":
                self._send_json(HTTPStatus.OK, app.overview())
                return
            if path == "/api/controls":
                self._send_json(HTTPStatus.OK, {
                    "status": "OK",
                    "controls": sorted(model.CONTROLS),
                    "git_write": False,
                })
                return
            if path == "/api/telemetry":
                from trajectory_os.agents import telemetry as agent_telemetry

                try:
                    document = agent_telemetry.reconstruct(
                        app.config.root)
                except Exception as exc:  # noqa: BLE001 - operator surface
                    document = {"status": "UNAVAILABLE",
                                "error": type(exc).__name__,
                                "count": 0, "records": []}
                self._send_json(HTTPStatus.OK, document)
                return
            match = _GOAL_HTML_ROUTE.match(path)
            if match:
                goal_id = match.group("goal")
                try:
                    document = app.goal_document(goal_id)
                except Exception as exc:  # noqa: BLE001
                    self._send_html(HTTPStatus.NOT_FOUND,
                                    "<h1>goal not available</h1>"
                                    f"<p>{model.E_MALFORMED}: "
                                    f"{type(exc).__name__}</p>")
                    return
                self._send_html(HTTPStatus.OK, render.render_goal(document))
                return
            match = _GOAL_ROUTE.match(path)
            if match:
                goal_id = match.group("goal")
                try:
                    if match.group("sub") == "events":
                        self._send_json(HTTPStatus.OK,
                                        app.events(goal_id))
                    else:
                        self._send_json(HTTPStatus.OK,
                                        app.goal_document(goal_id))
                except Exception as exc:  # noqa: BLE001 - operator surface
                    self._send_json(HTTPStatus.NOT_FOUND, {
                        "status": "NOT_FOUND",
                        "error": model.E_MALFORMED,
                        "detail": type(exc).__name__,
                    })
                return
            self._send_json(HTTPStatus.NOT_FOUND,
                            {"status": "NOT_FOUND"})

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_POST(self) -> None:  # noqa: N802
            if not self._guard():
                return
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            prefix = "/api/control/"
            if not path.startswith(prefix):
                self._send_json(HTTPStatus.NOT_FOUND,
                                {"status": "NOT_FOUND"})
                return
            name = path[len(prefix):]
            length_header = self.headers.get("Content-Length") or "0"
            try:
                length = int(length_header)
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST,
                                {"status": "BAD_REQUEST"})
                return
            if length < 0 or length > app.config.max_body_bytes:
                self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {
                    "status": "PAYLOAD_TOO_LARGE",
                    "error": model.E_MALFORMED})
                return
            raw = self.rfile.read(length) if length else b""
            payload: dict[str, Any] = {}
            if raw:
                try:
                    decoded = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._send_json(HTTPStatus.BAD_REQUEST, {
                        "status": "BAD_REQUEST",
                        "error": model.E_MALFORMED,
                        "detail": "body is not JSON"})
                    return
                if not isinstance(decoded, dict):
                    self._send_json(HTTPStatus.BAD_REQUEST, {
                        "status": "BAD_REQUEST",
                        "error": model.E_MALFORMED,
                        "detail": "body must be an object"})
                    return
                payload = decoded
            try:
                result = app.control(name, payload)
            except ControlError as exc:
                self._send_json(exc.status, {
                    "status": "REJECTED", "error": exc.code,
                    "detail": exc.detail})
                return
            except Exception as exc:  # noqa: BLE001 - bounded operator error
                self._send_json(HTTPStatus.CONFLICT, {
                    "status": "ERROR", "error": type(exc).__name__,
                    "detail": str(exc)[:256]})
                return
            self._send_json(HTTPStatus.OK, {"status": "OK", **result})

    return _Handler


def create_server(app: WebApp) -> ThreadingHTTPServer:
    """Create (but do not start) a loopback-bound dashboard server."""
    handler = make_handler(app)
    server = ThreadingHTTPServer((app.config.host, app.config.port), handler)
    server.daemon_threads = True
    return server


def serve(app: WebApp, *,
          on_ready: Callable[[ThreadingHTTPServer], None] | None = None,
          ) -> None:
    """Serve until interrupted (blocking)."""
    server = create_server(app)
    if on_ready is not None:
        on_ready(server)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
