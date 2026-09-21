"""MVP — local cockpit HTTP server (stdlib, loopback-only).

Serves the interactive daily cockpit as HTML plus a small set of REST-style
JSON endpoints for project/task management. It follows the same trust model as
the existing web server: loopback bind only, optional bearer token, bounded
body, never a Git write.

Every mutation goes through the validated domain/store functions in
:mod:`trajectory_os.mvp.mutations` and :mod:`trajectory_os.mvp.outcomes`; the
browser never rewrites ``portfolio.json`` directly and never re-implements
business rules in JavaScript.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from collections.abc import Callable, Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from trajectory_os.mvp import (
    capture,
    document,
    engine,
    enrichment,
    import_engine,
    import_jobs,
    importer,
    model,
    mutations,
    outcomes,
    render,
    store,
    superproductivity,
    uistate,
    visualization,
)

MAX_BODY_BYTES = 64 * 1024
MAX_IMPORT_BODY_BYTES = 16 * 1024 * 1024


class Dashboard:
    """Bounded application object for the cockpit server."""

    def __init__(
        self,
        root: str,
        token: str | None = None,
        llm: Callable[[str, str], Mapping[str, Any]] | None = None,
    ) -> None:
        self.root = root
        self.token = token
        self.llm = llm
        self.jobs = import_jobs.ImportJobManager(root, llm=llm)

    def cockpit(self) -> engine.Cockpit:
        return engine.build_cockpit(self.root, persist=False)

    def portfolio(self) -> model.Portfolio:
        portfolio = store.load_portfolio(self.root)
        if portfolio is None:
            raise model.MvpError(model.E_MALFORMED, "no portfolio loaded")
        return portfolio

    def record(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return _record_outcome(self.root, payload)

    # --- visual execution layer ----------------------------------------------

    def views(self) -> dict[str, Any]:
        return visualization.build_views(self.root)

    def update_preferences(self,
                           payload: Mapping[str, Any]) -> dict[str, Any]:
        prefs = uistate.load_preferences(self.root)
        updated = uistate.apply_update(prefs, dict(payload))
        uistate.save_preferences(self.root, updated)
        return {"preferences": updated.to_dict()}

    def set_highlight(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        object_id = payload.get("object_id")
        if not isinstance(object_id, str) or not object_id:
            raise model.MvpError(model.E_MALFORMED, "object_id required")
        colour = payload.get("colour")
        if colour is not None and not isinstance(colour, str):
            raise model.MvpError(model.E_MALFORMED, "colour must be a string")
        prefs = uistate.set_highlight(uistate.load_preferences(self.root),
                                      object_id, colour)
        uistate.save_preferences(self.root, prefs)
        return {"preferences": prefs.to_dict()}

    def set_colour(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        scope = payload.get("scope")
        key = payload.get("key")
        if not isinstance(scope, str) or scope not in ("domain", "project"):
            raise model.MvpError(model.E_MALFORMED,
                                 "scope must be domain or project")
        if not isinstance(key, str) or not key:
            raise model.MvpError(model.E_MALFORMED, "key required")
        colour = payload.get("colour")
        if colour is not None and not isinstance(colour, str):
            raise model.MvpError(model.E_MALFORMED, "colour must be a string")
        prefs = uistate.set_identity_colour(
            uistate.load_preferences(self.root), scope, key, colour)
        uistate.save_preferences(self.root, prefs)
        return {"preferences": prefs.to_dict()}

    # --- saved presentation views (filter presets) ---------------------------

    def saved_views(self) -> dict[str, Any]:
        prefs = uistate.load_preferences(self.root)
        return {"saved_views": prefs.saved_views}

    @staticmethod
    def _view_name(payload: Mapping[str, Any], key: str = "name") -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise model.MvpError(model.E_MALFORMED, f"{key} required")
        return value

    def save_saved_view(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        name = self._view_name(payload)
        prefs = uistate.load_preferences(self.root)
        updated = uistate.save_view(prefs, name)
        uistate.save_preferences(self.root, updated)
        return {"preferences": updated.to_dict()}

    def load_saved_view(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        name = self._view_name(payload)
        prefs = uistate.load_preferences(self.root)
        updated = uistate.load_view(prefs, name)
        uistate.save_preferences(self.root, updated)
        return {"preferences": updated.to_dict()}

    def rename_saved_view(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        name = self._view_name(payload)
        new_name = self._view_name(payload, "new_name")
        prefs = uistate.load_preferences(self.root)
        updated = uistate.rename_view(prefs, name, new_name)
        uistate.save_preferences(self.root, updated)
        return {"preferences": updated.to_dict()}

    def delete_saved_view(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        name = self._view_name(payload)
        prefs = uistate.load_preferences(self.root)
        updated = uistate.delete_view(prefs, name)
        uistate.save_preferences(self.root, updated)
        return {"preferences": updated.to_dict()}

    # --- quick capture (deterministic, preview then confirm) -----------------

    def capture_preview(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise model.MvpError(model.E_MALFORMED, "text required")
        try:
            return capture.preview(self.root, text).to_dict()
        except capture.CaptureError as exc:
            raise model.MvpError(model.E_MALFORMED, str(exc)) from exc

    def capture_confirm(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise model.MvpError(model.E_MALFORMED, "text required")
        decisions = payload.get("decisions")
        if not isinstance(decisions, list):
            raise model.MvpError(model.E_MALFORMED,
                                 "decisions must be a list")
        try:
            return capture.confirm(self.root, text, decisions)
        except capture.CaptureError as exc:
            raise model.MvpError(model.E_MALFORMED, str(exc)) from exc

    # --- Super Productivity handoff (dry-run preview, no server write) -------

    def export_preview(self, task_ids: list[str],
                       limit: int) -> dict[str, Any]:
        # Accept both "task:abc" object ids and bare "abc" task ids.
        cleaned = [tid.split(":", 1)[1]
                   if tid.startswith("task:") else tid
                   for tid in task_ids]
        return superproductivity.preview_export(
            self.root, task_ids=cleaned or None, limit=limit)

    # --- document import (factual first) -------------------------------------

    def engine_catalog(self, chars: int = 0) -> dict[str, Any]:
        """Describe selectable engines, pricing state and cost estimates."""
        from datetime import UTC, datetime

        now = datetime.now(tz=UTC)
        api_key = bool(os.environ.get("DEEPSEEK_API_KEY"))
        catalog = import_engine.engine_catalog(
            now, api_key_configured=api_key, chars=max(0, chars))
        default = import_engine.default_engine(
            now, api_key_configured=api_key)
        return {
            "default_engine": default,
            "recommended_engine": default,
            "peak_windows": import_engine.peak_windows_label(),
            "pricing_state": import_engine.pricing_state(
                now, configured=api_key),
            "engines": [entry.to_dict() for entry in catalog],
        }

    def _resolve_llm_for(
        self, engine: str,
    ) -> Callable[[str, str], Mapping[str, Any]] | None:
        if self.llm is not None:
            return self.llm
        llm, _engine_id, _model = importer.make_engine_llm(engine)
        return llm

    def analyze_import(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        from datetime import UTC, datetime

        filename = payload.get("filename")
        if not isinstance(filename, str) or not filename:
            raise model.MvpError(model.E_MALFORMED, "filename required")
        content = payload.get("content_base64")
        if not isinstance(content, str) or not content:
            raise model.MvpError(model.E_MALFORMED, "content_base64 required")
        try:
            data = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise model.MvpError(model.E_MALFORMED,
                                 "invalid base64 content") from exc
        mode = payload.get("mode", importer.MODE_FACTUAL)
        if mode not in importer.MODES:
            raise model.MvpError(model.E_MALFORMED, "unknown import mode")
        now = datetime.now(tz=UTC)
        api_key = bool(os.environ.get("DEEPSEEK_API_KEY"))
        try:
            selection = import_engine.resolve_selection(
                payload.get("engine"), now=now, api_key_configured=api_key,
                chars=len(data))
            llm = self._resolve_llm_for(selection.engine)
        except (import_engine.EngineError,
                importer.PortfolioImportError) as exc:
            raise model.MvpError(model.E_MALFORMED, str(exc)) from exc
        try:
            analysis = importer.analyze_document(
                self.root, filename, data, llm=llm, mode=mode,
                engine=selection.engine, model=selection.model,
                pricing_state=selection.pricing_state)
        except importer.PortfolioImportError as exc:
            raise model.MvpError(model.E_MALFORMED, str(exc)) from exc
        draft_id = importer.save_draft(self.root, analysis)
        return {"draft_id": draft_id, "analysis": analysis.to_dict()}

    # --- asynchronous import jobs --------------------------------------------

    def start_import_job(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        filename = payload.get("filename")
        if not isinstance(filename, str) or not filename:
            raise model.MvpError(model.E_MALFORMED, "filename required")
        content = payload.get("content_base64")
        if not isinstance(content, str) or not content:
            raise model.MvpError(model.E_MALFORMED, "content_base64 required")
        try:
            data = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise model.MvpError(model.E_MALFORMED,
                                 "invalid base64 content") from exc
        mode = payload.get("mode", importer.MODE_FACTUAL)
        try:
            job = self.jobs.start(filename, data,
                                  engine=payload.get("engine"), mode=mode)
        except import_jobs.ImportJobError as exc:
            raise model.MvpError(model.E_MALFORMED, str(exc)) from exc
        return job.to_dict()

    def get_import_job(self, job_id: str) -> dict[str, Any]:
        try:
            return self.jobs.get(job_id).to_dict()
        except import_jobs.ImportJobError as exc:
            raise model.MvpError(model.E_UNKNOWN_ID, str(exc)) from exc

    def cancel_import_job(self, job_id: str) -> dict[str, Any]:
        try:
            return self.jobs.cancel(job_id).to_dict()
        except import_jobs.ImportJobError as exc:
            raise model.MvpError(model.E_UNKNOWN_ID, str(exc)) from exc

    def confirm_import(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        draft_id = payload.get("draft_id")
        if not isinstance(draft_id, str) or not draft_id:
            raise model.MvpError(model.E_MALFORMED, "draft_id required")
        decisions = payload.get("decisions")
        if not isinstance(decisions, list):
            raise model.MvpError(model.E_MALFORMED, "decisions must be a list")
        analysis = importer.load_draft(self.root, draft_id)
        return importer.confirm_import(self.root, analysis, decisions)

    # --- on-demand enrichment -------------------------------------------------

    def get_enrichment(self, project_id: str) -> dict[str, Any]:
        suggestions = enrichment.load_suggestions(self.root, project_id)
        return {
            "project_id": project_id,
            "suggestions": [s.to_dict() for s in suggestions],
            "pending": sum(1 for s in suggestions
                           if s.state == enrichment.PENDING),
        }

    def run_enrichment(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        project_id = payload.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            raise model.MvpError(model.E_MALFORMED, "project_id required")
        kind = payload.get("kind")
        if not isinstance(kind, str) or kind not in enrichment.ENRICHMENT_KINDS:
            raise model.MvpError(model.E_MALFORMED,
                                 "unknown enrichment kind")
        try:
            result = enrichment.generate(
                self.root, project_id, kind, llm=self.llm,
                engine=payload.get("engine"))
        except (enrichment.EnrichmentError,
                importer.PortfolioImportError) as exc:
            raise model.MvpError(model.E_MALFORMED, str(exc)) from exc
        return result.to_dict()

    def accept_suggestion(self, project_id: str,
                          payload: Mapping[str, Any]) -> dict[str, Any]:
        suggestion_id = payload.get("suggestion_id")
        if not isinstance(suggestion_id, str) or not suggestion_id:
            raise model.MvpError(model.E_MALFORMED,
                                 "suggestion_id required")
        title = payload.get("title")
        try:
            return enrichment.accept(
                self.root, project_id, suggestion_id,
                title=(title if isinstance(title, str) else None))
        except enrichment.EnrichmentError as exc:
            raise model.MvpError(model.E_MALFORMED, str(exc)) from exc

    def reject_suggestion(self, project_id: str,
                          payload: Mapping[str, Any]) -> dict[str, Any]:
        suggestion_id = payload.get("suggestion_id")
        if not isinstance(suggestion_id, str) or not suggestion_id:
            raise model.MvpError(model.E_MALFORMED,
                                 "suggestion_id required")
        try:
            return enrichment.reject(self.root, project_id, suggestion_id)
        except enrichment.EnrichmentError as exc:
            raise model.MvpError(model.E_MALFORMED, str(exc)) from exc

    # --- mutations (validated, atomic) ---------------------------------------

    def create_project(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return mutations.create_project(self.root, payload)

    def update_project(self, project_id: str,
                       payload: Mapping[str, Any]) -> dict[str, Any]:
        return mutations.update_project(self.root, project_id, payload)

    def create_task(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return mutations.create_task(self.root, payload)

    def update_task(self, task_id: str,
                    payload: Mapping[str, Any]) -> dict[str, Any]:
        return mutations.update_task(self.root, task_id, payload)

    def add_dependency(self, task_id: str,
                       dependency: str) -> dict[str, Any]:
        return mutations.add_dependency(self.root, task_id, dependency)

    def remove_dependency(self, task_id: str,
                          dependency: str) -> dict[str, Any]:
        return mutations.remove_dependency(self.root, task_id, dependency)


def _record_outcome(root: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise model.MvpError(model.E_MALFORMED, "task_id required")
    outcome = payload.get("outcome")
    if not isinstance(outcome, str) or outcome not in model.OUTCOMES:
        raise model.MvpError(model.E_MALFORMED, "outcome required")
    minutes = payload.get("actual_minutes")
    if minutes is not None and not isinstance(minutes, int):
        raise model.MvpError(model.E_MALFORMED, "actual_minutes must be int")
    note = payload.get("note")
    return outcomes.record_and_save(
        root, task_id=task_id, outcome=outcome,
        actual_minutes=minutes,
        note=(str(note) if isinstance(note, str) else ""))


def _host_is_loopback(header: str | None) -> bool:
    if not header:
        return True
    host = header.strip()
    if host.startswith("["):
        host = host[1:host.find("]")] if "]" in host else host
    else:
        host = host.rsplit(":", 1)[0]
    return host in ("127.0.0.1", "localhost", "::1")


def make_handler(app: Dashboard) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        server_version = "TrajectoryOS-MVP/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            return

        def _authorized(self) -> bool:
            if app.token is None:
                return True
            header = self.headers.get("Authorization") or ""
            presented = ""
            if header.startswith("Bearer "):
                presented = header[len("Bearer "):].strip()
            if not presented:
                query = parse_qs(urlparse(self.path).query)
                values = query.get("token")
                presented = values[0].strip() if values else ""
            return presented == app.token

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

        def _send_error(self, exc: model.MvpError) -> None:
            self._send_json(HTTPStatus.CONFLICT,
                            {"status": "ERROR", "error": exc.code,
                             "detail": exc.detail})

        def _guard(self) -> bool:
            if not _host_is_loopback(self.headers.get("Host")):
                self._send_json(HTTPStatus.FORBIDDEN,
                                {"status": "FORBIDDEN"})
                return False
            if not self._authorized():
                self.send_response(HTTPStatus.UNAUTHORIZED)
                self.send_header("WWW-Authenticate", "Bearer")
                body = b'{"status":"UNAUTHORIZED"}\n'
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return False
            return True

        def _read_json(self, maximum: int = MAX_BODY_BYTES) -> dict[str, Any] | None:
            length = int(self.headers.get("Content-Length") or "0")
            if length < 0 or length > maximum:
                self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                                {"status": "PAYLOAD_TOO_LARGE"})
                return None
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(HTTPStatus.BAD_REQUEST,
                                {"status": "BAD_REQUEST"})
                return None
            if not isinstance(payload, dict):
                self._send_json(HTTPStatus.BAD_REQUEST,
                                {"status": "BAD_REQUEST"})
                return None
            return payload

        def _path_parts(self) -> list[str]:
            return [part for part in urlparse(self.path).path.split("/")
                    if part]

        def _query_int(self, name: str) -> int:
            query = parse_qs(urlparse(self.path).query)
            values = query.get(name)
            if not values:
                return 0
            try:
                return max(0, int(values[0]))
            except ValueError:
                return 0

        # --- GET -------------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            if not self._guard():
                return
            parts = self._path_parts()
            try:
                if parts in ([], ["index.html"]):
                    cockpit = app.cockpit()
                    self._send(HTTPStatus.OK,
                               render.render_html(cockpit).encode("utf-8"),
                               "text/html; charset=utf-8")
                    return
                if parts == ["healthz"]:
                    self._send_json(HTTPStatus.OK, {"status": "OK"})
                    return
                if parts == ["api", "cockpit"]:
                    self._send_json(HTTPStatus.OK, app.cockpit().to_dict())
                    return
                if parts == ["api", "portfolio"]:
                    self._send_json(HTTPStatus.OK, app.portfolio().to_dict())
                    return
                if parts == ["api", "views"]:
                    self._send_json(HTTPStatus.OK, app.views())
                    return
                if parts == ["api", "preferences"]:
                    self._send_json(
                        HTTPStatus.OK,
                        {"status": "OK",
                         "preferences": uistate.load_preferences(
                             app.root).to_dict()})
                    return
                if parts == ["api", "saved-views"]:
                    self._send_json(HTTPStatus.OK,
                                    {"status": "OK", **app.saved_views()})
                    return
                if parts == ["api", "export", "superproductivity"]:
                    query = parse_qs(urlparse(self.path).query)
                    raw_ids = query.get("ids", [""])[0]
                    task_ids = [x for x in raw_ids.split(",") if x]
                    limit = (self._query_int("limit")
                             if "limit" in query else 20)
                    self._send_json(
                        HTTPStatus.OK,
                        {"status": "OK",
                         **app.export_preview(task_ids, limit)})
                    return
                if parts == ["api", "import", "engines"]:
                    self._send_json(
                        HTTPStatus.OK,
                        app.engine_catalog(self._query_int("chars")))
                    return
                if len(parts) == 4 and parts[:3] == ["api", "import",
                                                      "jobs"]:
                    self._send_json(HTTPStatus.OK,
                                    app.get_import_job(parts[3]))
                    return
                if len(parts) == 3 and parts[:2] == ["api", "enrichment"]:
                    self._send_json(HTTPStatus.OK,
                                    app.get_enrichment(parts[2]))
                    return
            except model.MvpError as exc:
                self._send_error(exc)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"status": "NOT_FOUND"})

        # --- POST ------------------------------------------------------------

        def do_POST(self) -> None:  # noqa: N802
            if not self._guard():
                return
            parts = self._path_parts()
            maximum = (MAX_IMPORT_BODY_BYTES
                       if parts[:2] == ["api", "import"] else MAX_BODY_BYTES)
            payload = self._read_json(maximum)
            if payload is None:
                return
            try:
                result = self._dispatch_post(parts, payload)
            except model.MvpError as exc:
                self._send_error(exc)
                return
            except (document.DocumentError, importer.PortfolioImportError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST,
                                {"status": "ERROR", "error": "IMPORT_ERROR",
                                 "detail": str(exc)})
                return
            except Exception as exc:  # pragma: no cover - defensive
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR,
                                {"status": "ERROR", "error": "INTERNAL",
                                 "detail": str(exc)})
                return
            if result is None:
                self._send_json(HTTPStatus.NOT_FOUND,
                                {"status": "NOT_FOUND"})
                return
            self._send_json(HTTPStatus.OK, {"status": "OK", **result})

        def _dispatch_post(self, parts: list[str],
                           payload: dict[str, Any]) -> dict[str, Any] | None:
            if parts == ["api", "import", "analyze"]:
                return app.analyze_import(payload)
            if parts == ["api", "import", "jobs"]:
                return app.start_import_job(payload)
            if len(parts) == 5 and parts[:3] == ["api", "import", "jobs"] \
                    and parts[4] == "cancel":
                return app.cancel_import_job(parts[3])
            if len(parts) == 5 and parts[:3] == ["api", "import", "jobs"] \
                    and parts[4] == "confirm":
                return app.confirm_import(payload)
            if parts == ["api", "import", "confirm"]:
                return app.confirm_import(payload)
            if parts == ["api", "enrichment"]:
                return app.run_enrichment(payload)
            if len(parts) == 4 and parts[:2] == ["api", "enrichment"] \
                    and parts[3] == "accept":
                return app.accept_suggestion(parts[2], payload)
            if len(parts) == 4 and parts[:2] == ["api", "enrichment"] \
                    and parts[3] == "reject":
                return app.reject_suggestion(parts[2], payload)
            if parts == ["api", "projects"]:
                return app.create_project(payload)
            if len(parts) == 3 and parts[:2] == ["api", "projects"]:
                return app.update_project(parts[2], payload)
            if parts == ["api", "tasks"]:
                return app.create_task(payload)
            if len(parts) == 4 and parts[:2] == ["api", "tasks"] \
                    and parts[3] == "outcome":
                outcome_payload = dict(payload)
                outcome_payload.setdefault("task_id", parts[2])
                return app.record(outcome_payload)
            if len(parts) == 4 and parts[:2] == ["api", "tasks"] \
                    and parts[3] == "dependency":
                dependency = payload.get("dependency")
                if not isinstance(dependency, str) or not dependency:
                    raise model.MvpError(model.E_MALFORMED,
                                         "dependency required")
                return app.add_dependency(parts[2], dependency)
            if len(parts) == 3 and parts[:2] == ["api", "tasks"]:
                return app.update_task(parts[2], payload)
            if parts == ["api", "record"]:
                return app.record(payload)
            if parts == ["api", "capture"]:
                return app.capture_preview(payload)
            if parts == ["api", "capture", "confirm"]:
                return app.capture_confirm(payload)
            if parts == ["api", "saved-views"]:
                return app.save_saved_view(payload)
            if parts == ["api", "saved-views", "load"]:
                return app.load_saved_view(payload)
            if parts == ["api", "saved-views", "rename"]:
                return app.rename_saved_view(payload)
            if parts == ["api", "saved-views", "delete"]:
                return app.delete_saved_view(payload)
            if parts == ["api", "preferences"]:
                return app.update_preferences(payload)
            if parts == ["api", "highlight"]:
                return app.set_highlight(payload)
            if parts == ["api", "colour"]:
                return app.set_colour(payload)
            return None

        # --- DELETE ----------------------------------------------------------

        def do_DELETE(self) -> None:  # noqa: N802
            if not self._guard():
                return
            parts = self._path_parts()
            try:
                if len(parts) == 5 and parts[:2] == ["api", "tasks"] \
                        and parts[3] == "dependency":
                    result = app.remove_dependency(parts[2], parts[4])
                    self._send_json(HTTPStatus.OK,
                                    {"status": "OK", **result})
                    return
            except model.MvpError as exc:
                self._send_error(exc)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"status": "NOT_FOUND"})

    return _Handler


def create_server(app: Dashboard, host: str = "127.0.0.1",
                  port: int = 8787) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    return server


def serve(app: Dashboard, host: str = "127.0.0.1", port: int = 8787,
           on_ready: Callable[[ThreadingHTTPServer], None] | None = None,
           ) -> None:
    server = create_server(app, host, port)
    if on_ready is not None:
        on_ready(server)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()


__all__ = ["Dashboard", "create_server", "serve"]
