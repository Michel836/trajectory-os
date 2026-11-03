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
import contextlib
import json
import os
from collections.abc import Callable, Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from trajectory_os.mvp import (
    assistant,
    capture,
    changes,
    document,
    engine,
    enrichment,
    import_engine,
    import_jobs,
    importer,
    insights,
    model,
    mutations,
    outcomes,
    planning,
    project_review,
    proposals,
    render,
    signals,
    sources,
    store,
    superproductivity,
    trash,
    uistate,
    visualization,
)
from trajectory_os.mvp import (
    recall as recall_module,
)
from trajectory_os.mvp import (
    search as search_module,
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
        # Best-effort daily safety net: a missing/corrupt portfolio or a
        # read-only root must never prevent the cockpit from opening.
        with contextlib.suppress(OSError, model.MvpError):
            store.maybe_daily_backup(root)

    def cockpit(self) -> engine.Cockpit:
        return engine.build_cockpit(self.root, persist=False)

    def portfolio(self) -> model.Portfolio:
        portfolio = store.load_portfolio(self.root)
        if portfolio is None:
            raise model.MvpError(model.E_MALFORMED, "no portfolio loaded")
        return portfolio

    def record(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return _record_outcome(self.root, payload)

    def undo_outcome(self, task_id: str) -> dict[str, Any]:
        return outcomes.undo_last(self.root, task_id=task_id)

    def changes(self, limit: int = 50, since: str = "") -> dict[str, Any]:
        return {"changes": changes.recent_changes(
            self.root, self.portfolio(), limit=limit, since=since)}

    def rederive_projects(self) -> dict[str, Any]:
        return outcomes.rederive_project_statuses(self.root)

    def explain_project(self, project_id: str) -> dict[str, Any]:
        return insights.explain_project(self.root, project_id)

    def project_review_queue(self) -> dict[str, Any]:
        return project_review.build_queue(self.root)

    def review_project(self, project_id: str,
                       payload: Mapping[str, Any]) -> dict[str, Any]:
        decision = payload.get("decision")
        if not isinstance(decision, str) or not decision:
            raise model.MvpError(model.E_MALFORMED, "decision required")
        return project_review.decide(
            self.root, project_id, decision,
            note=str(payload.get("note", "")))

    def project_update(self, project_id: str,
                       payload: Mapping[str, Any]) -> dict[str, Any]:
        portfolio = self.portfolio()
        project = portfolio.project_map().get(project_id)
        if project is None:
            raise model.MvpError(model.E_UNKNOWN_PROJECT, project_id)
        state = str(payload.get("state", ""))
        if state and state not in visualization.PROJECT_HEALTH:
            raise model.MvpError(model.E_MALFORMED, "invalid project state")
        fields = {
            "state": state,
            "note": str(payload.get("note", ""))[:2000],
            "risks": str(payload.get("risks", ""))[:2000],
            "decision": str(payload.get("decision", ""))[:500],
            "next_step": str(payload.get("next_step", ""))[:500],
        }
        change = changes.append_change(
            self.root, kind=changes.KIND_UPDATE,
            object_kind=changes.OBJECT_PROJECT, object_id=project_id,
            title=project.name, summary="mise à jour de projet",
            before={}, after=fields, source="human", undoable=False)
        return {"project_id": project_id, "update": change.to_dict()}

    def project_timeline(self, project_id: str) -> dict[str, Any]:
        portfolio = self.portfolio()
        project = portfolio.project_map().get(project_id)
        if project is None:
            raise model.MvpError(model.E_UNKNOWN_PROJECT, project_id)
        task_ids = {task.task_id for task in portfolio.tasks
                    if task.project_id == project_id}
        entries: list[dict[str, Any]] = []
        for change in changes.read_changes(self.root, limit=200):
            if change.object_id not in task_ids | {project_id}:
                continue
            entries.append({
                "kind": "change", "at": change.at,
                "title": change.title, "summary": change.summary,
                "source": change.source,
            })
        for signal in signals.read_signals(self.root, limit=200):
            if signal.project_hint != project_id:
                continue
            entries.append({
                "kind": "signal", "at": signal.received_at,
                "title": signal.title, "summary": signal.source_ref,
                "source": signal.source_ref,
            })
        for record in store.read_outcomes(self.root):
            if record.task_id not in task_ids:
                continue
            entries.append({
                "kind": "outcome", "at": record.recorded_at,
                "title": record.task_id, "summary": record.outcome,
                "source": "human",
            })
        entries.sort(key=lambda item: str(item["at"]), reverse=True)
        return {"project_id": project_id, "entries": entries[:100]}

    def assistant_project(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        project_id = payload.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            raise model.MvpError(model.E_MALFORMED, "project_id required")
        return assistant.run_assistant(
            self.root,
            assistant.AssistantRequest(
                kind=assistant.KIND_PROJECT, project_id=project_id,
                question=str(payload.get("question", ""))))

    def assistant_weekly(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return assistant.run_assistant(
            self.root,
            assistant.AssistantRequest(
                kind=assistant.KIND_WEEKLY,
                question=str(payload.get("question", ""))))

    def trash_entries(self) -> dict[str, Any]:
        return {"trash": [entry.to_dict()
                          for entry in trash.list_trash(self.root)]}

    def trash_object(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        kind = payload.get("kind")
        ref_id = payload.get("ref_id")
        if kind not in ("task", "project"):
            raise model.MvpError(model.E_MALFORMED, "kind must be task/project")
        if not isinstance(ref_id, str) or not ref_id:
            raise model.MvpError(model.E_MALFORMED, "ref_id required")
        cascade = bool(payload.get("cascade", False))
        if kind == "task":
            return trash.trash_task(self.root, ref_id)
        return trash.trash_project(self.root, ref_id, cascade=cascade)

    def restore_trash(self, trash_id: str) -> dict[str, Any]:
        return trash.restore_trash(self.root, trash_id)

    def purge_trash(self, trash_id: str) -> dict[str, Any]:
        return trash.purge_trash(self.root, trash_id)

    def undo_change(self, change_id: str) -> dict[str, Any]:
        return changes.undo_change(self.root, change_id)

    def backups(self) -> dict[str, Any]:
        return {"backups": list(store.list_backups(self.root))}

    def search(self, query: str, limit: int = 30) -> dict[str, Any]:
        return search_module.search(self.root, query, limit=limit)

    def intake(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Dry intake: propose a dossier and the proposals, write nothing.

        This is the "paste the whole message" entry point. It never assigns a
        project: it proposes candidates with a reason and abstains when unsure
        (ADR-046). Only `confirm_intake` writes.
        """
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise model.MvpError(model.E_MALFORMED, "text required")
        source_ref = str(payload.get("source_ref", "manuel")) or "manuel"
        route = recall_module.route(self.root, text)
        preview = signals.preview_signal(
            self.root, text=text,
            source_ref=source_ref,
            title=str(payload.get("title", "")),
            project_hint=route["top"] or "",
            sensitivity=str(payload.get("sensitivity", "INTERNAL")))
        return {
            "canonical": False,
            "route": route,
            "duplicate": preview["duplicate"],
            "signal": preview["signal"],
            "proposals": preview["proposals"],
            "note_kinds": sorted(model.NOTE_KINDS),
        }

    def confirm_intake(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Persist the intake after human validation: signal + note (+ accepted).

        One click records the provenance (signal with its hash), attaches the
        readable note to the dossier, and applies only the proposals the human
        explicitly accepted. Nothing else is mutated.
        """
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise model.MvpError(model.E_MALFORMED, "text required")
        project_id = payload.get("project_id")
        if not isinstance(project_id, str) or not project_id.strip():
            raise model.MvpError(model.E_MALFORMED, "project_id required")
        portfolio = self.portfolio()
        if project_id not in portfolio.project_map():
            raise model.MvpError(model.E_UNKNOWN_PROJECT, project_id)
        source_ref = str(payload.get("source_ref", "manuel")) or "manuel"
        note_kind = str(payload.get("note_kind", model.NK_NOTE))
        sensitive = str(payload.get("sensitivity", "INTERNAL"))
        accept = payload.get("accept_proposals")
        accept_ids = ([str(item) for item in accept]
                      if isinstance(accept, list) else [])

        signal = signals.create_signal(
            self.root, text=text, source_ref=source_ref,
            project_hint=project_id,
            title=str(payload.get("title", "")),
            sensitivity=sensitive)["signal"]
        note = mutations.add_note(self.root, {
            "project_id": project_id,
            "text": text,
            "kind": note_kind,
            "source_ref": source_ref,
            "sensitivity": sensitive,
            "content_hash": signal["content_hash"],
        })
        applied: list[str] = []
        rejected: list[dict[str, Any]] = []
        for proposal_id in accept_ids:
            try:
                proposals.apply_proposal(self.root, signal["signal_id"],
                                         proposal_id)
                applied.append(proposal_id)
            except (model.MvpError, signals.SignalError) as exc:
                rejected.append({"proposal_id": proposal_id,
                                 "detail": str(exc)})
        remaining = [proposal.to_dict()
                     for proposal in signals.load_proposals(
                         self.root, signal["signal_id"])
                     if proposal.state == signals.STATE_PENDING]
        return {
            "signal_id": signal["signal_id"],
            "note": note,
            "applied": applied,
            "rejected": rejected,
            "pending": remaining,
        }

    def recall(self, query: str, limit: int = 3) -> dict[str, Any]:
        """Resolve a word into a resumable dossier context (read-only)."""
        return recall_module.resolve(self.root, query, limit_projects=limit)

    def set_aliases(self, project_id: str,
                    payload: Mapping[str, Any]) -> dict[str, Any]:
        return mutations.set_aliases(self.root, project_id,
                                     {"aliases": payload.get("aliases")})

    # --- inbound signals / proposals ---------------------------------------

    def signals(self) -> dict[str, Any]:
        return {"signals": signals.list_bundles(self.root)}

    def create_signal(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        text = payload.get("text")
        filename = payload.get("filename")
        content_b64 = payload.get("content_b64")
        source_ref = str(payload.get("source_ref", "manual"))
        project_hint = str(payload.get("project_hint", ""))
        sensitivity = str(payload.get("sensitivity", "INTERNAL"))
        if isinstance(filename, str) and isinstance(content_b64, str):
            try:
                raw = base64.b64decode(content_b64.encode("ascii"),
                                       validate=True)
            except (ValueError, binascii.Error) as exc:
                raise model.MvpError(model.E_MALFORMED,
                                     "invalid file payload") from exc
            lowered = filename.lower()
            if lowered.endswith(".eml"):
                return sources.ingest_eml_bytes(
                    self.root, filename, raw, sensitivity=sensitivity)
            if lowered.endswith(".mbox"):
                result = sources.ingest_mbox_bytes(
                    self.root, filename, raw, sensitivity=sensitivity)
                return {
                    "count": result["count"],
                    "results": result["results"],
                    "proposals": [proposal
                                  for item in result["results"]
                                  for proposal in item["proposals"]],
                    "errors": result["errors"],
                    "limitations": result["limitations"],
                    "duplicate": False,
                }
            return sources.ingest_document_bytes(
                self.root, filename, raw, project_hint=project_hint,
                sensitivity=sensitivity)
        if not isinstance(text, str) or not text.strip():
            raise model.MvpError(model.E_MALFORMED, "text required")
        return sources.ingest_text(
            self.root, text=text, source_ref=source_ref,
            title=str(payload.get("title", "")),
            project_hint=project_hint, sensitivity=sensitivity)

    def dismiss_signal(self, signal_id: str) -> dict[str, Any]:
        return signals.dismiss_signal(self.root, signal_id)

    def set_source_rule(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        source_ref = payload.get("source_ref")
        if not isinstance(source_ref, str) or not source_ref:
            raise model.MvpError(model.E_MALFORMED, "source_ref required")
        return signals.set_source_rule(
            self.root, source_ref,
            project_id=str(payload.get("project_id", "")),
            mute=bool(payload.get("mute", False)),
            sensitivity=str(payload.get("sensitivity", "INTERNAL")),
            max_proposals=int(payload.get("max_proposals", 5)))

    def mute_signal_source(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        source_ref = payload.get("source_ref")
        if not isinstance(source_ref, str) or not source_ref:
            raise model.MvpError(model.E_MALFORMED, "source_ref required")
        return signals.mute_source(
            self.root, source_ref, muted=bool(payload.get("muted", True)))

    def accept_proposal(self, signal_id: str,
                        proposal_id: str) -> dict[str, Any]:
        return proposals.apply_proposal(self.root, signal_id, proposal_id)

    def reject_proposal(self, signal_id: str, proposal_id: str,
                        payload: Mapping[str, Any]) -> dict[str, Any]:
        return proposals.reject_proposal(
            self.root, signal_id, proposal_id,
            reason=str(payload.get("reason", "")))

    def defer_proposal(self, signal_id: str,
                       proposal_id: str) -> dict[str, Any]:
        return proposals.defer_proposal(self.root, signal_id, proposal_id)

    def backup_now(self) -> dict[str, Any]:
        return store.backup_portfolio(self.root, reason="ui")

    def restore_backup(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        name = payload.get("name")
        if name is not None and not isinstance(name, str):
            raise model.MvpError(model.E_MALFORMED, "name must be a string")
        return store.restore_portfolio(self.root, name=name or "")

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

    def _import_decisions(
        self, payload: Mapping[str, Any],
    ) -> tuple[str, list[Any], dict[str, Any]]:
        draft_id = payload.get("draft_id")
        if not isinstance(draft_id, str) or not draft_id:
            raise model.MvpError(model.E_MALFORMED, "draft_id required")
        decisions = payload.get("decisions")
        if not isinstance(decisions, list):
            raise model.MvpError(model.E_MALFORMED, "decisions must be a list")
        analysis = importer.load_draft(self.root, draft_id)
        return draft_id, decisions, importer.validate_decisions(
            self.root, analysis, decisions)

    def validate_import(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Read-only pre-confirm validation (no portfolio writes)."""
        draft_id, _decisions, report = self._import_decisions(payload)
        return {**report, "draft_id": draft_id}

    def confirm_import(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        draft_id, decisions, report = self._import_decisions(payload)
        analysis = importer.load_draft(self.root, draft_id)
        if not report["ok"]:
            raise importer.ImportValidationError(
                {**report, "draft_id": draft_id})
        return importer.confirm_import(self.root, analysis, decisions)

    def list_import_drafts(self) -> dict[str, Any]:
        return {"drafts": importer.list_drafts(self.root)}

    def get_import_draft(self, draft_id: str) -> dict[str, Any]:
        analysis = importer.load_draft(self.root, draft_id)
        return {"draft_id": draft_id, "analysis": analysis.to_dict()}

    # --- week + tomorrow planning (presentation sidecar, no portfolio write) -

    def planning_view(self) -> dict[str, Any]:
        return planning.build_planning_view(self.root)

    def tomorrow_view(self) -> dict[str, Any]:
        return {"status": "OK", **planning.build_tomorrow_view(self.root)}

    def week_view(self) -> dict[str, Any]:
        return {"status": "OK", **planning.build_week_view(self.root)}

    def end_of_day_review(self) -> dict[str, Any]:
        return {"status": "OK", **planning.build_review(self.root)}

    def save_tomorrow_plan(self,
                           payload: Mapping[str, Any]) -> dict[str, Any]:
        from datetime import date, timedelta

        portfolio = self.portfolio()
        day = payload.get("date")
        target = (str(day) if isinstance(day, str) and day
                  else (date.today() + timedelta(days=1)).isoformat())
        main = payload.get("main_task_ids")
        optional = payload.get("optional_task_ids")
        if not isinstance(main, (list, tuple)):
            raise model.MvpError(model.E_MALFORMED,
                                 "main_task_ids must be a list")
        if not isinstance(optional, (list, tuple)):
            raise model.MvpError(model.E_MALFORMED,
                                 "optional_task_ids must be a list")
        note = payload.get("note")
        state = planning.load_planning(self.root)
        try:
            updated = planning.plan_tomorrow(
                state, portfolio, day=target,
                main_task_ids=[str(x) for x in main],
                optional_task_ids=[str(x) for x in optional],
                note=(str(note) if isinstance(note, str) else ""),
                saved_at=planning.utc_timestamp())
        except model.MvpError as exc:
            raise model.MvpError(exc.code, exc.detail) from exc
        planning.save_planning(self.root, updated)
        target_date = date.fromisoformat(target)
        return {
            "planning": updated.to_dict(),
            "tomorrow": planning.build_tomorrow_view(
                self.root, today=target_date - timedelta(days=1),
                target=target_date, portfolio=portfolio),
        }

    def save_week_plan(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        from datetime import date

        portfolio = self.portfolio()
        week_start = payload.get("week_start")
        if not isinstance(week_start, str) or not week_start:
            week_start = planning.monday_of(date.today()).isoformat()
        outcomes = payload.get("outcomes")
        if not isinstance(outcomes, list):
            raise model.MvpError(model.E_MALFORMED,
                                 "outcomes must be a list")
        note = payload.get("note")
        state = planning.load_planning(self.root)
        updated = planning.plan_week(
            state, week_start=week_start, outcomes=outcomes,
            note=(str(note) if isinstance(note, str) else ""),
            saved_at=planning.utc_timestamp())
        planning.save_planning(self.root, updated)
        return {
            "planning": updated.to_dict(),
            "week": planning.build_week_view(
                self.root, week_start=week_start, portfolio=portfolio),
        }

    def review_week(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        action = payload.get("action")
        if not isinstance(action, str) or action not in (
                "close", "reopen", "drop", "restate", "carry"):
            raise model.MvpError(model.E_MALFORMED, "unknown review action")
        outcome_id = payload.get("outcome_id")
        if not isinstance(outcome_id, str) or not outcome_id:
            raise model.MvpError(model.E_MALFORMED, "outcome_id required")
        state = planning.load_planning(self.root)
        if action == "close":
            updated = planning.set_outcome_state(
                state, outcome_id, planning.OUTCOME_DONE)
        elif action == "drop":
            updated = planning.set_outcome_state(
                state, outcome_id, planning.OUTCOME_DROPPED)
        elif action == "reopen":
            updated = planning.set_outcome_state(
                state, outcome_id, planning.OUTCOME_OPEN)
        elif action == "restate":
            title = payload.get("title")
            note = payload.get("note")
            project_id = payload.get("project_id")
            updated = planning.restate_outcome(
                state, outcome_id,
                title=(str(title) if isinstance(title, str) else None),
                note=(str(note) if isinstance(note, str) else None),
                project_id=(str(project_id)
                            if isinstance(project_id, str) else None),
            )
        else:
            new_week_start = payload.get("new_week_start")
            if not isinstance(new_week_start, str) or not new_week_start:
                raise model.MvpError(model.E_MALFORMED,
                                     "new_week_start required for carry")
            updated = planning.carry_outcomes(
                state, [outcome_id], new_week_start=new_week_start)
        planning.save_planning(self.root, updated)
        return {
            "planning": updated.to_dict(),
            "week": planning.build_week_view(self.root),
        }

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

    def estimate_tasks(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        updates = payload.get("updates")
        if not isinstance(updates, list):
            raise model.MvpError(model.E_MALFORMED, "updates must be a list")
        return mutations.estimate_tasks(self.root, updates)

    def rename_domain(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        old = payload.get("from")
        new = payload.get("to")
        if not isinstance(old, str) or not isinstance(new, str):
            raise model.MvpError(model.E_MALFORMED, "from/to required")
        return mutations.rename_domain(self.root, old=old, new=new)

    def update_task(self, task_id: str,
                    payload: Mapping[str, Any]) -> dict[str, Any]:
        return mutations.update_task(self.root, task_id, payload)

    def add_dependency(self, task_id: str,
                       dependency: str) -> dict[str, Any]:
        return mutations.add_dependency(self.root, task_id, dependency)

    def calibrate_task(self, task_id: str) -> dict[str, Any]:
        return mutations.apply_calibrated_estimate(self.root, task_id)

    def remove_dependency(self, task_id: str,
                          dependency: str) -> dict[str, Any]:
        return mutations.remove_dependency(self.root, task_id, dependency)

    # --- project / task conversion & parking ---------------------------------

    def park_project(self, project_id: str,
                     payload: Mapping[str, Any]) -> dict[str, Any]:
        return mutations.park_project(self.root, project_id, payload)

    def promote_task(self, task_id: str,
                     payload: Mapping[str, Any]) -> dict[str, Any]:
        return mutations.promote_task(self.root, task_id, payload)

    def demote_project(self, project_id: str,
                       payload: Mapping[str, Any]) -> dict[str, Any]:
        return mutations.demote_project(self.root, project_id, payload)

    # --- calendar & days off -------------------------------------------------

    def create_event(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return mutations.create_event(self.root, payload)

    def update_event(self, event_id: str,
                     payload: Mapping[str, Any]) -> dict[str, Any]:
        return mutations.update_event(self.root, event_id, payload)

    def delete_event(self, event_id: str) -> dict[str, Any]:
        return mutations.delete_event(self.root, event_id)

    def set_day_off(self, day: str, off: bool) -> dict[str, Any]:
        return mutations.set_day_off(self.root, day, off=off)

    # --- routines ------------------------------------------------------------

    def create_routine(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return mutations.create_routine(self.root, payload)

    def update_routine(self, routine_id: str,
                       payload: Mapping[str, Any]) -> dict[str, Any]:
        return mutations.update_routine(self.root, routine_id, payload)

    def delete_routine(self, routine_id: str) -> dict[str, Any]:
        return mutations.delete_routine(self.root, routine_id)

    def materialize_routines(self, day: str) -> dict[str, Any]:
        return mutations.materialize_routines(self.root, day)

    # --- decisions -----------------------------------------------------------

    def create_decision(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return mutations.create_decision(self.root, payload)

    def update_decision(self, decision_id: str,
                        payload: Mapping[str, Any]) -> dict[str, Any]:
        return mutations.update_decision(self.root, decision_id, payload)

    def decide(self, decision_id: str,
               payload: Mapping[str, Any]) -> dict[str, Any]:
        return mutations.decide(self.root, decision_id, payload)

    def delete_decision(self, decision_id: str) -> dict[str, Any]:
        return mutations.delete_decision(self.root, decision_id)


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
            payload: dict[str, Any] = {"status": "ERROR", "error": exc.code,
                                      "detail": exc.detail}
            payload.update(exc.metadata)
            self._send_json(HTTPStatus.CONFLICT, payload)

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
                if parts == ["api", "planning"]:
                    self._send_json(HTTPStatus.OK, app.planning_view())
                    return
                if parts == ["api", "tomorrow"]:
                    self._send_json(HTTPStatus.OK, app.tomorrow_view())
                    return
                if parts == ["api", "week"]:
                    self._send_json(HTTPStatus.OK, app.week_view())
                    return
                if parts == ["api", "review"]:
                    self._send_json(HTTPStatus.OK, app.end_of_day_review())
                    return
                if parts == ["api", "changes"]:
                    query = parse_qs(urlparse(self.path).query)
                    limit = (self._query_int("limit")
                             if "limit" in query else 50)
                    since = query.get("since", [""])[0]
                    self._send_json(
                        HTTPStatus.OK,
                        {"status": "OK", **app.changes(limit, since)})
                    return
                if parts == ["api", "trash"]:
                    self._send_json(HTTPStatus.OK, app.trash_entries())
                    return
                if parts == ["api", "backups"]:
                    self._send_json(HTTPStatus.OK, app.backups())
                    return
                if parts == ["api", "projects", "review"]:
                    self._send_json(HTTPStatus.OK,
                                    app.project_review_queue())
                    return
                if len(parts) == 4 and parts[:2] == ["api", "projects"] \
                        and parts[3] == "timeline":
                    self._send_json(HTTPStatus.OK,
                                    app.project_timeline(parts[2]))
                    return
                if len(parts) == 4 and parts[:2] == ["api", "projects"] \
                        and parts[3] == "explain":
                    self._send_json(HTTPStatus.OK,
                                    app.explain_project(parts[2]))
                    return
                if parts == ["api", "signals"]:
                    self._send_json(HTTPStatus.OK, app.signals())
                    return
                if parts == ["api", "search"]:
                    query = parse_qs(urlparse(self.path).query)
                    self._send_json(
                        HTTPStatus.OK,
                        app.search(query.get("q", [""])[0],
                                   self._query_int("limit")
                                   if "limit" in query else 30))
                    return
                if parts == ["api", "recall"]:
                    query = parse_qs(urlparse(self.path).query)
                    self._send_json(
                        HTTPStatus.OK,
                        app.recall(query.get("q", [""])[0],
                                   self._query_int("limit")
                                   if "limit" in query else 3))
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
                if parts == ["api", "import", "drafts"]:
                    self._send_json(HTTPStatus.OK,
                                    app.list_import_drafts())
                    return
                if len(parts) == 4 and parts[:3] == ["api", "import",
                                                      "drafts"]:
                    self._send_json(HTTPStatus.OK,
                                    app.get_import_draft(parts[3]))
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
                       if parts[:2] in (["api", "import"],
                                        ["api", "signals"])
                       else MAX_BODY_BYTES)
            payload = self._read_json(maximum)
            if payload is None:
                return
            try:
                result = self._dispatch_post(parts, payload)
            except model.MvpError as exc:
                self._send_error(exc)
                return
            except importer.ImportValidationError as exc:
                self._send_json(HTTPStatus.CONFLICT,
                                {"status": "ERROR",
                                 "error": "IMPORT_UNRESOLVED",
                                 **exc.payload})
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
            if parts == ["api", "import", "validate"]:
                return app.validate_import(payload)
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
            if len(parts) == 4 and parts[:2] == ["api", "changes"] \
                    and parts[3] == "undo":
                return app.undo_change(parts[2])
            if parts == ["api", "trash"]:
                return app.trash_object(payload)
            if len(parts) == 4 and parts[:2] == ["api", "trash"] \
                    and parts[3] == "restore":
                return app.restore_trash(parts[2])
            if len(parts) == 4 and parts[:2] == ["api", "trash"] \
                    and parts[3] == "purge":
                return app.purge_trash(parts[2])
            if parts == ["api", "backups"]:
                return app.backup_now()
            if parts == ["api", "backups", "restore"]:
                return app.restore_backup(payload)
            if parts == ["api", "assistant", "project"]:
                return app.assistant_project(payload)
            if parts == ["api", "assistant", "weekly"]:
                return app.assistant_weekly(payload)
            if parts == ["api", "sources", "rule"]:
                return app.set_source_rule(payload)
            if parts == ["api", "signals"]:
                return app.create_signal(payload)
            if len(parts) == 4 and parts[:2] == ["api", "signals"] \
                    and parts[3] == "dismiss":
                return app.dismiss_signal(parts[2])
            if len(parts) == 4 and parts[:2] == ["api", "signals"] \
                    and parts[3] == "mute":
                return app.mute_signal_source(payload)
            if len(parts) == 6 and parts[:2] == ["api", "signals"] \
                    and parts[3] == "proposals":
                if parts[5] == "accept":
                    return app.accept_proposal(parts[2], parts[4])
                if parts[5] == "reject":
                    return app.reject_proposal(parts[2], parts[4], payload)
                if parts[5] == "defer":
                    return app.defer_proposal(parts[2], parts[4])
            if parts == ["api", "enrichment"]:
                return app.run_enrichment(payload)
            if len(parts) == 4 and parts[:2] == ["api", "enrichment"] \
                    and parts[3] == "accept":
                return app.accept_suggestion(parts[2], payload)
            if len(parts) == 4 and parts[:2] == ["api", "enrichment"] \
                    and parts[3] == "reject":
                return app.reject_suggestion(parts[2], payload)
            if parts == ["api", "events"]:
                return app.create_event(payload)
            if len(parts) == 4 and parts[:2] == ["api", "events"] \
                    and parts[3] == "delete":
                return app.delete_event(parts[2])
            if len(parts) == 3 and parts[:2] == ["api", "events"]:
                return app.update_event(parts[2], payload)
            if parts == ["api", "calendar", "day-off"]:
                day = payload.get("day")
                if not isinstance(day, str) or not day:
                    raise model.MvpError(model.E_MALFORMED, "day required")
                return app.set_day_off(day, bool(payload.get("off", True)))
            if parts == ["api", "routines"]:
                return app.create_routine(payload)
            if parts == ["api", "routines", "materialize"]:
                day = payload.get("day")
                if not isinstance(day, str) or not day:
                    raise model.MvpError(model.E_MALFORMED, "day required")
                return app.materialize_routines(day)
            if len(parts) == 4 and parts[:2] == ["api", "routines"] \
                    and parts[3] == "delete":
                return app.delete_routine(parts[2])
            if len(parts) == 3 and parts[:2] == ["api", "routines"]:
                return app.update_routine(parts[2], payload)
            if parts == ["api", "decisions"]:
                return app.create_decision(payload)
            if len(parts) == 4 and parts[:2] == ["api", "decisions"] \
                    and parts[3] == "decide":
                return app.decide(parts[2], payload)
            if len(parts) == 4 and parts[:2] == ["api", "decisions"] \
                    and parts[3] == "delete":
                return app.delete_decision(parts[2])
            if len(parts) == 3 and parts[:2] == ["api", "decisions"]:
                return app.update_decision(parts[2], payload)
            if len(parts) == 4 and parts[:2] == ["api", "projects"] \
                    and parts[3] == "review":
                return app.review_project(parts[2], payload)
            if len(parts) == 4 and parts[:2] == ["api", "projects"] \
                    and parts[3] == "update":
                return app.project_update(parts[2], payload)
            if parts == ["api", "projects", "rederive"]:
                return app.rederive_projects()
            if parts == ["api", "projects"]:
                return app.create_project(payload)
            if len(parts) == 4 and parts[:2] == ["api", "projects"] \
                    and parts[3] == "demote":
                return app.demote_project(parts[2], payload)
            if len(parts) == 4 and parts[:2] == ["api", "projects"] \
                    and parts[3] == "park":
                return app.park_project(parts[2], payload)
            if len(parts) == 4 and parts[:2] == ["api", "projects"] \
                    and parts[3] == "aliases":
                return app.set_aliases(parts[2], payload)
            if len(parts) == 3 and parts[:2] == ["api", "projects"]:
                return app.update_project(parts[2], payload)
            if parts == ["api", "domains", "rename"]:
                return app.rename_domain(payload)
            if parts == ["api", "tasks", "estimate"]:
                return app.estimate_tasks(payload)
            if parts == ["api", "tasks"]:
                return app.create_task(payload)
            if len(parts) == 4 and parts[:2] == ["api", "tasks"] \
                    and parts[3] == "promote":
                return app.promote_task(parts[2], payload)
            if len(parts) == 4 and parts[:2] == ["api", "tasks"] \
                    and parts[3] == "calibrate":
                return app.calibrate_task(parts[2])
            if len(parts) == 5 and parts[:2] == ["api", "tasks"] \
                    and parts[3] == "outcome" and parts[4] == "undo":
                return app.undo_outcome(parts[2])
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
            if parts == ["api", "intake"]:
                return app.intake(payload)
            if parts == ["api", "intake", "confirm"]:
                return app.confirm_intake(payload)
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
            if parts == ["api", "planning", "tomorrow"]:
                return app.save_tomorrow_plan(payload)
            if parts == ["api", "planning", "week"]:
                return app.save_week_plan(payload)
            if parts == ["api", "planning", "week", "review"]:
                return app.review_week(payload)
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
