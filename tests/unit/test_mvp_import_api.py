"""Unit tests for the MVP document import HTTP endpoints (two-phase)."""

from __future__ import annotations

import base64
import http.client
import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from trajectory_os.mvp import dashboard, model, store


def _seed(root: str) -> None:
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=(model.Project(project_id="inventory-tool", name="Inventory Tool",
                                objective="Recover funds"),),
        tasks=(),
    ))


_FAKE_LLM_RESULT: dict[str, Any] = {
    "items": [
        {"kind": "PROJECT", "title": "Inventory Tool", "parent": None,
         "source_text": "Inventory Tool : activation", "evidence": "FACT",
         "merge_project_id": "inventory-tool",
         "needs_review": False, "confidence": 0.9},
        {"kind": "TASK", "title": "Enable the workspace",
         "parent": "Inventory Tool", "source_text": "workspace activation",
         "evidence": "FACT", "needs_review": False,
         "confidence": 0.8},
    ],
}


def _fake_llm(system: str, user: str) -> Mapping[str, Any]:
    del system, user
    return _FAKE_LLM_RESULT


@contextmanager
def _server(root: str) -> Iterator[int]:
    app = dashboard.Dashboard(root, llm=_fake_llm)  # type: ignore[arg-type]
    server = dashboard.create_server(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(port: int, method: str, path: str,
             body: dict[str, object] | None = None
             ) -> tuple[int, dict[str, object]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    payload = json.dumps(body) if body is not None else None
    headers = {"Content-Type": "application/json"} if payload else {}
    conn.request(method, path, body=payload, headers=headers)
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        data = {"_raw": raw.decode("utf-8", "replace")}
    return response.status, data


def _doc_payload(filename: str, content: bytes) -> dict[str, object]:
    return {
        "filename": filename,
        "content_base64": base64.b64encode(content).decode("ascii"),
    }


def test_analyze_then_confirm_flow(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        payload = _doc_payload("notes.txt", b"Inventory Tool : activation")
        # Explicit engine keeps this flow deterministic regardless of the
        # current DeepSeek peak/off-peak default.
        payload["engine"] = "local"
        status, result = _request(
            port, "POST", "/api/import/analyze", payload)
        assert status == 200
        draft_id = result["draft_id"]
        analysis = result["analysis"]
        assert analysis["engine"] == "local"
        assert analysis["mode"] == "factual"
        assert analysis["completion_report"]["ai_generated_items"] == 0
        assert len(analysis["candidates"]) == 2
        # Portfolio unchanged after analyze (no mutation).
        portfolio = store.load_portfolio(root)
        assert portfolio is not None and len(portfolio.projects) == 1

        orc = next(c for c in analysis["candidates"]
                   if c["title"] == "Inventory Tool")
        task = next(c for c in analysis["candidates"]
                    if c["title"] == "Enable the workspace")
        status, confirm = _request(port, "POST", "/api/import/confirm", {
            "draft_id": draft_id,
            "decisions": [
                {"candidate_id": orc["candidate_id"], "action": "import",
                 "merge_project_id": "inventory-tool"},
                {"candidate_id": task["candidate_id"], "action": "import"},
            ],
        })
        assert status == 200
        assert confirm["merged"] == 1
        assert confirm["created_tasks"] == 1
        # Merge did not duplicate the project; task attached to it.
        portfolio = store.load_portfolio(root)
        assert portfolio is not None
        assert len(portfolio.projects) == 1
        assert any(t.title == "Enable the workspace"
                   and t.project_id == "inventory-tool" for t in portfolio.tasks)


def test_analyze_unsupported_format_rejected(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        status, result = _request(
            port, "POST", "/api/import/analyze",
            _doc_payload("notes.xlsx", b"whatever"))
        assert status == 400
        assert result["error"] == "IMPORT_ERROR"


def test_confirm_unknown_draft_rejected(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        status, result = _request(port, "POST", "/api/import/confirm", {
            "draft_id": "deadbeefdead", "decisions": []})
        assert status == 400
        assert result["error"] == "IMPORT_ERROR"


def test_import_accepts_body_larger_than_regular_limit(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        # ~50KB document -> base64 body exceeds the regular 64KB endpoint cap
        # but is well under the import cap; the import endpoint must accept it.
        big = base64.b64encode(b"x" * 50_000).decode("ascii")
        status, result = _request(port, "POST", "/api/import/analyze",
                                  {"filename": "big.txt",
                                   "content_base64": big})
        assert status == 200
        assert result["analysis"]["text_chars"] == 50_000
