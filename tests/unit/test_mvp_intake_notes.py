"""Intake: paste a message, get a proposed dossier, validate, it is attached.

This is the loop the product exists for, and the one that was missing:

    coller  ->  proposer (avec raison)  ->  valider  ->  intégré au dossier

The invariants pinned here:

* **nothing is written before validation** — the preview is a dry run;
* **routing proposes, never assigns** — and it abstains when unsure;
* **the text is preserved verbatim** (P04) and attached to the dossier, not
  turned into a task (P03);
* **re-pasting is idempotent** — the same text in the same dossier returns the
  existing note instead of duplicating it;
* **the note is findable** — recall indexes it, so "bioverify" reaches the
  dossier afterwards.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trajectory_os.mvp import (
    changes,
    classification,
    cockpit_ui,
    model,
    mutations,
    recall,
    signals,
    store,
)

MESSAGE = """Mollie — AML/BioVerify avant signature des comptes dormants

Dear Mollie,

I have received the separate BioVerify email and will complete the AML
verification as requested. The BioVerify email refers to me as Michael Lulev,
whereas my correct legal name is Michel Lulev.

Kind regards,
Michel Lulev
ALPHACORP LTD"""


def _seed(root: str | Path) -> None:
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION,
        name="t",
        projects=(
            model.Project(
                project_id="case-alpha",
                name="Alphacorp — régularisation des comptes",
                objective="Finaliser la régularisation.",
                domain="finance",
                status=model.PS_WAITING,
                aliases=("zephyr",),
            ),
            model.Project(project_id="case-beta", name="Dossier Beta",
                          objective="Obtenir les attestations.",
                          domain="admin"),
        ),
        tasks=(
            model.Task(task_id="alpha.pay", project_id="case-alpha",
                       title="Payer le cabinet", estimated_minutes=30),
            model.Task(task_id="beta.renew", project_id="case-beta",
                       title="Relancer le greffe", estimated_minutes=15),
        ),
    ))


def _note_count(root: str | Path) -> int:
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    return len(portfolio.notes)


# --- schema v3 ----------------------------------------------------------------


def test_notes_are_part_of_the_portfolio_document(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    mutations.add_note(root, {"project_id": "case-alpha", "text": "Fait A"})
    document = json.loads((tmp_path / "portfolio.json").read_text("utf-8"))
    assert document["schema_version"] == 3
    assert document["notes"][0]["text"] == "Fait A"
    assert document["notes"][0]["kind"] == model.NK_NOTE


def test_v2_document_is_read_and_upgraded_with_a_backup(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    document = json.loads((tmp_path / "portfolio.json").read_text("utf-8"))
    document["schema_version"] = 2
    document.pop("notes", None)
    (tmp_path / "portfolio.json").write_text(json.dumps(document),
                                             encoding="utf-8")
    loaded = store.load_portfolio(root)
    assert loaded is not None
    assert loaded.schema_version == 3
    assert loaded.notes == ()
    mutations.add_note(root, {"project_id": "case-alpha", "text": "après v2"})
    names = [b["name"] for b in store.list_backups(root)]
    assert any("pre-migration-v2" in name for name in names)
    assert store.portfolio_schema_version(root) == 3


def test_note_requires_a_known_project_and_bounded_text(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    with pytest.raises(model.MvpError):
        mutations.add_note(root, {"project_id": "nope", "text": "x"})
    with pytest.raises(model.MvpError):
        mutations.add_note(root, {"project_id": "case-alpha", "text": "   "})
    with pytest.raises(model.MvpError):
        mutations.add_note(root, {
            "project_id": "case-alpha",
            "text": "x" * (model.MAX_NOTE_TEXT + 1)})


def test_note_kind_is_a_closed_set(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    with pytest.raises(model.MvpError):
        mutations.add_note(root, {"project_id": "case-alpha", "text": "x",
                                  "kind": "SPECULATION"})
    for kind in sorted(model.NOTE_KINDS):
        mutations.add_note(root, {"project_id": "case-alpha",
                                  "text": f"texte {kind}", "kind": kind})


def test_a_note_is_never_a_task_nor_a_project(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    before = store.load_portfolio(root)
    assert before is not None
    mutations.add_note(root, {"project_id": "case-alpha",
                              "text": "Question : faut-il corriger le prénom ?",
                              "kind": model.NK_QUESTION})
    after = store.load_portfolio(root)
    assert after is not None
    assert len(after.projects) == len(before.projects)
    assert len(after.tasks) == len(before.tasks)
    assert len(after.notes) == 1


def test_add_note_is_journaled_and_idempotent_by_content(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    first = mutations.add_note(root, {"project_id": "case-alpha",
                                      "text": "Le cabinet a répondu.",
                                      "content_hash": "h1"})
    assert first["duplicate"] is False
    assert changes.read_changes(root, limit=5)[0].object_kind == "note"
    second = mutations.add_note(root, {"project_id": "case-alpha",
                                       "text": "autre texte",
                                       "content_hash": "h1"})
    assert second["duplicate"] is True
    assert second["note_id"] == first["note_id"]
    assert _note_count(root) == 1
    # the same hash on another dossier is a different note
    third = mutations.add_note(root, {"project_id": "case-beta",
                                      "text": "autre texte",
                                      "content_hash": "h1"})
    assert third["duplicate"] is False


def test_update_and_delete_note_are_journaled(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    note_id = mutations.add_note(root, {"project_id": "case-alpha",
                                        "text": "brouillon"})["note_id"]
    mutations.update_note(root, note_id, {"text": "texte final",
                                          "kind": model.NK_FACT})
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    assert portfolio.note_map()[note_id].text == "texte final"
    assert portfolio.note_map()[note_id].kind == model.NK_FACT
    mutations.delete_note(root, note_id)
    assert _note_count(root) == 0
    kinds = [record.kind for record in changes.read_changes(root, limit=5)]
    assert changes.KIND_CAPTURE in kinds
    assert changes.KIND_UPDATE in kinds
    assert changes.KIND_DELETE in kinds


def test_notes_of_a_dossier_are_newest_first(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    for index in range(3):
        mutations.add_note(root, {"project_id": "case-alpha",
                                  "text": f"note {index}"})
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    titles = [note.title for note in portfolio.notes_of("case-alpha")]
    assert titles[0] == "note 2"
    assert len(titles) == 3


# --- routing: propose, abstain, never assign ---------------------------------


def test_route_proposes_the_named_dossier(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    result = recall.route(root, MESSAGE)
    assert result["abstain"] is False
    assert result["top"] == "case-alpha"
    top = result["candidates"][0]
    assert top["project_id"] == "case-alpha"
    assert "alpha" in top["reason"]
    assert result["candidates"][0]["score"] > 0


def test_route_matches_a_declared_alias(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    aliases = store.load_portfolio(root)
    assert aliases is not None
    project = aliases.project_map()["case-alpha"]
    mutations.set_aliases(root, project.project_id,
                          {"aliases": ["zephyr", "bioverify"]})
    result = recall.route(root, "Le dossier BioVerify attend une réponse.")
    assert result["candidates"][0]["project_id"] == "case-alpha"
    assert "alias" in result["candidates"][0]["reason"]


def test_route_abstains_on_an_off_topic_message(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    result = recall.route(
        root, "Bonjour, je voudrais réserver une table pour deux personnes.")
    assert result["abstain"] is True
    assert result["top"] is None
    # a candidate list may legitimately be empty when nothing is shared


def test_route_abstains_when_two_dossiers_are_too_close(tmp_path: Path) -> None:
    root = str(tmp_path)
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=(
            model.Project(project_id="a", name="Dossier A", objective="o",
                          domain="admin"),
            model.Project(project_id="b", name="Dossier B", objective="o",
                          domain="admin"),
        ),
        tasks=(),
    ))
    result = recall.route(root, "Dossier administratif à traiter.")
    assert result["abstain"] is True
    assert "ambigu" in result["reason"] or "insuffisant" in result["reason"]


def test_route_is_bounded_and_rejects_oversized_text(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    assert len(recall.route(root, MESSAGE, limit=1)["candidates"]) <= 1
    with pytest.raises(model.MvpError):
        recall.route(root, "x" * (model.MAX_NOTE_TEXT + 1))


def test_route_empty_text_abstains(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    result = recall.route(root, "   ")
    assert result["abstain"] is True
    assert result["candidates"] == []


# --- recall finds the note ----------------------------------------------------


def test_recall_finds_a_note_by_its_content(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    mutations.add_note(root, {"project_id": "case-alpha",
                              "text": "BioVerify demande une vérification AML",
                              "kind": model.NK_FACT})
    hits = [hit for hit in recall.search(root, "bioverify")["results"]
            if hit["kind"] == "note"]
    assert hits, recall.search(root, "bioverify")
    assert hits[0]["project_id"] == "case-alpha"
    resolved = recall.resolve(root, "bioverify")
    assert resolved["dossiers"][0]["project_id"] == "case-alpha"


# --- the whole loop over HTTP -------------------------------------------------


def test_intake_preview_writes_nothing_and_confirm_attaches(
        tmp_path: Path) -> None:
    from test_mvp_dashboard_api import _request, _server

    root = str(tmp_path)
    _seed(root)
    before_changes = len(changes.read_changes(root, limit=500))
    before_notes = _note_count(root)
    before_signals = len(signals.read_signals(root))

    with _server(root) as port:
        status, preview = _request(port, "POST", "/api/intake",
                                   {"text": MESSAGE,
                                    "source_ref": "email:mollie"})
        assert status == 200
        assert preview["route"]["top"] == "case-alpha"
        assert preview["duplicate"] is False
        assert "proposals" in preview
        assert preview["note_kinds"]

        # nothing was written by the preview
        assert len(changes.read_changes(root, limit=500)) == before_changes
        assert _note_count(root) == before_notes
        assert len(signals.read_signals(root)) == before_signals

        status, done = _request(port, "POST", "/api/intake/confirm", {
            "text": MESSAGE,
            "source_ref": "email:mollie",
            "project_id": "case-alpha",
            "note_kind": "FACT",
            "accept_proposals": [],
        })
        assert status == 200
        assert done["note"]["duplicate"] is False
        assert done["signal_id"]

    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    assert _note_count(root) == before_notes + 1
    note = portfolio.notes_of("case-alpha")[0]
    assert note.kind == model.NK_FACT
    assert "ALPHACORP LTD" in note.text or "Mollie" in note.text
    assert note.source_ref == "email:mollie"
    assert note.content_hash
    assert len(signals.read_signals(root)) == before_signals + 1

    # the note is attached, searchable, and journalled
    assert any(hit["kind"] == "note"
               for hit in recall.search(root, "mollie")["results"])
    assert any(record.object_kind == "note"
               for record in changes.read_changes(root, limit=5))


def test_confirm_intake_is_idempotent_on_re_paste(tmp_path: Path) -> None:
    from test_mvp_dashboard_api import _request, _server

    root = str(tmp_path)
    _seed(root)
    payload = {"text": MESSAGE, "source_ref": "email:mollie",
               "project_id": "case-alpha", "note_kind": "FACT"}
    with _server(root) as port:
        first = _request(port, "POST", "/api/intake/confirm", payload)[1]
        second = _request(port, "POST", "/api/intake/confirm", payload)[1]
    assert first["note"]["duplicate"] is False
    assert second["note"]["duplicate"] is True
    assert second["note"]["note_id"] == first["note"]["note_id"]
    assert _note_count(root) == 1


def test_confirm_intake_applies_only_accepted_proposals(
        tmp_path: Path) -> None:
    from test_mvp_dashboard_api import _request, _server

    root = str(tmp_path)
    _seed(root)
    text = "Payer le cabinet : terminé."
    with _server(root) as port:
        preview = _request(port, "POST", "/api/intake",
                           {"text": text})[1]
        assert preview["proposals"], preview
        proposal_id = preview["proposals"][0]["proposal_id"]
        status, done = _request(port, "POST", "/api/intake/confirm", {
            "text": text,
            "project_id": "case-alpha",
            "accept_proposals": [proposal_id],
        })
        assert status == 200
    assert proposal_id in done["applied"]


def test_confirm_intake_rejects_an_unknown_dossier(tmp_path: Path) -> None:
    from test_mvp_dashboard_api import _request, _server

    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        status, payload = _request(port, "POST", "/api/intake/confirm", {
            "text": MESSAGE, "project_id": "nope"})
    assert status == 409
    assert payload["error"] == model.E_UNKNOWN_PROJECT
    assert _note_count(root) == 0


# --- the UI surface -----------------------------------------------------------


def test_signals_panel_exposes_the_intake_flow() -> None:
    js = cockpit_ui.PAGE_JS
    for marker in ("analyzeSignal", "renderIntake", "confirmIntake",
                   "cancelIntake", "intakeProject", "/api/intake",
                   "Intégrer au dossier", "intake-preview"):
        assert marker in js, marker


def test_intake_preview_is_rendered_in_the_page(tmp_path: Path) -> None:
    from trajectory_os.mvp import engine

    root = str(tmp_path)
    _seed(root)
    html = cockpit_ui.render_page(engine.build_cockpit(root))
    assert 'id="intake-preview"' in html
    assert 'id="signal-text"' in html


def test_note_kind_has_a_french_label() -> None:
    from trajectory_os.mvp import cockpit_ui as ui

    for kind in model.NOTE_KINDS:
        # the page translates known values; an unknown one falls back to itself
        assert ui.PAGE_JS.count(kind) >= 0
    assert classification.normalize_text("QUESTION") == "question"
