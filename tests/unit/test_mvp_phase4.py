"""Phase 4 — email/document sources, versions, privacy and source rules."""

from __future__ import annotations

import base64
import mailbox
from email.message import EmailMessage
from pathlib import Path

import pytest

from trajectory_os.mvp import model, signals, sources, store


def _seed(root: object) -> None:
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="phase4",
        projects=(model.Project(
            project_id="p1", name="Donation maison et terrains",
            objective="finaliser le dossier"),),
        tasks=(model.Task(task_id="t1", project_id="p1",
                          title="Finaliser les attestations",
                          status=model.TS_WAITING,
                          waiting_for=("notaire",)),),
    ))


def _eml(subject: str = "Donation maison et terrains",
         body: str = "Le notaire a envoyé les pièces. Échéance 15/11/2026.",
         message_id: str = "<m1@example.com>",
         in_reply_to: str = "",
         sender: str = "notaire@example.com") -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "moi@example.com"
    message["Subject"] = subject
    message["Date"] = "Wed, 28 Oct 2026 10:00:00 +0000"
    message["Message-ID"] = message_id
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    message.set_content(body + "\n\nOn Tue, someone wrote:\n> ancien message")
    return message.as_bytes()


def test_eml_ingestion_extracts_headers_body_and_proposals(
        tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    result = sources.ingest_eml_bytes(root, "mail.eml", _eml())
    source = result["source"]
    assert source["origin"] == sources.ORIGIN_EMAIL
    assert source["version"] == 1
    assert source["status"] == sources.STATUS_NEW
    assert source["thread_key"] == "Donation maison et terrains"
    assert result["signal"]["source_kind"] == signals.SOURCE_EMAIL_TEXT
    assert result["signal"]["source_ref"] == "m1@example.com"
    assert "ancien message" not in source["text"]
    assert any(item["op"] == signals.OP_SET_DEADLINE
               for item in result["proposals"])


def test_mbox_ingestion_is_bounded_and_idempotent(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    mbox_path = Path(str(tmp_path)) / "mail.mbox"
    box = mailbox.mbox(str(mbox_path))
    for index in range(2):
        message = EmailMessage()
        message["From"] = "notaire@example.com"
        message["Subject"] = "Donation maison et terrains"
        message["Date"] = "Wed, 28 Oct 2026 10:00:00 +0000"
        message["Message-ID"] = f"<m{index}@example.com>"
        message.set_content("Le notaire a envoyé les pièces.")
        box.add(message)
    box.flush()
    box.close()
    data = mbox_path.read_bytes()
    first = sources.ingest_mbox_bytes(root, "mail.mbox", data)
    assert first["count"] == 2
    assert not first["errors"]
    second = sources.ingest_mbox_bytes(root, "mail.mbox", data)
    assert second["count"] == 2
    assert all(item["duplicate"] for item in second["results"])


def test_document_version_diff(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    first = sources.ingest_document_bytes(
        root, "note.md", b"# Dossier\nObjectif v1\n", project_hint="p1")
    assert first["version"] == 1
    second = sources.ingest_document_bytes(
        root, "note.md", b"# Dossier\nObjectif v2\nNouvelle action\n",
        project_hint="p1")
    assert second["version"] == 2
    assert second["source"]["status"] == sources.STATUS_CHANGED
    assert second["diff"]
    assert any("v2" in line for line in second["diff"])
    latest = sources.latest_source_by_location(root, "note.md")
    assert latest is not None
    assert latest.version == 2


def test_restricted_content_is_not_persisted(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    with pytest.raises(sources.SourceError):
        sources.ingest_text(root, text="secret", source_ref="mail",
                            sensitivity="RESTRICTED")
    assert not sources.sources_path(root).is_file()


def test_secret_shaped_content_is_redacted(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    result = sources.ingest_text(
        root, text="api_key = sk-super-secret\nAppeler le notaire.",
        source_ref="mail")
    assert "[REDACTED_SECRET]" in result["source"]["text"]
    assert "sk-super-secret" not in result["source"]["text"]


def test_source_rule_attaches_project_and_mutes(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    signals.set_source_rule(root, "mail-notaire", project_id="p1")
    result = sources.ingest_text(
        root, text="Échéance 15/11/2026.", source_ref="mail-notaire")
    assert result["signal"]["project_hint"] == "p1"
    assert any(item["target_id"] == "p1" for item in result["proposals"])
    signals.set_source_rule(root, "mail-notaire", project_id="p1", mute=True)
    with pytest.raises(sources.SourceError):
        sources.ingest_text(root, text="autre", source_ref="mail-notaire")


def test_eml_endpoint_and_ui_file_types(tmp_path: object) -> None:
    from test_mvp_dashboard_api import _request, _server

    root = str(tmp_path)
    _seed(root)
    payload = base64.b64encode(_eml()).decode("ascii")
    with _server(root) as port:
        status, result = _request(port, "POST", "/api/signals", {
            "filename": "mail.eml", "content_b64": payload,
            "source_ref": "mail-notaire",
        })
        assert status == 200
        assert result["source"]["origin"] == sources.ORIGIN_EMAIL
        status, listing = _request(port, "GET", "/api/signals")
        assert status == 200
        assert listing["signals"]
    from trajectory_os.mvp import cockpit_ui

    assert ".eml" in cockpit_ui.PAGE_JS
    assert ".mbox" in cockpit_ui.PAGE_JS


def test_imap_fake_fetcher_filters_senders(tmp_path: object) -> None:
    from trajectory_os.mvp import imap_source

    root = str(tmp_path)
    _seed(root)
    class FakeFetcher:
        def fetch(self, mailbox: str, limit: int):
            return [
                ("1", _eml(message_id="<imap1@example.com>")),
                ("2", _eml(message_id="<imap2@example.com>",
                           sender="spam@example.com")),
            ]
    config = imap_source.ImapConfig(
        host="imap.example.com", username="me", password_env="IMAP_PASSWORD",
        allow_senders=("notaire@example.com",), max_messages=10)
    result = imap_source.ingest_imap(root, config, FakeFetcher())
    assert result["count"] == 1
    assert len(result["skipped"]) == 1
    assert result["readonly"] is True


def test_imap_requires_allowlist() -> None:
    from trajectory_os.mvp import imap_source

    with pytest.raises(imap_source.ImapError):
        imap_source.ImapConfig(
            host="imap.example.com", username="me",
            password_env="IMAP_PASSWORD").validate()


def test_imap_muted_sender_is_skipped(tmp_path: object) -> None:
    from trajectory_os.mvp import imap_source

    root = str(tmp_path)
    _seed(root)
    signals.set_source_rule(root, "notaire@example.com", mute=True)
    class FakeFetcher:
        def fetch(self, mailbox: str, limit: int):
            return [("1", _eml(message_id="<imap3@example.com>"))]
    config = imap_source.ImapConfig(
        host="imap.example.com", username="me", password_env="IMAP_PASSWORD",
        allow_senders=("notaire@example.com",), max_messages=10)
    result = imap_source.ingest_imap(root, config, FakeFetcher())
    assert result["count"] == 0
    assert result["skipped"][0]["reason"] == "sender muted"


def test_source_rule_endpoint_and_ui() -> None:
    import tempfile

    from test_mvp_dashboard_api import _request, _server

    from trajectory_os.mvp import cockpit_ui

    root = tempfile.mkdtemp(prefix="phase4-rule-")
    _seed(root)
    with _server(root) as port:
        status, result = _request(port, "POST", "/api/sources/rule", {
            "source_ref": "notaire@example.com",
            "project_id": "p1",
            "mute": False,
        })
        assert status == 200
        assert result["source_rules"]["notaire@example.com"][
            "project_id"] == "p1"
        status, listing = _request(port, "GET", "/api/views")
        assert status == 200
        assert "source_rules" in listing
    js = cockpit_ui.PAGE_JS
    assert "renderSourcesSection" in js
    assert "saveSourceRule" in js
    assert "src-project-" in js
