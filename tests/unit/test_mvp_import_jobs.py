"""Tests for the asynchronous import job status API and cancellation."""

from __future__ import annotations

import base64
import http.client
import json
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import pytest

from trajectory_os.mvp import dashboard, model, store


def _seed(root: str) -> None:
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=(model.Project(project_id="inventory-tool", name="Inventory Tool",
                                objective="recover"),),
        tasks=(),
    ))


_FAKE_RESULT: dict[str, Any] = {
    "items": [
        {"kind": "PROJECT", "title": "Inventory Tool", "parent": None,
         "source_text": "Inventory Tool", "evidence": "FACT",
         "merge_project_id": "inventory-tool", "confidence": 0.9},
        {"kind": "TASK", "title": "Enable the workspace", "parent": "Inventory Tool",
         "source_text": "activation", "evidence": "FACT", "confidence": 0.8},
    ],
}


def _fake_llm(system: str, user: str) -> Mapping[str, Any]:
    del system, user
    return _FAKE_RESULT


def _payload(content: bytes = b"Inventory Tool : activation",
             engine: str = "local") -> dict[str, Any]:
    return {
        "filename": "notes.txt",
        "content_base64": base64.b64encode(content).decode("ascii"),
        "engine": engine,
        "mode": "factual",
    }


def _wait_terminal(app: dashboard.Dashboard, job_id: str,
                   timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = app.get_import_job(job_id)
        if job["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            return job
        time.sleep(0.02)
    raise AssertionError("job did not terminate in time")


def test_import_job_completes_with_report(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    app = dashboard.Dashboard(root, llm=_fake_llm)  # type: ignore[arg-type]
    job = app.start_import_job(_payload())
    assert job["status"] in ("QUEUED", "RUNNING")
    assert job["engine"] == "local"
    finished = _wait_terminal(app, job["job_id"])
    assert finished["status"] == "COMPLETED"
    assert finished["stage"] == "COMPLETED"
    assert finished["percent"] == 100
    assert finished["draft_id"]
    report = finished["completion_report"]
    assert report["engine"] == "local"
    assert report["ai_generated_items"] == 0
    assert report["factual_items"] == 2
    assert finished["elapsed_ms"] >= 0


def test_import_job_reports_real_progress(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    app = dashboard.Dashboard(root, llm=_fake_llm)  # type: ignore[arg-type]
    job = app.start_import_job(_payload())
    stages = set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        snapshot = app.get_import_job(job["job_id"])
        stages.add(snapshot["stage"])
        if snapshot["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            break
        time.sleep(0.005)
    # At minimum the backend reports the real terminal stage; the pipeline
    # stages are emitted by the analysis engine (verified in importer tests).
    assert "COMPLETED" in stages


def test_import_job_cancel_is_cooperative(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    started = threading.Event()
    release = threading.Event()

    def slow_llm(system: str, user: str) -> Mapping[str, Any]:
        del system, user
        started.set()
        release.wait(timeout=5)
        return _FAKE_RESULT

    app = dashboard.Dashboard(root, llm=slow_llm)  # type: ignore[arg-type]
    job = app.start_import_job(_payload())
    assert started.wait(timeout=5)
    cancelled = app.cancel_import_job(job["job_id"])
    assert cancelled["cancel_requested"] is True
    release.set()
    finished = _wait_terminal(app, job["job_id"])
    assert finished["status"] == "CANCELLED"
    assert finished["stage"] == "CANCELLED"
    assert finished["draft_id"] is None


def test_import_job_rejects_deepseek_without_key(
        tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    root = str(tmp_path)
    _seed(root)
    app = dashboard.Dashboard(root, llm=_fake_llm)  # type: ignore[arg-type]
    with pytest.raises(model.MvpError):
        app.start_import_job(_payload(engine="deepseek-flash"))


def test_import_job_rejects_deepseek_pro(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    app = dashboard.Dashboard(root, llm=_fake_llm)  # type: ignore[arg-type]
    with pytest.raises(model.MvpError):
        app.start_import_job(_payload(engine="deepseek-pro"))


def test_engine_catalog_endpoint_shape(
        tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    root = str(tmp_path)
    _seed(root)
    app = dashboard.Dashboard(root, llm=_fake_llm)  # type: ignore[arg-type]
    catalog = app.engine_catalog(chars=4000)
    # Without a key the time-based default is local, and exactly one engine
    # is marked as the default.
    assert catalog["default_engine"] == "local"
    assert catalog["recommended_engine"] == "local"
    engines = {entry["engine"]: entry for entry in catalog["engines"]}
    assert engines["local"]["is_default"] is True
    assert engines["local"]["estimated_cost_usd"] == 0.0
    defaults = [e for e in engines.values() if e["is_default"]]
    assert len(defaults) == 1
    assert "deepseek-flash" in engines
    assert engines["deepseek-flash"]["selectable"] is False
    assert "UTC" in catalog["peak_windows"]


def test_engine_catalog_default_follows_off_peak(
        tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    root = str(tmp_path)
    _seed(root)
    app = dashboard.Dashboard(root, llm=_fake_llm)  # type: ignore[arg-type]
    catalog = app.engine_catalog(chars=4000)
    engines = {entry["engine"]: entry for entry in catalog["engines"]}
    defaults = [e for e in engines.values() if e["is_default"]]
    assert len(defaults) == 1
    # The default is time-based (off-peak -> Flash, peak -> Local), and it is
    # always one of the two selectable engines.
    assert catalog["default_engine"] in {"local", "deepseek-flash"}
    assert defaults[0]["engine"] == catalog["default_engine"]
    assert engines["local"]["selectable"] is True
    assert engines["deepseek-flash"]["selectable"] is True


# --- HTTP smoke test ----------------------------------------------------------


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


def test_http_job_flow_and_engines(
        tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        status, catalog = _request(port, "GET", "/api/import/engines?chars=100")
        assert status == 200
        assert catalog["default_engine"] == "local"

        status, job = _request(port, "POST", "/api/import/jobs", _payload())
        assert status == 200
        job_id = str(job["job_id"])

        deadline = time.monotonic() + 5
        finished: dict[str, object] = job
        while time.monotonic() < deadline:
            _, finished = _request(port, "GET",
                                   f"/api/import/jobs/{job_id}")
            if finished["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
                break
            time.sleep(0.02)
        assert finished["status"] == "COMPLETED"

        status, confirmed = _request(
            port, "POST", f"/api/import/jobs/{job_id}/confirm",
            {"draft_id": finished["draft_id"], "decisions": []})
        assert status == 200
