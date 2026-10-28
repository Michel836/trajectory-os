"""MVP — opt-in, read-only IMAP ingestion (no send/delete/move).

The network client is deliberately isolated behind :class:`MessageFetcher`.
Tests use a fake fetcher and never open a socket. The real fetcher requires:

* an explicit configuration with a non-empty sender allowlist;
* credentials supplied through an environment variable (never persisted);
* ``readonly=True`` on SELECT;
* a bounded number of recent messages.

There is no background daemon, no auto-sync and no write-back to the mailbox.
"""

from __future__ import annotations

import contextlib
import email
import imaplib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from email import policy
from pathlib import Path
from typing import Any, Protocol

from trajectory_os.mvp import model, signals, sources

MAX_IMAP_MESSAGES = 200
DEFAULT_IMAP_PORT = 993


class ImapError(Exception):
    """An IMAP configuration or fetch failed."""


@dataclass(frozen=True)
class ImapConfig:
    host: str
    username: str
    password_env: str
    allow_senders: tuple[str, ...] = ()
    mailbox: str = "INBOX"
    port: int = DEFAULT_IMAP_PORT
    max_messages: int = 50
    sensitivity: str = "INTERNAL"

    def validate(self) -> ImapConfig:
        if not self.host or not self.username or not self.password_env:
            raise ImapError("host, username and password_env are required")
        if not self.allow_senders:
            raise ImapError("an explicit sender allowlist is required")
        if not 1 <= self.max_messages <= MAX_IMAP_MESSAGES:
            raise ImapError("max_messages out of bounds")
        if self.sensitivity not in (
                "PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"):
            raise ImapError("invalid sensitivity")
        return self

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> ImapConfig:
        raw_senders = data.get("allow_senders") or []
        senders = tuple(str(item) for item in raw_senders
                        if isinstance(item, str) and item)
        return ImapConfig(
            host=str(data.get("host", "")),
            username=str(data.get("username", "")),
            password_env=str(data.get("password_env", "")),
            allow_senders=senders,
            mailbox=str(data.get("mailbox", "INBOX")),
            port=int(data.get("port", DEFAULT_IMAP_PORT)),
            max_messages=int(data.get("max_messages", 50)),
            sensitivity=str(data.get("sensitivity", "INTERNAL")),
        ).validate()

    @staticmethod
    def load(path: str | Path) -> ImapConfig:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ImapError("IMAP config must be a JSON object")
        return ImapConfig.from_dict(document)


class MessageFetcher(Protocol):
    """Minimal read-only fetch contract (fakeable in tests)."""

    def fetch(self, mailbox: str, limit: int) -> Sequence[tuple[str, bytes]]:
        ...  # pragma: no cover


class ImapReadOnlyFetcher:
    """Real ``imaplib`` adapter; never sends, deletes or moves messages."""

    def __init__(self, config: ImapConfig) -> None:
        self.config = config.validate()

    def fetch(self, mailbox: str, limit: int) -> Sequence[tuple[str, bytes]]:
        password = os.environ.get(self.config.password_env)
        if not password:
            raise ImapError("IMAP credentials are unavailable")
        client = imaplib.IMAP4_SSL(self.config.host, self.config.port)
        try:
            client.login(self.config.username, password)
            status, _ = client.select(mailbox, readonly=True)
            if status != "OK":
                raise ImapError("cannot open mailbox read-only")
            status, data = client.search(None, "ALL")
            if status != "OK" or not data:
                raise ImapError("mailbox search failed")
            uids = data[0].split()[-max(1, min(limit,
                                               MAX_IMAP_MESSAGES)):]
            messages: list[tuple[str, bytes]] = []
            for uid in uids:
                status, parts = client.fetch(uid, "(RFC822)")
                if status != "OK":
                    continue
                for part in parts:
                    if isinstance(part, tuple) and isinstance(part[1], bytes):
                        messages.append((uid.decode("ascii", "replace"),
                                         part[1]))
            return messages
        finally:
            with contextlib.suppress(Exception):
                client.logout()


def ingest_imap(root: str | Path, config: ImapConfig,
                fetcher: MessageFetcher) -> dict[str, Any]:
    """Fetch recent messages and feed them through the signal pipeline."""
    config.validate()
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for uid, raw in fetcher.fetch(config.mailbox, config.max_messages):
        try:
            message = email.message_from_bytes(raw, policy=policy.default)
            sender = str(message.get("From", "")).strip()
            if not any(allowed.lower() in sender.lower()
                       for allowed in config.allow_senders):
                skipped.append({"uid": uid, "reason": "sender not allowed"})
                continue
            if signals.source_rule(root, sender)["mute"]:
                skipped.append({"uid": uid, "reason": "sender muted"})
                continue
            result = sources.ingest_email_message(
                root, message, f"imap-{uid}", config.sensitivity)
            results.append({"uid": uid, **result})
        except (sources.SourceError, model.MvpError, ValueError) as exc:
            errors.append({"uid": uid, "error": str(exc)})
    return {
        "count": len(results),
        "results": results,
        "skipped": skipped,
        "errors": errors,
        "readonly": True,
    }


__all__ = [
    "DEFAULT_IMAP_PORT", "ImapConfig", "ImapError", "ImapReadOnlyFetcher",
    "MAX_IMAP_MESSAGES", "MessageFetcher", "ingest_imap",
]
