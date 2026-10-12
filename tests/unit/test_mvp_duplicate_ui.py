"""Focused tests for the manual-creation duplicate decision UI.

The backend is authoritative: a create that matches an existing object's
normalized title/name returns ``409 DUPLICATE_MVP_OBJECT`` with a structured
``duplicate`` candidate. The frontend must never silently retry or silently
alert: it presents Open existing / Create anyway / Cancel, and only "Create
anyway" retries with an explicit ``allow_duplicate`` flag.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from trajectory_os.mvp import cockpit_ui


def _js_block(source: str, name: str) -> str:
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


def _page() -> str:
    from trajectory_os.mvp.render import render_html

    class _Cockpit:
        today = "2026-01-01"
        generated_at = "2026-01-01T00:00:00"

    return render_html(_Cockpit())  # type: ignore[arg-type]


# --- structural / wiring ------------------------------------------------------


def test_duplicate_decision_ui_is_wired() -> None:
    html = _page()
    for marker in ("id='duplicate-overlay'", "id='duplicate-body'",
                   "duplicate-create-btn", "openExistingDuplicate",
                   "createDuplicateAnyway", "cancelDuplicate"):
        assert marker in html, marker
    source = cockpit_ui.PAGE_JS
    for fn in ("handleDuplicate", "duplicateHtml", "cancelDuplicate",
               "openExistingDuplicate", "createDuplicateAnyway"):
        assert "function " + fn + "(" in source, fn
    # create paths route conflicts to the decision UI
    assert "handleDuplicate" in _js_block(source, "submitNewTask")
    assert "handleDuplicate" in _js_block(source, "saveProject")
    assert "submitNewTask" in _js_block(source, "saveNewTask")
    # "Create anyway" is the only explicit bypass
    bypass = _js_block(source, "createDuplicateAnyway")
    assert "allow_duplicate:true" in bypass
    assert "pending.busy" in bypass
    # "Open existing" reuses the OPEN primitive
    existing = _js_block(source, "openExistingDuplicate")
    assert "openObject(id)" in existing
    assert "selectObject(id)" in existing
    assert "closeTaskForm()" in existing
    assert "closeProjectForm()" in existing


def test_cancel_never_posts() -> None:
    cancel = _js_block(cockpit_ui.PAGE_JS, "cancelDuplicate")
    assert "api(" not in cancel
    assert "S.duplicatePending=null" in cancel


# --- behavioural smoke --------------------------------------------------------

_STUBS = r"""'use strict';
function fakeClassList(){const set={};return {
  add:function(c){set[c]=true;},remove:function(c){delete set[c];},
  toggle:function(c,on){if(on===undefined)on=!set[c];
    if(on)set[c]=true;else delete set[c];return on;},
  contains:function(c){return !!set[c];}};}
function fakeEl(id){return {id:id,innerHTML:'',textContent:'',value:'',
  clientWidth:1200,scrollTop:0,
  style:{setProperty:function(k,v){this[k]=v;}},
  classList:fakeClassList(),listeners:{},disabled:false,
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
global.__calls=[];
global.fetch=function(url,opts){
  const method=(opts&&opts.method)||'GET';
  const body=opts&&opts.body?JSON.parse(opts.body):null;
  global.__calls.push({url:url,method:method,body:body});
  if(method==='POST'&&url.indexOf('/api/')===0){
    return Promise.resolve({ok:true,json:function(){
      return Promise.resolve({task_id:'new-id'});}});}
  if(url==='/api/views'){
    return Promise.resolve({ok:true,json:function(){
      return Promise.resolve(S.views);}});}
  return Promise.resolve({ok:true,json:function(){return Promise.resolve({});}});
};
"""


_SMOKE_BODY = r"""
const assert=require('assert');
function taskObj(){return {id:'task:a',kind:'task',ref_id:'a',title:'Alpha',
  description:'',project_id:'p1',project:'P1',domain:'career',workstream:'',
  work_package:'',deliverable:'',status:'TODO',readiness:'READY',ready:true,
  urgency:'MEDIUM',impact:'MEDIUM',urgency_rank:2,impact_rank:2,
  priority_rank:1,priority_score:9,priority_reasons:[],estimated_minutes:30,
  actual_minutes:null,effort_minutes:30,remaining_minutes:30,
  estimate_error_minutes:null,deadline:null,next_action:'',dependencies:[],
  dependency_titles:[],suggested_dependencies:[],blocked_by:[],
  blocked_by_titles:[],waiting_for:[],resources:[],upstream:[],downstream:[],
  upstream_levels:[],downstream_levels:[],unlocks_count:0,outcomes:[],
  latest_outcome:null,evidence:'FACT',confidence:'human-confirmed',
  provenance:'human',notes:'',colour:{domain:'#123456'},highlight:null,
  reasons:[],blockers:[]};}
function projectObj(){return {id:'project:p1',kind:'project',ref_id:'p1',
  title:'P1',description:'d',objective:'o',project_id:'p1',project:'P1',
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

function isDupOpen(){return document.getElementById('duplicate-overlay')
  .classList.contains('open');}
function createCalls(){return global.__calls.filter(function(c){
  return c.method==='POST'&&
    (c.url==='/api/tasks'||c.url==='/api/projects');});}
function conflict(kind,id,title){
  return {message:'conflict',data:{error:'DUPLICATE_MVP_OBJECT',
    detail:'duplicate',duplicate:{id:id,type:kind,ref_id:id.split(':')[1],
      title:title,status:'READY',project_id:'p1',project:'P1'}}};}

// a non-duplicate error is not intercepted
assert.strictEqual(handleDuplicate({data:{error:'MALFORMED_MVP'}},'task',{}),
  false);
assert.strictEqual(isDupOpen(),false);

// task conflict -> modal with the existing candidate
const body={title:'Alpha',project_id:'p1'};
assert.strictEqual(handleDuplicate(conflict('task','task:a','Alpha'),
  'task',body),true);
assert.strictEqual(isDupOpen(),true);
assert.ok(document.getElementById('duplicate-body').innerHTML
  .indexOf('Alpha')>=0);
assert.strictEqual(S.duplicatePending.body.title,'Alpha');

// Cancel -> closes, no create request
cancelDuplicate();
assert.strictEqual(isDupOpen(),false);
assert.strictEqual(S.duplicatePending,null);
assert.strictEqual(createCalls().length,0);

// Open existing -> closes and reuses openObject
handleDuplicate(conflict('project','project:p1','P1'),'project',{name:'P1'});
openExistingDuplicate();
assert.strictEqual(isDupOpen(),false);
assert.strictEqual(document.getElementById('open-overlay').classList
  .contains('open'),true);
assert.strictEqual(S.openObject,'project:p1');
assert.strictEqual(createCalls().length,0);

// Create anyway -> exactly one POST with the explicit bypass, even on double click
closeObjectDetail();
handleDuplicate(conflict('task','task:a','Alpha'),'task',body);
const before=createCalls().length;
const first=createDuplicateAnyway();
const second=createDuplicateAnyway();
Promise.all([first,second]).then(function(){
  const posts=createCalls().slice(before);
  assert.strictEqual(posts.length,1,'exactly one create request');
  assert.strictEqual(posts[0].body.allow_duplicate,true);
  assert.strictEqual(posts[0].body.title,'Alpha');
  assert.strictEqual(isDupOpen(),false);
  assert.strictEqual(S.duplicatePending,null);
  console.log('SMOKE_OK');
}).catch(function(err){console.error(err);process.exit(1);});
"""


def test_duplicate_ui_behaviour_smoke(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    script = tmp_path / "duplicate_smoke.js"
    script.write_text(_STUBS + "\n" + cockpit_ui.PAGE_JS + "\n" + _SMOKE_BODY,
                      encoding="utf-8")
    result = subprocess.run([node, str(script)], capture_output=True,
                            text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "SMOKE_OK" in result.stdout
