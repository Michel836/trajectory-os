"""Focused tests for the List / Inspector / Edit workspace ergonomics.

This bundle hardens the dense desktop workspace added on top of the cockpit:

* a single click inspects, a double click opens a read-only detail
  (and never the reverse);
* the Inspector is a resizable, collapsible pane whose width is persisted in
  ``localStorage`` (presentation only, never business data);
* Edit reuses the Inspector as a real working surface instead of a fixed
  modal, keeps every existing field and still writes through the validated
  ``/api/projects`` / ``/api/tasks`` endpoints;
* when search or a global filter hides the inspected object the selection is
  deterministically dropped so the Inspector never shows a stale object.

The pure width/selection helpers run in Node exactly like the table helpers;
the wiring is pinned against the rendered page.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from trajectory_os.mvp import cockpit_ui, model, store


def _node_assert(tmp_path: Path, body: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    script = tmp_path / "workspace_helpers.js"
    script.write_text(cockpit_ui.TABLE_JS_HELPERS + "\n" + body,
                      encoding="utf-8")
    result = subprocess.run([node, str(script)], capture_output=True,
                            text=True, check=False)
    assert result.returncode == 0, result.stderr


def _js_block(source: str, name: str) -> str:
    """Return the source of a top-level ``function name(...) {...}`` body."""
    start = source.index("function " + name + "(")
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError("unbalanced function " + name)


# --- pure helpers (Node) ------------------------------------------------------


def test_inspector_clamp_width_respects_min_max_and_default(
        tmp_path: Path) -> None:
    _node_assert(tmp_path, r"""
const assert=require('assert');
// comfortable default is preserved when the value is missing/NaN
assert.strictEqual(inspectorClampWidth(undefined,1400),420);
assert.strictEqual(inspectorClampWidth(NaN,1400),420);
// below the minimum is clamped to the readable minimum
assert.strictEqual(inspectorClampWidth(100,1400),320);
// a normal value passes through
assert.strictEqual(inspectorClampWidth(450,1400),450);
// above the maximum never makes the left workspace unusable (70%)
assert.strictEqual(inspectorClampWidth(5000,1400),980);
assert.deepStrictEqual(inspectorBounds(1400),{min:320,max:980});
// fail-safe on degenerate widths
assert.deepStrictEqual(inspectorBounds(0),{min:320,max:320});
assert.strictEqual(inspectorClampWidth(900,0),320);
""")


def test_reconcile_selection_id_is_deterministic(tmp_path: Path) -> None:
    _node_assert(tmp_path, r"""
const assert=require('assert');
const visible=['task:a','task:b'];
// present -> kept
assert.strictEqual(reconcileSelectionId('task:a',visible),'task:a');
// hidden by search/filter -> dropped
assert.strictEqual(reconcileSelectionId('task:z',visible),null);
// zero result -> dropped
assert.strictEqual(reconcileSelectionId('task:a',[]),null);
// nothing selected stays nothing
assert.strictEqual(reconcileSelectionId(null,visible),null);
assert.strictEqual(reconcileSelectionId('',visible),null);
""")


# --- rendered page wiring -----------------------------------------------------


def _seed(root: str) -> None:
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=(
            model.Project(project_id="p1", name="P1", objective="objective one",
                          domain="career"),
        ),
        tasks=(
            model.Task(task_id="a", project_id="p1", title="Alpha task",
                       estimated_minutes=30),
        ),
    ))


def _page() -> str:
    from trajectory_os.mvp.render import render_html

    class _Cockpit:
        today = "2026-01-01"
        generated_at = "2026-01-01T00:00:00"

    return render_html(_Cockpit())  # type: ignore[arg-type]


def test_inspector_workspace_ui_present() -> None:
    html = _page()
    for marker in (
        "id='inspector-splitter'",
        "id='inspector-body'",
        "id='inspector-collapse-btn'",
        "id='inspector-toggle'",
        "toggleInspectorCollapsed",
        "setInspectorWidth",
        "applyInspectorLayout",
        "initInspectorSplitter",
        "INSPECTOR_LS_KEY",
        "window.localStorage",
    ):
        assert marker in html, marker
    # the splitter is a native, dependency-free pointer surface
    assert "pointerdown" in cockpit_ui.PAGE_JS
    assert "col-resize" in cockpit_ui.PAGE_CSS


def test_click_inspects_and_double_click_opens() -> None:
    row = _js_block(cockpit_ui.PAGE_JS, "tableRowHtml")
    # a single click is strictly an inspection
    assert "onclick=\"selectObject" in row
    # the row exposes the explicit DOM open contract; the central listener
    # (not an inline ondblclick) turns a repeat click into OPEN
    assert "data-object-id" in row
    assert "ondblclick" not in row
    assert "openObject" in cockpit_ui.PAGE_JS
    assert "initObjectOpen" in cockpit_ui.PAGE_JS
    assert "isRepeatClick" in cockpit_ui.TABLE_JS_HELPERS
    # keyboard access to the same inspection
    assert "tableRowKey" in cockpit_ui.PAGE_JS
    assert "tabindex" in row


def test_edit_reuses_inspector_not_the_removed_modal() -> None:
    html = _page()
    # the old cramped task edit modal is gone; creation modal stays
    assert "id='task-overlay'" not in html
    assert "id='task-form-overlay'" in html
    # edit renders inside the inspector pane
    assert "renderInspectorEdit" in cockpit_ui.PAGE_JS
    assert "$('inspector-body')" in cockpit_ui.PAGE_JS


def test_edit_form_keeps_all_existing_fields() -> None:
    source = cockpit_ui.PAGE_JS
    for field in (
        "pf-id", "pf-name", "pf-objective", "pf-status", "pf-domain",
        "pf-urgency", "pf-impact", "pf-deadline", "pf-description",
        "tk-title", "tk-status", "tk-workstream", "tk-deliverable",
        "tk-est", "tk-actual", "tk-deadline", "tk-urgency", "tk-impact",
        "tk-next", "tk-desc", "tk-waiting", "tk-blocked", "tk-deps",
    ):
        assert ("id=\"" + field + "\"" in source) or (
            "editSelect('" + field + "'" in source), field
    # grouped layout, not a flat label/input succession
    for marker in ("editSection", "form-grid", "Identity", "Execution",
                   "Structure", "Provenance", "Save changes"):
        assert marker in source, marker


def test_cancel_never_saves_and_save_uses_the_existing_paths() -> None:
    cancel = _js_block(cockpit_ui.PAGE_JS, "cancelInspectorEdit")
    assert "S.inspectorEdit=null" in cancel
    assert "api(" not in cancel
    save_task = _js_block(cockpit_ui.PAGE_JS, "saveTask")
    assert "/api/tasks/" in save_task
    save_project = _js_block(cockpit_ui.PAGE_JS, "saveProject")
    assert "/api/projects/" in save_project


def test_selection_is_reconciled_on_search_and_filters() -> None:
    assert "reconcileSelectionId" in cockpit_ui.TABLE_JS_HELPERS
    for fn in ("searchInput", "chipToggle", "scopeChange", "levelChange",
               "tableTextFilter", "tableRangeFilter", "tableSelectFilter"):
        assert "reconcile" in _js_block(cockpit_ui.PAGE_JS, fn), fn
    reconcile = _js_block(cockpit_ui.PAGE_JS, "reconcileSelection")
    assert "reconcileSelectionId" in reconcile
    assert "S.inspectorEdit=null" in reconcile
    table_reconcile = _js_block(cockpit_ui.PAGE_JS,
                                "reconcileTableSelection")
    assert "tableRows(kind)" in table_reconcile
    assert "S.inspectorEdit=null" in table_reconcile


def test_workspace_resize_does_not_lose_view_functions() -> None:
    # The workspace only changes presentation; every existing view survives.
    for marker in ("renderList", "renderKanban", "renderWbs", "renderGraph",
                   "renderGantt", "renderInspector", "renderToday",
                   "renderReady", "renderTomorrow", "renderWeek"):
        assert marker in cockpit_ui.PAGE_JS, marker
    # table-local filters still only refresh the table body
    assert "renderTableBody" in cockpit_ui.PAGE_JS


# --- behavioural smoke test with a minimal DOM stub --------------------------

_STUBS = r"""'use strict';
function fakeClassList(){const set={};return {
  add:function(c){set[c]=true;},remove:function(c){delete set[c];},
  toggle:function(c,on){if(on===undefined)on=!set[c];
    if(on)set[c]=true;else delete set[c];return on;},
  contains:function(c){return !!set[c];}};}
function fakeEl(id){return {id:id,innerHTML:'',textContent:'',value:'',
  clientWidth:1200,scrollTop:0,
  style:{setProperty:function(k,v){this[k]=v;}},
  classList:fakeClassList(),addEventListener:function(){},
  setPointerCapture:function(){},
  getBoundingClientRect:function(){return {right:1200};}};}
const els={};
global.document={getElementById:function(id){if(!els[id])els[id]=fakeEl(id);
    return els[id];},querySelector:function(){return null;},
  addEventListener:function(){},
  body:{classList:fakeClassList()}};
const lsStore={};
global.window={addEventListener:function(){},
  localStorage:{getItem:function(k){return lsStore[k]==null?null:lsStore[k];},
    setItem:function(k,v){lsStore[k]=String(v);}}};
global.fetch=function(){return Promise.reject(new Error('no network'));};
"""


_SMOKE_BODY = r"""
const assert=require('assert');
function taskObj(){return {id:'task:a',kind:'task',ref_id:'a',title:'Alpha task',
  description:'desc',project_id:'p1',project:'P1',domain:'career',
  workstream:'ws',work_package:'d1',deliverable:'d1',status:'TODO',
  readiness:'READY',ready:true,urgency:'MEDIUM',impact:'MEDIUM',
  urgency_rank:2,impact_rank:2,priority_rank:1,priority_score:9,
  priority_reasons:['because'],estimated_minutes:30,actual_minutes:null,
  effort_minutes:30,remaining_minutes:30,estimate_error_minutes:null,
  deadline:'2026-02-01',next_action:'do it',dependencies:[],
  dependency_titles:[],suggested_dependencies:[],blocked_by:[],
  blocked_by_titles:[],waiting_for:[],resources:[],upstream:[],
  downstream:[],upstream_levels:[],downstream_levels:[],unlocks_count:0,
  outcomes:[],latest_outcome:null,evidence:'FACT',
  confidence:'human-confirmed',provenance:'human',notes:'n',
  colour:{domain:'#123456'},highlight:null,reasons:[],blockers:[]};}
function projectObj(){return {id:'project:p1',kind:'project',ref_id:'p1',
  title:'P1',description:'d',objective:'obj',project_id:'p1',project:'P1',
  domain:'career',workstream:'',work_package:'',deliverable:'',
  status:'ACTIVE',readiness:null,ready:true,urgency:'MEDIUM',
  impact:'MEDIUM',urgency_rank:2,impact_rank:2,priority_rank:null,
  priority_score:null,priority_reasons:[],estimated_minutes:30,
  actual_minutes:null,effort_minutes:30,remaining_minutes:30,
  estimate_error_minutes:null,deadline:null,next_action:'',
  dependencies:[],dependency_titles:[],suggested_dependencies:[],
  blocked_by:[],blocked_by_titles:[],waiting_for:[],resources:[],
  upstream:[],downstream:[],upstream_levels:[],downstream_levels:[],
  unlocks_count:0,outcomes:[],latest_outcome:null,evidence:'FACT',
  confidence:'human-confirmed',provenance:'human',notes:'',
  colour:{domain:'#123456'},highlight:null,reasons:[],blockers:[],
  task_count:1,completed:0,progress:0};}
const T=taskObj();const P=projectObj();
S.views={objects:[T,P],object_index:{'task:a':T,'project:p1':P},
  attribute_catalog:[],wbs:{roots:[]},graph:{nodes:[],edges:[]}};
S.cockpit={today_plan:{total_capacity:0,planned_minutes:0,planned:[]},
  blocked:[],waiting:[],projects:[]};
S.filters={scope:'all',scope_id:null,level:'all',status:[],evidence:[]};
S.search='';S.view='list';S.selection=null;S.inspectorEdit=null;
S.prefs={density:'normal',color_by:'domain',highlights:{}};
S.tables={projects:{sort:[],filters:{}},tasks:{sort:[],filters:{}}};

// simple click inspects, never edits
selectObject('task:a');
assert.strictEqual(S.selection,'task:a');
assert.strictEqual(S.inspectorEdit,null);
assert.ok(document.getElementById('inspector-body').innerHTML
  .indexOf('Alpha')>=0);

// Edit / double click enters the inspector edit surface with every field
openTask('a');
assert.ok(S.inspectorEdit&&S.inspectorEdit.kind==='task');
let body=document.getElementById('inspector-body').innerHTML;
for(const marker of ['tk-title','tk-est','tk-actual','tk-urgency','tk-impact',
  'tk-status','tk-workstream','tk-deliverable','tk-deadline','tk-next',
  'tk-desc','tk-waiting','tk-blocked','tk-deps','Save changes',
  'cancelInspectorEdit']){assert.ok(body.indexOf(marker)>=0,marker);}

// Cancel leaves edit without writing
cancelInspectorEdit();
assert.strictEqual(S.inspectorEdit,null);
assert.strictEqual(S.selection,'task:a');

// project edit reuses the same pane
openProjectForm('p1');
assert.ok(S.inspectorEdit&&S.inspectorEdit.kind==='project');
body=document.getElementById('inspector-body').innerHTML;
for(const marker of ['pf-name','pf-objective','pf-status','pf-domain',
  'pf-urgency','pf-impact','pf-deadline','pf-description','Save changes']){
  assert.ok(body.indexOf(marker)>=0,marker);}
cancelInspectorEdit();

// width clamp / collapse / persistence
setInspectorWidth(9999,false);
assert.strictEqual(S.inspectorWidth,840);
assert.strictEqual(document.getElementById('layout').style['--inspector-width'],
  '840px');
setInspectorWidth(10,false);
assert.strictEqual(S.inspectorWidth,320);
toggleInspectorCollapsed();
assert.strictEqual(S.inspectorCollapsed,true);
assert.strictEqual(document.getElementById('layout').classList.contains(
  'inspector-collapsed'),true);
toggleInspectorCollapsed();
assert.strictEqual(S.inspectorCollapsed,false);
saveInspectorPrefs();
assert.ok(lsStore[INSPECTOR_LS_KEY].indexOf('"width"')>=0);
assert.ok(lsStore[INSPECTOR_LS_KEY].indexOf('"collapsed"')>=0);

// search hides the inspected object -> deterministic deselect
S.selection='task:a';S.inspectorEdit={kind:'task',id:'a'};
S.search='zzz';
reconcileSelection();
assert.strictEqual(S.selection,null);
assert.strictEqual(S.inspectorEdit,null);
// zero result is the same rule
S.selection='task:a';S.search='alpha';
reconcileSelection();
assert.strictEqual(S.selection,'task:a');

// a local List-table column filter hides the row -> same deterministic rule
S.selection='task:a';S.inspectorEdit={kind:'task',id:'a'};
S.tables.tasks.filters={title:'zzz'};
reconcileTableSelection('tasks');
assert.strictEqual(S.selection,null);
assert.strictEqual(S.inspectorEdit,null);
S.tables.tasks.filters={};
"""


def test_inspector_workspace_behaviour_smoke(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    script = tmp_path / "workspace_smoke.js"
    script.write_text(_STUBS + "\n" + cockpit_ui.PAGE_JS + "\n" + _SMOKE_BODY,
                      encoding="utf-8")
    result = subprocess.run([node, str(script)], capture_output=True,
                            text=True, check=False)
    assert result.returncode == 0, result.stderr
