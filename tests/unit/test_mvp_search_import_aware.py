"""Import-aware Search V1: derived search memory from persisted artifacts.

The cockpit search must retrieve a Project/Task when a term only exists in the
imported source vocabulary linked to it (via ``entity_map``), while staying a
pure, deterministic and fail-soft presentation filter. These tests pin:

* the existing search behaviour and filters semantics are preserved;
* ``source_text`` linked to a project, a task and a ``merged:<project>``
  enriches the right (and only the right) object;
* ``skipped`` / ``area`` and stale references never create fake matches;
* a missing, malformed, incomplete or legacy import history never breaks the
  dashboard;
* several artifacts enrich the same entity without duplicating objects;
* accent-insensitive matching (``sante`` finds ``santé``).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from trajectory_os.mvp import cockpit_ui, model, store, visualization

SOURCE_NAME_A = "a.md"
SOURCE_NAME_B = "b.md"


# --- fixtures -----------------------------------------------------------------


def _seed(root: object) -> None:
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION,
        name="t",
        projects=(
            model.Project(project_id="job-search", name="Recherche emploi",
                          objective="trouver un poste", domain="career"),
            model.Project(project_id="other", name="Autre projet",
                          objective="autre objectif", domain="personal"),
        ),
        tasks=(
            model.Task(task_id="t1", project_id="job-search",
                       title="Relancer le cabinet", estimated_minutes=30),
        ),
    ))


def _candidate(candidate_id: str, *, title: str = "",
               description: str = "", source_text: str = "") -> dict[str, str]:
    return {
        "candidate_id": candidate_id,
        "kind": "PROJECT",
        "title": title,
        "description": description,
        "source_text": source_text,
    }


def _write_artifact(root: object, name: str, *, entity_map: dict[str, str],
                    candidates: list[dict[str, str]],
                    source_name: str | None = None) -> None:
    directory = Path(str(root)) / "imports"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": model.SCHEMA_VERSION,
        "kind": "mvp_import",
        "source_name": source_name or f"{name}.md",
        "imported_at": "2026-01-01T00:00:00Z",
        "engine": "factual",
        "text_chars": 0,
        "candidates": candidates,
        "matches": [],
        "decisions": [],
        "entity_map": entity_map,
    }
    (directory / f"{name}.json").write_text(
        json.dumps(payload), encoding="utf-8")


def _object(views: dict[str, Any], kind: str, ref_id: str) -> dict[str, Any]:
    return views["object_index"][f"{kind}:{ref_id}"]


# --- node helpers -------------------------------------------------------------


def _run_node(tmp_path: Path, body: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    script = tmp_path / "search_memory.js"
    script.write_text(cockpit_ui.TABLE_JS_HELPERS + "\n" + body,
                      encoding="utf-8")
    result = subprocess.run([node, str(script)], capture_output=True,
                            text=True, check=False)
    assert result.returncode == 0, result.stderr


def _assert_js_match(tmp_path: Path, obj: dict[str, Any], query: str,
                     expected: bool = True) -> None:
    body = (
        "const assert=require('assert');\n"
        f"const o={json.dumps(obj, ensure_ascii=False)};\n"
        f"const q={json.dumps(query, ensure_ascii=False)};\n"
        f"assert.strictEqual(searchMatches(o,searchTermsOf(q)),"
        f"{'true' if expected else 'false'});\n"
    )
    _run_node(tmp_path, body)


# --- baseline / fail-soft -----------------------------------------------------


def test_artifact_absent_leaves_objects_unchanged(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    views = visualization.build_views(root)
    assert all("search_text" not in obj for obj in views["objects"])
    assert all("search_sources" not in obj for obj in views["objects"])
    ids = [obj["id"] for obj in views["objects"]]
    assert len(ids) == len(set(ids))
    assert set(views["object_index"]) == set(ids)


def test_malformed_artifacts_never_break_the_dashboard(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    directory = Path(root) / "imports"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "broken.json").write_text("{ not json", encoding="utf-8")
    (directory / "array.json").write_text(json.dumps([1, 2, 3]),
                                          encoding="utf-8")
    (directory / "empty.json").write_text("", encoding="utf-8")
    (directory / "incomplete.json").write_text(
        json.dumps({"kind": "mvp_import", "candidates": "nope"}),
        encoding="utf-8")
    (directory / "legacy.json").write_text(
        json.dumps({"kind": "mvp_import", "candidates": [
            _candidate("c9", source_text="legacy-term")]}),
        encoding="utf-8")
    (directory / "unrelated.json").write_text(
        json.dumps({"kind": "something_else",
                    "entity_map": {"c1": "job-search"}}),
        encoding="utf-8")
    _write_artifact(root, "valid", entity_map={"c1": "job-search"},
                    candidates=[_candidate("c1", source_text="terme-valide")])
    views = visualization.build_views(root)
    project = _object(views, "project", "job-search")
    assert "terme-valide" in project["search_text"]
    assert "legacy-term" not in project.get("search_text", "")


# --- direct / merged enrichment ----------------------------------------------


def test_source_text_linked_to_project_is_searchable(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    _write_artifact(root, "cv", entity_map={"c1": "job-search"},
                    candidates=[_candidate(
                        "c1", title="Préparer le dossier",
                        source_text=("Ramsay Santé candidature Head of AI "
                                     "Engineering"))])
    views = visualization.build_views(root)
    project = _object(views, "project", "job-search")
    assert "Ramsay" in project["search_text"]
    assert "Ramsay" not in project["title"]
    assert project["search_sources"] == ["cv.md"]
    _assert_js_match(tmp_path, project, "Ramsay")
    _assert_js_match(tmp_path, project, "head of ai engineering")


def test_source_text_linked_to_task_is_searchable(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    _write_artifact(root, "notes", entity_map={"c2": "t1"},
                    candidates=[_candidate(
                        "c2", source_text="appeler le cabinet Alpha")])
    views = visualization.build_views(root)
    task = _object(views, "task", "t1")
    assert "Alpha" in task["search_text"]
    assert "Alpha" not in task["title"]
    assert task["search_sources"] == ["notes.md"]
    _assert_js_match(tmp_path, task, "cabinet alpha")


def test_merged_candidate_enriches_target_project(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    _write_artifact(root, "merge", entity_map={"c5": "merged:other"},
                    candidates=[_candidate(
                        "c5", title="licorne violette")])
    views = visualization.build_views(root)
    other = _object(views, "project", "other")
    project = _object(views, "project", "job-search")
    assert "licorne" in other["search_text"]
    assert "search_text" not in project
    _assert_js_match(tmp_path, other, "LICORNE")


def test_skipped_entity_is_ignored(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    _write_artifact(root, "skip", entity_map={"c6": "skipped"},
                    candidates=[_candidate("c6", source_text="secret-zeta")])
    views = visualization.build_views(root)
    assert all("secret-zeta" not in obj.get("search_text", "")
               for obj in views["objects"])
    assert all("search_text" not in obj for obj in views["objects"])


def test_area_entity_is_ignored(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    _write_artifact(root, "area", entity_map={"c7": "area"},
                    candidates=[_candidate("c7", source_text="secret-omega")])
    views = visualization.build_views(root)
    assert all("secret-omega" not in obj.get("search_text", "")
               for obj in views["objects"])


def test_stale_reference_is_ignored(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    _write_artifact(root, "stale", entity_map={"c8": "ghost-project"},
                    candidates=[_candidate("c8", source_text="ghost-term")])
    views = visualization.build_views(root)
    assert all("ghost-term" not in obj.get("search_text", "")
               for obj in views["objects"])


# --- aggregation / dedup ------------------------------------------------------


def test_multiple_artifacts_enrich_the_same_entity(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    _write_artifact(root, "a", source_name=SOURCE_NAME_A,
                    entity_map={"c1": "job-search"},
                    candidates=[_candidate("c1", source_text="alpha-term")])
    _write_artifact(root, "b", source_name=SOURCE_NAME_B,
                    entity_map={"c2": "job-search"},
                    candidates=[_candidate("c2", source_text="beta-term")])
    views = visualization.build_views(root)
    project = _object(views, "project", "job-search")
    assert "alpha-term" in project["search_text"]
    assert "beta-term" in project["search_text"]
    assert project["search_sources"] == [SOURCE_NAME_A, SOURCE_NAME_B]
    _assert_js_match(tmp_path, project, "alpha-term beta-term")


def test_enrichment_does_not_duplicate_objects(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    _write_artifact(root, "dupes", entity_map={"c1": "job-search",
                                               "c2": "job-search"},
                    candidates=[_candidate("c1", source_text="one-term"),
                                _candidate("c2", source_text="two-term")])
    views = visualization.build_views(root)
    ids = [obj["id"] for obj in views["objects"]]
    assert len(ids) == len(set(ids))
    matching = [obj for obj in views["objects"]
                if obj["ref_id"] == "job-search"]
    assert len(matching) == 1


# --- accent normalisation -----------------------------------------------------


def test_source_text_accent_insensitive_search(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed(root)
    _write_artifact(root, "cv", entity_map={"c1": "job-search"},
                    candidates=[_candidate("c1", source_text="Ramsay Santé")])
    views = visualization.build_views(root)
    project = _object(views, "project", "job-search")
    assert "Santé" in project["search_text"]
    _assert_js_match(tmp_path, project, "sante")
    _assert_js_match(tmp_path, project, "SANTÉ")


# --- pure JS helper -----------------------------------------------------------


def test_search_helper_preserves_existing_behaviour(tmp_path: Path) -> None:
    _run_node(tmp_path, r"""
const assert=require('assert');
const o={title:'Update the plans',project:'Garden planning',domain:'career',
  workstream:'positioning',deliverable:'cv',description:'refresh roles',
  objective:'find a role',next_action:'rewrite',status:'TODO',
  evidence:'FACT',ref_id:'update-cv'};
assert.strictEqual(searchMatches(o,searchTermsOf('update')),true);
assert.strictEqual(searchMatches(o,searchTermsOf('GARDEN')),true);
assert.strictEqual(searchMatches(o,searchTermsOf('career rewrite')),true);
assert.strictEqual(searchMatches(o,searchTermsOf('renewal')),false);
assert.strictEqual(searchMatches(o,searchTermsOf('')),true);
assert.strictEqual(searchMatches(null,searchTermsOf('x')),false);
""")


def test_search_helper_matches_derived_search_text(tmp_path: Path) -> None:
    _run_node(tmp_path, r"""
const assert=require('assert');
const o={title:'X',project:'',domain:'',workstream:'',deliverable:'',
  description:'',objective:'',next_action:'',status:'',evidence:'',ref_id:'x',
  search_text:'Ramsay Santé candidature'};
assert.strictEqual(searchMatches(o,searchTermsOf('ramsay')),true);
assert.strictEqual(searchMatches(o,searchTermsOf('ramsay sante')),true);
assert.strictEqual(searchMatches(o,searchTermsOf('ramsay absent')),false);
""")


def test_search_helper_is_accent_insensitive(tmp_path: Path) -> None:
    _run_node(tmp_path, r"""
const assert=require('assert');
const o={title:'Rapport santé',project:'',domain:'',workstream:'',
  deliverable:'',description:'',objective:'',next_action:'',status:'',
  evidence:'',ref_id:'x'};
assert.strictEqual(searchMatches(o,searchTermsOf('sante')),true);
assert.strictEqual(searchMatches(o,searchTermsOf('SANTÉ')),true);
assert.strictEqual(searchMatches(o,searchTermsOf('santos')),false);
""")
