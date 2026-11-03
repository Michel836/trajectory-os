#!/usr/bin/env python3
"""Headless usage simulation of the MVP cockpit — every surface.

What it does, and what it deliberately does not do
-------------------------------------------------
It drives the **real** `PAGE_JS` in node with a permissive fake DOM, against the
**real** `/api/views` and `/api/preferences` payloads of a data root, and then:

1. renders every view and checks that something visible was produced;
2. opens every UI entry surface (capture, forms, import, review, palette,
   recall, estimate, assistant, explain, update, outcome, export) and checks the
   overlay actually opens;
3. chains the gestures a real session performs (filter by alias, open a
   dossier, select, open the detail sheet, colour mode, density, scope, status
   chips, table sort/filter, highlight, saved view, inspector collapse,
   keyboard, WBS collapse, Kanban/Today sort, view switching);
4. sweeps every HTTP route of `mvp.dashboard`, GET and POST/DELETE.

It is a **screen-level** simulation: "did the shipped code run without throwing
and produce visible output?". It is not a browser, it does not render pixels, and
it does not claim to replace a human looking at the screen.

Safety
------
The source data root is never written to. Everything runs on a temporary copy,
and the copy is deleted at the end unless `--keep` is passed.

Usage
-----
    python scripts/mvp_ui_simulation.py                     # local_data/mvp
    python scripts/mvp_ui_simulation.py --root local_data/mvp
    python scripts/mvp_ui_simulation.py --json              # machine output
    python scripts/mvp_ui_simulation.py --skip-api          # UI only

Requirements: node on PATH (UI part), the project venv for the server.
Exit code: 0 = no anomaly, 1 = anomalies, 2 = setup problem.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests" / "unit"))

# --- fake DOM ---------------------------------------------------------------
#
# Reused from the cockpit UI test suite so the simulation and the unit tests
# cannot drift apart. It provides just enough of the browser for the page code
# to run: elements, class lists, listeners, localStorage, fetch.
from test_mvp_duplicate_ui import _STUBS  # noqa: E402

_EXTRA_STUBS = r"""
if (!global.getComputedStyle) global.getComputedStyle = function(){
  return {getPropertyValue: function(){return '';}}; };
document.createElement = function(tag){
  const node = fakeEl('created:' + tag);
  node.tagName = String(tag).toUpperCase();
  node.appendChild = function(){ return node; };
  node.setAttribute = function(k, v){ this[k] = v; };
  node.querySelectorAll = function(){ return []; };
  node.getElementsByTagName = function(){ return []; };
  return node;
};
document.body.appendChild = function(){};
window.matchMedia = function(){
  return {matches: false, addEventListener: function(){}};
};
if (!window.location) window.location = {hash: ''};
else if (window.location.hash === undefined) window.location.hash = '';
// A real element supports these; the minimal stub does not.
const _getById = document.getElementById.bind(document);
document.getElementById = function(id){
  const node = _getById(id);
  if (node) {
    if (!node.focus) node.focus = function(){};
    if (!node.blur) node.blur = function(){};
    if (!node.select) node.select = function(){};
    if (!node.click) node.click = function(){};
    if (!node.remove) node.remove = function(){};
    if (!node.scrollIntoView) node.scrollIntoView = function(){};
    if (!node.getAttribute) node.getAttribute = function(){ return null; };
    if (!node.removeAttribute) node.removeAttribute = function(){};
    if (!node.files) node.files = [];
  }
  return node;
};
window.scrollTo = function(){};
window.prompt = function(){ return 'Vue simulation'; };
window.confirm = function(){ return true; };
// A browser resolves these against the rendered DOM. The stub parses the
// admin-rendered markup for the one selector shape the page uses on inputs.
document.querySelectorAll = function(selectors){
  const match = /input\[name="([^"]+)"\]/.exec(String(selectors));
  if (!match) return [];
  const out = [];
  Object.keys(els).forEach(function(id){
    const html = els[id].innerHTML || '';
    const pattern = new RegExp('<input[^>]*name="' + match[1] + '"[^>]*>', 'g');
    let found;
    while ((found = pattern.exec(html))) {
      const tag = found[0];
      const value = (/value="([^"]*)"/.exec(tag) || [])[1] || '';
      out.push({name: match[1], value: value, checked: /checked/.test(tag)});
    }
  });
  return out;
};
"""

_SIM = r"""
const fs = require('fs');
const views = JSON.parse(fs.readFileSync(process.env.VIEWS, 'utf8'));
const prefs = JSON.parse(fs.readFileSync(process.env.PREFS, 'utf8'));
const RESPONSES = JSON.parse(fs.readFileSync(process.env.RESPONSES, 'utf8'));
global.fetch = function(url, opts){
  const method = ((opts && opts.method) || 'GET').toUpperCase();
  const key = String(url);
  const path = key.split('?')[0];
  let body = RESPONSES[key];
  if (body === undefined) body = RESPONSES[path];
  if (body === undefined) body = {};
  return Promise.resolve({ok: true, status: 200,
    json: function(){ return Promise.resolve(body); }});
};
const ALIAS = process.env.ALIAS;
const DOSSIER_ID = process.env.DOSSIER_ID;
const DOSSIER_TITLE = process.env.DOSSIER_TITLE;
const PROJECT_OBJ = process.env.PROJECT_OBJ;
const TASK_OBJ = process.env.TASK_OBJ;

const SIM_VIEWS = ['today','inbox','waiting','decisions','ready','tomorrow',
  'week','list','kanban','wbs','graph','gantt','eisenhower','impact_effort',
  'portfolio_map','treemap','progress','heatmap','goal_flow','changes','debt',
  'trash','signals','project_review'];

const results = {views: [], entries: [], interactions: [], errors: []};
function el(id){ return $(id); }
function tick(){ return new Promise(function(r){ setTimeout(r, 0); }); }
function snapshot(){
  return {root: el('view-root').innerHTML,
          inspector: el('inspector-body').innerHTML,
          controls: el('controls').innerHTML};
}
function visible(html){
  var text = String(html || '');
  return text.replace(/\s+/g, '').length >= 40 &&
         text.indexOf('loading…') < 0;
}
function record(bucket, label, fn, note){
  var err = '', out = null, ok = false, detail = '';
  try { fn(); } catch (e) { err = (e && e.message) || String(e); }
  try { out = snapshot(); } catch (e) { detail = 'snapshot:' + (e && e.message); }
  if (out) {
    ok = visible(out.root);
    detail = 'vue=' + out.root.length + ' insp=' + out.inspector.length;
  }
  if (err) { results.errors.push(label + ' → ' + err); ok = false; }
  else if (!ok) { results.errors.push(label + ' → aucun rendu visible'); }
  var row = {label: label, error: err, visible: ok, detail: (note || '') + detail};
  results[bucket].push(row);
  return row;
}

(async function(){
  // Boot exactly like a page load: the real boot() -> loadEngineCatalog() ->
  // refresh() path, served from pre-fetched real responses.
  await boot();
  await tick();
  if (!S.views) throw new Error('boot() n\'a pas chargé les vues');

  // 1. every view
  SIM_VIEWS.forEach(function(v){
    record('views', 'vue:' + v, function(){ setView(v); });
  });

  // 2. every entry surface
  const ENTRIES = [
    ['openCapture', 'capture-overlay', function(){ openCapture(); }],
    ['openTaskForm', 'task-form-overlay', function(){ openTaskForm(null); }],
    ['openProjectForm', 'project-form-overlay', function(){ openProjectForm(null); }],
    ['openImport', 'import-overlay', function(){ openImport(); }],
    ['openReview', 'review-overlay', function(){ openReview(); }],
    ['openHelp', 'help-overlay', function(){ openHelp(); }],
    ['openCommandPalette', 'command-overlay', function(){ openCommandPalette(); }],
    ['openRecall', 'recall-overlay', function(){ openRecall(ALIAS); }],
    ['openEstimateModal', 'estimate-overlay', function(){ openEstimateModal([], []); }],
    ['openAssistantWeekly', 'assistant-overlay', function(){ openAssistantWeekly(); }],
    ['openExplain', 'explain-overlay', function(){ explainProject(DOSSIER_ID); }],
    ['openProjectUpdate', 'update-overlay', function(){ openProjectUpdate(DOSSIER_ID); }],
    ['openOutcomeForm', 'outcome-overlay', function(){ openOutcomeForm(process.env.TASK_ID); }],
    ['openExportPreview', 'export-overlay', function(){ openExportPreview(); }],
    ['openObject', 'open-overlay', function(){
       selectObject(PROJECT_OBJ); openObject(PROJECT_OBJ); }]
  ];
  for (const entry of ENTRIES) {
    var name = entry[0], overlayId = entry[1], fn = entry[2];
    setView('today');
    var row = record('entries', 'entrée:' + name, fn);
    await tick(); await tick();
    var ov = el(overlayId);
    var open = !!(ov && ov.classList && ov.classList.contains('open'));
    row.detail = 'overlay ' + overlayId + (open ? ' ouvert' : ' FERMÉ');
    if (!row.error && !open) {
      row.visible = false;
      results.errors.push('entrée:' + name + ' → ' + overlayId + ' ne s\'ouvre pas');
    }
    try { el(overlayId).classList.remove('open'); } catch (e) {}
  }

  // 3. chained gestures
  function use(label, fn){ record('interactions', 'geste:' + label, fn); }

  use('filtrer la vue avec l\'alias déclaré', function(){
    setView('list'); searchInput(ALIAS);
    if (el('view-root').innerHTML.indexOf(DOSSIER_TITLE.slice(0, 18)) < 0)
      throw new Error('le dossier n\'apparaît pas dans le filtre');
  });
  use('vider le filtre', function(){ searchInput(''); setView('list'); });
  use('ouvrir le dossier depuis le panneau', function(){
    openRecall(ALIAS); recallOpen(DOSSIER_ID);
    if (S.view !== 'list') throw new Error('vue=' + S.view);
    if (el('inspector-body').innerHTML.indexOf(DOSSIER_TITLE.slice(0, 18)) < 0)
      throw new Error('Inspector vide');
  });
  use('sélectionner une tâche', function(){
    setView('list'); selectObject(TASK_OBJ);
    if (el('inspector-body').innerHTML.length < 200) throw new Error('Inspector vide');
  });
  use('ouvrir la fiche détaillée', function(){
    selectObject(PROJECT_OBJ); openObject(PROJECT_OBJ);
    if (el('open-body').innerHTML.length < 50) throw new Error('fiche vide');
    closeObjectDetail();
  });
  use('changer le mode couleur', function(){
    el('f-colour').value = 'status'; colourChange();
    if (S.prefs.color_by !== 'status') throw new Error('color_by=' + S.prefs.color_by);
    el('f-colour').value = 'domain'; colourChange();
  });
  use('changer la densité', function(){
    el('f-density').value = 'compact'; densityChange();
    el('f-density').value = 'detailed'; densityChange();
    if (!visible(el('view-root').innerHTML)) throw new Error('vue vide');
  });
  use('changer le périmètre et le niveau', function(){
    el('f-scope').value = 'all'; scopeChange();
    el('f-level').value = 'all'; levelChange();
    if (!visible(el('view-root').innerHTML)) throw new Error('vue vide');
  });
  use('filtrer par statut (chip)', function(){
    setView('list'); chipToggle('status', 'TODO'); chipToggle('status', 'TODO');
    if (!visible(el('view-root').innerHTML)) throw new Error('vue vide');
  });
  use('trier une colonne de tableau', function(){
    setView('list');
    tableHeaderClick('tasks', 'title', {shiftKey: false});
    tableClearSort('tasks');
  });
  use('filtrer une colonne de tableau', function(){
    tableTextFilter('tasks', 'title', 'a');
    tableClearFilters('tasks');
  });
  use('surligner un objet', function(){
    setHighlight(PROJECT_OBJ, 'purple'); setHighlight(PROJECT_OBJ, '');
  });
  use('sauvegarder une vue', function(){ saveCurrentView(); });
  use('replier puis déplier l\'Inspector', function(){
    toggleInspectorCollapsed(true);
    if (S.inspectorCollapsed !== true) throw new Error('pas replié');
    toggleInspectorCollapsed(false);
  });
  use('palette + clavier', function(){
    var ev = function(k){ return {key: k, preventDefault: function(){},
      ctrlKey: false, metaKey: false, altKey: false, target: {value: ''}}; };
    openCommandPalette(); commandInput(ALIAS);
    if (el('command-results').innerHTML.length < 10)
      throw new Error('palette vide');
    commandKey(ev('Escape'));
    openHelp(); closeHelp();
  });
  use('déplier le WBS', function(){
    setView('wbs');
    var k = Object.keys(S.collapsed)[0];
    if (k) { toggleWbs(k); toggleWbs(k); }
  });
  use('trier Kanban puis Today', function(){
    setView('kanban'); kanbanSortChange('priority');
    setView('today'); todaySortChange('effort');
    if (!visible(el('view-root').innerHTML)) throw new Error('vue vide');
  });
  use('carte portefeuille et signaux', function(){
    setView('portfolio_map'); treemapClick('domain');
    setView('signals'); signalFilterChange('pending');
    if (!visible(el('view-root').innerHTML)) throw new Error('vue vide');
  });
  use('coller un message', function(){
    setView('signals');
    el('signal-text').value = 'Mollie — AML/BioVerify\n\nDear Mollie, ' +
      'kind regards — ' + DOSSIER_TITLE;
    el('signal-source').value = 'email:simulation';
    el('signal-project').value = '';
    analyzeSignal();
  });
  await tick(); await tick(); await tick();
  use('l\'aperçu propose le dossier et sa raison', function(){
    var html = el('intake-preview').innerHTML;
    if (html.indexOf(DOSSIER_TITLE.slice(0, 18)) < 0)
      throw new Error('aucun dossier proposé dans l\'aperçu');
    if (html.indexOf('Intégrer au dossier') < 0)
      throw new Error('pas de bouton de validation');
    if (html.indexOf('name="intake-project"') < 0)
      throw new Error('pas de choix de dossier');
  });
  use('choisir le dossier puis intégrer', function(){
    var radios = document.querySelectorAll('input[name="intake-project"]');
    if (!radios.length) throw new Error('aucun candidat à choisir');
    var target = null;
    for (var i = 0; i < radios.length; i++) {
      if (radios[i].value === DOSSIER_ID) target = radios[i];
    }
    if (!target) throw new Error('le dossier attendu n\'est pas candidat');
    return confirmIntake();
  });
  await tick(); await tick(); await tick();
  use('capture en aperçu', function(){
    setView('today'); openCapture();
    el('capture-text').value = 'Préparer le dossier pour la revue';
    try { capturePreview(); } catch (e) {}
    closeCapture();
  });

  console.log('###SIM###' + JSON.stringify(results));
})().catch(function(e){
  console.log('###SIM###' + JSON.stringify({views: [], entries: [], interactions: [],
    errors: ['fatal: ' + ((e && e.stack) || String(e))]}));
});
"""

# --- API sweep ----------------------------------------------------------------
#
# (method, path, body, expectation)
# expect "ok": must be 2xx. "either": 2xx or a fail-closed 4xx is acceptable for
# a route whose exact contract depends on data the sweep cannot invent; a 5xx is
# always a failure.
_API_PLAN: list[tuple[str, str, dict[str, Any] | None, str]] = [
    # read projections
    ("GET", "/healthz", None, "ok"),
    ("GET", "/", None, "ok"),
    ("GET", "/api/cockpit", None, "ok"),
    ("GET", "/api/portfolio", None, "ok"),
    ("GET", "/api/views", None, "ok"),
    ("GET", "/api/planning", None, "ok"),
    ("GET", "/api/tomorrow", None, "ok"),
    ("GET", "/api/week", None, "ok"),
    ("GET", "/api/review", None, "ok"),
    ("GET", "/api/changes?limit=5", None, "ok"),
    ("GET", "/api/trash", None, "ok"),
    ("GET", "/api/backups", None, "ok"),
    ("GET", "/api/projects/review", None, "ok"),
    ("GET", "/api/signals", None, "ok"),
    ("GET", "/api/search?q=alvis", None, "ok"),
    ("GET", "/api/recall?q=alvis", None, "ok"),
    ("GET", "/api/preferences", None, "ok"),
    ("GET", "/api/saved-views", None, "ok"),
    ("GET", "/api/import/engines", None, "ok"),
    ("GET", "/api/import/drafts", None, "ok"),
    ("GET", "/api/export/superproductivity", None, "ok"),
    # per-object projections
    ("GET", "/api/projects/{project}/timeline", None, "ok"),
    ("GET", "/api/projects/{project}/explain", None, "ok"),
    ("GET", "/api/enrichment/{project}", None, "ok"),
    # writes (on the temporary copy)
    ("POST", "/api/projects/{project}/aliases", {"aliases": "alvis, orc"}, "ok"),
    ("POST", "/api/projects/{project}", {"description": "simulation"}, "ok"),
    ("POST", "/api/projects/{project}/update", {"note": "simulation"}, "either"),
    ("POST", "/api/projects/{project}/review", {"decision": "KEEP"}, "either"),
    ("POST", "/api/projects/{project}/park", {"kind": "IDEA"}, "ok"),
    ("POST", "/api/projects/{project}/park", {"status": "ACTIVE"}, "ok"),
    ("POST", "/api/projects/rederive", {}, "ok"),
    ("POST", "/api/tasks/{task}", {"next_action": "simulation"}, "ok"),
    ("POST", "/api/tasks/{task}/outcome",
     {"outcome": "COMPLETED", "minutes": 5}, "ok"),
    ("POST", "/api/tasks/{task}/outcome/undo", {}, "ok"),
    ("POST", "/api/tasks/{task}/calibrate", {}, "either"),
    # a dependency that would close a cycle must be refused, never accepted
    ("POST", "/api/tasks/{task}/dependency", {"dependency": "{other_task}"},
     "either"),
    ("DELETE", "/api/tasks/{task}/dependency/{other_task}", None, "ok"),
    ("POST", "/api/tasks/estimate", {"updates": []}, "ok"),
    ("POST", "/api/capture", {"text": "Une ligne de plus"}, "ok"),
    ("POST", "/api/preferences", {"density": "normal"}, "ok"),
    ("POST", "/api/highlight", {"object_id": "{project_obj}", "colour": "yellow"}, "ok"),
    ("POST", "/api/colour", {"scope": "project", "key": "{project}"}, "ok"),
    ("POST", "/api/saved-views", {"name": "simulation"}, "ok"),
    ("POST", "/api/saved-views/load", {"name": "simulation"}, "ok"),
    ("POST", "/api/saved-views/rename",
     {"name": "simulation", "new_name": "simulation2"}, "ok"),
    ("POST", "/api/saved-views/delete", {"name": "simulation2"}, "ok"),
    ("POST", "/api/backups", {}, "ok"),
    ("POST", "/api/decisions", {"title": "Décision simulation"}, "ok"),
    ("POST", "/api/events",
     {"title": "Événement simulation", "day": "{today}",
      "start_minutes": 600, "end_minutes": 660}, "ok"),
    ("POST", "/api/calendar/day-off", {"day": "{today}", "off": True}, "ok"),
    ("POST", "/api/routines/materialize", {"day": "{today}"}, "ok"),
    ("POST", "/api/tasks", {"title": "Tâche simulation",
                            "project_id": "{project}"}, "ok"),
    ("POST", "/api/projects", {"name": "Projet simulation",
                               "objective": "objectif"}, "ok"),
    ("POST", "/api/signals", {"text": "Le cabinet a envoyé les pièces."}, "ok"),
    ("POST", "/api/assistant/weekly", {}, "ok"),
    ("POST", "/api/assistant/project", {"project_id": "{project}"}, "ok"),
]


def _copy_root(src: Path) -> Path:
    dst = Path(tempfile.mkdtemp(prefix="tos-sim-")) / "mvp"
    shutil.copytree(src, dst,
                    ignore=shutil.ignore_patterns(".portfolio.lock", "*.lock"))
    return dst


def _start(root: Path) -> tuple[Any, int, threading.Thread]:
    from trajectory_os.mvp import dashboard

    app = dashboard.Dashboard(str(root))
    server = dashboard.create_server(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1], thread


def _call(port: int, method: str, path: str,
          body: dict[str, Any] | None) -> tuple[int, Any]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    payload = json.dumps(body) if body is not None else None
    headers = {"Content-Type": "application/json"} if payload else {}
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return resp.status, raw[:200].decode("utf-8", "replace")


def _response_map(port: int, project: str, alias: str,
                  dossier_name: str) -> dict[str, Any]:
    """Pre-fetch every GET payload the page asks for during a session.

    A browser would call these; the simulation serves them from this map so the
    shipped `boot()` / `refresh()` / handlers run unmodified.
    """
    paths = [
        "/api/views", "/api/cockpit", "/api/portfolio", "/api/preferences",
        "/api/import/engines", "/api/import/drafts", "/api/trash",
        "/api/backups", "/api/changes", "/api/signals",
        "/api/projects/review", "/api/saved-views", "/api/planning",
        "/api/tomorrow", "/api/week", "/api/review",
        "/api/export/superproductivity",
        f"/api/projects/{project}/explain",
        f"/api/projects/{project}/timeline",
        f"/api/enrichment/{project}",
        f"/api/search?q={alias}&limit=20",
        f"/api/search?q={alias}",
        f"/api/recall?q={alias}&limit=3",
        f"/api/recall?q={alias}",
        "/api/intake",
    ]
    responses: dict[str, Any] = {}
    for path in paths:
        status, payload = _call(port, "GET", path, None)
        responses[path] = payload if 200 <= status < 300 else {}
    # POST endpoints the UI calls during the simulated session: the dry intake
    # must answer, the confirm must succeed without touching the source root.
    # The canned intake answer must propose the reference dossier, so the text
    # used to build it names that dossier (the router needs the brand token).
    intake_text = "Message de simulation\n\n" + dossier_name
    status, payload = _call(port, "POST", "/api/intake",
                            {"text": intake_text,
                             "source_ref": "email:simulation"})
    responses["/api/intake"] = payload if 200 <= status < 300 else {}
    return responses


def _ui_simulation(port: int, aliases: list[str]) -> dict[str, Any]:
    from trajectory_os.mvp import cockpit_ui

    status, views = _call(port, "GET", "/api/views", None)
    if status != 200:
        return {"errors": [f"/api/views → {status}"], "views": [],
                "entries": [], "interactions": []}
    status, prefs_doc = _call(port, "GET", "/api/preferences", None)
    status, portfolio = _call(port, "GET", "/api/portfolio", None)
    projects = portfolio["projects"]
    tasks = portfolio["tasks"]
    dossier = next((p for p in projects if p.get("aliases")), None)
    if dossier is None:
        return {"errors": [
            "aucun projet n'a d'alias déclaré : la simulation ne peut pas "
            "couvrir le chemin de reprise. Déclarer un alias d'abord."],
            "views": [], "entries": [], "interactions": []}
    alias = dossier["aliases"][0]
    project = projects[0]
    task = tasks[0]

    responses = _response_map(port, project["project_id"], alias,
                              dossier["name"])
    work = Path(tempfile.mkdtemp(prefix="tos-sim-js-"))
    (work / "views.json").write_text(json.dumps(views), encoding="utf-8")
    (work / "prefs.json").write_text(
        json.dumps(prefs_doc["preferences"]), encoding="utf-8")
    (work / "responses.json").write_text(json.dumps(responses),
                                        encoding="utf-8")
    script = work / "sim.js"
    script.write_text(
        _STUBS + "\n" + _EXTRA_STUBS + "\n" + cockpit_ui.PAGE_JS + "\n" + _SIM,
        encoding="utf-8")
    env = dict(
        os.environ,
        VIEWS=str(work / "views.json"),
        PREFS=str(work / "prefs.json"),
        RESPONSES=str(work / "responses.json"),
        ALIAS=alias,
        DOSSIER_ID=dossier["project_id"],
        DOSSIER_TITLE=dossier["name"],
        PROJECT_OBJ="project:" + project["project_id"],
        TASK_OBJ="task:" + task["task_id"],
        TASK_ID=task["task_id"],
    )
    node = shutil.which("node")
    if node is None:
        return {"errors": ["node introuvable : simulation UI impossible"],
                "views": [], "entries": [], "interactions": []}
    proc = subprocess.run([node, str(script)], capture_output=True, text=True,
                          env=env)
    for line in proc.stdout.splitlines():
        if line.startswith("###SIM###"):
            data = json.loads(line[len("###SIM###"):])
            data["reference"] = {"alias": alias, "dossier": dossier["name"],
                                 "project": project["project_id"],
                                 "task": task["task_id"]}
            return data
    return {"errors": ["simulation node sans résultat: "
                       + (proc.stderr or proc.stdout)[-600:]],
            "views": [], "entries": [], "interactions": []}


def _api_sweep(port: int) -> list[dict[str, Any]]:
    status, portfolio = _call(port, "GET", "/api/portfolio", None)
    projects = portfolio["projects"]
    tasks = portfolio["tasks"]
    project = projects[0]["project_id"]
    task = tasks[0]["task_id"]
    other = tasks[1]["task_id"] if len(tasks) > 1 else task
    from datetime import date

    today = date.today().isoformat()
    subs = {"{project}": project, "{task}": task, "{other_task}": other,
            "{project_obj}": "project:" + project, "{today}": today}
    rows: list[dict[str, Any]] = []
    for method, path, body, expect in _API_PLAN:
        real_path = path
        for key, value in subs.items():
            real_path = real_path.replace(key, str(value))
        real_body = body
        if body is not None:
            text = json.dumps(body)
            for key, value in subs.items():
                text = text.replace(key, str(value))
            real_body = json.loads(text)
        try:
            status_code, payload = _call(port, method, real_path, real_body)
        except Exception as exc:  # pragma: no cover - transport failure
            rows.append({"route": f"{method} {real_path}", "status": 0,
                         "ok": False, "detail": repr(exc)})
            continue
        error = payload.get("error") if isinstance(payload, dict) else None
        ok = 200 <= status_code < 300
        if not ok and expect == "either" and 400 <= status_code < 500:
            ok = True
            detail = f"{status_code} refus explicite: {error}"
        else:
            detail = f"{status_code}"
            if not ok and error:
                detail += f" · {error}"
        rows.append({"route": f"{method} {real_path}", "status": status_code,
                     "ok": ok, "detail": detail})
    return rows


def _verdict(ui: dict[str, Any], api: list[dict[str, Any]]) -> tuple[int, int]:
    bad = 0
    for bucket in ("views", "entries", "interactions"):
        for row in ui.get(bucket, []):
            if not row.get("visible") or row.get("error"):
                bad += 1
    bad += len(ui.get("errors") or [])
    bad += sum(0 if row["ok"] else 1 for row in api)
    total = (len(ui.get("views", [])) + len(ui.get("entries", []))
             + len(ui.get("interactions", [])) + len(api))
    return bad, total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Simulation d'usage sans navigateur du cockpit MVP.")
    parser.add_argument("--root", default="local_data/mvp",
                        help="data root à simuler (jamais modifié)")
    parser.add_argument("--json", action="store_true",
                        help="sortie machine")
    parser.add_argument("--skip-api", action="store_true",
                        help="ne pas balayer les routes HTTP")
    parser.add_argument("--skip-ui", action="store_true",
                        help="ne pas simuler l'interface")
    parser.add_argument("--keep", action="store_true",
                        help="conserver la copie temporaire pour inspection")
    args = parser.parse_args(argv)

    source = Path(args.root)
    if not (source / "portfolio.json").is_file():
        print(f"aucun portefeuille dans {source}", file=sys.stderr)
        return 2

    try:
        from trajectory_os.mvp import store  # noqa: F401
    except Exception:  # pragma: no cover - setup failure
        print("impossible d'importer trajectory_os.mvp ; lancer depuis le "
              "dépôt avec le venv du projet", file=sys.stderr)
        traceback.print_exc()
        return 2

    copy = _copy_root(source)
    server, port, thread = _start(copy)
    try:
        ui: dict[str, Any] = {"views": [], "entries": [], "interactions": [],
                              "errors": []}
        if not args.skip_ui:
            ui = _ui_simulation(port, [])
        api = [] if args.skip_api else _api_sweep(port)
        bad, total = _verdict(ui, api)

        if args.json:
            print(json.dumps({"ui": ui, "api": api, "anomalies": bad,
                              "surfaces": total}, ensure_ascii=False, indent=2))
            return 0 if bad == 0 else 1

        bar = "=" * 78
        ref = ui.get("reference") or {}
        print(bar)
        print("SIMULATION D'USAGE — cockpit MVP")
        print(f"source : {source} (copiée, jamais modifiée)")
        if ref:
            print(f"référence : {ref['dossier']} · alias « {ref['alias']} » · "
                  f"projet {ref['project']} · tâche {ref['task']}")
        print(bar)
        for title, bucket in (("1. VUES", "views"),
                              ("2. POINTS D'ENTRÉE", "entries"),
                              ("3. GESTES ENCHAÎNÉS", "interactions")):
            rows = ui.get(bucket) or []
            if not rows:
                continue
            print(f"\n{title}")
            for row in rows:
                ok = row.get("visible") and not row.get("error")
                detail = row.get("error") or row.get("detail") or ""
                print(f"  {'OK  ' if ok else 'ÉCHEC'} {row['label']:<44} {detail}")
        if api:
            print("\n4. ROUTES HTTP")
            for row in api:
                print(f"  {'OK  ' if row['ok'] else 'ÉCHEC'} "
                      f"{row['route']:<58} {row['detail']}")
        if ui.get("errors"):
            print("\nANOMALIES")
            for err in ui["errors"]:
                print(f"  - {err}")
        print(f"\nBILAN : {bad} anomalie(s) sur {total} surface(s) testée(s)")
        return 0 if bad == 0 else 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if not args.keep:
            shutil.rmtree(copy.parent, ignore_errors=True)
        else:
            print(f"copie conservée : {copy}")


if __name__ == "__main__":
    sys.exit(main())
