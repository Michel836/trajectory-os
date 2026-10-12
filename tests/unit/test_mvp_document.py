"""Unit tests for MVP document text extraction (stdlib only)."""

from __future__ import annotations

import io
import zipfile

import pytest

from trajectory_os.mvp import document


def _odt_bytes() -> bytes:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content '
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
        "<office:body><office:text>"
        '<text:h text:outline-level="1">Project A</text:h>'
        "<text:p>First task</text:p>"
        "<text:list><text:list-item><text:p>Bullet one</text:p></text:list-item>"
        "</text:list>"
        "</office:text></office:body></office:document-content>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("content.xml", xml)
    return buffer.getvalue()


def _docx_bytes() -> bytes:
    xml = (
        '<?xml version="1.0"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body>'
        "<w:p><w:pPr><w:pStyle w:val=\"Heading1\"/></w:pPr>"
        "<w:r><w:t>Title</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Body text</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


def test_extract_odt_preserves_structure() -> None:
    text = document.extract_text("notes.odt", _odt_bytes())
    assert "# Project A" in text
    assert "First task" in text
    assert "- Bullet one" in text


def test_extract_docx_preserves_headings() -> None:
    text = document.extract_text("notes.docx", _docx_bytes())
    assert "# Title" in text
    assert "Body text" in text


def test_extract_plain_text_formats() -> None:
    assert document.extract_text("a.txt", b"hello\nworld\n") == \
        "hello\nworld\n"
    assert "world" in document.extract_text("a.md", b"# hi\nworld")


def test_unsupported_format_fails_closed() -> None:
    with pytest.raises(document.DocumentError):
        document.extract_text("a.xlsx", b"data")


def test_malformed_odt_fails_closed() -> None:
    with pytest.raises(document.DocumentError):
        document.extract_text("bad.odt", b"not a zip")


def test_pdf_uses_pdftotext(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(args: object, **kwargs: object) -> object:
        captured["args"] = args
        captured["input"] = kwargs.get("input")
        return _FakeResult(b"Hello PDF\n")

    monkeypatch.setattr(document.subprocess, "run", fake_run)
    text = document.extract_text("a.pdf", b"%PDF")
    assert text == "Hello PDF\n"
    assert captured["args"] == ["pdftotext", "-layout", "-", "-"]


class _FakeResult:
    def __init__(self, stdout: bytes) -> None:
        self.stdout = stdout
        self.stderr = b""
        self.returncode = 0
