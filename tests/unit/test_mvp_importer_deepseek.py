"""Tests for the DeepSeek Flash import provider and explicit engine routing."""

from __future__ import annotations

import json

import pytest

from trajectory_os.mvp import import_engine, importer


def test_deepseek_llm_unconfigured_without_key(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    llm = importer.DeepSeekLlm()
    assert llm.configured is False
    with pytest.raises(importer.PortfolioImportError):
        llm.complete_json("sys", "user")


def test_deepseek_llm_parses_openai_response(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    def fake_urlopen(req, timeout):  # noqa: ANN001
        body = json.dumps({
            "choices": [{"message": {"content": json.dumps({
                "items": [{"kind": "TASK", "title": "Do it"}]})}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        }).encode("utf-8")

        class _Resp:
            def read(self):  # noqa: ANN201
                return body

            def __enter__(self):  # noqa: ANN201
                return self

            def __exit__(self, *a):  # noqa: ANN201
                return False

        return _Resp()

    monkeypatch.setattr(importer.request, "urlopen", fake_urlopen)
    llm = importer.DeepSeekLlm()
    result = llm.complete_json("sys", "user")
    assert result["items"] == [{"kind": "TASK", "title": "Do it"}]
    assert result["_usage"] == {"input_tokens": 12, "output_tokens": 3}


def test_deepseek_pro_model_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    with pytest.raises(importer.PortfolioImportError):
        importer.DeepSeekLlm(model="deepseek-pro")
    with pytest.raises(importer.PortfolioImportError):
        importer.DeepSeekLlm(model="deepseek-reasoner")


def test_make_import_llm_defaults_to_local(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("TRAJECTORY_MVP_IMPORT_OFFLINE", raising=False)
    fn = importer.make_import_llm()
    assert fn is not None
    assert getattr(fn, "__self__", None).__class__.__name__ == "LocalLlm"


def test_make_import_llm_never_silently_selects_deepseek(monkeypatch) -> None:
    # A configured DeepSeek key must NOT be picked without an explicit choice.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.delenv("TRAJECTORY_MVP_IMPORT_OFFLINE", raising=False)
    fn = importer.make_import_llm()
    assert getattr(fn, "__self__", None).__class__.__name__ == "LocalLlm"


def test_make_import_llm_explicit_deepseek(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    fn = importer.make_import_llm(import_engine.ENGINE_DEEPSEEK_FLASH)
    assert getattr(fn, "__self__", None).__class__.__name__ == "DeepSeekLlm"


def test_make_import_llm_deepseek_without_key_fails(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(importer.PortfolioImportError):
        importer.make_import_llm(import_engine.ENGINE_DEEPSEEK_FLASH)


def test_make_import_llm_offline_local_returns_none(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("TRAJECTORY_MVP_IMPORT_OFFLINE", "1")
    assert importer.make_import_llm() is None
