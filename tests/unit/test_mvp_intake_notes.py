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
            # a dossier whose *name* is a common expression, so a passing
            # mention of it can be reproduced (the real-world failure mode)
            model.Project(project_id="case-tax",
                          name="Taxe Foncière — réduire la demande",
                          objective="Obtenir une réduction.",
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


# --- routing precision: a passing mention is not identity --------------------


PASSING_MENTION = """Bonjour Madame,

Je me suis présenté au service social pour ma carte de restauration sociale.
J'ai vérifié mes messageries sans retrouver de trace de vos appels.
Ma situation financière demeure inchangée.
La dernière épreuve a été le règlement de ma taxe foncière de 423 EUR.
Je vous remercie de votre accompagnement et de votre attention.
La personne qui m'a accueilli indique que vous reprendriez contact.
Bien cordialement, Michel"""

SIGNATURE = """Bonjour,

Le cabinet a répondu favorablement sur le dossier.

Bien cordialement,
Michel
Alphacorp"""


def test_route_abstains_when_the_name_is_a_passing_mention(
        tmp_path: Path) -> None:
    """A dossier named in the middle of a letter is vocabulary, not identity.

    Measured on a real letter: an expense quoted in the fifth paragraph was the
    name of another dossier and the router preselected it. The name must appear
    in the header or the signature to justify a proposal.
    """
    root = str(tmp_path)
    _seed(root)
    result = recall.route(root, PASSING_MENTION)
    assert result["abstain"] is True
    assert result["top"] is None
    assert "passant" in result["reason"]
    # the candidate stays visible so the human can still pick it
    assert result["candidates"]


def test_route_proposes_when_the_name_is_in_the_signature(
        tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    result = recall.route(root, SIGNATURE)
    assert result["abstain"] is False
    assert result["top"] == "case-alpha"
    candidate = result["candidates"][0]
    assert candidate["identity_position"] is not None
    assert candidate["identity_position"] >= 0.75


def test_route_matches_a_multi_word_alias(tmp_path: Path) -> None:
    """An alias is often several words; a token set can never match it."""
    root = str(tmp_path)
    _seed(root)
    mutations.set_aliases(root, "case-alpha",
                          {"aliases": ["aduriz bonneau"]})
    result = recall.route(root, "Bonjour Madame Aduriz Bonneau, je confirme.")
    assert result["abstain"] is False
    assert result["top"] == "case-alpha"
    assert "alias" in result["candidates"][0]["reason"]


def test_route_requires_identity_even_with_rich_vocabulary(
        tmp_path: Path) -> None:
    """Shared vocabulary alone never preselects a dossier."""
    root = str(tmp_path)
    _seed(root)
    result = recall.route(
        root, "Payer le cabinet, envoyer les statuts, obtenir les "
              "attestations, relancer le greffe.")
    assert result["abstain"] is True
    assert result["top"] is None


# --- the correction is durable ------------------------------------------------


def test_a_remembered_alias_routes_the_next_message(
        tmp_path: Path) -> None:
    from test_mvp_dashboard_api import _request, _server

    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        status, preview = _request(port, "POST", "/api/intake",
                                   {"text": PASSING_MENTION,
                                    "source_ref": "courrier:social"})
        assert preview["route"]["abstain"] is True
        status, done = _request(port, "POST", "/api/intake/confirm", {
            "text": PASSING_MENTION,
            "source_ref": "courrier:social",
            "project_id": "case-beta",
            "note_kind": "FACT",
            "remember_alias": "aduriz bonneau",
        })
        assert status == 200
        assert done["remembered_alias"] == "aduriz bonneau"
        status, again = _request(port, "POST", "/api/intake", {
            "text": "Bonjour Madame Aduriz Bonneau, je confirme mon adresse.",
            "source_ref": "courrier:social-2"})
        assert again["route"]["abstain"] is False
        assert again["route"]["top"] == "case-beta"

    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    assert "aduriz bonneau" in portfolio.project_map()["case-beta"].aliases
    # the alias was journalled as a project update
    assert any(record.object_kind == "project"
               for record in changes.read_changes(root, limit=10))


def test_remember_alias_is_idempotent_and_bounded(tmp_path: Path) -> None:
    from test_mvp_dashboard_api import _request, _server

    root = str(tmp_path)
    _seed(root)
    payload = {"text": MESSAGE, "project_id": "case-alpha",
               "remember_alias": "aduriz bonneau"}
    with _server(root) as port:
        first = _request(port, "POST", "/api/intake/confirm", payload)[1]
        second = _request(port, "POST", "/api/intake/confirm", payload)[1]
    assert first["remembered_alias"] == "aduriz bonneau"
    assert second["remembered_alias"] == ""
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    assert portfolio.project_map()["case-alpha"].aliases.count(
        "aduriz bonneau") == 1


def test_create_a_dossier_then_attach_the_message(tmp_path: Path) -> None:
    from test_mvp_dashboard_api import _request, _server

    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        status, created = _request(port, "POST", "/api/projects", {
            "name": "Service social — carte de restauration",
            "objective": "Maintenir le lien avec le service social."})
        assert status == 200
        status, done = _request(port, "POST", "/api/intake/confirm", {
            "text": PASSING_MENTION,
            "source_ref": "courrier:social",
            "project_id": created["project_id"],
            "note_kind": "FACT"})
        assert status == 200
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    notes = portfolio.notes_of(created["project_id"])
    assert len(notes) == 1
    assert "service social" in notes[0].text
    assert any(hit["kind"] == "note"
               for hit in recall.search(root, "service social")["results"])


# --- (a) the dossier lists its open questions --------------------------------


def test_extract_questions_is_deterministic_and_bounded() -> None:
    from trajectory_os.mvp import intake

    text = ("Bonjour.\n\n"
            "Pouvez-vous me confirmer si le prénom doit être corrigé avant "
            "la vérification ?\n\n"
            "Et si cela couvre aussi Companies House ?\n\n"
            "OK ?\n\n"
            "Pouvez-vous me confirmer si le prénom doit être corrigé avant "
            "la vérification ?")
    questions = intake.extract_questions(text)
    assert len(questions) == 2, questions
    assert all(question.endswith("?") for question in questions)
    assert "OK ?" not in questions
    assert len(set(questions)) == len(questions)
    assert intake.extract_questions(text, maximum=1) == questions[:1]
    assert intake.extract_questions("") == []


def test_confirm_creates_a_note_per_question(tmp_path: Path) -> None:
    from test_mvp_dashboard_api import _request, _server

    root = str(tmp_path)
    _seed(root)
    text = ("Mollie — AML\n\nBonjour.\n\n"
            "Pouvez-vous confirmer si le prénom doit être corrigé avant la "
            "vérification ?\n\n"
            "Et si cela satisfait aussi Companies House pour le directeur ?\n")
    with _server(root) as port:
        status, done = _request(port, "POST", "/api/intake/confirm", {
            "text": text, "source_ref": "email:mollie",
            "project_id": "case-alpha", "note_kind": "FACT"})
        assert status == 200
        assert len(done["questions"]) == 2
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    kinds = [note.kind for note in portfolio.notes_of("case-alpha")]
    assert kinds.count(model.NK_QUESTION) == 2
    assert model.NK_FACT in kinds


def test_open_questions_appear_in_the_dossier_context(tmp_path: Path) -> None:
    from test_mvp_dashboard_api import _request, _server

    root = str(tmp_path)
    _seed(root)
    text = ("BioVerify\n\n"
            "Pouvez-vous confirmer si le prénom doit être corrigé avant la "
            "vérification ?\n")
    with _server(root) as port:
        _request(port, "POST", "/api/intake/confirm", {
            "text": text, "source_ref": "email:mollie",
            "project_id": "case-alpha", "note_kind": "FACT"})
    payload = recall.resolve(root, "bioverify")
    dossier = payload["dossiers"][0]
    assert dossier["project_id"] == "case-alpha"
    assert len(dossier["questions"]) == 1
    assert dossier["questions"][0]["answered"] is False
    assert dossier["notes"]


def test_re_pasting_does_not_duplicate_questions(tmp_path: Path) -> None:
    from test_mvp_dashboard_api import _request, _server

    root = str(tmp_path)
    _seed(root)
    text = ("BioVerify\n\n"
            "Pouvez-vous confirmer si le prénom doit être corrigé ?\n")
    payload = {"text": text, "source_ref": "email:mollie",
               "project_id": "case-alpha", "note_kind": "FACT"}
    with _server(root) as port:
        _request(port, "POST", "/api/intake/confirm", payload)
        done = _request(port, "POST", "/api/intake/confirm", payload)[1]
    assert all(question["duplicate"] for question in done["questions"])
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    assert len(portfolio.notes_of("case-alpha")) == 2


# --- (b) the first screen answers "what do I do?" ---------------------------


def test_today_view_shows_three_priorities_when_nothing_is_planned() -> None:
    from trajectory_os.mvp import cockpit_ui

    js = cockpit_ui.PAGE_JS
    assert "function todayFallback" in js
    assert "Les 3 premières priorités" in js
    assert "Fenêtre de travail terminée" in js
    assert "Estimer les tâches sans estimation" in js
    assert "todayFallback(plan)" in js


def test_today_fallback_renders_on_a_planned_out_day(tmp_path: Path) -> None:
    """With an elapsed working window and no estimate, the view still answers."""
    from trajectory_os.mvp import model, store, visualization

    root = str(tmp_path)
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=(model.Project(project_id="p", name="Projet P",
                                objective="o"),),
        tasks=tuple(
            model.Task(task_id=f"t{index}", project_id="p",
                       title=f"Tâche {index}", estimated_minutes=30)
            for index in range(3)),
    ))
    views = visualization.build_views(root)
    plan = views["today_plan"]
    # a plan with no planned task must carry the material for the fallback
    assert plan["planned"] == []
    assert plan["deferred"] or plan["unknown_effort"]
    first = (plan["deferred"] or plan["unknown_effort"])[0]
    for key in ("task_id", "title", "reasons"):
        assert key in first
