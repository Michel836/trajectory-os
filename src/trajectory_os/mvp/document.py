"""MVP — dependency-free document text extraction for portfolio import.

Supported formats (all extracted with the standard library or an installed
system tool, never via a private service):

* ``.odt`` — OpenDocument text (ZIP + ``content.xml``);
* ``.docx`` — WordprocessingML (ZIP + ``word/document.xml``);
* ``.pdf`` — extracted via ``pdftotext`` when available on PATH;
* ``.md`` / ``.markdown`` / ``.txt`` / ``.text`` — plain UTF-8 text.

The extractor returns plain text with lightweight structure hints (headings
are prefixed with ``#``, list items with ``- ``) so the downstream semantic
analysis keeps document context. It never interprets content — that belongs to
:mod:`trajectory_os.mvp.importer`.
"""

from __future__ import annotations

import io
import subprocess
import zipfile
from xml.etree import ElementTree as ET

#: Formats accepted by the import workflow (lowercase suffixes).
SUPPORTED_FORMATS = frozenset({
    ".odt", ".docx", ".pdf", ".md", ".markdown", ".txt", ".text",
})

_ODT_NS = {
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
}

_DOCX_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
}


class DocumentError(Exception):
    """A document cannot be read or is not a supported import format."""


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _suffix(filename: str) -> str:
    lowered = filename.lower()
    for suffix in (".markdown", ".text"):
        if lowered.endswith(suffix):
            return suffix
    return lowered[lowered.rfind("."):] if "." in lowered else ""


def extract_text(filename: str, data: bytes) -> str:
    """Extract plain text from one document; raise :class:`DocumentError`."""
    suffix = _suffix(filename)
    if suffix not in SUPPORTED_FORMATS:
        raise DocumentError(
            f"unsupported document format {suffix!r} "
            f"(supported: {', '.join(sorted(SUPPORTED_FORMATS))})")
    if suffix == ".odt":
        return _extract_odt(data)
    if suffix == ".docx":
        return _extract_docx(data)
    if suffix == ".pdf":
        return _extract_pdf(data)
    return _decode_text(data)


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# --- ODT ----------------------------------------------------------------------


def _collect_text(element: ET.Element) -> str:
    parts: list[str] = []
    _collect(element, parts)
    return "".join(parts).strip()


def _collect(element: ET.Element, parts: list[str]) -> None:
    if element.text:
        parts.append(element.text)
    for child in element:
        tag = _localname(child.tag)
        if tag == "s":
            count = int(child.get(f"{{{_ODT_NS['text']}}}c", "1") or "1")
            parts.append(" " * max(1, count))
        elif tag == "tab":
            parts.append("\t")
        elif tag == "line-break":
            parts.append("\n")
        else:
            _collect(child, parts)
        if child.tail:
            parts.append(child.tail)


def _extract_odt(data: bytes) -> str:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise DocumentError(f"ODT is not a valid zip archive: {exc}") from exc
    try:
        xml = archive.read("content.xml")
    except KeyError as exc:
        raise DocumentError("ODT is missing content.xml") from exc
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise DocumentError(f"ODT content.xml is malformed: {exc}") from exc

    body = root.find(".//office:body", _ODT_NS)
    if body is None:
        return ""

    lines: list[str] = []
    _walk_odt(body, lines, in_list=False)
    return "\n".join(lines).strip() + "\n"


def _walk_odt(element: ET.Element, lines: list[str], *, in_list: bool) -> None:
    tag = _localname(element.tag)
    if tag == "list":
        in_list = True
    elif tag == "h":
        level = _odt_heading_level(element)
        lines.append("\n" + "#" * level + " " + _collect_text(element))
        return
    elif tag == "p":
        text = _collect_text(element)
        if text:
            lines.append(("- " if in_list else "") + text)
        return
    for child in element:
        _walk_odt(child, lines, in_list=in_list)


def _odt_heading_level(element: ET.Element) -> int:
    value = element.get(f"{{{_ODT_NS['text']}}}outline-level", "")
    if value.isdigit():
        return max(1, min(6, int(value)))
    return 1


# --- DOCX ---------------------------------------------------------------------


def _extract_docx(data: bytes) -> str:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise DocumentError(f"DOCX is not a valid zip archive: {exc}") from exc
    try:
        xml = archive.read("word/document.xml")
    except KeyError as exc:
        raise DocumentError("DOCX is missing word/document.xml") from exc
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise DocumentError(f"DOCX document.xml is malformed: {exc}") from exc

    lines: list[str] = []
    for paragraph in root.iter(f"{{{_DOCX_NS['w']}}}p"):
        level = _docx_heading_level(paragraph)
        text = "".join(node.text or ""
                       for node in paragraph.iter(f"{{{_DOCX_NS['w']}}}t"))
        text = text.strip()
        if not text:
            continue
        if level:
            lines.append("\n" + "#" * level + " " + text)
        else:
            lines.append(text)
    return "\n".join(lines).strip() + "\n"


def _docx_heading_level(paragraph: ET.Element) -> int:
    style = paragraph.find(f"./{{{_DOCX_NS['w']}}}pPr/"
                           f"{{{_DOCX_NS['w']}}}pStyle")
    if style is None:
        return 0
    value = style.get(f"{{{_DOCX_NS['w']}}}val", "")
    if not value.startswith("Heading"):
        return 0
    digits = value[len("Heading"):]
    return int(digits) if digits.isdigit() else 1


# --- PDF ----------------------------------------------------------------------


def _extract_pdf(data: bytes) -> str:
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", "-", "-"],
            input=data, capture_output=True, timeout=120, check=False,
        )
    except FileNotFoundError as exc:
        raise DocumentError(
            "PDF extraction requires the 'pdftotext' command (poppler-utils) "
            "on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise DocumentError("PDF extraction timed out") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise DocumentError(f"PDF extraction failed: {detail}")
    return result.stdout.decode("utf-8", "replace").strip() + "\n"


__all__ = ["SUPPORTED_FORMATS", "DocumentError", "extract_text"]
