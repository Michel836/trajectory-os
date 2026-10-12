"""Regression tests for the on-demand enrichment response schema.

These tests exercise the *real* :class:`~trajectory_os.mvp.importer.LocalLlm`
and :class:`~trajectory_os.mvp.importer.DeepSeekLlm` schema plumbing (via the
HTTP request body), not only a stubbed callable, so the previous importer
schema mismatch cannot silently return.

All fixtures are synthetic and generic.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from trajectory_os.intelligence import model as intel_model
from trajectory_os.mvp import enrichment, importer, model, store


def _seed(root: object) -> None:
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=(model.Project(project_id="demo",
                                name="Sample Documentation Project",
                                objective="Publish a small docs set"),),
        tasks=(
            model.Task(task_id="draft-outline", project_id="demo",
                       title="Draft the outline"),
            model.Task(task_id="review-draft", project_id="demo",
                       title="Review the draft"),
        ),
    ))


def _stub(result: Mapping[str, Any]):
    def _llm(system: str, user: str) -> Mapping[str, Any]:
        del system, user
        return result
    return _llm


def _local_response(suggestions: list[dict[str, Any]]) -> bytes:
    return json.dumps({
        "message": {"content": json.dumps({"suggestions": suggestions})},
        "prompt_eval_count": 11,
        "eval_count": 4,
    }).encode("utf-8")


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRAJECTORY_MVP_IMPORT_OFFLINE", raising=False)
    monkeypatch.delenv("TRAJECTORY_MVP_IMPORT_MODEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)


# --- A — local enrichment uses the suggestion schema (real client) ------------


def test_local_enrichment_sends_suggestion_schema(
        monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    _seed(tmp_path)
    _clean_env(monkeypatch)
    captured: dict[str, Any] = {}

    def fake_urlopen(req: Any, timeout: float | None = None) -> _Resp:
        del timeout
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return _Resp(_local_response([
            {"kind": "NEXT_ACTION", "title": "Inventory the sections",
             "detail": "begin with the introduction"}]))

    monkeypatch.setattr(importer.request, "urlopen", fake_urlopen)
    result = enrichment.generate(
        str(tmp_path), "demo", enrichment.ENRICH_NEXT_ACTIONS, engine="local")

    assert captured["payload"]["format"] == enrichment._SUGGESTION_SCHEMA
    assert captured["payload"]["model"] == "qwen3.8:27b-q4_K_M"
    assert "items" not in captured["payload"]["format"]["properties"]
    assert result.suggestions[0].kind == enrichment.NEXT_ACTION
    assert result.suggestions[0].state == enrichment.PENDING
    assert result.engine == "local"
    assert result.model == "qwen3.8:27b-q4_K_M"


# --- B — DeepSeek enrichment uses the suggestion schema (real client) --------


def test_deepseek_enrichment_sends_suggestion_schema(
        monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    _seed(tmp_path)
    _clean_env(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    captured: dict[str, Any] = {}

    def fake_urlopen(req: Any, timeout: float | None = None) -> _Resp:
        del timeout
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        body = json.dumps({
            "choices": [{"message": {"content": json.dumps({
                "suggestions": [{"kind": "DELIVERABLE",
                                 "title": "Published documentation page"}]})}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 2},
        }).encode("utf-8")
        return _Resp(body)

    monkeypatch.setattr(importer.request, "urlopen", fake_urlopen)
    result = enrichment.generate(
        str(tmp_path), "demo", enrichment.ENRICH_DELIVERABLES,
        engine="deepseek-flash")

    sent = captured["payload"]["response_format"]["json_schema"]
    assert sent == enrichment._SUGGESTION_SCHEMA
    assert result.engine == "deepseek-flash"
    assert result.model == "deepseek-flash"
    assert result.suggestions[0].kind == enrichment.DELIVERABLE


# --- C — import without a schema still uses the importer schema --------------


def test_import_default_still_uses_import_schema(
        monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_env(monkeypatch)
    captured: dict[str, Any] = {}

    def fake_urlopen(req: Any, timeout: float | None = None) -> _Resp:
        del timeout
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        body = json.dumps({
            "message": {"content": json.dumps({"items": []})},
        }).encode("utf-8")
        return _Resp(body)

    monkeypatch.setattr(importer.request, "urlopen", fake_urlopen)
    fn, engine_id, model_name = importer.make_engine_llm("local")
    assert fn is not None
    assert engine_id == "local"
    assert model_name == "qwen3.8:27b-q4_K_M"
    assert captured == {}  # not called until invoked
    fn("sys", "user")
    assert captured["payload"]["format"] == importer._LLM_SCHEMA

    # The legacy import entry point keeps the same default.
    import_fn = importer.make_import_llm("local")
    assert import_fn is not None
    import_fn("sys", "user")
    assert captured["payload"]["format"] == importer._LLM_SCHEMA


# --- D/E/F — parser accepts the schema shape, NEXT_ACTION and DEPENDENCY ------


def test_parser_accepts_schema_shaped_response(tmp_path: object) -> None:
    _seed(tmp_path)
    result = enrichment.generate(
        str(tmp_path), "demo", enrichment.ENRICH_DEPENDENCIES,
        llm=_stub({"suggestions": [{
            "kind": "DEPENDENCY", "title": "Draft before review",
            "detail": "outline first", "source_title": "Draft the outline",
            "target_title": "Review the draft"}]}),
        engine="local")  # type: ignore[arg-type]
    assert result.suggestions[0].source_title == "Draft the outline"
    assert result.suggestions[0].target_title == "Review the draft"
    assert result.suggestions[0].kind == enrichment.DEPENDENCY


def test_next_action_and_dependency_kinds_are_emittable(tmp_path: object) -> None:
    _seed(tmp_path)
    next_result = enrichment.generate(
        str(tmp_path), "demo", enrichment.ENRICH_NEXT_ACTIONS,
        llm=_stub({"suggestions": [
            {"kind": "NEXT_ACTION", "title": "Inventory sections"}]}),
        engine="local")  # type: ignore[arg-type]
    assert next_result.suggestions[0].kind == enrichment.NEXT_ACTION
    dep_result = enrichment.generate(
        str(tmp_path), "demo", enrichment.ENRICH_DEPENDENCIES,
        llm=_stub({"suggestions": [
            {"kind": "DEPENDENCY", "title": "dep",
             "source_title": "Draft the outline",
             "target_title": "Review the draft"}]}),
        engine="local")  # type: ignore[arg-type]
    assert dep_result.suggestions[0].kind == enrichment.DEPENDENCY
    # The real schema enum advertises both enrichment-only kinds.
    enum = enrichment._SUGGESTION_SCHEMA["properties"]["suggestions"]["items"][
        "properties"]["kind"]["enum"]
    assert enrichment.NEXT_ACTION in enum
    assert enrichment.DEPENDENCY in enum
    assert "PROJECT" not in enum


# --- G — nothing is auto-accepted --------------------------------------------


def test_enrichment_never_mutates_portfolio(tmp_path: object) -> None:
    _seed(tmp_path)
    before = store.load_portfolio(str(tmp_path))
    enrichment.generate(
        str(tmp_path), "demo", enrichment.ENRICH_NEXT_ACTIONS,
        llm=_stub({"suggestions": [
            {"kind": "NEXT_ACTION", "title": "Inventory sections"}]}),
        engine="local")  # type: ignore[arg-type]
    assert store.load_portfolio(str(tmp_path)) == before


# --- H — selected engine failure still fails closed ---------------------------


def test_selected_engine_failure_fails_closed(
        monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    _seed(tmp_path)
    _clean_env(monkeypatch)
    monkeypatch.setenv("TRAJECTORY_MVP_IMPORT_OFFLINE", "1")
    with pytest.raises(enrichment.EnrichmentError):
        enrichment.generate(
            str(tmp_path), "demo", enrichment.ENRICH_WBS, engine="local")

    monkeypatch.delenv("TRAJECTORY_MVP_IMPORT_OFFLINE", raising=False)
    with pytest.raises(enrichment.EnrichmentError):
        enrichment.generate(
            str(tmp_path), "demo", enrichment.ENRICH_WBS,
            engine="deepseek-flash")


def test_selected_engine_transport_failure_fails_closed(
        monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    _seed(tmp_path)
    _clean_env(monkeypatch)

    def fake_urlopen(req: Any, timeout: float | None = None) -> _Resp:
        raise OSError("connection refused")

    monkeypatch.setattr(importer.request, "urlopen", fake_urlopen)
    with pytest.raises(enrichment.EnrichmentError):
        enrichment.generate(
            str(tmp_path), "demo", enrichment.ENRICH_WBS, engine="local")


# --- I — usage metadata preserved ---------------------------------------------


def test_usage_and_engine_metadata_preserved(
        monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    _seed(tmp_path)
    _clean_env(monkeypatch)

    def fake_urlopen(req: Any, timeout: float | None = None) -> _Resp:
        del req, timeout
        return _Resp(_local_response([
            {"kind": "NEXT_ACTION", "title": "Inventory sections"}]))

    monkeypatch.setattr(importer.request, "urlopen", fake_urlopen)
    result = enrichment.generate(
        str(tmp_path), "demo", enrichment.ENRICH_NEXT_ACTIONS, engine="local")
    assert result.input_tokens == 11
    assert result.output_tokens == 4
    payload = intel_model.read_json(
        enrichment._store_path(str(tmp_path), "demo"))
    assert payload is not None
    assert payload["engine"] == "local"
    assert payload["model"] == "qwen3.8:27b-q4_K_M"
    assert payload["input_tokens"] == 11
    assert payload["output_tokens"] == 4


# --- P2 side effects (localized) ---------------------------------------------


def test_generate_preserves_pending_other_kinds(tmp_path: object) -> None:
    _seed(tmp_path)
    enrichment.generate(
        str(tmp_path), "demo", enrichment.ENRICH_NEXT_ACTIONS,
        llm=_stub({"suggestions": [
            {"kind": "NEXT_ACTION", "title": "Inventory sections"}]}),
        engine="local")  # type: ignore[arg-type]
    enrichment.generate(
        str(tmp_path), "demo", enrichment.ENRICH_WBS,
        llm=_stub({"suggestions": [
            {"kind": "TASK", "title": "Write the overview"}]}),
        engine="local")  # type: ignore[arg-type]
    stored = enrichment.load_suggestions(str(tmp_path), "demo")
    assert sorted(s.kind for s in stored) == ["NEXT_ACTION", "TASK"]
    assert all(s.state == enrichment.PENDING for s in stored)


def test_accept_preserves_engine_model_metadata(tmp_path: object) -> None:
    _seed(tmp_path)
    result = enrichment.generate(
        str(tmp_path), "demo", enrichment.ENRICH_NEXT_ACTIONS,
        llm=_stub({"suggestions": [
            {"kind": "NEXT_ACTION", "title": "Inventory sections"}]}),
        engine="local")  # type: ignore[arg-type]
    enrichment.accept(
        str(tmp_path), "demo", result.suggestions[0].suggestion_id)
    payload = intel_model.read_json(
        enrichment._store_path(str(tmp_path), "demo"))
    assert payload is not None
    assert payload["engine"] == "local"
    assert payload["model"] == "qwen3.8:27b-q4_K_M"
    assert "generated_at" in payload
    assert "reviewed_at" in payload


def test_reject_preserves_engine_model_metadata(tmp_path: object) -> None:
    _seed(tmp_path)
    result = enrichment.generate(
        str(tmp_path), "demo", enrichment.ENRICH_NEXT_ACTIONS,
        llm=_stub({"suggestions": [
            {"kind": "NEXT_ACTION", "title": "Inventory sections"}]}),
        engine="local")  # type: ignore[arg-type]
    enrichment.reject(
        str(tmp_path), "demo", result.suggestions[0].suggestion_id)
    payload = intel_model.read_json(
        enrichment._store_path(str(tmp_path), "demo"))
    assert payload is not None
    assert payload["engine"] == "local"
    assert payload["model"] == "qwen3.8:27b-q4_K_M"
    assert "reviewed_at" in payload
