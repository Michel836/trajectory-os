"""MVP — inbound source ingestion: text, email files, documents and versions.

This module is the bridge between real inputs and :mod:`trajectory_os.mvp.signals`:

* ``.eml`` and ``.mbox`` are parsed with the standard library, never a network
  client;
* ODT/DOCX/PDF/MD/TXT use the existing ``mvp.document`` extractor;
* every source is recorded in ``inbox/sources.jsonl`` with a hash, version,
  status (NEW/DUPLICATE/CHANGED), sensitivity, thread key and bounded diff;
* secret-shaped assignments are redacted before persistence and ``RESTRICTED``
  content is never stored;
* no message is sent, deleted or moved.
"""

from __future__ import annotations

import difflib
import email
import html
import mailbox
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email import policy
from email.message import Message
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from trajectory_os.intelligence import knowledge
from trajectory_os.intelligence import model as intel_model
from trajectory_os.mvp import document, model, signals
from trajectory_os.realworld import model as realworld_model

SOURCE_SCHEMA_VERSION = 1
SOURCES_NAME = "sources.jsonl"

ORIGIN_TEXT = "TEXT"
ORIGIN_FILE = "FILE"
ORIGIN_EMAIL = "EMAIL"
ORIGIN_DOCUMENT = "DOCUMENT"
ORIGINS = frozenset({ORIGIN_TEXT, ORIGIN_FILE, ORIGIN_EMAIL, ORIGIN_DOCUMENT})

STATUS_NEW = "NEW"
STATUS_DUPLICATE = "DUPLICATE"
STATUS_CHANGED = "CHANGED"
STATUSES = frozenset({STATUS_NEW, STATUS_DUPLICATE, STATUS_CHANGED})

LIM_ATTACHMENTS = "ATTACHMENTS_LISTED_NOT_EXTRACTED"
LIM_REDACTED = "SECRET_SHAPED_CONTENT_REDACTED"
LIM_MBOX_BOUNDED = "MBOX_MESSAGE_COUNT_BOUNDED"
LIM_EMPTY_BODY = "EMAIL_BODY_EMPTY"
LIM_RESTRICTED = "RESTRICTED_CONTENT_NOT_PERSISTED"

MAX_SOURCE_TEXT = 20_000
MAX_DIFF_LINES = 200
MAX_MBOX_MESSAGES = 200
MAX_SOURCES = 2_000

_QUOTED_RE = re.compile(
    r"\n\s*(?:On .*wrote:|-{2,}\s*Original Message\s*-{2,}|"
    r"De\s*:\s|From\s*:\s|Le .* a écrit\s*:)", re.IGNORECASE)


class SourceError(model.MvpError):
    """An inbound source cannot be ingested (fail closed)."""


@dataclass(frozen=True)
class SourceRecord:
    source_id: str
    origin: str
    location: str
    title: str
    text: str
    content_hash: str
    received_at: str
    sensitivity: str = knowledge.SENS_INTERNAL
    project_hint: str = ""
    sender: str = ""
    thread_key: str = ""
    version: int = 1
    previous_hash: str = ""
    changed_from: str = ""
    status: str = STATUS_NEW
    limitations: tuple[str, ...] = ()
    diff: tuple[str, ...] = ()
    schema_version: int = SOURCE_SCHEMA_VERSION

    def validate(self) -> SourceRecord:
        if self.schema_version != SOURCE_SCHEMA_VERSION:
            raise SourceError(model.E_UNSUPPORTED_VERSION,
                              str(self.schema_version))
        if self.origin not in ORIGINS:
            raise SourceError(model.E_MALFORMED, f"origin {self.origin!r}")
        if self.status not in STATUSES:
            raise SourceError(model.E_MALFORMED, f"status {self.status!r}")
        if self.sensitivity not in knowledge.SENSITIVITIES:
            raise SourceError(model.E_MALFORMED, "sensitivity")
        if not self.source_id or not self.location or not self.text:
            raise SourceError(model.E_MALFORMED, "identity/text required")
        if len(self.text) > MAX_SOURCE_TEXT:
            raise SourceError(model.E_MALFORMED, "source text exceeds bound")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "origin": self.origin,
            "location": self.location,
            "title": self.title,
            "text": self.text,
            "content_hash": self.content_hash,
            "received_at": self.received_at,
            "sensitivity": self.sensitivity,
            "project_hint": self.project_hint,
            "sender": self.sender,
            "thread_key": self.thread_key,
            "version": self.version,
            "previous_hash": self.previous_hash,
            "changed_from": self.changed_from,
            "status": self.status,
            "limitations": list(self.limitations),
            "diff": list(self.diff),
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> SourceRecord:
        raw_limits = data.get("limitations")
        raw_diff = data.get("diff")
        return SourceRecord(
            source_id=str(data.get("source_id", "")),
            origin=str(data.get("origin", ORIGIN_TEXT)),
            location=str(data.get("location", "")),
            title=str(data.get("title", "")),
            text=str(data.get("text", "")),
            content_hash=str(data.get("content_hash", "")),
            received_at=str(data.get("received_at", "")),
            sensitivity=str(data.get("sensitivity",
                                     knowledge.SENS_INTERNAL)),
            project_hint=str(data.get("project_hint", "")),
            sender=str(data.get("sender", "")),
            thread_key=str(data.get("thread_key", "")),
            version=int(data.get("version", 1)),
            previous_hash=str(data.get("previous_hash", "")),
            changed_from=str(data.get("changed_from", "")),
            status=str(data.get("status", STATUS_NEW)),
            limitations=tuple(str(item) for item in raw_limits
                              if isinstance(item, str))
            if isinstance(raw_limits, (list, tuple)) else (),
            diff=tuple(str(item) for item in raw_diff
                       if isinstance(item, str))
            if isinstance(raw_diff, (list, tuple)) else (),
            schema_version=int(data.get("schema_version",
                                       SOURCE_SCHEMA_VERSION)),
        ).validate()


def sources_path(root: str | Path) -> Path:
    return Path(root) / signals.INBOX_DIR / SOURCES_NAME


def read_sources(root: str | Path, limit: int = 200
                 ) -> tuple[SourceRecord, ...]:
    records: list[SourceRecord] = []
    for document_data in intel_model.read_jsonl(sources_path(root)):
        try:
            records.append(SourceRecord.from_dict(document_data))
        except (SourceError, ValueError):
            continue
    records.reverse()
    return tuple(records[:max(1, min(limit, MAX_SOURCES))])


def latest_source_by_location(root: str | Path,
                              location: str) -> SourceRecord | None:
    for record in read_sources(root, limit=MAX_SOURCES):
        if record.location == location:
            return record
    return None


def _append_source(root: str | Path, record: SourceRecord) -> None:
    intel_model.append_jsonl(sources_path(root), record.to_dict())


def _hash(text: str) -> str:
    return knowledge.hash_content(text)


def _source_id(origin: str, location: str, content_hash: str) -> str:
    return intel_model.digest(
        {"origin": origin, "location": location,
         "content_hash": content_hash},
        domain="trajectory-os.mvp.source.v1")[:32]


def diff_texts(old: str, new: str,
               max_lines: int = MAX_DIFF_LINES) -> tuple[str, ...]:
    lines = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(), fromfile="avant",
        tofile="après", lineterm="", n=2))
    return tuple(lines[:max(1, max_lines)])


class _HtmlText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]
                        ) -> None:
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        elif tag in ("p", "div", "li", "tr"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


def _strip_html(value: str) -> str:
    parser = _HtmlText()
    parser.feed(html.unescape(value))
    return "".join(parser.parts)


def _clean_body(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    quoted = _QUOTED_RE.search(text)
    if quoted is not None:
        text = text[:quoted.start()]
    text = re.sub(r"\n--\s*\n.*$", "", text, flags=re.DOTALL)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _payload_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")
    raw = part.get_payload()
    return raw if isinstance(raw, str) else ""


def _message_text(message: Message) -> str:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain" \
                    and part.get_content_disposition() != "attachment":
                return _clean_body(_payload_text(part))
        for part in message.walk():
            if part.get_content_type() == "text/html" \
                    and part.get_content_disposition() != "attachment":
                return _clean_body(_strip_html(_payload_text(part)))
        return ""
    return _clean_body(_payload_text(message))


def _subject(message: Message) -> str:
    return str(message.get("Subject", "")).strip()


def _thread_key(message: Message, subject: str) -> str:
    references = str(message.get("References", "")).strip()
    if references:
        parts = references.split()
        if parts:
            return parts[-1].strip("<>")
    reply_to = str(message.get("In-Reply-To", "")).strip()
    if reply_to:
        return reply_to.strip("<>")
    return re.sub(r"^(?:(?:re|fw|fwd)\s*:\s*)+", "", subject,
                  flags=re.IGNORECASE).strip()


def _received_at(message: Message) -> str:
    raw = message.get("Date")
    if raw:
        try:
            parsed = parsedate_to_datetime(str(raw))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC).replace(
                microsecond=0).isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError, OverflowError):
            pass
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z")


def _attachments(message: Message) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not message.is_multipart():
        return items
    for part in message.walk():
        if part.get_content_disposition() != "attachment":
            continue
        payload = part.get_payload(decode=True)
        items.append({
            "filename": part.get_filename() or "",
            "content_type": part.get_content_type(),
            "size": len(payload) if isinstance(payload, bytes) else 0,
        })
    return items


def create_source(
    root: str | Path,
    *,
    origin: str,
    location: str,
    title: str,
    text: str,
    source_kind: str,
    source_ref: str = "",
    received_at: str = "",
    sensitivity: str = knowledge.SENS_INTERNAL,
    project_hint: str = "",
    sender: str = "",
    thread_key: str = "",
    limitations: Sequence[str] = (),
) -> dict[str, Any]:
    """Persist a source, create its signal and derive proposals."""
    if origin not in ORIGINS:
        raise SourceError(model.E_MALFORMED, f"origin {origin!r}")
    if sensitivity not in knowledge.SENSITIVITIES:
        raise SourceError(model.E_MALFORMED, "sensitivity")
    if sensitivity == knowledge.SENS_RESTRICTED:
        raise SourceError(model.E_MALFORMED, LIM_RESTRICTED)
    cleaned = realworld_model.redact_secrets(text).strip()
    if not cleaned:
        raise SourceError(model.E_MALFORMED, "source text is empty")
    if len(cleaned) > MAX_SOURCE_TEXT:
        cleaned = cleaned[:MAX_SOURCE_TEXT]
        limitations = tuple(limitations) + ("CONTENT_TRUNCATED",)
    if cleaned != text:
        limitations = tuple(limitations) + (LIM_REDACTED,)
    ref = source_ref or location
    if signals.is_source_muted(root, ref) or signals.source_rule(
            root, ref)["mute"]:
        raise SourceError(model.E_MALFORMED, "source is muted")
    content_hash = _hash(cleaned)
    prior = latest_source_by_location(root, location)
    if prior is not None and prior.content_hash == content_hash:
        bundle = signals.create_signal(
            root, text=cleaned, source_kind=source_kind,
            source_ref=source_ref or location, title=title,
            project_hint=project_hint, sensitivity=sensitivity)
        return {
            "source": prior.to_dict(),
            "signal": bundle["signal"],
            "proposals": bundle["proposals"],
            "duplicate": True,
            "version": prior.version,
            "diff": list(prior.diff),
        }
    version = prior.version + 1 if prior is not None else 1
    changed_from = prior.source_id if prior is not None else ""
    diff = diff_texts(prior.text, cleaned) if prior is not None else ()
    status = STATUS_CHANGED if prior is not None else STATUS_NEW
    stamp = received_at or datetime.now(UTC).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")
    record = SourceRecord(
        source_id=_source_id(origin, location, content_hash),
        origin=origin, location=location, title=title[:200],
        text=cleaned, content_hash=content_hash, received_at=stamp,
        sensitivity=sensitivity, project_hint=project_hint,
        sender=sender, thread_key=thread_key, version=version,
        previous_hash=prior.content_hash if prior is not None else "",
        changed_from=changed_from, status=status,
        limitations=tuple(dict.fromkeys(limitations)),
        diff=diff).validate()
    _append_source(root, record)
    bundle = signals.create_signal(
        root, text=cleaned, source_kind=source_kind,
        source_ref=source_ref or location, title=title,
        project_hint=project_hint, sensitivity=sensitivity)
    return {
        "source": record.to_dict(),
        "signal": bundle["signal"],
        "proposals": bundle["proposals"],
        "duplicate": bool(bundle.get("duplicate", False)),
        "version": version,
        "diff": list(diff),
    }


def ingest_text(root: str | Path, *, text: str,
                source_ref: str = "manual", title: str = "",
                project_hint: str = "",
                sensitivity: str = knowledge.SENS_INTERNAL
                ) -> dict[str, Any]:
    return create_source(
        root, origin=ORIGIN_TEXT, location=source_ref,
        title=title or text.strip().splitlines()[0][:200],
        text=text, source_kind=signals.SOURCE_TEXT,
        source_ref=source_ref, sensitivity=sensitivity,
        project_hint=project_hint)


def ingest_document_bytes(root: str | Path, filename: str, data: bytes,
                          *, source_ref: str = "",
                          project_hint: str = "",
                          sensitivity: str = knowledge.SENS_INTERNAL
                          ) -> dict[str, Any]:
    text = document.extract_text(filename, data)
    return create_source(
        root, origin=ORIGIN_DOCUMENT, location=filename,
        title=filename, text=text,
        source_kind=signals.SOURCE_FILE,
        source_ref=source_ref or filename, sensitivity=sensitivity,
        project_hint=project_hint)


def ingest_email_message(root: str | Path, message: Message,
                          fallback_ref: str,
                          sensitivity: str = knowledge.SENS_INTERNAL
                          ) -> dict[str, Any]:
    subject = _subject(message)
    body = _message_text(message)
    attachments = _attachments(message)
    sender = str(message.get("From", "")).strip()
    rule = signals.source_rule(root, sender)
    if rule["mute"]:
        raise SourceError(model.E_MALFORMED, "source is muted")
    sensitivity = str(rule["sensitivity"] or sensitivity)
    project_hint = str(rule["project_id"] or "")
    text = (subject + "\n\n" + body).strip()
    if not text:
        text = subject or fallback_ref
    message_id = str(message.get("Message-ID", "")).strip().strip("<>")
    source_ref = message_id or fallback_ref
    limitations = [LIM_EMPTY_BODY] if not body else []
    if attachments:
        limitations.append(LIM_ATTACHMENTS)
    result = create_source(
        root, origin=ORIGIN_EMAIL, location=source_ref,
        title=subject or fallback_ref, text=text,
        source_kind=signals.SOURCE_EMAIL_TEXT, source_ref=source_ref,
        received_at=_received_at(message),
        sensitivity=sensitivity, project_hint=project_hint,
        sender=sender, thread_key=_thread_key(message, subject),
        limitations=limitations)
    result["attachments"] = attachments
    return result


def ingest_eml_bytes(root: str | Path, filename: str, data: bytes,
                     *, sensitivity: str = knowledge.SENS_INTERNAL
                     ) -> dict[str, Any]:
    message = email.message_from_bytes(data, policy=policy.default)
    return ingest_email_message(root, message, filename, sensitivity)


def ingest_mbox_bytes(root: str | Path, filename: str, data: bytes,
                      *, sensitivity: str = knowledge.SENS_INTERNAL
                      ) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    with tempfile.NamedTemporaryFile(suffix=".mbox", delete=False) as handle:
        handle.write(data)
        path = handle.name
    try:
        box = mailbox.mbox(path, create=False)
        try:
            for index, (_key, message) in enumerate(box.iteritems()):
                if index >= MAX_MBOX_MESSAGES:
                    break
                try:
                    parsed = email.message_from_bytes(
                        message.as_bytes(), policy=policy.default)
                    results.append(ingest_email_message(
                        root, parsed, f"{filename}#{index + 1}",
                        sensitivity))
                except (SourceError, ValueError, KeyError) as exc:
                    errors.append(f"message {index + 1}: {exc}")
        finally:
            box.close()
    finally:
        Path(path).unlink(missing_ok=True)
    return {
        "count": len(results),
        "results": results,
        "errors": errors,
        "bounded": len(results) >= MAX_MBOX_MESSAGES,
        "limitations": [LIM_MBOX_BOUNDED]
        if len(results) >= MAX_MBOX_MESSAGES else [],
    }


def list_sources(root: str | Path, limit: int = 100,
                 ) -> list[dict[str, Any]]:
    return [record.to_dict() for record in read_sources(root, limit=limit)]


__all__ = [
    "LIM_ATTACHMENTS", "LIM_EMPTY_BODY", "LIM_MBOX_BOUNDED",
    "LIM_REDACTED", "LIM_RESTRICTED", "MAX_MBOX_MESSAGES", "ORIGINS",
    "ORIGIN_DOCUMENT", "ORIGIN_EMAIL", "ORIGIN_FILE", "ORIGIN_TEXT",
    "SOURCES_NAME", "STATUSES", "STATUS_CHANGED", "STATUS_DUPLICATE",
    "STATUS_NEW", "SourceError", "SourceRecord", "create_source",
    "diff_texts", "ingest_document_bytes", "ingest_eml_bytes",
    "ingest_mbox_bytes", "ingest_text", "latest_source_by_location",
    "list_sources", "read_sources", "sources_path",
]
