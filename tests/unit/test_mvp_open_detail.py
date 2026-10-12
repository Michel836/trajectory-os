"""Focused tests for the SELECT / OPEN / EDIT interaction model.

The cockpit has three distinct states:

* simple click   -> ``selectObject`` -> Inspector preview (unchanged);
* double click   -> ``openObject`` -> read-only OPEN overlay;
* Edit           -> the existing Inspector edit mechanism.

``selectObject`` rebuilds ``#view-root`` on the first click, so a native
``dblclick`` is unreliable. OPEN is therefore detected by one central capture
listener on the persistent ``#view-root`` using the explicit ``data-object-id``
DOM contract and the pure ``isRepeatClick`` predicate. These tests pin the
contract, the read-only detail, the inner-control guard and the click timing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from trajectory_os.mvp import cockpit_ui


def _node_page_assert(tmp_path: Path, body: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    script = tmp_path / "open_smoke.js"
    script.write_text(_STUBS + "\n" + cockpit_ui.PAGE_JS + "\n" + body,
                      encoding="utf-8")
    result = subprocess.run([node, str(script)], capture_output=True,
                            text=True, check=False)
    assert result.returncode == 0, result.stderr


def _node_helper_assert(tmp_path: Path, body: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    script = tmp_path / "open_helpers.js"
    script.write_text(cockpit_ui.TABLE_JS_HELPERS + "\n" + body,
                      encoding="utf-8")
    result = subprocess.run([node, str(script)], capture_output=True,
                            text=True, check=False)
    assert result.returncode == 0, result.stderr


def _page() -> str:
    from trajectory_os.mvp.render import render_html

    class _Cockpit:
        today = "2026-01-01"
        generated_at = "2026-01-01T00:00:00"

    return render_html(_Cockpit())  # type: ignore[arg-type]


# --- pure click-timing helper -------------------------------------------------


def test_is_repeat_click_semantics(tmp_path: Path) -> None:
    _node_helper_assert(tmp_path, r"""
const assert=require('assert');
// same id inside the window -> OPEN
assert.strictEqual(isRepeatClick('task:a',1000,'task:a',1200,400),true);
// same id past the window -> no OPEN
assert.strictEqual(isRepeatClick('task:a',1000,'task:a',2000,400),false);
// different id -> no OPEN
assert.strictEqual(isRepeatClick('task:a',1000,'task:b',1100,400),false);
// no previous click / empty id -> no OPEN
assert.strictEqual(isRepeatClick(null,0,'task:a',1100,400),false);
assert.strictEqual(isRepeatClick('task:a',1000,'',1100,400),false);
// exact window boundary is inclusive
assert.strictEqual(isRepeatClick('task:a',1000,'task:a',1400,400),true);
""")


# --- rendered contracts -------------------------------------------------------


def test_open_overlay_is_wired_and_inline_dblclick_is_gone() -> None:
    html = _page()
    for marker in ("id='open-overlay'", "open-modal", "id='open-body'",
                   "closeObjectDetail", "openEditFromDetail"):
        assert marker in html, marker
    source = cockpit_ui.PAGE_JS
    for fn in ("openObject", "closeObjectDetail", "openEditFromDetail",
               "openObjectHtml", "objectIdFromEvent", "initObjectOpen"):
        assert "function " + fn + "(" in source, fn
    # no inline ondblclick / legacy editCall anywhere
    assert "ondblclick" not in source
    assert "editCall" not in source
    # simple click contract is preserved
    assert "onclick=\"selectObject" in source


def test_data_object_id_contract_and_inner_control_guard() -> None:
    source = cockpit_ui.PAGE_JS
    assert "data-object-id" in source
    assert "data-no-open" in source
    assert "closest('[data-object-id]')" in source
    guard = "closest('input,select,textarea,button,[data-no-open]')"
    assert guard in source


def test_simple_click_and_open_use_the_same_primitive() -> None:
    # the Inspector exposes an explicit Open action calling openObject(o.id)
    assert "'+o.id+'\\')\">Open</button>'" in cockpit_ui.PAGE_JS
    assert "openObject(id);return;" in cockpit_ui.PAGE_JS


# --- behavioural smoke (DOM + clock stubs) -----------------------------------

_STUBS = r"""'use strict';
function fakeClassList(){const set={};return {
  add:function(c){set[c]=true;},remove:function(c){delete set[c];},
  toggle:function(c,on){if(on===undefined)on=!set[c];
    if(on)set[c]=true;else delete set[c];return on;},
  contains:function(c){return !!set[c];}};}
function fakeEl(id){return {id:id,innerHTML:'',textContent:'',value:'',
  clientWidth:1200,scrollTop:0,
  style:{setProperty:function(k,v){this[k]=v;}},
  classList:fakeClassList(),listeners:{},
  addEventListener:function(t,fn){(this.listeners[t]=this.listeners[t]||[])
    .push(fn);},
  setPointerCapture:function(){},
  getBoundingClientRect:function(){return {right:1200};}};}
const els={};
const docListeners={};
global.document={getElementById:function(id){if(!els[id])els[id]=fakeEl(id);
    return els[id];},querySelector:function(){return null;},
  addEventListener:function(t,fn){(docListeners[t]=docListeners[t]||[])
    .push(fn);},
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
  description:'a long task description',project_id:'p1',project:'P1',
  domain:'career',workstream:'ws',work_package:'d1',deliverable:'d1',
  status:'TODO',readiness:'READY',ready:true,urgency:'MEDIUM',impact:'MEDIUM',
  urgency_rank:2,impact_rank:2,priority_rank:1,priority_score:9,
  priority_reasons:['because it matters'],estimated_minutes:30,
  actual_minutes:null,effort_minutes:30,remaining_minutes:30,
  estimate_error_minutes:null,deadline:'2026-02-01',next_action:'do it now',
  dependencies:[],dependency_titles:['dep one'],suggested_dependencies:[],
  blocked_by:[],blocked_by_titles:[],waiting_for:[],resources:[],upstream:[],
  downstream:[],upstream_levels:[],downstream_levels:[],unlocks_count:0,
  outcomes:[],latest_outcome:null,evidence:'FACT',
  confidence:'human-confirmed',provenance:'human',notes:'some notes',
  colour:{domain:'#123456'},highlight:null,reasons:[],blockers:[]};}
function projectObj(){return {id:'project:p1',kind:'project',ref_id:'p1',
  title:'P1',description:'d',objective:'obj',project_id:'p1',project:'P1',
  domain:'career',workstream:'',work_package:'',deliverable:'',
  status:'ACTIVE',readiness:null,ready:true,urgency:'MEDIUM',impact:'MEDIUM',
  urgency_rank:2,impact_rank:2,priority_rank:null,priority_score:null,
  priority_reasons:[],estimated_minutes:30,actual_minutes:null,
  effort_minutes:30,remaining_minutes:30,estimate_error_minutes:null,
  deadline:null,next_action:'',dependencies:[],dependency_titles:[],
  suggested_dependencies:[],blocked_by:[],blocked_by_titles:[],waiting_for:[],
  resources:[],upstream:[],downstream:[],upstream_levels:[],
  downstream_levels:[],unlocks_count:0,outcomes:[],latest_outcome:null,
  evidence:'FACT',confidence:'human-confirmed',provenance:'human',notes:'',
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

// read-only detail: no editable control, long fields present
const detail=openObjectHtml(T);
assert.ok(detail.indexOf('Alpha task')>=0);
assert.ok(detail.indexOf('Why selected')>=0);
assert.ok(detail.indexOf('Next action')>=0);
assert.ok(detail.indexOf('<input')<0,'detail must not contain inputs');
assert.ok(detail.indexOf('<textarea')<0,'detail must not contain textareas');
const detailP=openObjectHtml(P);
assert.ok(detailP.indexOf('P1')>=0);
assert.ok(detailP.indexOf('<input')<0);

// data-object-id is emitted on the openable representations
const row=tableRowHtml('tasks',tableColumns('tasks'),T);
assert.ok(row.indexOf('data-object-id="task:a"')>=0);
assert.ok(card(T).indexOf('data-object-id="task:a"')>=0);
assert.ok(card(P).indexOf('data-object-id="project:p1"')>=0);

// central click detector: reset on open, same/different/delayed behaviour
let clock=1000;Date.now=function(){return clock;};
function clickEvent(id_blocked,id){
  return {button:0,preventDefault:function(){},target:{closest:function(sel){
    if(sel==='input,select,textarea,button,[data-no-open]')
      return id_blocked?{}:null;
    if(sel==='[data-object-id]')
      return id?{getAttribute:function(){return id;}}:null;
    return null;}}};}
initObjectOpen();
const handler=document.getElementById('view-root').listeners.click[0];
function isOpen(){return document.getElementById('open-overlay').classList
  .contains('open');}
function reset(){closeObjectDetail();}

// same id within window -> OPEN
handler(clickEvent(false,'task:a'));
assert.strictEqual(isOpen(),false);
handler(clickEvent(false,'task:a'));
assert.strictEqual(isOpen(),true);
assert.strictEqual(S.openObject,'task:a');
// OPEN resets the repeat-click state: a third click does not reopen
reset();
clock=2000;
handler(clickEvent(false,'task:a'));
assert.strictEqual(isOpen(),false);

// different id -> no OPEN
reset();
clock=3000;handler(clickEvent(false,'task:a'));
clock=3100;handler(clickEvent(false,'task:b'));
assert.strictEqual(isOpen(),false);

// delayed second click -> no OPEN
reset();
clock=4000;handler(clickEvent(false,'task:a'));
clock=4800;handler(clickEvent(false,'task:a'));
assert.strictEqual(isOpen(),false);

// inner control -> OPEN is never triggered
reset();
clock=5000;handler(clickEvent(true,'task:a'));
clock=5100;handler(clickEvent(true,'task:a'));
assert.strictEqual(isOpen(),false);

// Close works
openObject('task:a');
assert.strictEqual(isOpen(),true);
closeObjectDetail();
assert.strictEqual(isOpen(),false);
assert.strictEqual(S.openObject,null);

// Edit from OPEN closes OPEN and reuses the existing Inspector edit
openObject('task:a');
openEditFromDetail();
assert.strictEqual(isOpen(),false);
assert.ok(S.inspectorEdit&&S.inspectorEdit.kind==='task');

// Escape closes OPEN
cancelInspectorEdit();
openObject('project:p1');
assert.strictEqual(isOpen(),true);
initInspectorWorkspace();
const keyHandler=docListeners.keydown[docListeners.keydown.length-1];
keyHandler({key:'Escape',preventDefault:function(){}});
assert.strictEqual(isOpen(),false);
"""


def test_open_behaviour_smoke(tmp_path: Path) -> None:
    _node_page_assert(tmp_path, _SMOKE_BODY)
