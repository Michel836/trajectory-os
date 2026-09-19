"""M052 — local API and web dashboard over one canonical projection.

The projection is the single coherent operator view. The web layer renders it
and exposes a closed set of explicit mutations; it duplicates **no** business
logic — every fact comes from :func:`build_projection` and every mutation
delegates to the canonical platform/operator functions.

Security invariants:

* localhost-only by default — binding to a non-loopback host is refused
  unless explicitly authorized;
* observation is read-only;
* mutations require an explicit session token *and* a matching CSRF token;
* contradictory canonical state fails closed.
"""

from __future__ import annotations

import hmac
import json
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trajectory_os.assembly import store as assembly_store
from trajectory_os.operator import model as operator_model
from trajectory_os.operator import state as operator_state
from trajectory_os.operator._util import read_optional_json, utc_now
from trajectory_os.platform import inbox as inbox_module
from trajectory_os.platform import model
from trajectory_os.platform import projects as project_registry
from trajectory_os.platform import queue as queue_module
from trajectory_os.platform import supervisor as supervisor_module
from trajectory_os.release import store as release_store

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "127.0.0.2"})

CONTENT_JSON = "application/json"
CONTENT_HTML = "text/html; charset=utf-8"


def is_loopback(host: str) -> bool:
    return host in LOOPBACK_HOSTS


def check_bind(host: str, *, allow_remote: bool = False) -> None:
    if not is_loopback(host) and not allow_remote:
        model.fail(model.E_API_BIND_FORBIDDEN,
                   f"{host!r} is not loopback")


# --- projection ---------------------------------------------------------------


def _mission_ids(root: str | Path) -> list[str]:
    discovered = set(inbox_module.iter_missions(root))
    for project in project_registry.list_projects(root):
        for objective in project.objectives:
            discovered.update(objective.mission_ids)
    return sorted(discovered)


def _mission_projection(root: str | Path, mission_id: str,
                        project_id: str | None) -> dict[str, Any]:
    try:
        state = operator_state.build_operator_state(str(root), mission_id)
        document = state.to_dict()
    except (model.PlatformError, operator_model.OperatorError, OSError):
        document = {"mission_id": mission_id, "available": False}
    document["project_id"] = project_id
    document["contradiction"] = _mission_contradiction(root, mission_id)
    return document


def _mission_contradiction(root: str | Path,
                           mission_id: str) -> dict[str, Any] | None:
    mission_root = Path(assembly_store.mission_root(root, mission_id))
    status = read_optional_json(mission_root / "status.json")
    merge_result = read_optional_json(
        mission_root / release_store.MERGE_RESULT_NAME)
    closure = read_optional_json(
        mission_root / release_store.RELEASE_CLOSURE_NAME)
    if (closure is not None and closure.get("status") == "CLOSED"
            and not (merge_result or {}).get("merged")):
        return {"reason": "CLOSED_WITHOUT_MERGE"}
    reviewed = (status or {}).get("reviewed_patch")
    current = (status or {}).get("current_patch")
    readiness = (status or {}).get("readiness")
    if (readiness == "READY_FOR_COMMIT" and isinstance(reviewed, str)
            and isinstance(current, str) and reviewed != current):
        return {"reason": "READY_WITH_STALE_PATCH"}
    return None


def _project_id_for(root: str | Path, mission_id: str) -> str | None:
    for project in project_registry.list_projects(root):
        for objective in project.objectives:
            if mission_id in objective.mission_ids:
                return project.project_id
    return None


def build_projection(root: str | Path, *,
                     clock: Callable[[], str] = utc_now) -> dict[str, Any]:
    """Build the one canonical read-only projection (never mutates)."""
    mission_ids = _mission_ids(root)
    missions: list[dict[str, Any]] = []
    contradictions: list[dict[str, Any]] = []
    for mission_id in mission_ids:
        projection = _mission_projection(
            root, mission_id, _project_id_for(root, mission_id))
        missions.append(projection)
        contradiction = projection.get("contradiction")
        if contradiction is not None:
            contradictions.append({"mission_id": mission_id,
                                   **contradiction})
    if contradictions:
        model.fail(model.E_STATE_CONTRADICTION,
                   json.dumps(contradictions, sort_keys=True))
    projects = [project_registry.project_status(root, project.project_id)
                for project in project_registry.list_projects(root)]
    queue_state = queue_module.load_queue(root)
    resources = queue_module.resource_accounting(root)
    inbox = inbox_module.inbox_document(root)
    supervisor = supervisor_module.status_document(root)
    queued = [entry.to_dict() for entry in queue_state.entries
              if entry.state == queue_module.Q_QUEUED]
    active = [entry.to_dict() for entry in queue_state.entries
              if entry.state == queue_module.Q_ACTIVE]
    next_action = _next_action(missions, inbox, supervisor)
    return {
        "schema_version": model.SCHEMA_VERSION,
        "platform_version": model.PLATFORM_VERSION,
        "generated_at": clock(),
        "projection_id": _projection_id(mission_ids, projects, queue_state),
        "projects": projects,
        "missions": missions,
        "queued_missions": queued,
        "active_missions": active,
        "resources": resources,
        "inbox": inbox,
        "supervisor": supervisor,
        "pending_human_gates": inbox_module.pending_human_gates(root),
        "next_action": next_action,
        "read_only": True,
    }


def _projection_id(mission_ids: list[str], projects: list[dict[str, Any]],
                   queue_state: queue_module.QueueState) -> str:
    from trajectory_os.operator._util import digest

    return digest({
        "mission_ids": mission_ids,
        "projects": [p["project_id"] for p in projects],
        "queue": [e.mission_id for e in queue_state.entries],
        "statuses": {e.mission_id: e.state for e in queue_state.entries},
    }, domain="trajectory-os.platform-projection.v1")


def _next_action(missions: list[dict[str, Any]], inbox: Mapping[str, Any],
                 supervisor: Mapping[str, Any]) -> dict[str, Any]:
    gates = [r for r in inbox.get("notifications", [])
             if isinstance(r, Mapping)
             and r.get("type") in (inbox_module.N_GO_COMMIT_REQUIRED,
                                   inbox_module.N_GO_MERGE_REQUIRED)
             and r.get("state") == inbox_module.NS_UNREAD]
    if gates:
        first = gates[0]
        return {"kind": "HUMAN_GATE", "mission_id": first.get("mission_id"),
                "notification_type": first.get("type"),
                "detail": "explicit human authorization required"}
    ready = [m for m in missions
             if m.get("pending_human_gate") == "GO_COMMIT"]
    if ready:
        return {"kind": "HUMAN_GATE", "mission_id": ready[0].get("mission_id"),
                "notification_type": inbox_module.N_GO_COMMIT_REQUIRED,
                "detail": "GO COMMIT required"}
    if supervisor.get("status") in (supervisor_module.SS_CRASHED,
                                    supervisor_module.SS_RECOVERED):
        return {"kind": "RECOVERY", "mission_id": None,
                "detail": "supervisor recovery available"}
    if inbox.get("unread"):
        return {"kind": "INBOX", "mission_id": None,
                "detail": f"{inbox['unread']} unread notifications"}
    return {"kind": "NONE", "mission_id": None, "detail": "no action required"}


# --- dashboard ----------------------------------------------------------------


def render_dashboard(projection: Mapping[str, Any]) -> str:
    """Render the canonical projection as a self-contained dashboard page."""
    rows: list[str] = []
    for mission in projection.get("missions", []):
        if not isinstance(mission, Mapping):
            continue
        rows.append(
            "<tr>"
            f"<td>{_escape(mission.get('mission_id'))}</td>"
            f"<td>{_escape(mission.get('project_id'))}</td>"
            f"<td>{_escape(mission.get('lifecycle'))}</td>"
            f"<td>{_escape(mission.get('readiness'))}</td>"
            f"<td>{_escape(mission.get('phase'))}</td>"
            f"<td>{_escape(mission.get('attempt'))}</td>"
            f"<td>{_escape(mission.get('pending_human_gate'))}</td>"
            f"<td>{_escape(mission.get('next_action'))}</td>"
            "</tr>")
    queued = "".join(
        f"<li>{_escape(e.get('mission_id'))} "
        f"(priority {_escape(e.get('priority'))})</li>"
        for e in projection.get("queued_missions", [])
        if isinstance(e, Mapping))
    active = "".join(
        f"<li>{_escape(e.get('mission_id'))}</li>"
        for e in projection.get("active_missions", [])
        if isinstance(e, Mapping))
    inbox = projection.get("inbox", {})
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>TrajectoryOS Operator Dashboard</title></head><body>"
        "<h1>TrajectoryOS Operator Dashboard</h1>"
        f"<p>projection <code>{_escape(projection.get('projection_id'))}"
        f"</code> generated {_escape(projection.get('generated_at'))}</p>"
        "<h2>Missions</h2><table border='1'>"
        "<tr><th>mission</th><th>project</th><th>lifecycle</th>"
        "<th>readiness</th><th>phase</th><th>attempt</th><th>gate</th>"
        "<th>next action</th></tr>"
        + "".join(rows) + "</table>"
        "<h2>Queued</h2><ul>" + (queued or "<li>none</li>") + "</ul>"
        "<h2>Active</h2><ul>" + (active or "<li>none</li>") + "</ul>"
        f"<h2>Resources</h2><pre>{_escape(_json(projection.get('resources')))}"
        "</pre>"
        f"<h2>Inbox ({_escape(inbox.get('unread'))} unread)</h2>"
        f"<pre>{_escape(_json(inbox.get('notifications')))}</pre>"
        f"<h2>Next action</h2><pre>{_escape(_json(projection.get('next_action')))}"
        "</pre>"
        "</body></html>")


def _escape(value: object) -> str:
    text = "" if value is None else str(value)
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)


# --- API ----------------------------------------------------------------------


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: bytes
    content_type: str

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


@dataclass
class LocalApi:
    """Localhost API over the canonical projection with explicit mutations.

    A single instance may be shared by the threaded HTTP server, so every
    request is serialised behind an internal re-entrant lock. This makes token
    validation and the following state mutation atomic with respect to other
    requests: two concurrent mutations can never interleave authorization and
    state update, and read projections never observe a half-applied mutation.
    """

    root: str
    token: str = ""
    allow_remote: bool = False
    clock: Callable[[], str] = field(default=utc_now)
    _lock: Any = field(default_factory=threading.RLock, repr=False,
                       compare=False)

    def __post_init__(self) -> None:
        if not self.token:
            self.token = secrets.token_hex(16)
        self.csrf_token = secrets.token_hex(16)

    def authorize(self, headers: Mapping[str, str]) -> None:
        lowered = {key.lower(): value for key, value in headers.items()}
        bearer = lowered.get("authorization", "")
        presented = bearer[7:] if bearer.lower().startswith("bearer ") else ""
        csrf = lowered.get("x-csrf-token", "")
        if not presented or not hmac.compare_digest(presented, self.token):
            model.fail(model.E_API_UNAUTHORIZED, "missing/invalid token")
        if not csrf or not hmac.compare_digest(csrf, self.csrf_token):
            model.fail(model.E_API_CSRF, "missing/invalid CSRF token")

    def handle(self, method: str, path: str,
               headers: Mapping[str, str] | None = None,
               body: bytes = b"") -> ApiResponse:
        headers = headers or {}
        with self._lock:
            try:
                return self._handle(method.upper(), path, dict(headers), body)
            except model.PlatformError as exc:
                return ApiResponse(
                    status=403 if exc.code in (
                        model.E_API_UNAUTHORIZED, model.E_API_CSRF) else 400,
                    body=json.dumps({"error": exc.code,
                                     "detail": exc.detail},
                                    sort_keys=True).encode("utf-8"),
                    content_type=CONTENT_JSON)

    def _handle(self, method: str, path: str,
                headers: Mapping[str, str], body: bytes) -> ApiResponse:
        clean = path.split("?", 1)[0]
        if method == "GET":
            return self._read(clean)
        if method == "POST":
            self.authorize(headers)
            return self._mutate(clean, _parse_body(body))
        return ApiResponse(405, b'{"error":"METHOD_NOT_ALLOWED"}',
                           CONTENT_JSON)

    def _read(self, path: str) -> ApiResponse:
        if path in ("/", "/dashboard", "/index.html"):
            projection = build_projection(self.root, clock=self.clock)
            return ApiResponse(200, render_dashboard(projection).encode("utf-8"),
                               CONTENT_HTML)
        if path == "/api/projection":
            return self._json(build_projection(self.root, clock=self.clock))
        if path == "/api/projects":
            return self._json({
                "projects": [project_registry.project_status(
                    self.root, p.project_id)
                    for p in project_registry.list_projects(self.root)]})
        if path == "/api/queue":
            return self._json(queue_module.resource_accounting(self.root))
        if path == "/api/inbox":
            return self._json(inbox_module.inbox_document(self.root))
        if path == "/api/supervisor":
            return self._json(supervisor_module.status_document(self.root))
        if path == "/api/health":
            return self._json({"status": "OK",
                               "platform_version": model.PLATFORM_VERSION})
        if path == "/api/intelligence":
            from trajectory_os.intelligence import projection as intel_projection

            return self._json(intel_projection.build_intelligence_projection(
                self.root, clock=self.clock))
        if path == "/api/intelligence/decisions":
            from trajectory_os.intelligence import decision as intel_decision

            return self._json(intel_decision.decision_summary(str(self.root)))
        return ApiResponse(404, b'{"error":"NOT_FOUND"}', CONTENT_JSON)

    def _mutate(self, path: str, payload: Mapping[str, Any]) -> ApiResponse:
        if path == "/api/inbox/refresh":
            return self._json(inbox_module.refresh(self.root, clock=self.clock))
        if path == "/api/inbox/ack":
            record = inbox_module.acknowledge(
                self.root, _required(payload, "notification_id"),
                clock=self.clock)
            return self._json(record.to_dict())
        if path == "/api/inbox/resolve":
            record = inbox_module.resolve(
                self.root, _required(payload, "notification_id"),
                clock=self.clock)
            return self._json(record.to_dict())
        if path == "/api/queue/pause":
            return self._json(queue_module.pause(
                self.root, _required(payload, "mission_id"),
                reason=str(payload.get("reason", "API_PAUSE")),
                clock=self.clock).to_dict())
        if path == "/api/queue/resume":
            return self._json(queue_module.resume(
                self.root, _required(payload, "mission_id"),
                clock=self.clock).to_dict())
        if path == "/api/queue/cancel":
            return self._json(queue_module.cancel(
                self.root, _required(payload, "mission_id"),
                reason=str(payload.get("reason", "API_CANCEL")),
                clock=self.clock).to_dict())
        if path == "/api/queue/requeue":
            return self._json(queue_module.requeue(
                self.root, _required(payload, "mission_id"),
                clock=self.clock).to_dict())
        if path == "/api/supervisor/stop":
            return self._json(supervisor_module.request_stop(
                self.root, reason=str(payload.get("reason", "API_STOP")),
                clock=self.clock))
        return ApiResponse(404, b'{"error":"NOT_FOUND"}', CONTENT_JSON)

    def _json(self, payload: object) -> ApiResponse:
        text = json.dumps(payload, indent=2, sort_keys=True, default=str)
        return ApiResponse(200, text.encode("utf-8"), CONTENT_JSON)


def _parse_body(body: bytes) -> Mapping[str, Any]:
    if not body:
        return {}
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        model.fail(model.E_MALFORMED, "request body must be JSON")
    if not isinstance(document, Mapping):
        model.fail(model.E_MALFORMED, "request body must be an object")
    return document


def _required(payload: Mapping[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        model.fail(model.E_MALFORMED, f"{field_name} required")
    return value


def serve(root: str | Path, *, host: str = "127.0.0.1", port: int = 8765,
          token: str | None = None, allow_remote: bool = False,
          clock: Callable[[], str] = utc_now,
          background: bool = True) -> Any:
    """Serve the local dashboard/API (localhost-only by default)."""
    import http.server
    import threading

    check_bind(host, allow_remote=allow_remote)
    api = LocalApi(str(root), token=token or "", allow_remote=allow_remote,
                   clock=clock)

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # pragma: no cover
            return

        def _dispatch(self, method: str) -> None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length else b""
            response = api.handle(method, self.path, dict(self.headers), body)
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            self.wfile.write(response.body)

        # Explicit methods (not lambdas) so the HTTP method surface is
        # unambiguous and introspectable.
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
            self._dispatch("POST")

    server = http.server.ThreadingHTTPServer((host, port), _Handler)
    server.api = api  # type: ignore[attr-defined]
    server.daemon_threads = True
    if background:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
    return server


__all__ = [
    "CONTENT_HTML",
    "CONTENT_JSON",
    "LOOPBACK_HOSTS",
    "ApiResponse",
    "LocalApi",
    "build_projection",
    "check_bind",
    "is_loopback",
    "render_dashboard",
    "serve",
]
