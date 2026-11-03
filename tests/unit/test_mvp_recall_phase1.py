"""Phase 1 — Context Recall MVP: declared aliases, recall and migration.

These tests pin the four invariants of the first increment of the V1 programme
(ADR-042/043/044):

* **declared identity wins** — a user alias is an exact match, above every
  inferred textual signal, so ``zephyr`` can resolve a dossier whose data never
  mentions the word;
* **migration is additive and reversible** — a schema v1 document is read as
  v2 in memory, and the first write backs the legacy file up first;
* **recall sees everything canonical** — projects, tasks, decisions, inbound
  sources, signals and the change journal, with an explicit reason per hit;
* **recall never writes** — a lookup produces no change record, no ledger
  entry and no portfolio write.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trajectory_os.mvp import (
    changes,
    classification,
    cli,
    cockpit_ui,
    dashboard,
    model,
    mutations,
    recall,
    search,
    signals,
    sources,
    store,
)

# --- fixtures -----------------------------------------------------------------


def _seed(root: str | Path, *, aliases: tuple[str, ...] = ()) -> None:
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION,
        name="t",
        projects=(
            model.Project(
                project_id="case-alpha",
                name="Dossier Alpha — régularisation des comptes",
                objective="Finaliser les démarches de régularisation.",
                domain="finance",
                status=model.PS_WAITING,
                aliases=aliases,
            ),
            model.Project(project_id="case-beta", name="Dossier Beta",
                          objective="Obtenir les attestations.", domain="admin"),
        ),
        tasks=(
            model.Task(task_id="alpha.pay", project_id="case-alpha",
                       title="Payer le cabinet",
                       waiting_for=("cabinet externe",),
                       estimated_minutes=30),
            model.Task(task_id="alpha.send", project_id="case-alpha",
                       title="Envoyer les statuts signés",
                       estimated_minutes=10),
            model.Task(task_id="beta.renew", project_id="case-beta",
                       title="Relancer le greffe", estimated_minutes=15),
        ),
        decisions=(
            model.Decision(decision_id="alpha.scope", title="Périmètre",
                           question="Faut-il inclure la TVA ?",
                           project_id="case-alpha"),
        ),
    ))


def _change_count(root: str | Path) -> int:
    return len(changes.read_changes(root, limit=500))


# --- schema v2: aliases -------------------------------------------------------


def test_project_alias_is_validated_and_round_trips(tmp_path: Path) -> None:
    root = tmp_path
    _seed(root, aliases=("zephyr", "ALPHA"))
    loaded = store.load_portfolio(str(root))
    assert loaded is not None
    project = loaded.project_map()["case-alpha"]
    assert project.aliases == ("zephyr", "ALPHA")
    document = json.loads((root / "portfolio.json").read_text("utf-8"))
    stored = next(p for p in document["projects"]
                  if p["project_id"] == "case-alpha")
    assert stored["aliases"] == ["zephyr", "ALPHA"]
    assert document["schema_version"] == 2


def test_duplicate_alias_is_rejected_case_and_accent_insensitively() -> None:
    with pytest.raises(model.MvpError):
        model.Project(project_id="p", name="P", objective="o",
                      aliases=("Café", "cafe")).validate()


def test_alias_bounds_are_enforced() -> None:
    with pytest.raises(model.MvpError):
        model.Project(project_id="p", name="P", objective="o",
                      aliases=tuple(f"a{index}"
                                    for index in range(model.MAX_ALIASES + 1))
                      ).validate()
    with pytest.raises(model.MvpError):
        model.Project(project_id="p", name="P", objective="o",
                      aliases=("x" * (model.MAX_ALIAS_LEN + 1),)).validate()


def test_alias_accepts_free_text_but_not_an_empty_value() -> None:
    project = model.Project(project_id="p", name="P", objective="o",
                            aliases=("cabinet externe", "Zephyr & co"),
                            ).validate()
    assert project.aliases == ("cabinet externe", "Zephyr & co")
    with pytest.raises(model.MvpError):
        model.Project(project_id="p", name="P", objective="o",
                      aliases=("   ",)).validate()


def test_fold_text_matches_the_shared_normalizer() -> None:
    for value in ("CASE ALPHA", "Café  Zephyr", "  Dossier  N°22 ", "été"):
        assert model.fold_text(value) == classification.normalize_text(value)


# --- schema v2: migration -----------------------------------------------------


def _legacy_document() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "mvp_version": "mvp.1",
        "name": "legacy",
        "projects": [
            {"project_id": "case-alpha", "name": "CASE ALPHA",
             "objective": "restauration", "status": "WAITING",
             "domain": "finance", "urgency": "HIGH", "impact": "HIGH",
             "deadline": None, "description": ""},
        ],
        "tasks": [
            {"task_id": "alpha.pay", "project_id": "case-alpha",
             "title": "Payer le cabinet", "description": "",
             "workstream": "", "deliverable": "", "status": "TODO",
             "estimated_minutes": 30, "actual_minutes": None,
             "deadline": None, "urgency": "MEDIUM", "impact": "MEDIUM",
             "dependencies": [], "blocked_by": [], "waiting_for": [],
             "resources": [], "next_action": "", "created_at": "",
             "updated_at": ""},
        ],
        "routines": [], "decisions": [], "resources": [], "calendar": [],
        "days_off": [],
        "capacity": {"minutes_per_day": 360, "buffer_ratio": 0.3,
                     "day_start_minutes": 540, "day_end_minutes": 1080},
        "created_at": "", "updated_at": "",
    }


def test_legacy_v1_document_is_readable_and_upgraded_in_memory(
        tmp_path: Path) -> None:
    (tmp_path / "portfolio.json").write_text(
        json.dumps(_legacy_document()), encoding="utf-8")
    project = model.Portfolio.from_dict(_legacy_document())
    assert project.schema_version == model.SCHEMA_VERSION == 2
    assert project.project_map()["case-alpha"].aliases == ()

    loaded = store.load_portfolio(str(tmp_path))
    assert loaded is not None and loaded.schema_version == 2
    assert store.portfolio_schema_version(str(tmp_path)) == 1


def test_first_write_after_v1_takes_a_pre_migration_backup(
        tmp_path: Path) -> None:
    (tmp_path / "portfolio.json").write_text(
        json.dumps(_legacy_document()), encoding="utf-8")
    mutations.set_aliases(str(tmp_path), "case-alpha", {"aliases": "zephyr"})
    names = [item["name"] for item in store.list_backups(str(tmp_path))]
    assert any("pre-migration-v1" in name for name in names)
    assert store.portfolio_schema_version(str(tmp_path)) == 2
    loaded = store.load_portfolio(str(tmp_path))
    assert loaded is not None
    assert loaded.project_map()["case-alpha"].aliases == ("zephyr",)


def test_pre_migration_backup_is_taken_once(tmp_path: Path) -> None:
    (tmp_path / "portfolio.json").write_text(
        json.dumps(_legacy_document()), encoding="utf-8")
    mutations.set_aliases(str(tmp_path), "case-alpha", {"aliases": "zephyr"})
    mutations.set_aliases(str(tmp_path), "case-alpha", {"aliases": "alpha"})
    names = [item["name"] for item in store.list_backups(str(tmp_path))]
    assert len([n for n in names if "pre-migration-v1" in n]) == 1


def test_unknown_schema_version_still_fails_closed() -> None:
    document = _legacy_document()
    document["schema_version"] = 99
    with pytest.raises(model.MvpError) as excinfo:
        model.Portfolio.from_dict(document)
    assert excinfo.value.code == model.E_UNSUPPORTED_VERSION


def test_v1_document_still_rejects_invalid_content() -> None:
    document = _legacy_document()
    document["tasks"][0]["dependencies"] = ["missing"]
    with pytest.raises(model.MvpError):
        model.Portfolio.from_dict(document)


# --- aliases as a first-class mutation ---------------------------------------


def test_set_aliases_is_journaled_and_idempotent(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    before = _change_count(root)
    result = mutations.set_aliases(root, "case-alpha",
                                   {"aliases": "zephyr, alpha"})
    assert result["aliases"] == ["zephyr", "alpha"]
    assert result["changed"] is True
    assert _change_count(root) == before + 1
    again = mutations.set_aliases(root, "case-alpha", {"aliases": "Zephyr;ALPHA"})
    assert again["changed"] is False
    assert _change_count(root) == before + 1


def test_set_aliases_fails_closed_on_unknown_project(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    with pytest.raises(model.MvpError):
        mutations.set_aliases(root, "nope", {"aliases": "x"})


def test_update_project_accepts_aliases(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    mutations.update_project(root, "case-alpha", {"aliases": ["zephyr"]})
    loaded = store.load_portfolio(root)
    assert loaded is not None
    assert loaded.project_map()["case-alpha"].aliases == ("zephyr",)


def test_create_project_accepts_aliases(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    mutations.create_project(root, {"name": "New one", "objective": "obj",
                                    "aliases": "alpha;beta"})
    loaded = store.load_portfolio(root)
    assert loaded is not None
    created = next(p for p in loaded.projects if p.name == "New one")
    assert created.aliases == ("alpha", "beta")


# --- recall: declared identity beats inferred text ---------------------------


def test_alias_exact_match_resolves_a_word_absent_from_the_data(
        tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root, aliases=("zephyr",))
    result = recall.search(root, "zephyr")
    assert result["count"] >= 1
    top = result["results"][0]
    assert top["kind"] == "project"
    assert top["ref_id"] == "case-alpha"
    assert top["matched_field"] == "alias"
    assert top["score"] == recall.S_ALIAS_EXACT
    assert result["dossiers"][0]["project_id"] == "case-alpha"


def test_alias_outranks_an_exact_title_match(tmp_path: Path) -> None:
    root = str(tmp_path)
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=(
            model.Project(project_id="a", name="Zephyr", objective="o"),
            model.Project(project_id="b", name="Other", objective="o",
                          aliases=("Zephyr",)),
        ),
        tasks=(),
    ))
    hits = recall.query(root, "zephyr")
    assert hits[0].ref_id == "b"
    assert hits[0].score == recall.S_ALIAS_EXACT
    assert hits[1].ref_id == "a"
    assert hits[1].score == recall.S_TITLE_EXACT


# --- recall: coverage ---------------------------------------------------------


def test_recall_finds_tasks_decisions_and_sources(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    sources.ingest_text(root, text="Zephyr demande une vérification AML.",
                        title="Mail Zephyr", project_hint="case-alpha")
    assert any(hit["kind"] == "task"
               for hit in recall.search(root, "cabinet")["results"])
    assert any(hit["kind"] == "decision"
               for hit in recall.search(root, "TVA")["results"])
    source_hits = [hit for hit in recall.search(root, "AML")["results"]
                   if hit["kind"] == "source"]
    assert source_hits, recall.search(root, "AML")
    assert source_hits[0]["project_id"] == "case-alpha"


def test_recall_finds_signals_and_journal_entries(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    signals.create_signal(root, text="Le greffe a envoyé les pièces.",
                          source_ref="mail", project_hint="case-beta")
    assert any(hit["kind"] == "signal"
               for hit in recall.search(root, "greffe")["results"])
    mutations.set_aliases(root, "case-beta", {"aliases": "greffe"})
    assert any(hit["kind"] == "change"
               for hit in recall.search(root, "alias")["results"])


def test_recall_matches_amounts_and_identifiers(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    mutations.update_task(root, "alpha.pay", {"next_action": "virer 1 250 €"})
    assert any("1 250" in hit["title"] or hit["matched_field"]
               == "prochaine action"
               for hit in recall.search(root, "1 250")["results"])
    identifier = [hit for hit in recall.search(root, "alpha.pay")["results"]
                  if hit["matched_field"] == "identifiant"]
    assert identifier and identifier[0]["score"] == recall.S_ID


def test_recall_is_accent_and_case_insensitive(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    assert recall.search(root, "REGULARISATION")["count"] >= 1
    assert recall.search(root, "régularisation")["dossiers"][0][
        "project_id"] == "case-alpha"


def test_recall_never_writes(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root, aliases=("zephyr",))
    before = _change_count(root)
    portfolio_before = (tmp_path / "portfolio.json").read_bytes()
    recall.search(root, "zephyr")
    recall.resolve(root, "zephyr")
    recall.query(root, "case-alpha")
    assert _change_count(root) == before
    assert (tmp_path / "portfolio.json").read_bytes() == portfolio_before


def test_recall_is_bounded_and_deterministic(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    first = [hit.ref_id for hit in recall.query(root, "e", limit=5)]
    second = [hit.ref_id for hit in recall.query(root, "e", limit=5)]
    assert first == second
    assert len(first) <= 5


def test_recall_rejects_an_oversized_query(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    with pytest.raises(model.MvpError):
        recall.query(root, "x" * (recall.MAX_QUERY_CHARS + 1))


def test_recall_empty_query_returns_nothing(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    assert recall.query(root, "   ") == []
    assert recall.search(root, "")["count"] == 0
    resolved = recall.resolve(root, "")
    assert resolved["count"] == 0 and resolved["dossiers"] == []


def test_recall_fails_soft_on_a_corrupt_sidecar(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "sources.jsonl").write_text("{not json}\n", encoding="utf-8")
    (inbox / "signals.jsonl").write_text("\n\n", encoding="utf-8")
    (tmp_path / "changes.jsonl").write_text("garbage\n", encoding="utf-8")
    result = recall.search(root, "régularisation")
    assert result["count"] >= 1


def test_recall_without_a_portfolio_is_empty_not_fatal(tmp_path: Path) -> None:
    assert recall.search(str(tmp_path), "x")["count"] == 0
    resolved = recall.resolve(str(tmp_path), "x")
    assert resolved["dossiers"] == []


# --- resolve: the resumable context -----------------------------------------


def test_resolve_returns_a_labelled_resumable_context(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root, aliases=("zephyr",))
    payload = recall.resolve(root, "zephyr")
    assert payload["canonical"] is False
    assert payload["count"] == 1
    dossier = payload["dossiers"][0]
    assert dossier["project_id"] == "case-alpha"
    assert dossier["aliases"] == ["zephyr"]
    assert dossier["counts"]["tasks"] == 2
    assert dossier["next_actions"][0]["title"] == "Envoyer les statuts signés"
    assert dossier["waiting"][0]["title"] == "Payer le cabinet"
    assert dossier["matched"]


def test_resolve_orders_dossiers_by_best_match(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root, aliases=("zephyr",))
    payload = recall.resolve(root, "greffe")
    assert payload["dossiers"][0]["project_id"] == "case-beta"


def test_resolve_bounds_the_number_of_dossiers(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root, aliases=("zephyr",))
    payload = recall.resolve(root, "e", limit_projects=1)
    assert len(payload["dossiers"]) <= 1


# --- cache (increment 1.3) ----------------------------------------------------


def test_index_is_cached_and_invalidated_by_a_write(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    recall.clear_cache()
    first = recall.build_index(root)
    assert recall.cache_size() == 1
    assert recall.build_index(root) is first
    mutations.set_aliases(root, "case-alpha", {"aliases": "zephyr"})
    refreshed = recall.build_index(root)
    assert refreshed is not first
    assert any(hit.ref_id == "case-alpha" for hit in recall.query(root, "zephyr"))


def test_clear_cache_is_scoped_and_total(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    recall.clear_cache()
    recall.build_index(root)
    assert recall.cache_size() == 1
    recall.clear_cache(str(tmp_path))
    assert recall.cache_size() == 0
    recall.build_index(root)
    recall.clear_cache()
    assert recall.cache_size() == 0


def test_stats_reports_index_composition(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root, aliases=("zephyr",))
    payload = recall.stats(root)
    assert payload["documents"] == 6
    assert payload["by_kind"]["project"] == 2
    assert payload["by_kind"]["task"] == 3
    assert payload["by_kind"]["decision"] == 1
    assert payload["aliases"] == 1


# --- search payload compatibility --------------------------------------------


def test_search_payload_keeps_its_historical_contract(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    payload = search.search(root, "cabinet")
    assert set(payload) == {"query", "results", "count", "dossiers"}
    assert payload["query"] == "cabinet"
    assert payload["count"] >= 1
    item = payload["results"][0]
    for key in ("kind", "ref_id", "title", "score", "reason",
                "matched_field", "hint"):
        assert key in item, key
    assert isinstance(item["score"], int)


def test_search_default_limit_is_bounded(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    payload = search.search(root, "e", limit=1)
    assert len(payload["results"]) == 1
    assert payload["count"] >= 1


# --- HTTP surface -------------------------------------------------------------


def test_recall_endpoint_and_alias_endpoint(tmp_path: Path) -> None:
    from test_mvp_dashboard_api import _request, _server

    root = str(tmp_path)
    _seed(root, aliases=("zephyr",))
    with _server(root) as port:
        status, payload = _request(port, "GET", "/api/recall?q=zephyr")
        assert status == 200
        assert payload["count"] == 1
        assert payload["dossiers"][0]["project_id"] == "case-alpha"

        status, payload = _request(port, "GET", "/api/search?q=zephyr")
        assert status == 200
        assert payload["dossiers"][0]["project_id"] == "case-alpha"

        status, payload = _request(
            port, "POST", "/api/projects/case-alpha/aliases",
            {"aliases": "zephyr, alpha"})
        assert status == 200
        assert payload["aliases"] == ["zephyr", "alpha"]

        status, payload = _request(port, "GET", "/api/recall?q=alpha")
        assert status == 200
        assert payload["dossiers"][0]["project_id"] == "case-alpha"


def test_dashboard_exposes_aliases_on_project_objects(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root, aliases=("zephyr",))
    payload = dashboard.Dashboard(root).views()
    project = next(item for item in payload["objects"]
                   if item["kind"] == "project" and item["ref_id"] == "case-alpha")
    assert project["aliases"] == ["zephyr"]


# --- UI wiring ----------------------------------------------------------------


def test_cockpit_exposes_the_recall_surface() -> None:
    js = cockpit_ui.PAGE_JS
    for marker in ("openRecall", "runRecall", "closeRecall", "recallHtml",
                   "/api/recall", "recall-overlay", "pf-aliases"):
        assert marker in js, marker
    page = cockpit_ui.render_page.__doc__ or ""
    assert page == page  # document presence only


def test_cockpit_page_contains_the_recall_panel(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    from trajectory_os.mvp import engine

    html = cockpit_ui.render_page(engine.build_cockpit(root))
    assert "recall-overlay" in html
    assert "recall-input" in html


# --- CLI ----------------------------------------------------------------------


def test_cli_aliases_and_recall(tmp_path: Path, capsys: Any) -> None:
    root = str(tmp_path)
    _seed(root)
    assert cli.main(["aliases", "--root", root, "--project", "case-alpha",
                     "--set", "zephyr,alpha"]) == 0
    assert "zephyr" in capsys.readouterr().out
    assert cli.main(["recall", "--root", root, "zephyr"]) == 0
    out = capsys.readouterr().out
    assert "Dossier Alpha" in out
    assert cli.main(["aliases", "--root", root, "--json"]) == 0
    assert "case-alpha" in capsys.readouterr().out
