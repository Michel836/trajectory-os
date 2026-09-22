"""MVP — visual execution & decision cockpit (framework-free HTML/CSS/JS).

The cockpit is deliberately built on the existing architecture: a Python
backend, the small stdlib HTTP server and vanilla JavaScript. There is no
separate data store per view and no frontend framework. Every view is rendered
from the single ``/api/views`` payload derived in
:mod:`trajectory_os.mvp.visualization`, and every mutation is sent to the
validated backend endpoints.

Business rules (readiness, priority, dependency direction, aggregation,
quadrants, colours) live in Python. The browser only lays out and draws.
"""

from __future__ import annotations

from trajectory_os.mvp import engine

PAGE_CSS = r"""
:root{--ink:#1a1a1a;--muted:#667;--line:#e2e6ec;--accent:#0b5cad;
--accent-2:#0e6fd8;--bg:#f4f6f9;--card:#fff;--danger:#b3261e;
--ok:#1e7e34;--warn:#b26a00;--radius:10px}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;
background:var(--bg);color:var(--ink);line-height:1.45;font-size:14px}
button{font:inherit;cursor:pointer;border:1px solid var(--line);background:#fff;
border-radius:7px;padding:.32rem .6rem;color:var(--ink)}
button:hover{background:#f0f4f9}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.primary:hover{background:var(--accent-2)}
button.danger{color:var(--danger);border-color:#f0c8c4}
button.small{padding:.18rem .45rem;font-size:.78rem}
button.ghost{border-color:transparent;background:transparent}
button.active{background:#0b5cad;border-color:#0b5cad;color:#fff}
button:disabled{opacity:.5;cursor:not-allowed}
header{padding:.6rem 1rem;background:#0b2a4a;color:#fff;display:flex;
flex-wrap:wrap;gap:.6rem;align-items:center}
header h1{margin:0;font-size:1.15rem;margin-right:auto;letter-spacing:.2px}
header .sub{opacity:.85;font-size:.78rem}
header button{background:#123c63;border-color:#2b5b85;color:#fff}
header button:hover{background:#1a4d7a}
header button.primary{background:#0e6fd8;border-color:#0e6fd8}
#viewbar{display:flex;flex-wrap:wrap;gap:.3rem;padding:.5rem 1rem;
background:#fff;border-bottom:1px solid var(--line);position:sticky;top:0;
z-index:20}
#controls{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;
padding:.5rem 1rem;background:#eef2f7;border-bottom:1px solid var(--line);
position:sticky;top:41px;z-index:19;font-size:.85rem}
#controls label{color:var(--muted);font-size:.75rem;text-transform:uppercase;
letter-spacing:.4px;margin-right:.15rem}
#controls select{padding:.25rem .4rem;border:1px solid var(--line);
border-radius:6px;background:#fff}
.chips{display:flex;flex-wrap:wrap;gap:.2rem}
.chip-toggle{border:1px solid var(--line);border-radius:999px;padding:.12rem
.5rem;font-size:.75rem;background:#fff;cursor:pointer}
.chip-toggle.on{background:#0b5cad;color:#fff;border-color:#0b5cad}
#layout{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:0;
height:calc(100vh - 132px)}
#view-root{overflow:auto;padding:1rem}
#inspector{overflow:auto;background:#fff;border-left:1px solid var(--line);
padding:1rem}
@media(max-width:900px){#layout{grid-template-columns:1fr;height:auto}
#inspector{border-left:none;border-top:1px solid var(--line)}}
h2{margin:.2rem 0 .6rem;font-size:1rem;border-bottom:1px solid var(--line);
padding-bottom:.3rem}
h3{margin:.2rem 0 .5rem}
.muted{color:var(--muted);font-size:.85rem}
.notice{color:var(--danger);font-size:.85rem;min-height:1.1em;padding:0 1rem}
.pill{display:inline-block;padding:0 .45rem;border-radius:999px;
background:#eef2f7;font-size:.7rem;margin-right:.25rem;white-space:nowrap;
border:1px solid transparent}
.badge-TODO,.badge-ACTIVE{background:#e8f0fb;color:#1f5aa8}
.badge-READY,.badge-COMPLETED,.badge-DONE{background:#e6f4ea;color:#1e7e34}
.badge-IN_PROGRESS{background:#e7f1fb;color:#0b5cad}
.badge-BLOCKED{background:#fdecea;color:#b3261e}
.badge-WAITING{background:#fff4e5;color:#b26a00}
.badge-DEFERRED,.badge-ABANDONED,.badge-PROJECT_CLOSED
{background:#f1f3f4;color:#5f6368}
.badge-FACT{background:#eef7ee;color:#2f6b34}
.badge-INFERRED{background:#fff4e5;color:#8a5a00}
.badge-SUGGESTED{background:#f3ecfb;color:#6b3fa0}
.badge-UNKNOWN{background:#f1f3f4;color:#5f6368}
.badge-ACCEPTED{background:#e6f4ea;color:#1e7e34}
.badge-PENDING{background:#f3ecfb;color:#6b3fa0}
.badge-REJECTED{background:#fdecea;color:#b3261e}
.card{border:1px solid var(--line);border-radius:var(--radius);padding:.6rem;
background:#fff;box-shadow:0 1px 2px rgba(20,30,50,.04)}
.objcard{cursor:pointer;position:relative}
.objcard:hover{border-color:#b9c6d6}
.objcard.selected{outline:2px solid #0b5cad;outline-offset:1px}
.objcard .title{font-weight:600}
.hl-yellow{background:#fff8d6!important}
.hl-purple{background:#f0e6ff!important}
.hl-blue{background:#e2efff!important}
.hl-green{background:#e3f6e7!important}
.hl-red{background:#ffe6e4!important}
.hl-orange{background:#fff0dd!important}
.hl-teal{background:#ddf5f3!important}
.hl-grey{background:#eceff1!important}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:.5rem}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:.5rem}
.quad{border:1px solid var(--line);border-radius:var(--radius);padding:.5rem;
min-height:150px;background:#fbfcfe;display:flex;flex-direction:column;gap:.35rem}
.quad.drop{border-color:#0b5cad;background:#eef5fe}
.quad h4{margin:0 0 .3rem;font-size:.8rem;text-transform:uppercase;
letter-spacing:.4px;color:var(--muted)}
.kanban{display:flex;gap:.6rem;overflow-x:auto;align-items:flex-start}
.kcol{min-width:210px;flex:1 0 210px;background:#eef2f7;border-radius:var(--radius);
padding:.5rem;min-height:200px}
.kcol.drop{background:#dce9f8}
.kcol h4{margin:0 0 .4rem;font-size:.8rem;text-transform:uppercase;
letter-spacing:.4px;color:var(--muted);display:flex;justify-content:space-between}
.kcard{background:#fff;border:1px solid var(--line);border-radius:8px;
padding:.45rem;margin-bottom:.4rem;cursor:grab}
.kcard.selected{outline:2px solid #0b5cad}
.kcard .title{font-weight:600;font-size:.85rem}
.wbsrow{display:flex;align-items:center;gap:.35rem;padding:.15rem 0;
border-radius:6px;cursor:pointer}
.wbsrow:hover{background:#f2f6fb}
.wbsrow.selected{background:#e2efff}
.wbsrow .tw{width:1.1rem;text-align:center;color:var(--muted);cursor:pointer}
.wbsbar{height:.4rem;background:#e6ebf1;border-radius:999px;width:80px;
overflow:hidden;display:inline-block;vertical-align:middle}
.wbsbar>span{display:block;height:100%;background:#2ca02c}
svg text{font-family:inherit;fill:#1a1a1a}
table{border-collapse:collapse;width:100%;font-size:.85rem}
th,td{text-align:left;padding:.35rem .45rem;border-bottom:1px solid var(--line);
vertical-align:top}
th{color:var(--muted);font-weight:600;position:sticky;top:0;background:#fff}
tr.selected{background:#e2efff}
/* --- List-view data tables (presentation only) --------------------------- */
.grid-wrap{background:#fff;border:1px solid var(--line);border-radius:var(--radius);
margin-bottom:1rem}
.grid-table{table-layout:fixed;border-collapse:separate;border-spacing:0;
width:100%;font-size:.82rem}
.grid-table th,.grid-table td{padding:.34rem .6rem;text-align:left;
vertical-align:middle;border-bottom:1px solid var(--line);overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
.grid-table thead th{position:sticky;background:#f7f9fc;z-index:5;
color:var(--muted);font-weight:600;border-bottom:1px solid #d8e0ea}
.grid-table thead tr.grid-head th{top:0;z-index:7;height:30px;cursor:pointer;
user-select:none}
.grid-table thead tr.grid-head th:hover{background:#eef3fa}
.grid-table thead tr.grid-head th.sorted{color:#0b5cad}
.grid-table thead tr.grid-filter th{top:30px;z-index:6;background:#fff;
padding:.2rem .35rem}
.grid-table td.num,.grid-table th.num{text-align:right}
.grid-table .sort-ind{font-size:.7rem;opacity:.5;margin-left:.15rem}
.grid-table th.sorted .sort-ind{opacity:1;color:#0b5cad}
.grid-table .sort-pri{font-size:.6rem;vertical-align:super;opacity:.9}
.grid-table tbody tr{cursor:pointer}
.grid-table tbody tr:hover{background:#f5f8fc}
.grid-table tbody tr.selected{background:#e2efff}
.grid-table tbody td.cell-title .swatch{margin-right:.25rem}
.tf-text,.tf-select,.tf-range input{width:100%;font-size:.72rem;
padding:.12rem .3rem;border:1px solid var(--line);border-radius:5px;
background:#fff;min-width:0}
.tf-range{display:flex;align-items:center;gap:.15rem}
.tf-range input{width:50%}
.tf-range span{color:var(--muted);font-size:.7rem}
.table-tools{display:flex;align-items:center;gap:.4rem;padding:.35rem .5rem;
background:#fff;border-bottom:1px solid var(--line);flex-wrap:wrap}
.table-tools .spacer{flex:1}
.table-tools .count{color:var(--muted);font-size:.78rem}
.grid-empty{color:var(--muted);text-align:center;padding:.8rem}
.attr{display:grid;grid-template-columns:120px 1fr;gap:.2rem .5rem;
font-size:.82rem;margin:.4rem 0}
.attr dt{color:var(--muted)}
.attr dd{margin:0;word-break:break-word}
.actions{display:flex;flex-wrap:wrap;gap:.25rem;margin:.5rem 0}
.legend{display:flex;flex-wrap:wrap;gap:.5rem;font-size:.75rem;color:var(--muted);
margin-bottom:.6rem}
.swatch{display:inline-block;width:.8rem;height:.8rem;border-radius:3px;
vertical-align:middle;border:1px solid rgba(0,0,0,.15)}
.colourbtn{width:1.3rem;height:1.3rem;border-radius:4px;border:1px solid #ccc;
cursor:pointer;padding:0}
.bar{display:flex;gap:.4rem;flex-wrap:wrap;align-items:center;margin:.4rem 0}
.overlay{position:fixed;inset:0;background:rgba(20,25,35,.45);display:none;
align-items:flex-start;justify-content:center;padding:2rem 1rem;overflow:auto;
z-index:50}
.overlay.open{display:flex}
.modal{background:#fff;border-radius:12px;max-width:780px;width:100%;
padding:1.1rem;box-shadow:0 12px 40px rgba(0,0,0,.25)}
.modal h3{margin:0 0 .6rem}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:.5rem}
@media(max-width:640px){.form-row{grid-template-columns:1fr}}
label{display:block;font-size:.78rem;color:var(--muted);margin:.45rem 0 .1rem}
input,select,textarea{font:inherit;padding:.35rem .45rem;border:1px solid
var(--line);border-radius:6px;width:100%;background:#fff}
.inline{width:auto}
.insufficient{color:var(--warn);font-style:italic}
.tmnode{cursor:pointer}
.flowcol{background:#fbfcfe;border:1px dashed var(--line);border-radius:8px;
padding:.4rem;min-width:150px}
.flowcol h4{margin:0 0 .3rem;font-size:.72rem;color:var(--muted);
text-transform:uppercase}
.heatcell{display:inline-block;width:2.4rem;text-align:center;padding:.2rem;
border-radius:4px;margin:.1rem;font-size:.7rem}
.heatwrap{display:flex;flex-wrap:wrap}
.daygrid{display:flex;gap:.15rem;align-items:flex-end;height:120px}
.daybar{width:1.2rem;background:#0b5cad;border-radius:3px 3px 0 0}
"""

# --- pure List-table helpers (exposed for unit tests) ---------------------
#
# These functions are deliberately free of DOM and global access so they can
# be loaded directly by a Node harness. They are the single deterministic
# definition of the sort order and filter semantics used by the List view.
TABLE_JS_HELPERS = r"""'use strict';
var TABLE_URGENCY_RANK = {CRITICAL:4, HIGH:3, MEDIUM:2, LOW:1};
var TABLE_IMPACT_RANK = {HIGH:3, MEDIUM:2, LOW:1};
// Workflow-aware, deterministic status order (never alphabetical):
// IN_PROGRESS > READY > TODO > WAITING > BLOCKED > DEFERRED > DONE > ACTIVE.
var TABLE_STATUS_RANK = {
  IN_PROGRESS:0, READY:1, TODO:2, WAITING:3, BLOCKED:4, DEFERRED:5,
  DONE:6, COMPLETED:6, ACTIVE:7, ABANDONED:8, PROJECT_CLOSED:8
};
var TABLE_SORT_COLUMNS = {
  projects:['title','domain','status','urgency','impact','progress','effort'],
  tasks:['title','project','status','ready','urgency','impact','effort',
    'priority','deadline']
};
var TABLE_FILTER_KEYS = {
  projects:['title','domain','status','urgency','impact','progress_min',
    'progress_max','effort_min','effort_max'],
  tasks:['title','project','status','ready','urgency','impact','effort_min',
    'effort_max']
};
function tableDisplayStatus(status){
  return status==='COMPLETED'?'DONE':status;}
function tableStatusRank(status){
  if(status==null||status==='')return 99;
  var v=TABLE_STATUS_RANK[String(status)];
  return v==null?99:v;}
function tableUrgencyRank(u){return u==null?0:(TABLE_URGENCY_RANK[String(u)]||0);}
function tableImpactRank(i){return i==null?0:(TABLE_IMPACT_RANK[String(i)]||0);}
function tableEffort(kind,o){if(o==null)return null;
  var v=kind==='project'?o.effort_minutes:o.estimated_minutes;
  if(v==null)v=o.effort_minutes;
  return v==null?null:Number(v);}
function tableProgress(o){if(o==null||o.progress==null)return null;
  var v=Number(o.progress);return isFinite(v)?v:null;}
function tableSortValue(kind,column,o){
  if(o==null)return null;
  switch(column){
    case 'title':return o.title==null?null:String(o.title);
    case 'project':return o.project==null?null:String(o.project);
    case 'domain':return o.domain==null?null:String(o.domain);
    case 'status':return tableStatusRank(tableDisplayStatus(o.status));
    case 'ready':return o.ready?1:0;
    case 'urgency':return tableUrgencyRank(o.urgency);
    case 'impact':return tableImpactRank(o.impact);
    case 'progress':return tableProgress(o);
    case 'effort':return tableEffort(kind,o);
    case 'priority':return o.priority_rank==null?null:Number(o.priority_rank);
    case 'deadline':return o.deadline==null?null:String(o.deadline);
    default:return null;}}
function tableCompare(kind,a,b,sort){
  var av=tableSortValue(kind,sort.column,a);
  var bv=tableSortValue(kind,sort.column,b);
  var an=(av==null),bn=(bv==null);
  if(an||bn){if(an&&bn)return 0;return an?1:-1;}
  var cmp;
  if(typeof av==='number'&&typeof bv==='number'){cmp=av-bv;}
  else{cmp=String(av).localeCompare(String(bv),undefined,
    {numeric:true,sensitivity:'base'});}
  return sort.dir==='desc'?-cmp:cmp;}
function tableSortRows(kind,rows,sorts){
  var arr=(rows||[]).slice();
  if(!sorts||!sorts.length)return arr;
  arr.sort(function(a,b){
    for(var i=0;i<sorts.length;i++){
      var c=tableCompare(kind,a,b,sorts[i]);
      if(c)return c;}
    return 0;});
  return arr;}
// Tri-state cycle: asc -> desc -> none. Shift adds/replaces a secondary term.
function tableNextSort(sorts,column,additive){
  var copy=(sorts||[]).map(function(s){
    return {column:s.column,dir:s.dir};});
  var idx=-1;
  for(var i=0;i<copy.length;i++){
    if(copy[i].column===column){idx=i;break;}}
  var current=idx<0?null:copy[idx];
  if(!additive){
    if(current&&current.dir==='asc')return [{column:column,dir:'desc'}];
    if(current&&current.dir==='desc')return [];
    return [{column:column,dir:'asc'}];}
  if(current==null){copy.push({column:column,dir:'asc'});return copy;}
  if(current.dir==='asc'){copy[idx]={column:column,dir:'desc'};return copy;}
  copy.splice(idx,1);
  return copy;}
function tableNum(v){if(v==null||v==='')return null;
  var n=Number(v);return isFinite(n)?n:null;}
function tableMatchesFilter(kind,o,filters){
  if(!filters)return true;
  var low=function(v){return String(v==null?'':v).toLowerCase();};
  var eq=function(a,b){return String(a==null?'':a)===String(b);};
  if(filters.title&&low(o.title).indexOf(low(filters.title))<0)return false;
  if(filters.project&&!eq(o.project_id,filters.project)&&
    !eq(o.project,filters.project))return false;
  if(filters.domain&&!eq(o.domain,filters.domain))return false;
  if(filters.status&&!eq(tableDisplayStatus(o.status),filters.status))
    return false;
  if(filters.ready){if(!!o.ready!==(filters.ready==='yes'))return false;}
  if(filters.urgency&&!eq(o.urgency,filters.urgency))return false;
  if(filters.impact&&!eq(o.impact,filters.impact))return false;
  var pmin=tableNum(filters.progress_min),pmax=tableNum(filters.progress_max);
  if(pmin!=null||pmax!=null){
    var pct=o.progress==null?null:Number(o.progress)*100;
    if(pct==null||(pmin!=null&&pct<pmin)||(pmax!=null&&pct>pmax))
      return false;}
  var emin=tableNum(filters.effort_min),emax=tableNum(filters.effort_max);
  if(emin!=null||emax!=null){
    var eff=tableEffort(kind,o);
    if(eff==null||(emin!=null&&eff<emin)||(emax!=null&&eff>emax))
      return false;}
  return true;}
function tableFilterRows(kind,rows,filters){
  return (rows||[]).filter(function(o){
    return tableMatchesFilter(kind,o,filters);});}
function normaliseTables(value){
  var out={projects:{sort:[],filters:{}},tasks:{sort:[],filters:{}}};
  if(!value||typeof value!=='object')return out;
  ['projects','tasks'].forEach(function(k){
    var spec=value[k];
    if(!spec||typeof spec!=='object')return;
    var cols=TABLE_SORT_COLUMNS[k]||[];
    var seen={};
    (Array.isArray(spec.sort)?spec.sort:[]).forEach(function(term){
      if(!term||cols.indexOf(term.column)<0||seen[term.column])return;
      var dir=term.dir==='desc'?'desc':'asc';
      seen[term.column]=true;
      out[k].sort.push({column:term.column,dir:dir});});
    if(spec.filters&&typeof spec.filters==='object'){
      var allowed=TABLE_FILTER_KEYS[k]||[];
      Object.keys(spec.filters).forEach(function(key){
        if(allowed.indexOf(key)<0)return;
        var raw=spec.filters[key];
        if(raw==null||raw==='')return;
        out[k].filters[key]=String(raw);});}});
  return out;}
// Presentation-only global search: pure, case-insensitive token matching
// over the fields a human would search. Combined with (never replacing) the
// global scope/level/status/evidence filters. No business rule here.
function searchTermsOf(value){return String(value==null?'':value).toLowerCase()
  .split(/\s+/).filter(Boolean);}
function searchMatches(o,terms){
  if(!terms||!terms.length)return true;
  if(!o)return false;
  var hay=[o.title,o.project,o.domain,o.workstream,o.deliverable,
    o.description,o.objective,o.next_action,o.status,o.evidence,o.ref_id]
    .map(function(v){return v==null?'':String(v).toLowerCase();}).join(' ');
  return terms.every(function(t){return hay.indexOf(t)>=0;});}
"""


PAGE_JS = TABLE_JS_HELPERS + r"""
const VIEWS = [
  ['today','Today'],['ready','Ready'],
  ['list','List'],['kanban','Kanban'],['wbs','WBS'],['graph','Dependency Graph'],
  ['gantt','Gantt'],['eisenhower','Eisenhower'],['impact_effort','Impact / Effort'],
  ['portfolio_map','Portfolio Map'],['treemap','Treemap'],['progress','Progress'],
  ['heatmap','Heatmap'],['goal_flow','Goal Flow'],['focus','Focus']
];
const KANBAN_TASK = ['TODO','IN_PROGRESS','READY','WAITING','BLOCKED','DONE','DEFERRED'];
const KANBAN_PROJECT = ['ACTIVE','WAITING','BLOCKED','DEFERRED','COMPLETED'];
const S = {
  views:null, cockpit:null,
  view:'list', selection:null, filters:{scope:'all',scope_id:null,level:'all',
    status:[],evidence:[]},
  search:'', activeSavedView:'',
  capture:null, exportSelection:{}, exportPreview:null,
  tables:normaliseTables(null),
  prefs:{color_by:'domain',density:'normal',highlights:{}},
  graphDepth:1, kanbanMode:'tasks', eiMode:'tasks', ieMode:'tasks',
  ganttScale:'week', treemapGroup:'domain', treemapMetric:'task_count',
  mapX:'urgency', mapY:'impact', mapSize:'task_count',
  collapsed:{}, treemapFocus:'portfolio', wbsSelected:null,
  importJob:null, importPoll:null, importEngines:null,
  importEngineChoice:null,
  enrichment:{}, enrichmentOpen:{}, enrichmentBusy:null, enrichmentEngine:null
};
const $ = id => document.getElementById(id);
function esc(v){return String(v==null?'':v).replace(/&/g,'&amp;')
  .replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function fmtMin(m){if(m==null||m==='')return '—';m=Number(m);if(!m)return '0m';
  const h=Math.floor(m/60),r=m%60;return h&&r?h+'h'+(r<10?'0':'')+r+'m':
  (h?h+'h':r+'m');}
function pill(text,cls){return '<span class="pill '+(cls||'')+'">'+
  esc(text)+'</span>';}
function notice(msg){const el=$('notice');el.textContent=msg||'';
  if(msg)setTimeout(function(){if(el.textContent===msg)el.textContent='';},7000);}
async function api(path,method,body){const opts={method:method||'GET',headers:{}};
  if(body!==undefined){opts.headers['Content-Type']='application/json';
    opts.body=JSON.stringify(body);}
  const res=await fetch(path,opts);let data={};
  try{data=await res.json();}catch(e){/* ignore */}
  if(!res.ok){throw new Error(data.detail||data.error||data.status||
    ('HTTP '+res.status));}
  return data;}
function obj(id){return S.views&&S.views.object_index?
  S.views.object_index[id]:null;}
function objects(){return (S.views&&S.views.objects)||[];}
function uiStatus(o){return o.status==='COMPLETED'?'DONE':o.status;}
function colourOf(o){if(!o)return '#9aa4b2';
  return (o.colour&&o.colour[S.prefs.color_by])||'#9aa4b2';}
function highlightClass(o){return o&&o.highlight?('hl-'+o.highlight):'';}
function isTask(o){return o.kind==='task';}
function searchTerms(){return searchTermsOf(S.search);}
function matchesSearch(o){return searchMatches(o,searchTerms());}
function filtered(){
  const f=S.filters;
  return objects().filter(function(o){
    if(!matchesSearch(o))return false;
    if(f.scope==='domain'&&o.domain!==f.scope_id)return false;
    if(f.scope==='project'&&o.project_id!==f.scope_id)return false;
    if(f.level==='projects'&&o.kind!=='project')return false;
    if(f.level==='tasks'&&o.kind!=='task')return false;
    if(f.status.length&&f.status.indexOf(uiStatus(o))<0)return false;
    if(f.evidence.length&&f.evidence.indexOf(o.evidence)<0)return false;
    return true;});
}
function filteredTasks(){return filtered().filter(isTask);}
function allowedSet(){const s={};filtered().forEach(function(o){s[o.id]=true;});
  return s;}
function filteredProjects(){return filtered().filter(function(o){
  return o.kind==='project';});}
function persistPrefs(patch){Object.assign(S.prefs,patch);
  api('/api/preferences','POST',patch).catch(function(e){notice(e.message);});}
function applyPrefs(p){if(!p)return;S.prefs=p;
  S.filters=Object.assign({scope:'all',scope_id:null,level:'all',
    status:[],evidence:[]},p.filters||{});
  S.view=p.view||S.view;S.tables=normaliseTables(p.tables);
  S.selection=p.selected||null;}
async function refresh(){
  try{
    const res=await api('/api/views');
    S.views=res;
    applyPrefs(res.preferences);
    renderAll();notice('');
  }catch(err){notice(err.message);}
}
function setView(v){S.view=v;persistPrefs({view:v});renderAll();}
function selectObject(id){S.selection=id;
  if(id&&obj(id)&&obj(id).kind==='project'){S.treemapFocus='project:'+
    obj(id).project_id;}
  persistPrefs({selected:id});renderAll();}
function renderAll(){renderViewBar();renderControls();renderSummary();
  renderView();renderInspector();}
function renderViewBar(){$('viewbar').innerHTML=VIEWS.map(function(v){
  return '<button class="small '+(S.view===v[0]?'active':'')+
  '" onclick="setView(\''+v[0]+'\')">'+esc(v[1])+'</button>';}).join('');}
function chipToggle(group,value){
  const arr=S.filters[group].slice();const i=arr.indexOf(value);
  if(i<0)arr.push(value);else arr.splice(i,1);S.filters[group]=arr;
  persistPrefs({filters:S.filters});renderAll();}
function renderControls(){
  const domains=[].concat.apply([],objects().map(function(o){
    return o.kind==='project'?[o.domain]:[];})).filter(function(v,i,a){
    return a.indexOf(v)===i;}).sort();
  const projects=objects().filter(function(o){return o.kind==='project';});
  const scopeOpts=domains.map(function(d){return '<option value="d:'+esc(d)+
    '"'+((S.filters.scope==='domain'&&S.filters.scope_id===d)?' selected':'')+
    '>domain: '+esc(d)+'</option>';}).concat(projects.map(function(p){
    return '<option value="p:'+esc(p.project_id)+'"'+
    ((S.filters.scope==='project'&&S.filters.scope_id===p.project_id)?
      ' selected':'')+'>project: '+esc(p.title)+'</option>';})).join('');
  const statusList=['ACTIVE','READY','TODO','IN_PROGRESS','WAITING','BLOCKED',
    'DEFERRED','DONE'];
  const evList=['FACT','INFERRED','SUGGESTED','UNKNOWN'];
  const densityOpts=['compact','normal','detailed'].map(function(d){
    return '<option'+(S.prefs.density===d?' selected':'')+'>'+d+'</option>';
  }).join('');
  const colourOpts=['domain','project','status','urgency','impact'].map(
    function(c){return '<option'+(S.prefs.color_by===c?' selected':'')+'>'+
    c+'</option>';}).join('');
  $('controls').innerHTML=
    '<label>Scope</label><select id="f-scope" onchange="scopeChange()">'+
    '<option value="">All</option>'+scopeOpts+'</select>'+
    '<label>Level</label><select id="f-level" onchange="levelChange()">'+
    ['all','projects','tasks'].map(function(l){return '<option value="'+l+'"'+
      (S.filters.level===l?' selected':'')+'>'+l+'</option>';}).join('')+
    '</select>'+
    '<label>Status</label><div class="chips">'+statusList.map(function(s){
      return '<span class="chip-toggle'+(S.filters.status.indexOf(s)>=0?
        ' on':'')+'" onclick="chipToggle(\'status\',\''+s+'\')">'+s+
        '</span>';}).join('')+'</div>'+
    '<label>Evidence</label><div class="chips">'+evList.map(function(e){
      return '<span class="chip-toggle'+(S.filters.evidence.indexOf(e)>=0?
        ' on':'')+'" onclick="chipToggle(\'evidence\',\''+e+'\')">'+e+
        '</span>';}).join('')+'</div>'+
    '<label>Colour by</label><select id="f-colour" onchange="colourChange()">'+
    colourOpts+'</select>'+
    '<label>Density</label><select id="f-density" onchange="densityChange()">'+
    densityOpts+'</select>'+
    '<label>Search</label><input id="f-search" class="inline" type="search" '+
    'placeholder="projects &amp; tasks…" value="'+esc(S.search)+'" '+
    'style="min-width:170px" oninput="searchInput(this.value)">'+
    '<label>Saved view</label><select id="f-saved" onchange="savedViewChange()">'+
    savedViewOptions()+'</select>'+
    '<button class="small" onclick="saveCurrentView()">Save</button>'+
    '<button class="small" onclick="renameCurrentView()">Rename</button>'+
    '<button class="small" onclick="deleteCurrentView()">Delete</button>';
}
function searchInput(v){S.search=v||'';renderView();}
function savedViewOptions(){
  const views=(S.prefs&&S.prefs.saved_views)||{};
  return '<option value="">—</option>'+Object.keys(views).sort().map(
    function(n){return '<option value="'+esc(n)+'"'+
      (S.activeSavedView===n?' selected':'')+'>'+esc(n)+'</option>';}).join('');}
async function savedViewChange(){
  const sel=$('f-saved');const name=sel?sel.value:'';
  if(!name){S.activeSavedView='';return;}
  S.activeSavedView=name;
  try{const res=await api('/api/saved-views/load','POST',{name:name});
    applyPrefs(res.preferences);renderAll();notice('Loaded view: '+name);}
  catch(err){notice(err.message);}}
async function saveCurrentView(){
  const name=window.prompt('Save current view as:','');
  if(name==null||!name.trim())return;
  try{const res=await api('/api/saved-views','POST',{name:name.trim()});
    S.prefs=res.preferences;S.activeSavedView=name.trim();renderControls();
    notice('Saved view: '+name.trim());}catch(err){notice(err.message);}}
async function renameCurrentView(){
  const name=S.activeSavedView;
  if(!name){notice('Load or save a view first');return;}
  const next=window.prompt('Rename view "'+name+'" to:',name);
  if(next==null||!next.trim()||next.trim()===name)return;
  try{const res=await api('/api/saved-views/rename','POST',
    {name:name,new_name:next.trim()});
    S.prefs=res.preferences;S.activeSavedView=next.trim();renderControls();
    notice('Renamed to: '+next.trim());}catch(err){notice(err.message);}}
async function deleteCurrentView(){
  const name=S.activeSavedView;
  if(!name){notice('Load or save a view first');return;}
  if(!window.confirm('Delete saved view "'+name+'"?'))return;
  try{const res=await api('/api/saved-views/delete','POST',{name:name});
    S.prefs=res.preferences;S.activeSavedView='';renderControls();
    notice('Deleted view: '+name);}catch(err){notice(err.message);}}
function scopeChange(){const v=$('f-scope').value;
  if(!v){S.filters.scope='all';S.filters.scope_id=null;}
  else if(v.indexOf('d:')===0){S.filters.scope='domain';
    S.filters.scope_id=v.slice(2);}
  else{S.filters.scope='project';S.filters.scope_id=v.slice(2);}
  persistPrefs({filters:S.filters});renderAll();}
function levelChange(){S.filters.level=$('f-level').value;
  persistPrefs({filters:S.filters});renderAll();}
function colourChange(){S.prefs.color_by=$('f-colour').value;
  persistPrefs({color_by:S.prefs.color_by});renderAll();}
function densityChange(){S.prefs.density=$('f-density').value;
  persistPrefs({density:S.prefs.density});renderAll();}

// --- daily summary -----------------------------------------------------------
function renderSummary(){
  const t=$('today-summary');
  if(!S.cockpit){t.innerHTML='<span class="muted">loading…</span>';return;}
  const d=S.cockpit.today_plan;
  t.innerHTML='<span class="muted">capacity '+fmtMin(d.total_capacity)+
    ' · planned '+fmtMin(d.planned_minutes)+' · '+
    (d.planned.length)+' item(s)</span>';
  const b=$('blocked-summary');
  const rows=(S.cockpit.blocked||[]).concat(S.cockpit.waiting||[]);
  b.innerHTML=rows.length?rows.slice(0,6).map(function(r){
    return '<a onclick="selectObject(\'task:'+esc(r.task_id)+'\')">'+
    esc(r.title)+'</a>';}).join(', '):'<span class="muted">nothing</span>';
  const p=$('projects-summary');
  p.innerHTML=(S.cockpit.projects||[]).slice(0,8).map(function(pr){
    return '<a onclick="selectObject(\'project:'+esc(pr.project_id)+'\')">'+
    esc(pr.name)+'</a>';}).join(', ');
}

// --- dispatcher --------------------------------------------------------------
function renderView(){
  if(!S.views){$('view-root').innerHTML='<p class="muted">loading…</p>';return;}
  const fn={today:renderToday,ready:renderReady,list:renderList,
    kanban:renderKanban,wbs:renderWbs,graph:renderGraph,
    gantt:renderGantt,eisenhower:renderEisenhower,impact_effort:renderImpactEffort,
    portfolio_map:renderPortfolioMap,treemap:renderTreemap,progress:renderProgress,
    heatmap:renderHeatmap,goal_flow:renderGoalFlow,focus:renderFocus}[S.view]
    ||renderList;
  fn();
}
function card(o,extra){
  const cls='card objcard '+highlightClass(o)+
    (S.selection===o.id?' selected':'');
  return '<div class="'+cls+'" style="border-left:4px solid '+colourOf(o)+
    '" onclick="selectObject(\''+o.id+'\')">'+(extra||cardBody(o))+'</div>';}
function cardBody(o,deep){
  const badge=pill(o.kind==='project'?'project':'task','badge-'+
    (o.kind==='project'?o.status:o.status));
  let h='<div class="title">'+esc(o.title)+'</div>';
  if(deep||S.prefs.density!=='compact'){
    h+='<div class="muted">'+badge+
      (o.project&&o.kind==='task'?esc(o.project)+' · ':'')+
      (o.status!=='COMPLETED'?esc(o.status):'DONE')+
      (o.estimated_minutes?' · '+fmtMin(o.estimated_minutes):'')+'</div>';}
  if(deep){
    h+='<div class="muted">'+esc(o.domain)+
      (o.workstream?' · '+esc(o.workstream):'')+
      (o.deadline?' · due '+esc(o.deadline):'')+'</div>'+
      '<div class="muted">urgency '+esc(o.urgency)+' · impact '+esc(o.impact)+
      (o.priority_rank?' · priority #'+o.priority_rank:'')+
      ' · '+pill(o.evidence,'badge-'+o.evidence)+'</div>'+
      (o.next_action?'<div class="muted">next: '+esc(o.next_action)+'</div>':'')+
      (o.unlocks_count?'<div class="muted">unlocks '+o.unlocks_count+
        ' downstream</div>':'');}
  return h;}

// --- List --------------------------------------------------------------------
// Column definitions drive both rendering and the compact filter row. Each
// column declares a stable key, a CSS width class and (optionally) a filter
// control. Widths keep the name column dominant and the categorical columns
// compact so headers never fuse together.
const TABLE_DEFS = {
  projects: {columns: [
    {key:'title',label:'Project',cls:'c-title',width:'32%',
      filter:{type:'text'}},
    {key:'domain',label:'Domain',cls:'c-domain',width:'14%',
      filter:{type:'select',source:'domain'}},
    {key:'status',label:'Status',cls:'c-compact',width:'12%',
      filter:{type:'select',source:'status'}},
    {key:'urgency',label:'Urgency',cls:'c-compact',width:'10%',
      filter:{type:'select',source:'urgency'}},
    {key:'impact',label:'Impact',cls:'c-compact',width:'10%',
      filter:{type:'select',source:'impact'}},
    {key:'progress',label:'Progress',cls:'c-mini num',width:'11%',
      filter:{type:'range',minKey:'progress_min',maxKey:'progress_max'}},
    {key:'effort',label:'Effort',cls:'c-mini num',width:'11%',
      filter:{type:'range',minKey:'effort_min',maxKey:'effort_max'}}
  ]},
  tasks: {columns: [
    {key:'title',label:'Task',cls:'c-title',width:'30%',
      filter:{type:'text'}},
    {key:'project',label:'Project',cls:'c-project',width:'16%',
      filter:{type:'select',source:'project'}},
    {key:'status',label:'Status',cls:'c-compact',width:'11%',
      filter:{type:'select',source:'status'}},
    {key:'ready',label:'Ready',cls:'c-mini',width:'8%',
      filter:{type:'select',source:'ready'}},
    {key:'urgency',label:'Urgency',cls:'c-compact',width:'9%',
      filter:{type:'select',source:'urgency'}},
    {key:'impact',label:'Impact',cls:'c-compact',width:'9%',
      filter:{type:'select',source:'impact'}},
    {key:'effort',label:'Effort',cls:'c-mini num',width:'9%',
      filter:{type:'range',minKey:'effort_min',maxKey:'effort_max'}},
    {key:'priority',label:'Priority',cls:'c-mini num',width:'7%',
      detailed:true},
    {key:'deadline',label:'Deadline',cls:'c-mini',width:'9%',
      detailed:true}
  ]}
};
function tableState(kind){
  if(!S.tables[kind])S.tables[kind]={sort:[],filters:{}};
  if(!S.tables[kind].sort)S.tables[kind].sort=[];
  if(!S.tables[kind].filters)S.tables[kind].filters={};
  return S.tables[kind];}
function tableColumns(kind){
  return TABLE_DEFS[kind].columns.filter(function(c){
    return !c.detailed||S.prefs.density==='detailed';});}
function tableBaseRows(kind){
  return kind==='projects'?filteredProjects():filteredTasks();}
// Filter + sort the in-memory array exactly once, before any DOM work.
function tableRows(kind){
  const st=tableState(kind);
  return tableSortRows(kind,tableFilterRows(kind,tableBaseRows(kind),
    st.filters),st.sort);}
function persistTables(){persistPrefs({tables:S.tables});}
let tablePersistTimer=null;
function persistTablesSoon(){
  if(tablePersistTimer)clearTimeout(tablePersistTimer);
  tablePersistTimer=setTimeout(persistTables,350);}
function withViewScroll(fn){
  const vr=$('view-root');
  const psc=vr?vr.scrollTop:0;
  const wraps={};
  ['projects','tasks'].forEach(function(k){
    const w=document.getElementById('grid-'+k);
    if(w)wraps[k]=w.scrollTop;});
  fn();
  if(vr)vr.scrollTop=psc;
  ['projects','tasks'].forEach(function(k){
    const w=document.getElementById('grid-'+k);
    if(w)w.scrollTop=wraps[k]||0;});}
function tableSortIndicator(kind,column){
  const sorts=tableState(kind).sort;
  for(let i=0;i<sorts.length;i++){
    if(sorts[i].column===column){
      const arrow=sorts[i].dir==='asc'?'↑':'↓';
      const pri=sorts.length>1?
        '<sup class="sort-pri">'+(i+1)+'</sup>':'';
      return '<span class="sort-ind">'+arrow+pri+'</span>';}}
  return '<span class="sort-ind">↕</span>';}
function tableHeaderCell(kind,col){
  const sorted=tableState(kind).sort.some(function(s){
    return s.column===col.key;});
  return '<th class="'+col.cls+(sorted?' sorted':'')+'" '+
    'onclick="tableHeaderClick(\''+kind+'\',\''+col.key+'\',event)" '+
    'title="Click to sort · Shift+click to add a secondary sort">'+
    esc(col.label)+tableSortIndicator(kind,col.key)+'</th>';}
function tableUnique(rows,fn){
  const seen={};const out=[];
  rows.forEach(function(o){const v=fn(o);
    if(v==null||v==='')return;
    if(seen[v])return;seen[v]=true;out.push(v);});
  return out;}
function tableFilterOptions(kind,source,base){
  const list=function(arr){return arr.map(function(v){
    return {value:v,label:v};});};
  if(source==='domain')return list(tableUnique(base,function(o){
    return o.domain;}).sort());
  if(source==='status')return list(tableUnique(base,function(o){
    return tableDisplayStatus(o.status);}).sort());
  if(source==='urgency')return list(['CRITICAL','HIGH','MEDIUM','LOW']);
  if(source==='impact')return list(['HIGH','MEDIUM','LOW']);
  if(source==='ready')return [{value:'yes',label:'yes'},
    {value:'no',label:'no'}];
  if(source==='project'){
    const seen={};const out=[];
    base.forEach(function(o){
      if(!o.project_id||seen[o.project_id])return;
      seen[o.project_id]=true;
      out.push({value:o.project_id,label:o.project||o.project_id});});
    out.sort(function(a,b){return a.label.localeCompare(b.label);});
    return out;}
  return [];}
function tableFilterControl(kind,col,base){
  const f=col.filter;if(!f)return '';
  const filters=tableState(kind).filters;
  if(f.type==='text'){
    return '<input class="tf-text" type="text" placeholder="filter…" '+
      'value="'+esc(filters[col.key]||'')+'" '+
      'oninput="tableTextFilter(\''+kind+'\',\''+col.key+
      '\',this.value)">';}
  if(f.type==='select'){
    const cur=filters[col.key]||'';
    const opts=tableFilterOptions(kind,f.source,base);
    return '<select class="tf-select" onchange="tableSelectFilter(\''+
      kind+'\',\''+col.key+'\',this.value)">'+
      '<option value="">all</option>'+opts.map(function(o){
        return '<option value="'+esc(o.value)+'"'+
          (String(o.value)===String(cur)?' selected':'')+'>'+
          esc(o.label)+'</option>';}).join('')+'</select>';}
  if(f.type==='range'){
    const mn=filters[f.minKey]||'',mx=filters[f.maxKey]||'';
    return '<div class="tf-range"><input type="number" placeholder="min" '+
      'value="'+esc(mn)+'" oninput="tableRangeFilter(\''+kind+'\',\''+
      f.minKey+'\',this.value)"><span>–</span>'+
      '<input type="number" placeholder="max" value="'+esc(mx)+'" '+
      'oninput="tableRangeFilter(\''+kind+'\',\''+f.maxKey+
      '\',this.value)"></div>';}
  return '';}
function tableCell(kind,column,o){
  switch(column){
    case 'title':return '<span class="swatch" style="background:'+
      colourOf(o)+'"></span> '+esc(o.title);
    case 'project':return esc(o.project);
    case 'domain':return esc(o.domain);
    case 'status':return pill(tableDisplayStatus(o.status),
      'badge-'+tableDisplayStatus(o.status));
    case 'ready':return o.ready?'yes':'no';
    case 'urgency':return esc(o.urgency);
    case 'impact':return esc(o.impact);
    case 'progress':return o.progress==null?'—':
      Math.round(o.progress*100)+'%';
    case 'effort':return fmtMin(kind==='project'?o.effort_minutes:
      o.estimated_minutes);
    case 'priority':return o.priority_rank?'#'+o.priority_rank:'—';
    case 'deadline':return esc(o.deadline||'—');
    default:return '';}}
function tableRowHtml(kind,cols,o){
  return '<tr class="'+(S.selection===o.id?'selected':'')+'" '+
    'onclick="selectObject(\''+o.id+'\')">'+
    cols.map(function(c){
      return '<td class="'+c.cls+'" title="'+
        esc(c.key==='title'?o.title:'')+'">'+
        tableCell(kind,c.key,o)+'</td>';}).join('')+'</tr>';}
function tableBodyHtml(kind,cols,rows){
  if(!rows.length)return '<tr><td class="grid-empty" colspan="'+
    cols.length+'">no rows match the filters</td></tr>';
  return rows.map(function(o){return tableRowHtml(kind,cols,o);}).join('');}
function renderGrid(kind){
  const cols=tableColumns(kind);
  const st=tableState(kind);
  const base=tableBaseRows(kind);
  const rows=tableSortRows(kind,tableFilterRows(kind,base,st.filters),
    st.sort);
  const hasFilters=Object.keys(st.filters).some(function(k){
    return st.filters[k]!==''&&st.filters[k]!=null;});
  const hasSort=st.sort.length>0;
  let h='<div class="table-tools"><span class="count" id="grid-count-'+
    kind+'">'+rows.length+' of '+base.length+' row(s)</span>'+
    '<span class="spacer"></span>'+
    '<button class="small" onclick="tableClearFilters(\''+kind+'\')"'+
    (hasFilters?'':' disabled')+'>Clear filters</button>'+
    '<button class="small" onclick="tableClearSort(\''+kind+'\')"'+
    (hasSort?'':' disabled')+'>Clear sort</button>'+
    '<button class="small" onclick="tableReset(\''+kind+'\)">'+
    'Reset table</button></div>';
  h+='<div class="grid-wrap" id="grid-'+kind+'"><table class="grid-table">'+
    '<colgroup>'+cols.map(function(c){
      return '<col style="width:'+c.width+'">';}).join('')+'</colgroup>'+
    '<thead><tr class="grid-head">'+
    cols.map(function(c){return tableHeaderCell(kind,c);}).join('')+
    '</tr><tr class="grid-filter">'+
    cols.map(function(c){
      return '<th class="'+c.cls+'">'+tableFilterControl(kind,c,base)+
        '</th>';}).join('')+'</tr></thead>'+
    '<tbody id="grid-body-'+kind+'">'+tableBodyHtml(kind,cols,rows)+
    '</tbody></table></div>';
  return h;}
function renderTableBody(kind){
  const el=document.getElementById('grid-body-'+kind);
  if(!el)return;
  const cols=tableColumns(kind);
  const rows=tableRows(kind);
  el.innerHTML=tableBodyHtml(kind,cols,rows);
  const count=document.getElementById('grid-count-'+kind);
  if(count)count.textContent=rows.length+' of '+
    tableBaseRows(kind).length+' row(s)';}
function renderList(){
  let h='<div class="legend"><span>Colour: '+esc(S.prefs.color_by)+
    '</span><span><span class="swatch" style="background:#2ca02c"></span> '+
    'status badge</span><span>personal highlight overlay</span></div>';
  if(S.filters.level!=='tasks'){
    h+='<h2>Projects</h2>'+renderGrid('projects');}
  if(S.filters.level!=='projects'){
    h+='<h2>Tasks</h2>'+renderGrid('tasks');}
  if(!filtered().length)h+='<p class="muted">no objects match the global '+
    'filters</p>';
  $('view-root').innerHTML=h;}
function tableHeaderClick(kind,column,ev){
  ev=ev||window.event;
  const additive=!!(ev&&ev.shiftKey);
  const st=tableState(kind);
  st.sort=tableNextSort(st.sort,column,additive);
  persistTables();
  withViewScroll(renderAll);}
function tableTextFilter(kind,key,value){
  tableState(kind).filters[key]=value||'';
  persistTablesSoon();
  renderTableBody(kind);}
function tableRangeFilter(kind,key,value){
  tableState(kind).filters[key]=(value==null?'':String(value));
  persistTablesSoon();
  renderTableBody(kind);}
function tableSelectFilter(kind,key,value){
  tableState(kind).filters[key]=value||'';
  persistTables();
  withViewScroll(renderAll);}
function tableClearFilters(kind){
  tableState(kind).filters={};
  persistTables();
  withViewScroll(renderAll);}
function tableClearSort(kind){
  tableState(kind).sort=[];
  persistTables();
  withViewScroll(renderAll);}
function tableReset(kind){
  S.tables[kind]={sort:[],filters:{}};
  persistTables();
  withViewScroll(renderAll);}

// --- Kanban ------------------------------------------------------------------
function kanbanColumn(o,mode){
  if(mode==='projects'){return o.status;}
  if(o.status==='COMPLETED'||o.status==='ABANDONED')return 'DONE';
  if(o.status==='DEFERRED')return 'DEFERRED';
  if(o.readiness==='BLOCKED')return 'BLOCKED';
  if(o.readiness==='WAITING')return 'WAITING';
  if(o.status==='IN_PROGRESS')return 'IN_PROGRESS';
  if(o.ready)return 'READY';
  return 'TODO';}
function renderKanban(){
  const mode=S.kanbanMode;
  const cols=mode==='projects'?KANBAN_PROJECT:KANBAN_TASK;
  const rows=mode==='projects'?filteredProjects():filteredTasks();
  let h='<div class="bar"><button class="small '+(mode==='tasks'?'active':'')+
    '" onclick="S.kanbanMode=\'tasks\';renderAll()">Tasks</button>'+
    '<button class="small '+(mode==='projects'?'active':'')+
    '" onclick="S.kanbanMode=\'projects\';renderAll()">Projects</button>'+
    '<span class="muted">drag a card to a valid column to change status; '+
    'READY/WAITING are derived and not directly editable.</span></div>'+
    '<div class="kanban">';
  cols.forEach(function(col){
    const items=rows.filter(function(o){return kanbanColumn(o,mode)===col;});
    h+='<div class="kcol" ondragover="event.preventDefault();this.classList.add(\'drop\')" '+
      'ondragleave="this.classList.remove(\'drop\')" ondrop="dropKanban(event,\''+
      col+'\',\''+mode+'\')"><h4><span>'+col+'</span><span>'+
      items.length+'</span></h4>';
    h+=items.map(function(o){
      return '<div class="kcard '+highlightClass(o)+
      (S.selection===o.id?' selected':'')+'" draggable="true" '+
      'ondragstart="dragStart(event,\''+o.id+'\')" '+
      'style="border-left:4px solid '+colourOf(o)+'" '+
      'onclick="selectObject(\''+o.id+'\')">'+cardBody(o)+'</div>';}).join('');
    h+='</div>';});
  h+='</div>';$('view-root').innerHTML=h;}
function dragStart(e,id){e.dataTransfer.setData('text/plain',id);}
async function dropKanban(e,col,mode){
  e.preventDefault();
  const id=e.dataTransfer.getData('text/plain');const o=obj(id);
  if(!o)return;
  if(mode==='projects'){
    try{await api('/api/projects/'+encodeURIComponent(o.project_id),'POST',
      {status:col});await refresh();}catch(err){notice(err.message);}return;}
  if(col==='READY'||col==='WAITING'||col==='IN_PROGRESS'){
    notice('READY and WAITING are derived; use the inspector to set status or waiting_for.');
    if(col==='IN_PROGRESS'){await mutateTask(o.ref_id,{status:'IN_PROGRESS'});}
    return;}
  if(col==='TODO'){await mutateTask(o.ref_id,{status:'TODO'});return;}
  if(col==='BLOCKED'){await recordOutcome(o.ref_id,'BLOCKED');return;}
  if(col==='DEFERRED'){await recordOutcome(o.ref_id,'DEFERRED');return;}
  if(col==='DONE'){await recordOutcome(o.ref_id,'COMPLETED');return;}}

// --- WBS ---------------------------------------------------------------------
function renderWbs(){
  const roots=S.views.wbs.roots;
  const selected=S.selection?obj(S.selection):null;
  let h='<div class="bar"><button class="small" onclick="S.collapsed={};renderAll()">'+
    'Expand all</button><button class="small" onclick="collapseAll()">Collapse all</button>'+
    '<span class="muted">AREA → PROJECT → WORKSTREAM → PACKAGE → TASK. '+
    'Click to select; + adds a child task.</span></div>';
  h+=roots.map(function(r){return wbsNode(r,0);}).join('');
  if(!roots.length)h+='<p class="muted">no hierarchy</p>';
  $('view-root').innerHTML=h;
  if(selected&&selected.ref_id){/* selection highlighted via class */}}
function collapseAll(){const set={};objects().forEach(function(o){
  set['project:'+o.project_id]=true;});S.collapsed=set;renderAll();}
function wbsNode(node,depth){
  const pad=depth*18;
  const isTask=node.kind==='TASK';
  const objectId=isTask?('task:'+node.ref_id):(node.kind==='PROJECT'?
    ('project:'+node.ref_id):null);
  const collapsed=S.collapsed[node.id];
  const kids=node.children||[];
  const tw=kids.length?'<span class="tw" onclick="event.stopPropagation();'+
    'toggleWbs(\''+node.id+'\')">'+(collapsed?'▸':'▾')+'</span>':'<span class="tw"></span>';
  const sel=S.selection===objectId?' selected':'';
  const colour=isTask&&objectId&&obj(objectId)?colourOf(obj(objectId)):
    (node.kind==='PROJECT'&&obj('project:'+node.ref_id)?
      colourOf(obj('project:'+node.ref_id)):'#9aa4b2');
  let h='<div class="wbsrow'+sel+'" style="margin-left:'+pad+'px" '+
    (objectId?'onclick="selectObject(\''+objectId+'\')"':'')+'>'+tw+
    '<span class="swatch" style="background:'+colour+'"></span>'+
    pill(node.kind,'badge-ACTIVE')+'<strong>'+esc(node.label)+'</strong>';
  if(node.status)h+=' '+pill(node.status,'badge-'+node.status);
  if(node.progress!=null)h+=' <span class="wbsbar"><span style="width:'+
    Math.round(node.progress*100)+'%"></span></span> <span class="muted">'+
    Math.round(node.progress*100)+'%</span>';
  if(node.count)h+=' <span class="muted">'+node.completed+'/'+node.count+'</span>';
  if(node.effort_minutes)h+=' <span class="muted">'+fmtMin(node.effort_minutes)+
    '</span>';
  if(node.evidence&&node.evidence!=='FACT')h+=' '+pill(node.evidence,
    'badge-'+node.evidence);
  if(node.kind==='PROJECT')h+=' <button class="small" onclick="event.'+
    'stopPropagation();openTaskForm(\''+node.ref_id+'\')">+ child</button>';
  h+='</div>';
  if(!collapsed){for(let i=0;i<kids.length;i++){h+=wbsNode(kids[i],depth+1);}}
  return h;}
function toggleWbs(id){if(S.collapsed[id])delete S.collapsed[id];
  else S.collapsed[id]=true;renderAll();}

// --- Graph -------------------------------------------------------------------
function graphLayout(nodes,edges){
  const preds={};nodes.forEach(function(n){preds[n.id]=[];});
  edges.forEach(function(e){if(preds[e.to])preds[e.to].push(e.from);});
  const layer={};const visiting={};
  function depth(id){if(layer[id]!=null)return layer[id];
    if(visiting[id])return 0;visiting[id]=true;
    let d=0;preds[id].forEach(function(p){d=Math.max(d,depth(p)+1);});
    visiting[id]=false;layer[id]=d;return d;}
  nodes.forEach(function(n){depth(n.id);});
  const byLayer={};nodes.forEach(function(n){const l=layer[n.id]||0;
    (byLayer[l]=byLayer[l]||[]).push(n);});
  const pos={};Object.keys(byLayer).forEach(function(l){
    byLayer[l].sort(function(a,b){return a.title.localeCompare(b.title);});
    byLayer[l].forEach(function(n,i){pos[n.id]={x:60+Number(l)*190,
      y:40+i*54};});});
  return pos;}
function renderGraph(){
  const g=S.views.graph;const nodes=g.nodes;const edges=g.edges;
  const sel=S.selection&&obj(S.selection)&&obj(S.selection).kind==='task'?
    obj(S.selection).ref_id:null;
  let allowed=null;
  if(sel&&S.graphDepth!=='full'){
    const adj={};nodes.forEach(function(n){adj[n.id]=[];});
    edges.forEach(function(e){adj[e.from].push(e.to);});
    allowed={};allowed[sel]=true;let frontier=[sel];
    const levels=S.graphDepth===0?0:Number(S.graphDepth);
    for(let d=0;d<levels;d++){const nxt=[];
      frontier.forEach(function(n){(adj[n]||[]).forEach(function(m){
        if(!allowed[m]){allowed[m]=true;nxt.push(m);}});});frontier=nxt;}
  }
  const pos=graphLayout(nodes,edges);
  const width=Math.max(700,60+ (Math.max.apply(null,[0].concat(
    Object.keys(pos).map(function(k){return pos[k].x;}))) + 160));
  const height=Math.max(400,40+ (Math.max.apply(null,[0].concat(
    Object.keys(pos).map(function(k){return pos[k].y;}))) + 60));
  let h='<div class="bar">'+
    '<span class="muted">Depth:</span>'+
    ['0','1','2','full'].map(function(d){return '<button class="small '+
      (String(S.graphDepth)===d?'active':'')+'" onclick="S.graphDepth='+
      (d==='full'?'\'full\'':d)+';renderAll()">'+
      (d==='full'?'Full chain':d+' level')+'</button>';}).join('')+
    '<span class="muted">confirmed dependencies solid; suggested dashed. '+
    'Zoom with wheel, pan by dragging.</span></div>'+
    '<svg id="graphsvg" width="100%" height="'+(height+40)+
    '" viewBox="0 0 '+width+' '+height+'" style="background:#fff;'+
    'border:1px solid #e2e6ec;border-radius:10px">'+
    '<g id="gpan">';
  edges.forEach(function(e){const a=pos[e.from],b=pos[e.to];
    if(!a||!b)return;const dim=allowed&&!(allowed[e.from]&&allowed[e.to]);
    const dash=e.kind==='suggested'?'5,4':'';
    h+='<line x1="'+a.x+'" y1="'+a.y+'" x2="'+b.x+'" y2="'+b.y+
      '" stroke="'+(e.kind==='blocker'?'#e45756':'#8b98a8')+
      '" stroke-width="1.3" stroke-dasharray="'+dash+'" opacity="'+
      (dim?0.12:0.85)+'"/>';});
  nodes.forEach(function(n){const p=pos[n.id];const o=obj('task:'+n.id);
    const c=o?colourOf(o):'#9aa4b2';const dim=allowed&&!allowed[n.id];
    const r=10+Math.min(10,(n.unlocks_count||0)*3);
    h+='<g class="tmnode" opacity="'+(dim?0.18:1)+'" onclick="selectObject(\''+
      'task:'+esc(n.id)+'\')"><circle cx="'+p.x+'" cy="'+p.y+'" r="'+r+
      '" fill="'+c+'" stroke="'+(S.selection==='task:'+n.id?'#0b2a4a':'#fff')+
      '" stroke-width="2"/><text x="'+(p.x+r+4)+'" y="'+(p.y+4)+
      '" font-size="11">'+esc(n.title.length>34?n.title.slice(0,32)+'…':
      n.title)+'</text></g>';});
  h+='</g></svg>';
  $('view-root').innerHTML=h;
  const svg=$('graphsvg');if(svg){const pan=$('gpan');let dragging=false,
    sx=0,sy=0,tx=0,ty=0,k=1;
    function apply(){pan.setAttribute('transform','translate('+tx+','+ty+
      ') scale('+k+')');}
    svg.addEventListener('wheel',function(ev){ev.preventDefault();
      k=Math.max(0.3,Math.min(3,k*(ev.deltaY<0?1.1:0.9)));apply();},
      {passive:false});
    svg.addEventListener('mousedown',function(ev){dragging=true;sx=ev.clientX;
      sy=ev.clientY;});
    window.addEventListener('mouseup',function(){dragging=false;});
    svg.addEventListener('mousemove',function(ev){if(!dragging)return;
      tx+=ev.clientX-sx;ty+=ev.clientY-sy;sx=ev.clientX;sy=ev.clientY;apply();});}}

// --- Gantt -------------------------------------------------------------------
function addDays(iso,n){const d=new Date(iso+'T12:00:00');d.setDate(d.getDate()+n);
  const y=d.getFullYear();const m=String(d.getMonth()+1).padStart(2,'0');
  const dd=String(d.getDate()).padStart(2,'0');return y+'-'+m+'-'+dd;}
function renderGantt(){
  const gantt=S.views.gantt;
  const today=S.views.today;
  const scaleLen={week:14,month:31,quarter:92}[S.ganttScale]||14;
  const allowed=allowedSet();
  const scheduled=gantt.scheduled.filter(function(t){
    return allowed['task:'+t.id];});
  const dated=gantt.dated.filter(function(t){return allowed['task:'+t.id];});
  const unscheduled=gantt.unscheduled.filter(function(t){
    return allowed['task:'+t.id];});
  const waiting=gantt.waiting.filter(function(t){return allowed['task:'+t.id];});
  const blocked=gantt.blocked.filter(function(t){return allowed['task:'+t.id];});
  const dayMap={};scheduled.forEach(function(t){dayMap[t.id]=t.day;});
  const deadlineMap={};dated.forEach(function(t){deadlineMap[t.id]=t.deadline;});
  const allDays=[];for(let i=0;i<scaleLen;i++){allDays.push(addDays(today,i));}
  const cellW=S.ganttScale==='quarter'?14:(S.ganttScale==='month'?18:34);
  let h='<div class="bar"><span class="muted">Scale:</span>'+
    ['week','month','quarter'].map(function(s){return '<button class="small '+
      (S.ganttScale===s?'active':'')+'" onclick="S.ganttScale=\''+s+
      '\';renderAll()">'+s+'</button>';}).join('')+
    '<span class="muted">No date is invented: scheduled days come from the '+
    'deterministic plan, deadlines from stored data.</span></div>';
  if(!gantt.has_dates&&!gantt.has_schedule){
    h+='<p class="insufficient">No dated or scheduled work in the current '+
      'portfolio — everything is shown as unscheduled.</p>';}
  const rects={};
  h+='<div style="overflow:auto"><table><tr><th style="min-width:220px">'+
    'Task</th>'+allDays.map(function(d){return '<th style="font-size:.66rem;'+
      'transform:rotate(-45deg);white-space:nowrap">'+d.slice(5)+'</th>';}).
    join('')+'</tr>';
  const lanes=[['Scheduled',scheduled],['Dated',dated.filter(function(t){
    return dayMap[t.id]==null;})]];
  lanes.forEach(function(lane){
    lane[1].forEach(function(t){const o=obj('task:'+t.id);
      const colour=o?colourOf(o):'#0b5cad';
      h+='<tr class="'+(S.selection==='task:'+t.id?'selected':'')+
        '" onclick="selectObject(\'task:'+esc(t.id)+'\')"><td><span class="swatch" '+
        'style="background:'+colour+'"></span> '+esc(t.title)+
        ' <span class="muted">'+fmtMin(t.estimated_minutes)+'</span></td>';
      allDays.forEach(function(d){
        let cell='';
        if(dayMap[t.id]===d||(!dayMap[t.id]&&deadlineMap[t.id]===d)){
          const width=Math.max(1,Math.round((t.estimated_minutes||30)/
            60)*cellW*0.6);
          cell='<span style="display:inline-block;height:12px;width:'+
            width+'px;background:'+colour+';border-radius:3px"></span>';
          rects[t.id]={day:d};
        } else if(deadlineMap[t.id]===d&&dayMap[t.id]){cell='<span title="deadline" '+
          'style="color:#b3261e">◆</span>';}
        h+='<td>'+cell+'</td>';});
      h+='</tr>';});});
  h+='</table></div>';
  if(gantt.unscheduled.length){
    h+='<h3>Unscheduled lane ('+unscheduled.length+')</h3><div class="grid3">';
    h+=unscheduled.map(function(t){const o=obj('task:'+t.id);
      return card(o);}).join('')+'</div>';}
  if(gantt.waiting.length){h+='<h3>Waiting ('+waiting.length+')</h3>'+
    '<div class="grid3">'+waiting.map(function(t){
      return card(obj('task:'+t.id));}).join('')+'</div>';}
  if(gantt.blocked.length){h+='<h3>Blocked ('+blocked.length+')</h3>'+
    '<div class="grid3">'+blocked.map(function(t){
      return card(obj('task:'+t.id));}).join('')+'</div>';}
  $('view-root').innerHTML=h;}

// --- Eisenhower --------------------------------------------------------------
function renderEisenhower(){
  const q=S.views.eisenhower.quadrants;const mode=S.eiMode;
  const allowed=allowedSet();
  const ids=function(list){return list.filter(function(id){
    return allowed[id]&&(mode==='tasks'?isTask(obj(id)):
      obj(id).kind==='project');});};
  const order=['DO','SCHEDULE','DELEGATE','ELIMINATE'];
  let h='<div class="bar"><button class="small '+(mode==='tasks'?'active':'')+
    '" onclick="S.eiMode=\'tasks\';renderAll()">Tasks</button>'+
    '<button class="small '+(mode==='projects'?'active':'')+
    '" onclick="S.eiMode=\'projects\';renderAll()">Projects</button>'+
    '<span class="muted">Drag to a quadrant to set urgency/impact '+
    '(confirmed before writing).</span></div><div class="grid2">';
  order.forEach(function(qd){h+='<div class="quad" ondragover="event.'+
    'preventDefault();this.classList.add(\'drop\')" ondragleave="this.'+
    'classList.remove(\'drop\')" ondrop="dropEisenhower(event,\''+qd+
    '\')"><h4>'+esc(qd)+' ('+ids(q[qd]).length+')</h4>';
    h+=ids(q[qd]).map(function(id){const o=obj(id);
      return '<div class="kcard '+highlightClass(o)+
      (S.selection===id?' selected':'')+'" draggable="true" '+
      'ondragstart="dragStart(event,\''+id+'\')" style="border-left:4px solid '+
      colourOf(o)+'" onclick="selectObject(\''+id+'\')">'+cardBody(o,true)+
      '</div>';}).join('')+'</div>';});
  h+='</div>';$('view-root').innerHTML=h;}
async function dropEisenhower(e,qd){
  e.preventDefault();const id=e.dataTransfer.getData('text/plain');const o=obj(id);
  if(!o)return;const map={DO:['CRITICAL','HIGH'],SCHEDULE:['LOW','HIGH'],
    DELEGATE:['HIGH','LOW'],ELIMINATE:['LOW','LOW']};
  const u=map[qd][0],i=map[qd][1];
  if(!confirm('Move "'+o.title+'" to '+qd+'? This sets urgency='+u+
    ' and impact='+i+'.'))return;
  try{if(isTask(o))await api('/api/tasks/'+encodeURIComponent(o.ref_id),'POST',
    {urgency:u,impact:i});else await api('/api/projects/'+
    encodeURIComponent(o.project_id),'POST',{urgency:u,impact:i});
    await refresh();}catch(err){notice(err.message);}}

// --- Impact / effort ---------------------------------------------------------
function renderImpactEffort(){
  const q=S.views.impact_effort.quadrants;const mode=S.ieMode;
  const allowed=allowedSet();
  const ids=function(list){return list.filter(function(id){
    return allowed[id]&&(mode==='tasks'?isTask(obj(id)):
      obj(id).kind==='project');});};
  const order=['quick_win','major_bet','filler','low_return','midfield',
    'unknown_effort'];
  const labels={quick_win:'Quick wins',major_bet:'Major bets',filler:'Filler',
    low_return:'Low return',midfield:'Midfield',unknown_effort:'Unknown effort'};
  let h='<div class="bar"><button class="small '+(mode==='tasks'?'active':'')+
    '" onclick="S.ieMode=\'tasks\';renderAll()">Tasks</button>'+
    '<button class="small '+(mode==='projects'?'active':'')+
    '" onclick="S.ieMode=\'projects\';renderAll()">Projects</button>'+
    '<span class="muted">Effort basis: estimated (never actual). '+
    'Quick ≤'+S.views.impact_effort.thresholds.quick_win_max+'m · '+
    'Major ≥'+S.views.impact_effort.thresholds.major_bet_min+'m.</span>'+
    '</div><div class="grid3">';
  order.forEach(function(qd){h+='<div class="quad"><h4>'+esc(labels[qd])+
    ' ('+ids(q[qd]).length+')</h4>'+ids(q[qd]).map(function(id){
      return card(obj(id),cardBody(obj(id),true));}).join('')+'</div>';});
  h+='</div>';$('view-root').innerHTML=h;}

// --- Portfolio map -----------------------------------------------------------
function axisValue(o,axis){if(axis==='urgency')return o.urgency_rank;
  if(axis==='impact')return o.impact_rank;if(axis==='effort')return
  (o.effort_minutes||o.estimated_minutes||0);if(axis==='progress')return
  (o.progress==null?0:o.progress);return 0;}
function renderPortfolioMap(){
  const rows=filteredProjects();const axes=S.views.portfolio_map.axes;
  const xa=S.mapX,ya=S.mapY;const W=760,H=420,pad=50;
  const xmax=Math.max.apply(null,[1].concat(rows.map(function(o){
    return axisValue(o,xa);})));
  const ymax=Math.max.apply(null,[1].concat(rows.map(function(o){
    return axisValue(o,ya);})));
  let h='<div class="bar"><label>X</label><select class="inline" '+
    'onchange="S.mapX=this.value;renderAll()">'+axes.map(function(a){
      return '<option'+(a===xa?' selected':'')+'>'+a+'</option>';}).join('')+
    '</select><label>Y</label><select class="inline" onchange="S.mapY='+
    'this.value;renderAll()">'+axes.map(function(a){
      return '<option'+(a===ya?' selected':'')+'>'+a+'</option>';}).join('')+
    '</select><label>Size</label><select class="inline" onchange="S.mapSize='+
    'this.value;renderAll()">'+S.views.portfolio_map.sizes.map(function(s){
      return '<option'+(s===S.mapSize?' selected':'')+'>'+s+'</option>';
      }).join('')+'</select></div>';
  h+='<svg width="100%" height="'+H+'" viewBox="0 0 '+W+' '+H+
    '" style="background:#fff;border:1px solid #e2e6ec;border-radius:10px">';
  h+='<line x1="'+pad+'" y1="'+(H-pad)+'" x2="'+(W-20)+'" y2="'+(H-pad)+
    '" stroke="#c9d2dc"/><line x1="'+pad+'" y1="20" x2="'+pad+'" y2="'+
    (H-pad)+'" stroke="#c9d2dc"/>';
  h+='<text x="'+(W/2)+'" y="'+(H-12)+'" font-size="12" text-anchor="middle">'+
    esc(xa)+'</text><text x="14" y="'+(H/2)+'" font-size="12" '+
    'transform="rotate(-90 14 '+(H/2)+')" text-anchor="middle">'+esc(ya)+
    '</text>';
  rows.forEach(function(o){const x=pad+(axisValue(o,xa)/xmax)*(W-pad-30);
    const y=(H-pad)-(axisValue(o,ya)/ymax)*(H-pad-40);
    const size=6+Math.min(24,Math.sqrt(o.task_count||1)*4);
    h+='<g onclick="selectObject(\''+o.id+'\')" style="cursor:pointer">'+
      '<circle cx="'+x+'" cy="'+y+'" r="'+size+'" fill="'+colourOf(o)+
      '" opacity="0.85" stroke="'+(S.selection===o.id?'#0b2a4a':'#fff')+
      '" stroke-width="2"/><text x="'+(x+size+3)+'" y="'+(y+3)+
      '" font-size="10">'+esc(o.title.slice(0,22))+'</text></g>';});
  h+='</svg>';$('view-root').innerHTML=h;}

// --- Treemap -----------------------------------------------------------------
function renderTreemap(){
  const group=S.treemapGroup;const metric=S.treemapMetric;
  const root=S.views.treemap.groupings[group];
  const W=780,H=460;
  const total=Math.max(1,root[metric]||root.task_count||1);
  let rects=[];
  function layout(node,x,y,w,h,depth){
    const kids=(node.children||[]).slice().sort(function(a,b){
      return (b[metric]||b.task_count)-(a[metric]||a.task_count);});
    if(!kids.length||depth>=2){rects.push({node:node,x:x,y:y,w:w,h:h});
      return;}
    const sum=kids.reduce(function(a,k){return a+Math.max(1,
      k[metric]||k.task_count);},0);
    let cx=x;
    kids.forEach(function(k){const frac=Math.max(1,k[metric]||k.task_count)/sum;
      const kw=w*frac;layout(k,cx,y,kw,h,depth+1);cx+=kw;});}
  layout(root,0,0,W,H,0);
  const colours={};objects().forEach(function(o){
    if(o.kind==='project')colours['project:'+o.project_id]=colourOf(o);});
  let h='<div class="bar"><label>Group by</label><select class="inline" '+
    'onchange="S.treemapGroup=this.value;renderAll()">'+
    Object.keys(S.views.treemap.groupings).map(function(g){
      return '<option'+(g===group?' selected':'')+'>'+g+'</option>';}).join('')+
    '</select><label>Size by</label><select class="inline" onchange="S.'+
    'treemapMetric=this.value;renderAll()">'+S.views.treemap.metrics.map(
      function(m){return '<option'+(m===metric?' selected':'')+'>'+m+
      '</option>';}).join('')+'</select>'+
    '<span class="muted">Click a tile to select its project (when known).</span>'+
    '</div>';
  h+='<svg width="100%" height="'+H+'" viewBox="0 0 '+W+' '+H+
    '" style="background:#fff;border:1px solid #e2e6ec;border-radius:10px">';
  rects.forEach(function(r){const key=r.node.key;const colour=colours[key]||
    (r.node.key&&colours['project:'+r.node.key])||'#7f9bb5';
    h+='<g class="tmnode" onclick="treemapClick(\''+esc(String(r.node.key))+
      '\')"><rect x="'+r.x+'" y="'+r.y+'" width="'+Math.max(0,r.w-2)+
      '" height="'+Math.max(0,r.h-2)+'" fill="'+colour+
      '" opacity="0.82" stroke="#fff"/><text x="'+(r.x+5)+'" y="'+(r.y+15)+
      '" font-size="11" fill="#fff">'+esc(String(r.node.label).slice(0,20))+
      '</text></g>';});
  h+='</svg>';$('view-root').innerHTML=h;}
function treemapClick(key){for(let i=0;i<objects().length;i++){const o=objects()[i];
  if(o.kind==='project'&&(o.project_id===key||o.domain===key||
    o.workstream===key)){selectObject(o.id);return;}}
  notice('No project directly matches this group.');}

// --- Progress ----------------------------------------------------------------
function renderProgress(){
  const p=S.views.progress;const o=p.overall;
  let h='<h2>Overall</h2><div class="grid3">'+
    '<div class="card"><strong>'+o.completed+' / '+o.tasks+'</strong><br>'+
    '<span class="muted">tasks completed</span><div class="wbsbar" '+
    'style="width:100%"><span style="width:'+Math.round((o.progress||0)*100)+
    '%"></span></div></div>'+
    '<div class="card"><strong>'+fmtMin(o.estimated_minutes)+'</strong><br>'+
    '<span class="muted">estimated remaining portfolio effort</span></div>'+
    '<div class="card"><strong>'+p.planned_vs_actual.completed_with_actual+
    '</strong><br><span class="muted">completed with recorded actual effort'+
    '</span></div></div>';
  h+='<h2>Planned vs actual</h2><p class="muted">Planned '+
    fmtMin(p.planned_vs_actual.planned_minutes)+' · actual '+
    fmtMin(p.planned_vs_actual.actual_minutes)+
    (p.planned_vs_actual.mean_absolute_error_minutes!=null?
    ' · mean error '+fmtMin(Math.round(p.planned_vs_actual.
    mean_absolute_error_minutes)):'')+'</p>';
  h+='<h2>Projects</h2><table><tr><th>Project</th><th>Progress</th>'+
    '<th>Done</th><th>Estimated</th><th>Actual</th></tr>'+
    p.projects.map(function(pr){return '<tr class="'+
      (S.selection==='project:'+pr.project_id?'selected':'')+
      '" onclick="selectObject(\'project:'+esc(pr.project_id)+'\')"><td>'+
      esc(pr.title)+'</td><td>'+(pr.progress==null?'—':
      Math.round(pr.progress*100)+'%')+'</td><td>'+pr.completed+'/'+pr.tasks+
      '</td><td>'+fmtMin(pr.estimated_minutes)+'</td><td>'+
      fmtMin(pr.actual_minutes)+'</td></tr>';}).join('')+'</table>';
  h+='<h2>Throughput</h2>';
  if(p.throughput.insufficient_data){h+='<p class="insufficient">'+
    'Insufficient historical data for a meaningful throughput chart.'+'</p>';}
  else{h+='<table><tr><th>Day</th><th>Completed</th><th>Actual minutes</th>'+
    '</tr>'+p.throughput.days.map(function(d){return '<tr><td>'+esc(d.day)+
    '</td><td>'+d.completed+'</td><td>'+d.actual_minutes+'</td></tr>';}).
    join('')+'</table>';}
  $('view-root').innerHTML=h;}

// --- Heatmap -----------------------------------------------------------------
function renderHeatmap(){
  const hm=S.views.heatmap;const act=hm.activity;const plan=hm.planned_load;
  let h='<h2>Activity (completed outcomes)</h2>';
  if(act.insufficient_data){h+='<p class="insufficient">Insufficient '+
    'activity history to render a heatmap.</p>';}
  else{const max=Math.max(1,act.max_completed);
    h+='<div class="heatwrap">'+act.days.map(function(d){const t=
      d.completed/max;const col='rgba(11,92,173,'+Math.max(0.1,t)+')';
      return '<span class="heatcell" title="'+esc(d.day)+': '+d.completed+
      ' completed, '+d.actual_minutes+' min" style="background:'+col+'">'+
      esc(d.day.slice(5))+'<br>'+d.completed+'</span>';}).join('')+'</div>';}
  h+='<h2>Planned load (next days)</h2>';
  if(plan.insufficient_data){h+='<p class="insufficient">Nothing planned '+
    'in the current window.</p>';}
  else{let maxp=1;plan.days.forEach(function(d){maxp=Math.max(maxp,
      d.planned_minutes);});
    h+='<div class="daygrid">'+plan.days.map(function(d){return '<div '+
      'title="'+esc(d.day)+': '+d.planned_minutes+' min planned"><div '+
      'class="daybar" style="height:'+Math.round(d.planned_minutes/maxp*100)+
      'px"></div><div class="muted" style="font-size:.62rem">'+
      esc(d.day.slice(5))+'</div></div>';}).join('')+'</div>';}
  h+='<h2>Domain activity</h2><table><tr><th>Domain</th><th>Tasks</th>'+
    '<th>Completed</th><th>Progress</th></tr>'+hm.domains.map(function(d){
    return '<tr><td>'+esc(d.domain)+'</td><td>'+d.tasks+'</td><td>'+
    d.completed+'</td><td>'+(d.progress==null?'—':
    Math.round(d.progress*100)+'%')+'</td></tr>';}).join('')+'</table>';
  $('view-root').innerHTML=h;}

// --- Goal flow ---------------------------------------------------------------
function renderGoalFlow(){
  const f=S.views.goal_flow;const cols=f.columns;
  const byCol={};cols.forEach(function(c){byCol[c]=[];});
  f.nodes.forEach(function(n){if(byCol[n.kind])byCol[n.kind].push(n);});
  let h='<p class="muted">AREA → PROJECT → WORKSTREAM → TASK → DELIVERABLE '+
    '→ OUTCOME. Only real, stored relationships are shown.</p>'+
    '<div class="kanban">';
  cols.forEach(function(c){h+='<div class="flowcol"><h4>'+c+' ('+
    byCol[c].length+')</h4>'+byCol[c].map(function(n){
      const isTask=n.kind==='TASK';const o=isTask?obj('task:'+n.id.slice(5)):
        (n.kind==='PROJECT'?obj('project:'+n.id.slice(8)):null);
      return '<div class="kcard '+highlightClass(o)+
      (o&&S.selection===o.id?' selected':'')+'" '+
      (o?'onclick="selectObject(\''+o.id+'\')"':'')+' style="border-left:4px '+
      'solid '+(o?colourOf(o):'#9aa4b2')+'"><div style="font-size:.8rem">'+
      esc(n.label)+'</div>'+(n.status?'<span class="muted" '+
      'style="font-size:.7rem">'+esc(n.status)+'</span>':'')+'</div>';
      }).join('')+'</div>';});
  h+='</div>';$('view-root').innerHTML=h;}

// --- Focus / context ---------------------------------------------------------
function renderFocus(){
  const o=S.selection?obj(S.selection):null;
  if(!o){$('view-root').innerHTML='<p class="muted">Select a task or project '+
    'to see its execution context (upstream → selected → downstream).</p>';return;}
  const depthButtons=['Immediate','1 level','2 levels','Full chain'];
  const depthVals=[0,1,2,'full'];
  const maxLevels=S.graphDepth==='full'?99:(Number(S.graphDepth)+1);
  let h='<div class="bar"><span class="muted">Context depth:</span>'+
    depthButtons.map(function(label,i){return '<button class="small '+
      (String(S.graphDepth)===String(depthVals[i])?'active':'')+'" '+
      'onclick="S.graphDepth='+(depthVals[i]==='full'?'\'full\'':depthVals[i])+
      ';renderAll()">'+label+'</button>';}).join('')+
    '</div>';
  function listFor(levels){
    if(!levels||!levels.length)return '<span class="muted">none</span>';
    return levels.slice(0,maxLevels).map(function(lvl,i){return '<div style="margin-left:'+
      (i*12)+'px">'+lvl.map(function(id){const t=obj('task:'+id);return t?
      '<a onclick="selectObject(\'task:'+esc(id)+'\')">'+esc(t.title)+'</a>':
      esc(id);}).join(' · ')+'</div>';}).join('');}
  h+='<div class="grid2">';
  h+='<div class="quad"><h4>Upstream — what comes before</h4>'+
    '<div class="muted">Prerequisites / dependencies / blockers</div>'+
    listFor(o.upstream_levels)+'</div>';
  h+='<div class="quad"><h4>Downstream — what this unlocks</h4>'+
    '<div class="muted">Next actions / unlocked tasks / deliverables</div>'+
    listFor(o.downstream_levels)+'</div>';
  h+='</div>';
  h+='<div class="card" style="margin:.6rem 0;border-left:5px solid '+
    colourOf(o)+'"><h3>'+esc(o.title)+'</h3>'+
    '<div class="muted">'+pill(o.kind,'badge-ACTIVE')+
    (o.project_id!==o.ref_id?esc(o.project)+' · ':'')+esc(o.domain)+
    (o.workstream?' · '+esc(o.workstream):'')+'</div>'+
    '<div class="muted" style="margin-top:.3rem">This answers: why this '+
    'object? what do I need? what comes before? what does it block? what '+
    'does it unlock?</div></div>';
  h+='<div class="grid3"><div class="card"><h4>Why</h4>'+
    (o.priority_reasons.length?o.priority_reasons.map(function(r){
      return '<div class="muted">'+esc(r)+'</div>';}).join(''):
      '<span class="muted">no priority reasons</span>')+'</div>'+
    '<div class="card"><h4>What I need</h4>'+
    (o.blocked_by_titles.length||o.waiting_for.length?
      (o.blocked_by_titles.map(function(t){return '<div class="muted">blocked '+
      'by '+esc(t)+'</div>';}).join('')+o.waiting_for.map(function(t){
      return '<div class="muted">waiting for '+esc(t)+'</div>';}).join('')):
      '<span class="muted">no explicit prerequisites</span>')+'</div>'+
    '<div class="card"><h4>What it unlocks</h4><div class="muted">'+
    o.unlocks_count+' downstream object(s)</div></div></div>';
  $('view-root').innerHTML=h;}

// --- Inspector ---------------------------------------------------------------
function renderInspector(){
  const el=$('inspector');
  if(!el)return;
  const o=S.selection?obj(S.selection):null;
  if(!o){el.innerHTML='<h2>Inspector</h2><p class="muted">Select an object. '+
    'Attributes, colour identity and validated actions appear here.</p>';return;}
  const density=S.prefs.density;
  const catalog=S.views.attribute_catalog;
  function attr(key,label,value){if(density==='compact'&&
    (key!=='title'&&key!=='status'&&key!=='kind'))return '';
    if(density==='normal'&&catalog.filter(function(a){return a.key===key&&
      a.density==='detailed';}).length)return '';
    if(value==null||value===''||(Array.isArray(value)&&!value.length))return '';
    return '<dt>'+esc(label)+'</dt><dd>'+value+'</dd>';}
  let h='<h2>Inspector</h2>';
  h+='<div class="card" style="border-left:5px solid '+colourOf(o)+'">'+
    '<strong>'+esc(o.title)+'</strong><div class="muted">'+
    pill(o.kind,'badge-ACTIVE')+pill(o.evidence,'badge-'+o.evidence)+
    (o.kind==='task'?pill(uiStatus(o),'badge-'+uiStatus(o)):'')+
    (o.highlight?'<span class="pill">highlight: '+esc(o.highlight)+'</span>':'')+
    '</div></div>';
  h+='<dl class="attr">';
  h+=attr('title','Title',esc(o.title));
  h+=attr('kind','Type',esc(o.kind));
  h+=attr('status','Status',esc(o.status));
  h+=attr('project','Project',esc(o.project));
  h+=attr('domain','Area/domain',esc(o.domain));
  h+=attr('workstream','Workstream',esc(o.workstream||'—'));
  h+=attr('work_package','Work package',esc(o.work_package||'—'));
  h+=attr('readiness','Readiness',esc(o.readiness||'—'));
  h+=attr('urgency','Urgency',esc(o.urgency));
  h+=attr('impact','Impact',esc(o.impact));
  h+=attr('priority','Priority',o.priority_rank?'#'+o.priority_rank+' (score '+
    o.priority_score+')':'—');
  h+=attr('priority_reasons','Why',o.priority_reasons.map(function(r){
    return esc(r);}).join('<br>'));
  h+=attr('estimated_minutes','Estimated',fmtMin(o.estimated_minutes));
  h+=attr('actual_minutes','Actual',fmtMin(o.actual_minutes));
  h+=attr('planned_vs_actual','Planned vs actual',o.estimate_error_minutes!=null?
    ('error '+o.estimate_error_minutes+' min'):'—');
  h+=attr('estimation_error','Estimation error',
    o.estimate_error_minutes!=null?o.estimate_error_minutes+' min':'—');
  h+=attr('deadline','Deadline',esc(o.deadline||'—'));
  h+=attr('next_action','Next action',esc(o.next_action||'—'));
  h+=attr('prerequisites','Prerequisites',o.dependency_titles.map(function(t){
    return esc(t);}).join('<br>'));
  h+=attr('dependencies','Dependencies',o.dependencies.map(function(d){
    return esc(d);}).join(', '));
  h+=attr('suggested_dependencies','Suggested deps',
    o.suggested_dependencies.join(', '));
  h+=attr('blockers','Blockers',o.blocked_by_titles.map(function(t){
    return esc(t);}).join('<br>'));
  h+=attr('waiting_for','Waiting for',o.waiting_for.map(function(t){
    return esc(t);}).join('<br>'));
  h+=attr('downstream','Downstream',o.downstream.map(function(id){const t=
    obj('task:'+id);return t?esc(t.title):esc(id);}).join('<br>'));
  h+=attr('deliverables','Deliverables',esc(o.deliverable||'—'));
  h+=attr('notes','Notes',esc(o.notes||'—'));
  h+=attr('outcomes','Outcomes',o.outcomes.map(function(r){
    return esc(r.outcome+' @ '+r.recorded_at.slice(0,10));}).join('<br>'));
  h+=attr('provenance','Provenance',esc(o.provenance));
  h+=attr('confidence','Confidence',esc(o.confidence));
  h+='</dl>';
  h+='<div class="actions">';
  if(isTask(o)){h+='<button class="small primary" onclick="openOutcomeForm(\''+
    o.ref_id+'\')">Record outcome…</button>'+
    '<button class="small" onclick="recordOutcome(\''+o.ref_id+
    '\',\'BLOCKED\')">Block</button>'+
    '<button class="small" onclick="recordOutcome(\''+o.ref_id+
    '\',\'DEFERRED\')">Defer</button>'+
    '<button class="small" onclick="mutateTask(\''+o.ref_id+
    '\',{status:\'TODO\'})">Reactivate</button>'+
    '<button class="small" onclick="openTask(\''+o.ref_id+'\')">Edit</button>';}
  else{h+='<button class="small primary" onclick="mutateProject(\''+
    o.project_id+'\',{status:\'COMPLETED\'})">Complete</button>'+
    '<button class="small" onclick="mutateProject(\''+o.project_id+
    '\',{status:\'DEFERRED\'})">Defer</button>'+
    '<button class="small" onclick="mutateProject(\''+o.project_id+
    '\',{status:\'ACTIVE\'})">Reactivate</button>'+
    '<button class="small" onclick="openProjectForm(\''+o.project_id+
    '\')">Edit</button>'+
    '<button class="small" onclick="openTaskForm(\''+o.project_id+
    '\')">Add task</button>'+
    '<button class="small" onclick="runEnrichment(\''+o.project_id+
    '\',\'next_actions\')">Next actions</button>'+
    '<button class="small" onclick="runEnrichment(\''+o.project_id+
    '\',\'generate_wbs\')">Generate WBS</button>'+
    '<button class="small" onclick="runEnrichment(\''+o.project_id+
    '\',\'suggest_dependencies\')">Suggest dependencies</button>'+
    '<button class="small" onclick="runEnrichment(\''+o.project_id+
    '\',\'suggest_deliverables\')">Suggest deliverables</button>';}
  h+='</div>';
  if(!isTask(o)){h+='<div class="bar"><span class="muted">AI engine:'+
    '</span><select class="inline" id="enr-engine-'+esc(o.project_id)+
    '">'+enrEngineOptions()+'</select><span class="muted">suggestions stay '+
    'SUGGESTED until accepted</span></div>'+
    '<div id="ai-suggestions-'+esc(o.project_id)+'"></div>';}
  h+='<div class="bar"><span class="muted">Personal highlight:</span>';
  ['yellow','purple','blue','green','red','orange','teal','grey'].forEach(
    function(c){h+='<button class="colourbtn hl-'+c+'" style="background:'+
      c+'" title="'+c+'" onclick="setHighlight(\''+o.id+'\',\''+c+'\')"></button>';});
  h+='<button class="small" onclick="setHighlight(\''+o.id+'\',null)">clear</button>'+
    '</div>';
  h+='<div class="bar"><span class="muted">Identity colour ('+
    esc(S.prefs.color_by)+' override):</span>'+
    '<input type="color" class="inline" id="id-colour" value="'+
    colourOf(o)+'">'+
    '<button class="small" onclick="setIdentityColour()">Apply</button></div>';
  el.innerHTML=h;
  if(!isTask(o)){loadEnrichment(o.project_id,false);}}
async function setHighlight(id,colour){
  try{await api('/api/highlight','POST',{object_id:id,colour:colour});
    await refresh();}catch(err){notice(err.message);}}
async function setIdentityColour(){const o=obj(S.selection);if(!o)return;
  const c=$('id-colour').value;
  const scope=S.prefs.color_by==='project'?'project':'domain';
  const key=scope==='project'?o.project_id:o.domain;
  try{await api('/api/colour','POST',{scope:scope,key:key,colour:c});
    S.prefs.color_by=scope;await refresh();}catch(err){notice(err.message);}}

// --- mutations ---------------------------------------------------------------
async function mutateTask(id,patch){try{
  await api('/api/tasks/'+encodeURIComponent(id),'POST',patch);
  await refresh();}catch(err){notice(err.message);}}
async function mutateProject(id,patch){try{
  await api('/api/projects/'+encodeURIComponent(id),'POST',patch);
  await refresh();}catch(err){notice(err.message);}}
async function recordOutcome(id,outcome){const o=obj('task:'+id);
  if(!confirm('Record '+outcome+' for "'+(o?o.title:id)+'"?'))return;
  try{await api('/api/tasks/'+encodeURIComponent(id)+'/outcome','POST',
    {outcome:outcome});await refresh();}catch(err){notice(err.message);}}
async function addDependency(taskId){const dep=$('dep-add').value;
  try{await api('/api/tasks/'+encodeURIComponent(taskId)+'/dependency','POST',
    {dependency:dep});await refresh();}catch(err){notice(err.message);}}
async function removeDependency(taskId,dep){try{
  await api('/api/tasks/'+encodeURIComponent(taskId)+'/dependency/'+
    encodeURIComponent(dep),'DELETE');await refresh();}catch(err){
  notice(err.message);}}

// --- forms (create / edit) ---------------------------------------------------
function openProject(pid){selectObject('project:'+pid);return;
  // retained for compatibility with older call sites
}
function openProjectForm(pid){const p=pid&&obj('project:'+pid)?
  obj('project:'+pid):{};const s=p;
  const opts=['ACTIVE','WAITING','BLOCKED','DEFERRED','COMPLETED'];
  const urg=['CRITICAL','HIGH','MEDIUM','LOW'];const imp=['HIGH','MEDIUM','LOW'];
  const sel=function(list,cur){return list.map(function(v){
    return '<option'+(v===cur?' selected':'')+'>'+v+'</option>';}).join('');};
  $('project-form-body').innerHTML=
    '<input type="hidden" id="pf-id" value="'+esc(s.ref_id||'')+'">'+
    '<label>Name</label><input id="pf-name" value="'+esc(s.title||'')+'">'+
    '<label>Objective</label><textarea id="pf-objective" rows="2">'+
    esc(s.objective||'')+'</textarea>'+
    '<div class="form-row"><div><label>Status</label><select id="pf-status">'+
    sel(opts,s.status||'ACTIVE')+'</select></div>'+
    '<div><label>Domain</label><input id="pf-domain" value="'+
    esc(s.domain||'personal')+'"></div></div>'+
    '<div class="form-row"><div><label>Urgency</label><select id="pf-urgency">'+
    sel(urg,s.urgency||'MEDIUM')+'</select></div>'+
    '<div><label>Impact</label><select id="pf-impact">'+
    sel(imp,s.impact||'MEDIUM')+'</select></div></div>'+
    '<label>Deadline (blank = none)</label><input type="date" id="pf-deadline" '+
    'value="'+esc(s.deadline||'')+'">'+
    '<label>Description</label><textarea id="pf-description" rows="2">'+
    esc(s.description||'')+'</textarea>';
  $('project-form-title').textContent=pid?'Edit project':'New project';
  $('project-form-overlay').classList.add('open');}
function closeProjectForm(){$('project-form-overlay').classList.remove('open');}
async function saveProject(){const id=$('pf-id').value;const body={
    name:$('pf-name').value,objective:$('pf-objective').value,
    status:$('pf-status').value,domain:$('pf-domain').value,
    urgency:$('pf-urgency').value,impact:$('pf-impact').value,
    deadline:$('pf-deadline').value,description:$('pf-description').value};
  try{if(id){await api('/api/projects/'+encodeURIComponent(id),'POST',body);}
    else{await api('/api/projects','POST',body);}
    closeProjectForm();await refresh();}catch(err){notice(err.message);}}
function openTaskForm(pid){document.getElementById('task-form-body').innerHTML=
    '<h3 style="margin:0">New task</h3>'+
    '<label>Title</label><input id="nt-title">'+
    '<label>Project</label><select id="nt-project">'+
    objects().filter(function(o){return o.kind==='project';}).sort(function(a,b){
      return a.title.localeCompare(b.title);}).map(function(p){
      return '<option value="'+esc(p.project_id)+'"'+(p.project_id===pid?
        ' selected':'')+'>'+esc(p.title)+'</option>';}).join('')+'</select>'+
    '<label>Workstream</label><input id="nt-workstream">'+
    '<label>Deliverable / work package</label><input id="nt-deliverable">'+
    '<label>Description</label><textarea id="nt-desc" rows="2"></textarea>'+
    '<div class="form-row"><div><label>Estimated minutes</label>'+
    '<input id="nt-est" type="number" min="1"></div>'+
    '<div><label>Deadline</label><input id="nt-deadline" type="date"></div></div>'+
    '<div class="form-row"><div><label>Urgency</label><select id="nt-urgency">'+
    ['CRITICAL','HIGH','MEDIUM','LOW'].map(function(v){return '<option'+
      (v==='MEDIUM'?' selected':'')+'>'+v+'</option>';}).join('')+'</select></div>'+
    '<div><label>Impact</label><select id="nt-impact">'+
    ['HIGH','MEDIUM','LOW'].map(function(v){return '<option'+
      (v==='MEDIUM'?' selected':'')+'>'+v+'</option>';}).join('')+'</select></div>'+
    '</div><label>Next action</label><input id="nt-next">';
  $('task-form-overlay').classList.add('open');}
function closeTaskForm(){$('task-form-overlay').classList.remove('open');}
async function saveNewTask(){const estRaw=$('nt-est').value.trim();const body={
    title:$('nt-title').value,project_id:$('nt-project').value,
    description:$('nt-desc').value,workstream:$('nt-workstream').value,
    deliverable:$('nt-deliverable').value,deadline:$('nt-deadline').value,
    urgency:$('nt-urgency').value,impact:$('nt-impact').value,
    next_action:$('nt-next').value};
  if(estRaw!==''){const n=Number(estRaw);if(!Number.isFinite(n)||n<1){
    notice('estimated minutes must be a positive number');return;}
    body.estimated_minutes=Math.round(n);}
  try{await api('/api/tasks','POST',body);closeTaskForm();await refresh();}
  catch(err){notice(err.message);}}
function openTask(id){const t=obj('task:'+id);if(!t)return;
  selectObject('task:'+id);
  $('task-body').innerHTML='<h3 style="margin:0">'+esc(t.title)+'</h3>'+
    '<div class="muted" style="margin:.3rem 0">'+pill(uiStatus(t),
    'badge-'+uiStatus(t))+esc(t.project)+'</div>'+
    '<div class="form-row"><div><label>Title</label><input id="tk-title" value="'+
    esc(t.title)+'"></div><div><label>Status</label><select id="tk-status">'+
    ['TODO','IN_PROGRESS','BLOCKED','WAITING','COMPLETED','DEFERRED','ABANDONED'].
    map(function(v){return '<option'+(v===t.status?' selected':'')+'>'+v+
    '</option>';}).join('')+'</select></div></div>'+
    '<div class="form-row"><div><label>Workstream</label><input id="tk-workstream" '+
    'value="'+esc(t.workstream||'')+'"></div><div><label>Deliverable</label>'+
    '<input id="tk-deliverable" value="'+esc(t.deliverable||'')+'"></div></div>'+
    '<div class="form-row"><div><label>Estimated minutes</label>'+
    '<input id="tk-est" type="number" min="1" value="'+
    (t.estimated_minutes==null?'':t.estimated_minutes)+'"></div>'+
    '<div><label>Actual minutes</label><input id="tk-actual" type="number" '+
    'min="0" value="'+(t.actual_minutes==null?'':t.actual_minutes)+'"></div></div>'+
    '<div class="form-row"><div><label>Deadline</label><input id="tk-deadline" '+
    'type="date" value="'+esc(t.deadline||'')+'"></div>'+
    '<div><label>Waiting for (comma)</label><input id="tk-waiting" value="'+
    esc(t.waiting_for.join(', '))+'"></div></div>'+
    '<label>Blocked by (comma task ids)</label><input id="tk-blocked" value="'+
    esc(t.blocked_by.join(', '))+'">'+
    '<label>Next action</label><input id="tk-next" value="'+
    esc(t.next_action||'')+'">'+
    '<label>Description</label><textarea id="tk-desc" rows="2">'+
    esc(t.description||'')+'</textarea>'+
    '<h4>Dependencies</h4><div id="tk-deps"></div>';
  renderDepsEditor(id,t);
  $('task-overlay').classList.add('open');}
function closeTask(){$('task-overlay').classList.remove('open');}
function renderDepsEditor(taskId,t){const others=objects().filter(function(o){
    return o.kind==='task'&&o.ref_id!==taskId;}).sort(function(a,b){
    return a.title.localeCompare(b.title);});
  let h='';if(t.dependencies.length){h+='<div>'+t.dependencies.map(function(d){
    const dt=obj('task:'+d);return '<span class="chip">'+
    esc(dt?dt.title:d)+' <button title="remove" onclick="removeDependency(\''+
    taskId+'\',\''+d+'\')">×</button></span>';}).join('')+'</div>';}
  else{h+='<div class="muted">no dependencies</div>';}
  h+='<div class="bar"><select id="dep-add" class="inline">'+others.map(
    function(o){return '<option value="'+esc(o.ref_id)+'">'+esc(o.title)+
    '</option>';}).join('')+'</select><button class="small" onclick="'+
    'addDependency(\''+taskId+'\')">Add dependency</button></div>';
  $('tk-deps').innerHTML=h;}
function csv(v){return (v||'').split(',').map(function(x){return x.trim();}).
  filter(Boolean);}
async function saveTask(){const id=S.selection&&S.selection.indexOf('task:')===0?
    S.selection.slice(5):null;if(!id)return;const estRaw=$('tk-est').value.trim();
  const body={title:$('tk-title').value,status:$('tk-status').value,
    workstream:$('tk-workstream').value,deliverable:$('tk-deliverable').value,
    deadline:$('tk-deadline').value,next_action:$('tk-next').value,
    description:$('tk-desc').value,waiting_for:csv($('tk-waiting').value),
    blocked_by:csv($('tk-blocked').value)};
  if(estRaw!=='')body.estimated_minutes=Math.round(Number(estRaw));
  else body.estimated_minutes=null;
  const actRaw=$('tk-actual').value.trim();
  if(actRaw!=='')body.actual_minutes=Math.round(Number(actRaw));
  try{await api('/api/tasks/'+encodeURIComponent(id),'POST',body);
    closeTask();await refresh();}catch(err){notice(err.message);}}

// --- document import (factual first, explicit engine) ------------------------
let importState={draft_id:null,analysis:null};
function importEngine(){return $('import-engine')?$('import-engine').value:'local';}
function fmtUsd(v){if(v==null||v===0)return '$0';return '$'+Number(v).toFixed(4);}
function fmtSec(ms){if(ms==null)return '—';const s=Math.max(0,Math.round(ms/1000));
  const m=Math.floor(s/60);return m?m+'m'+(s%60)+'s':s+'s';}
function openImport(){importState={draft_id:null,analysis:null};
  S.importEngineChoice=null;
  clearImportPoll();$('import-picker').style.display='';
  $('import-preview').innerHTML='';$('import-progress').style.display='none';
  $('import-progress').innerHTML='';$('import-report').innerHTML='';
  $('import-actions').style.display='none';
  $('import-overlay').classList.add('open');notice('');loadEngineCatalog();}
function closeImport(){clearImportPoll();
  $('import-overlay').classList.remove('open');}
function clearImportPoll(){if(S.importPoll){clearInterval(S.importPoll);
  S.importPoll=null;}}
async function loadEngineCatalog(chars){
  try{const res=await api('/api/import/engines?chars='+(chars||0));
    S.importEngines=res;renderEngineBox();}
  catch(err){notice(err.message);}}
function renderEngineBox(){
  const box=$('import-engine-box');if(!box)return;
  const cat=S.importEngines;
  if(!cat){box.innerHTML='<span class="muted">loading engines…</span>';return;}
  let h='<label>Engine</label><select id="import-engine" '+
    'onchange="importEngineChanged()">';
  cat.engines.forEach(function(e){
    const chosen=S.importEngineChoice?S.importEngineChoice===e.engine:
      e.is_default;
    h+='<option value="'+esc(e.engine)+'"'+(chosen?' selected':'')+
      (e.selectable?'':' disabled')+'>'+esc(e.label)+' — '+esc(e.model)+
      (e.is_default?' [default]':'')+
      (e.recommended?' [recommended]':'')+
      (e.selectable?'':' [unavailable]')+'</option>';});
  h+='</select>';
  const rec=(cat.engines||[]).filter(function(e){return e.recommended;})[0];
  if(rec){h+='<div class="muted" style="margin-top:.3rem">AI recommends now: '+
    '<strong>'+esc(rec.label)+'</strong> ('+esc(rec.reason)+') — '+
    'default for new jobs; change it any time, your explicit choice is kept.'+
    '</div>';}
  h+='<div class="muted" style="margin-top:.35rem">pricing state: '+
    '<span class="pill '+(cat.pricing_state==='PEAK'?'badge-WAITING':'badge-READY')+
    '">'+esc(cat.pricing_state)+'</span> · '+esc(cat.peak_windows)+'</div>'+
    '<div class="muted" id="import-cost"></div>';
  box.innerHTML=h;renderEngineCost();}
function importEngineChanged(){S.importEngineChoice=importEngine();
  renderEngineCost();}
function renderEngineCost(){
  const cost=$('import-cost');if(!cost||!S.importEngines)return;
  const engine=importEngine();
  const e=(S.importEngines.engines||[]).filter(function(x){
    return x.engine===engine;})[0];
  if(!e){cost.textContent='';return;}
  cost.innerHTML='model '+esc(e.model)+' · '+esc(e.api_cost)+
    (e.engine==='deepseek-flash'?' · ~'+(e.estimated_input_tokens||0)+
    ' input / '+(e.estimated_output_tokens||0)+' output tokens':'');}
async function analyzeFile(){const input=$('import-file');
  const file=input.files&&input.files[0];
  if(!file){notice('Choose a document first');return;}
  notice('Starting factual analysis of '+file.name+' …');
  const reader=new FileReader();
  reader.onload=async function(){const base64=String(reader.result).split(',')[1]||'';
    try{const job=await api('/api/import/jobs','POST',{filename:file.name,
      content_base64:base64,engine:importEngine(),mode:'factual'});
      S.importJob=job;$('import-picker').style.display='none';
      renderImportProgress(job);pollImportJob(job.job_id);}
    catch(err){notice(err.message);}};reader.readAsDataURL(file);}
function pollImportJob(jobId){
  clearImportPoll();
  S.importPoll=setInterval(async function(){
    try{const job=await api('/api/import/jobs/'+encodeURIComponent(jobId));
      S.importJob=job;renderImportProgress(job);
      if(job.status==='COMPLETED'){clearImportPoll();renderImportReport(job);
        importState.draft_id=job.draft_id;importState.analysis=job.analysis;
        renderImportPreview();}
      else if(job.status==='FAILED'||job.status==='CANCELLED'){
        clearImportPoll();renderImportReport(job);
        if(job.error)notice(job.error);}}
    catch(err){clearImportPoll();notice(err.message);}},1500);}
async function cancelImport(){if(!S.importJob)return;
  try{const job=await api('/api/import/jobs/'+
    encodeURIComponent(S.importJob.job_id)+'/cancel','POST',{});
    S.importJob=job;renderImportProgress(job);}
  catch(err){notice(err.message);}}
function impStageBadge(stage){const cls=stage==='COMPLETED'?'badge-READY':
  (stage==='FAILED'?'badge-BLOCKED':(stage==='CANCELLED'?'badge-DEFERRED':
  'badge-IN_PROGRESS'));return '<span class="pill '+cls+'">'+esc(stage)+
  '</span>';}
function renderImportProgress(job){
  const el=$('import-progress');el.style.display='';
  const running=(job.status==='RUNNING'||job.status==='QUEUED');
  let h='<div style="border:1px solid #e2e6ec;border-radius:10px;'+
    'padding:.6rem;background:#f8fafc">';
  h+='<div><strong>'+esc(job.filename)+'</strong> '+impStageBadge(job.stage)+
    ' <span class="muted">'+esc(job.message||'')+'</span></div>';
  h+='<div class="wbsbar" style="width:100%;margin:.4rem 0"><span style="width:'+
    Math.max(0,Math.min(100,job.percent||0))+'%"></span></div>';
  h+='<div class="muted">'+Math.round(job.percent||0)+'% · engine '+
    esc(job.engine)+' · model '+esc(job.model||'—')+' · pricing '+
    esc(job.pricing_state||'—')+'</div>';
  h+='<div class="muted">elapsed '+fmtSec(job.elapsed_ms)+' · ETA '+
    (job.eta_ms!=null?fmtSec(job.eta_ms):'—')+' · chunks '+
    (job.chunks_total?(job.chunks_done+'/'+job.chunks_total):'—')+
    ' · API calls '+(job.api_calls||0)+' · fallbacks '+
    (job.fallback_count||0)+'</div>';
  h+='<div class="muted">cost so far '+fmtUsd(job.cost_so_far_usd)+
    ' / estimated '+fmtUsd(job.estimated_cost_usd)+' · last: '+
    esc(job.last_activity||'')+'</div>';
  h+='<div class="bar">';
  if(running)h+='<button class="small danger" onclick="cancelImport()">'+
    'Cancel analysis</button>';
  h+='</div></div>';el.innerHTML=h;}
function renderImportReport(job){
  const r=job.completion_report||{};const el=$('import-report');
  let h='<div style="margin-top:.6rem;border-top:1px solid #e2e6ec;'+
    'padding-top:.5rem">';
  h+='<h3 style="margin:.2rem 0">'+(job.status==='COMPLETED'?
    'Import complete':(job.status==='CANCELLED'?'Import cancelled':
    'Import failed'))+'</h3>';
  h+='<div class="grid2"><div class="muted">engine actually used: <strong>'+
    esc(r.engine||job.engine)+'</strong><br>model: '+
    esc(r.model||job.model||'—')+'<br>elapsed: '+fmtSec(r.elapsed_ms)+
    '<br>chunks: '+(r.chunks||0)+'<br>API calls: '+(r.api_calls||0)+'</div>'+
    '<div class="muted">input tokens: '+(r.input_tokens||0)+
    '<br>output tokens: '+(r.output_tokens||0)+'<br>estimated cost: '+
    fmtUsd(r.estimated_cost_usd)+'<br>factual items: '+(r.factual_items||0)+
    '<br>likely merges: '+(r.likely_merges||0)+'<br>requiring review: '+
    (r.requiring_review||0)+'<br>AI-generated items: '+
    (r.ai_generated_items||0)+'</div></div></div>';
  el.innerHTML=h;}
function impKindOrder(k){if(k==='AREA')return 0;
  if(k==='PROJECT'||k==='IDEA'||k==='SOMEDAY')return 1;
  if(k==='WORKSTREAM'||k==='WORK_PACKAGE'||k==='DELIVERABLE')return 2;
  if(k==='TASK'||k==='WAITING'||k==='BLOCKER')return 3;
  if(k==='SUBTASK')return 4;return 5;}
function impEvidenceBadge(ev){const cls=ev==='FACT'?'FACT':ev==='INFERRED'?
  'INFERRED':ev==='SUGGESTED'?'SUGGESTED':'UNKNOWN';
  return '<span class="pill badge-'+cls+'">'+esc(ev||'UNKNOWN')+'</span>';}
function impProjectOptions(selected){const opts=objects().filter(function(o){
    return o.kind==='project';}).sort(function(a,b){
    return a.title.localeCompare(b.title);}).map(function(p){
    return '<option value="'+esc(p.project_id)+'"'+(p.project_id===selected?
      ' selected':'')+'>'+esc(p.title)+'</option>';});
  return '<option value="">(keep document context)</option>'+opts.join('');}
function impEditForm(c,isProject,isTask){
  const statuses=isProject?['ACTIVE','WAITING','BLOCKED','DEFERRED','COMPLETED']:
    ['TODO','IN_PROGRESS','BLOCKED','WAITING','COMPLETED','DEFERRED','ABANDONED'];
  const sel=function(id,list,cur){return '<select id="'+id+'">'+
    '<option value="">(default)</option>'+list.map(function(v){
      return '<option'+(v===cur?' selected':'')+'>'+v+'</option>';}).join('')+
    '</select>';};
  return '<div style="margin-top:.5rem;border-top:1px dashed #e2e6ec;'+
    'padding-top:.5rem"><label>Title</label><input id="imp-title-'+
    c.candidate_id+'" value="'+esc(c.title)+'">'+
    (isTask?'<label>Project</label><select id="imp-project-'+
      c.candidate_id+'">'+impProjectOptions('')+'</select>':'')+
    '<div class="form-row"><div><label>Status</label>'+
      sel('imp-status-'+c.candidate_id,statuses,c.suggested_status||'')+
      '</div><div><label>Urgency</label>'+
      sel('imp-urg-'+c.candidate_id,['CRITICAL','HIGH','MEDIUM','LOW'],
        c.suggested_urgency||'')+'</div></div>'+
    '<div class="form-row"><div><label>Impact</label>'+
      sel('imp-imp-'+c.candidate_id,['HIGH','MEDIUM','LOW'],
        c.suggested_impact||'')+'</div><div><label>Effort (min)</label>'+
      '<input id="imp-effort-'+c.candidate_id+'" type="number" min="1" value="'+
      (c.suggested_effort_minutes||'')+'"></div></div></div>';}
function impRow(c){const matches=(importState.analysis.matches||[]).filter(
    function(m){return m.candidate_id===c.candidate_id;});
  const isProject=impKindOrder(c.kind)===1;const canMerge=isProject&&
    matches.length>0;
  const action=c.suggested_action==='merge'?'merge':
    (c.suggested_action==='skip'?'skip':'import');
  const badge=c.needs_review?' <span class="pill badge-WAITING">review</span>':'';
  return '<div class="taskrow" style="padding:.5rem 0"><div class="main">'+
    '<div class="title">'+pill(c.kind,'badge-ACTIVE')+' '+esc(c.title)+' '+
    impEvidenceBadge(c.evidence)+badge+'</div>'+
    (c.source_text?'<div class="muted">'+esc(String(c.source_text).slice(0,160))+
    '</div>':'')+
    '<div id="imp-edit-'+c.candidate_id+'" style="display:none">'+
    impEditForm(c,isProject,isTask)+'</div>'+
    '</div><div class="actions"><select id="imp-action-'+
    c.candidate_id+'" onchange="impActionChange(\''+c.candidate_id+'\')">'+
    '<option value="import"'+(action==='import'?' selected':'')+'>Import</option>'+
    '<option value="skip"'+(action==='skip'?' selected':'')+'>Skip</option>'+
    (canMerge?'<option value="merge"'+(action==='merge'?' selected':'')+
    '>Merge</option>':'')+'</select>'+
    (canMerge?'<select id="imp-merge-'+c.candidate_id+'">'+
    impProjectOptions(matches[0].matched_project_id)+'</select>':'')+
    '<button class="small" onclick="impToggleEdit(\''+c.candidate_id+
    '\')">Edit</button>'+
    '</div></div>';}
function impToggleEdit(cid){const el=$('imp-edit-'+cid);
  el.style.display=el.style.display==='none'?'':'none';}
function impToggleSuggestions(){const el=$('imp-suggestions');if(!el)return;
  const hidden=el.style.display==='none';
  el.style.display=hidden?'':'none';
  const b=$('imp-suggestions-toggle');if(b)b.textContent='AI Suggestions ('+
    b.getAttribute('data-count')+') '+(hidden?'[Hide]':'[Show]');}
function renderImportPreview(){const a=importState.analysis;const s=a.summary||{};
  $('import-picker').style.display='none';$('import-actions').style.display='';
  let h='<p class="muted"><strong>'+esc(a.source_name)+'</strong> · '+
    (s.items||0)+' factual items · mode '+esc(a.mode||'factual')+' · engine '+
    esc(a.engine||'')+'</p>';
  h+='<p class="muted">areas '+(s.areas||0)+' · projects '+(s.projects||0)+
    ' · workstreams '+(s.workstreams||0)+' · tasks '+(s.tasks||0)+
    ' · likely merges '+(s.likely_merges||s.merge_candidates||0)+
    ' · needing review '+(s.requiring_review||s.needs_review||0)+
    ' · AI suggestions '+(s.ai_suggested||0)+'</p>';
  const ordered=a.candidates.slice().sort(function(x,y){return impKindOrder(x.kind)-
    impKindOrder(y.kind)||x.candidate_id.localeCompare(y.candidate_id);});
  const suggested=ordered.filter(function(c){return c.evidence==='SUGGESTED';});
  const factual=ordered.filter(function(c){return c.evidence!=='SUGGESTED';});
  h+='<div>'+factual.map(impRow).join('')+'</div>';
  if(suggested.length){
    h+='<button class="small" id="imp-suggestions-toggle" data-count="'+
      suggested.length+'" onclick="impToggleSuggestions()">AI Suggestions ('+
      suggested.length+') [Show]</button>'+
      '<div id="imp-suggestions" style="display:none">'+
      suggested.map(impRow).join('')+'</div>';}
  $('import-preview').innerHTML=h;}
function impActionChange(cid){const merge=$('imp-merge-'+cid);if(merge){
    merge.style.display=$('imp-action-'+cid).value==='merge'?'':'none';}}
async function confirmImport(){const decisions=importState.analysis.candidates.map(
    function(c){const raw=$('imp-action-'+c.candidate_id).value;
    const d={candidate_id:c.candidate_id,action:raw==='merge'?'import':raw};
    if(raw==='merge'){const merge=$('imp-merge-'+c.candidate_id);
      if(!merge||!merge.value)throw new Error('choose a merge target');
      d.merge_project_id=merge.value;}
    const title=$('imp-title-'+c.candidate_id);
    if(title&&title.value&&title.value!==c.title)d.title=title.value;
    const project=$('imp-project-'+c.candidate_id);
    if(project&&project.value)d.project_id=project.value;
    const status=$('imp-status-'+c.candidate_id);
    if(status&&status.value)d.status=status.value;
    const urg=$('imp-urg-'+c.candidate_id);
    if(urg&&urg.value)d.urgency=urg.value;
    const imp=$('imp-imp-'+c.candidate_id);
    if(imp&&imp.value)d.impact=imp.value;
    const effort=$('imp-effort-'+c.candidate_id);
    if(effort&&effort.value&&effort.value.trim()!=='')d.effort_minutes=
      parseInt(effort.value,10);
    return d;});
  try{const res=await api('/api/import/confirm','POST',{draft_id:
    importState.draft_id,decisions:decisions});
    notice('Imported '+res.imported+' · merged '+res.merged+' · skipped '+
    res.skipped);closeImport();await refresh();}catch(err){notice(err.message);}}

// --- on-demand AI enrichment (SUGGESTED until explicitly accepted) -----------
function enrEngineOptions(){const cat=S.importEngines;
  if(!cat)return '<option value="local">Local Ollama</option>';
  return cat.engines.map(function(e){const chosen=S.enrichmentEngine?
    S.enrichmentEngine===e.engine:e.is_default;
    return '<option value="'+esc(e.engine)+'"'+(chosen?' selected':'')+
    (e.selectable?'':' disabled')+'>'+esc(e.label)+
    (e.recommended?' [recommended]':'')+(e.selectable?'':
    ' (unavailable)')+'</option>';}).join('');}
function enrBadge(){return '<span class="pill badge-SUGGESTED">SUGGESTED</span>';}
async function loadEnrichment(projectId,force){
  if(!force&&S.enrichment[projectId]){renderEnrichment(projectId);return;}
  try{const res=await api('/api/enrichment/'+encodeURIComponent(projectId));
    S.enrichment[projectId]=res;renderEnrichment(projectId);}
  catch(err){notice(err.message);}}
function toggleEnrichment(projectId){S.enrichmentOpen[projectId]=
  !S.enrichmentOpen[projectId];renderEnrichment(projectId);}
function renderEnrichment(projectId){
  const el=$('ai-suggestions-'+projectId);if(!el)return;
  const data=S.enrichment[projectId]||{suggestions:[]};
  const pending=(data.suggestions||[]).filter(function(s){
    return s.state==='PENDING';});
  const accepted=(data.suggestions||[]).filter(function(s){
    return s.state==='ACCEPTED';});
  const rejected=(data.suggestions||[]).filter(function(s){
    return s.state==='REJECTED';});
  const open=!!S.enrichmentOpen[projectId];
  const project=obj('project:'+projectId);
  let h='<button class="small" onclick="toggleEnrichment(\''+projectId+
    '\')">AI Suggestions ('+pending.length+') '+(open?'[Hide]':'[Show]')+
    '</button>';
  if(project){h+=' <span class="muted">for '+esc(project.title)+
    '</span>';}
  if(accepted.length||rejected.length){h+=' <span class="muted">'+
    accepted.length+' accepted · '+rejected.length+' rejected</span>';}
  if(open){
    if(!pending.length){h+='<div class="muted" style="margin-top:.3rem">'+
      'no pending suggestions — generate some with the buttons above</div>';}
    else{h+='<div style="border:1px dashed #c9b6e6;border-radius:8px;'+
      'padding:.5rem;margin-top:.4rem;background:#faf7ff">'+
      pending.map(function(s){return enrSuggestionRow(projectId,s);}).join('')+
      '</div>';}}
  el.innerHTML=h;}
function enrSuggestionRow(projectId,s){
  return '<div style="padding:.35rem 0;border-bottom:1px dashed #e6dcf5">'+
    '<div>'+pill(s.kind,'badge-ACTIVE')+' '+enrBadge()+' <strong>'+
    esc(s.title)+'</strong></div>'+
    (s.detail?'<div class="muted">'+esc(s.detail)+'</div>':'')+
    (s.kind==='DEPENDENCY'?'<div class="muted">'+esc(s.source_title)+
      ' → '+esc(s.target_title)+'</div>':'')+
    '<div class="actions"><button class="small primary" onclick="'+
    'acceptSuggestion(\''+projectId+'\',\''+s.suggestion_id+'\')">Accept</button>'+
    '<button class="small" onclick="editSuggestion(\''+projectId+'\',\''+
    s.suggestion_id+'\')">Edit &amp; accept</button>'+
    '<button class="small danger" onclick="rejectSuggestion(\''+projectId+
    '\',\''+s.suggestion_id+'\')">Reject</button></div></div>';}
async function runEnrichment(projectId,kind){
  const sel=$('enr-engine-'+projectId);const engine=sel?sel.value:'local';
  S.enrichmentEngine=engine;
  notice('Generating '+kind.replace(/_/g,' ')+' with '+(engine==='local'?
    'local Ollama':engine)+' …');
  try{await api('/api/enrichment','POST',{project_id:projectId,kind:kind,
    engine:engine});S.enrichmentOpen[projectId]=true;S.enrichment[projectId]=null;
    await loadEnrichment(projectId,true);
    notice('Suggestions generated — they stay SUGGESTED until you accept them.');}
  catch(err){notice(err.message);}}
async function acceptSuggestion(projectId,sid){
  try{await api('/api/enrichment/'+encodeURIComponent(projectId)+'/accept',
    'POST',{suggestion_id:sid});S.enrichment[projectId]=null;
    await loadEnrichment(projectId,true);notice('Accepted into the portfolio.');
    await refresh();}catch(err){notice(err.message);}}
function editSuggestion(projectId,sid){
  const data=S.enrichment[projectId]||{};
  const s=(data.suggestions||[]).filter(function(x){
    return x.suggestion_id===sid;})[0];
  const title=window.prompt('Edit suggestion before accepting',s?s.title:'');
  if(title==null||!title.trim())return;
  api('/api/enrichment/'+encodeURIComponent(projectId)+'/accept','POST',
    {suggestion_id:sid,title:title.trim()}).then(function(){
      S.enrichment[projectId]=null;return loadEnrichment(projectId,true);
    }).then(function(){notice('Edited and accepted into the portfolio.');
      return refresh();}).catch(function(err){notice(err.message);});}
async function rejectSuggestion(projectId,sid){
  try{await api('/api/enrichment/'+encodeURIComponent(projectId)+'/reject',
    'POST',{suggestion_id:sid});S.enrichment[projectId]=null;
    await loadEnrichment(projectId,true);}catch(err){notice(err.message);}}

// --- Today / Ready decision workflow -----------------------------------------
//
// Both views render the deterministic backend projection: the Today day plan
// (from the scheduler/prioritiser) and the readiness groups. The browser never
// invents a score or a blocking reason; it only shows what Python derived.
function exportPick(id,checked){if(checked)S.exportSelection[id]=true;
  else delete S.exportSelection[id];}
function toggleExportAll(checked){
  const plan=S.views&&S.views.today_plan;
  const wf=S.views&&S.views.ready_workflow;
  S.exportSelection={};
  if(!checked)return;
  ((plan&&plan.planned)||[]).forEach(function(item){
    S.exportSelection[item.task_id]=true;});
  if(wf&&wf.groups){['ready','in_progress'].forEach(function(g){
    (wf.groups[g]||[]).forEach(function(item){
      S.exportSelection[item.task_id]=true;});});}
  renderView();}
function decisionRow(o,rank,reasons){
  if(!o)return '';
  const why=(reasons&&reasons.length?reasons:(o.priority_reasons||[]))
    .map(function(r){return esc(r);}).join('<br>');
  return '<tr class="'+(S.selection===o.id?'selected':'')+'" '+
    'onclick="selectObject(\''+o.id+'\')">'+
    '<td onclick="event.stopPropagation()"><input type="checkbox" '+
    'value="'+esc(o.ref_id)+'"'+(S.exportSelection[o.ref_id]?' checked':'')+
    ' onchange="exportPick(\''+o.ref_id+'\',this.checked)"></td>'+
    '<td>'+(rank||'')+'</td><td>'+esc(o.title)+'</td>'+
    '<td>'+esc(o.project)+'</td>'+
    '<td>'+pill(o.readiness||uiStatus(o),'badge-'+
      (o.readiness==='READY'||o.readiness==='IN_PROGRESS'?'READY':
        uiStatus(o)))+'</td>'+
    '<td>'+esc(o.urgency)+'</td><td>'+esc(o.impact)+'</td>'+
    '<td>'+fmtMin(o.estimated_minutes)+'</td>'+
    '<td class="muted">'+why+'</td></tr>';}
function renderToday(){
  const plan=S.views.today_plan;
  if(!plan){$('view-root').innerHTML='<p class="muted">no plan</p>';return;}
  let h='<h2>Today — what to do now, and why</h2>'+
    '<p class="muted">capacity '+fmtMin(plan.total_capacity)+' · calendar '+
    fmtMin(plan.calendar_minutes)+' · buffer '+fmtMin(plan.buffer_minutes)+
    ' · planned '+fmtMin(plan.planned_minutes)+' / '+
    fmtMin(plan.plan_capacity)+'</p>'+
    '<div class="bar"><button class="small primary" '+
    'onclick="openExportPreview()">Preview Super Productivity export</button>'+
    '<span class="muted">select tasks below to hand a deliberate set to '+
    'Super Productivity</span></div>';
  h+='<table><tr><th></th><th>#</th><th>Task</th><th>Project</th>'+
    '<th>Readiness</th><th>Urgency</th><th>Impact</th><th>Effort</th>'+
    '<th>Why selected</th></tr>'+
    plan.planned.map(function(item,index){
      return decisionRow(obj('task:'+item.task_id),index+1,item.reasons);
    }).join('')+'</table>';
  if(!plan.planned.length){h+='<p class="muted">nothing fits in today\'s '+
    'remaining capacity</p>';}
  if((plan.unknown_effort||[]).length){
    h+='<h3>Ready but effort unknown ('+plan.unknown_effort.length+')</h3>'+
      '<p class="muted">These can be done but cannot be time-boxed; they are '+
      'never invented onto the plan.</p><ul>'+
      plan.unknown_effort.map(function(item){return '<li><a onclick="'+
        'selectObject(\'task:'+esc(item.task_id)+'\')">'+esc(item.title)+
        '</a></li>';}).join('')+'</ul>';}
  if((plan.deferred||[]).length){
    h+='<h3>Deferred past capacity ('+plan.deferred.length+')</h3><ul>'+
      plan.deferred.map(function(item){return '<li><a onclick="'+
        'selectObject(\'task:'+esc(item.task_id)+'\')">'+esc(item.title)+
        '</a></li>';}).join('')+'</ul>';}
  h+='<p><button class="small" onclick="setView(\'ready\')">See the full '+
    'Ready / Blocked / Waiting breakdown →</button></p>';
  $('view-root').innerHTML=h;}
function renderReady(){
  const wf=S.views.ready_workflow;
  if(!wf){$('view-root').innerHTML='<p class="muted">no readiness data</p>';
    return;}
  const labels={ready:'Ready',in_progress:'In progress',blocked:'Blocked',
    waiting:'Waiting',deferred:'Deferred',project_closed:'Project closed',
    terminal:'Done / abandoned'};
  let h='<h2>Ready — what can be done now</h2>'+
    '<p class="muted">'+wf.ready_total+' ready · '+wf.counts.blocked+
    ' blocked · '+wf.counts.waiting+' waiting · '+wf.counts.deferred+
    ' deferred · '+wf.counts.project_closed+' in closed projects</p>'+
    '<div class="bar"><button class="small primary" '+
    'onclick="openExportPreview()">Preview Super Productivity export</button>'+
    '<span class="muted">the export uses the tasks checked in Today, or the '+
    'top ready tasks</span></div>';
  wf.group_order.forEach(function(g){
    const items=wf.groups[g]||[];
    if(!items.length)return;
    h+='<h3>'+esc(labels[g]||g)+' ('+items.length+')</h3>';
    h+='<table><tr><th>Task</th><th>Project</th><th>Urgency</th>'+
      '<th>Impact</th><th>Effort</th><th>Deadline</th>'+
      '<th>Why / blocking reason</th></tr>';
    items.slice(0,300).forEach(function(item){
      const o=obj('task:'+item.task_id);
      const why=(g==='ready'||g==='in_progress')?
        (((o&&o.priority_reasons)||[]).join('; ')||'ready') :
        (item.blocking_reason||(item.reasons||[]).join('; '));
      h+='<tr class="'+(o&&S.selection===o.id?'selected':'')+'"'+
        (o?' onclick="selectObject(\''+o.id+'\')"':'')+'>'+
        '<td>'+esc(item.title)+'</td><td>'+esc(item.project)+'</td>'+
        '<td>'+esc(item.urgency)+'</td><td>'+esc(item.impact)+'</td>'+
        '<td>'+fmtMin(item.effort_minutes)+'</td>'+
        '<td>'+esc(item.deadline||'—')+'</td>'+
        '<td class="muted">'+esc(why)+(item.blocking_kind?' '+
          pill(item.blocking_kind,''):'')+'</td></tr>';});
    h+='</table>';});
  $('view-root').innerHTML=h;}

// --- Super Productivity handoff (dry-run preview only) -------------------------
function openExportPreview(){
  let ids=Object.keys(S.exportSelection);
  if(!ids.length){const plan=S.views.today_plan;
    ids=((plan&&plan.planned)||[]).map(function(item){return item.task_id;});}
  const query=ids.length?('?ids='+encodeURIComponent(ids.join(','))):'';
  api('/api/export/superproductivity'+query).then(function(res){
    S.exportPreview=res;renderExportPreview(res);
    $('export-overlay').classList.add('open');}).catch(function(err){
    notice(err.message);});}
function renderExportPreview(res){
  const el=$('export-body');if(!el)return;
  const skipped=(res.skipped_task_ids||[]);
  let h='<p class="muted"><strong>'+res.exported+'</strong> task(s) prepared · '+
    'nothing written on the server · '+(skipped.length?
      ('skipped '+skipped.length+' not ready: '+esc(skipped.join(', '))):
      'all selected are ready')+'</p>'+
    '<p class="muted">The stable id <code>trajectory-mvp-&lt;task_id&gt;</code> '+
    'de-duplicates re-imports; the original TrajectoryOS ids are preserved '+
    'inside each task.</p>'+
    '<div class="bar"><button class="small primary" '+
    'onclick="downloadExport()">Download JSON</button>'+
    '<button class="small" onclick="closeExportPreview()">Close</button></div>'+
    '<pre style="max-height:340px;overflow:auto;background:#f8fafc;'+
    'border:1px solid #e2e6ec;border-radius:8px;padding:.6rem;font-size:.72rem">'+
    esc(JSON.stringify(res.document,null,2))+'</pre>';
  el.innerHTML=h;}
function downloadExport(){if(!S.exportPreview)return;
  const blob=new Blob([JSON.stringify(S.exportPreview.document,null,2)],
    {type:'application/json'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download='super-productivity-trajectory-os.json';
  document.body.appendChild(a);a.click();a.remove();
  URL.revokeObjectURL(a.href);}
function closeExportPreview(){$('export-overlay').classList.remove('open');}

// --- Quick Capture ------------------------------------------------------------
function openCapture(){S.capture=null;
  $('capture-text').value='';$('capture-preview').innerHTML='';
  $('capture-actions').style.display='none';
  $('capture-overlay').classList.add('open');}
function closeCapture(){$('capture-overlay').classList.remove('open');}
async function analyzeCapture(){
  const text=$('capture-text').value;
  if(!text.trim()){notice('Type one or more lines first');return;}
  try{const res=await api('/api/capture','POST',{text:text});S.capture=res;
    renderCapturePreview(res);}catch(err){notice(err.message);}}
function capBadge(cat){const cls={new_task:'READY',possible_project:'IN_PROGRESS',
  existing_project:'ACTIVE',existing_task:'WAITING',duplicate:'DEFERRED'}[cat]||'TODO';
  return pill(cat,'badge-'+cls);}
function capActionOptions(item){
  let actions=['accept','skip'];
  if(item.category==='existing_project')actions=['attach','skip'];
  else if(item.category==='existing_task'||item.category==='duplicate')
    actions=['skip','accept'];
  const labels={accept:'Accept',attach:'Merge / attach',skip:'Skip'};
  return actions.map(function(a){return '<option value="'+a+'"'+
    (item.suggested_action===a?' selected':'')+'>'+labels[a]+'</option>';
  }).join('');}
function capItemRow(item){
  const projectOpts=objects().filter(function(o){return o.kind==='project';})
    .sort(function(a,b){return a.title.localeCompare(b.title);}).map(
      function(p){return '<option value="'+esc(p.project_id)+'"'+
        (p.project_id===item.project_id?' selected':'')+'>'+esc(p.title)+
        '</option>';}).join('');
  return '<div class="card" style="margin:.4rem 0"><div>'+capBadge(item.category)+
    ' <input id="cap-title-'+esc(item.capture_id)+'" value="'+esc(item.title)+
    '" style="width:62%"></div>'+
    (item.matched_title?'<div class="muted">matched: '+esc(item.matched_title)+
      ' (score '+(item.match_score)+')</div>':'')+
    '<div class="muted">'+esc(item.reason)+'</div>'+
    '<div class="bar"><select id="cap-action-'+esc(item.capture_id)+'">'+
    capActionOptions(item)+'</select>'+
    '<select id="cap-kind-'+esc(item.capture_id)+'">'+
    ['task','project'].map(function(k){return '<option value="'+k+'"'+
      (item.kind===k?' selected':'')+'>'+k+'</option>';}).join('')+'</select>'+
    '<select id="cap-project-'+esc(item.capture_id)+'">'+
    '<option value="">(choose project)</option>'+projectOpts+'</select>'+
    '</div></div>';}
function renderCapturePreview(res){
  const c=res.counts||{};const inst=res.instrumentation||{};
  let h='<div class="card" style="background:#f8fafc"><strong>'+c.items+
    '</strong> item(s) · '+c.matched_existing+' matched existing · '+
    c.duplicates+' duplicate(s) · '+c.new_tasks+' new task(s) · '+
    c.possible_projects+' possible project(s)<div class="muted">'+
    'engine '+esc(inst.engine||'')+' · model '+esc(inst.model||'')+' · '+
    (inst.elapsed_ms||0)+'ms · cost $'+(inst.cost_usd||0)+
    ' · preview only, nothing written</div></div>';
  h+=(res.items||[]).map(capItemRow).join('');
  $('capture-preview').innerHTML=h;
  $('capture-actions').style.display='';}
async function confirmCapture(){
  if(!S.capture)return;
  const decisions=S.capture.items.map(function(item){
    const action=$('cap-action-'+item.capture_id).value;
    const title=$('cap-title-'+item.capture_id).value;
    const kind=$('cap-kind-'+item.capture_id).value;
    const project=$('cap-project-'+item.capture_id).value;
    const d={capture_id:item.capture_id,action:action};
    if(title&&title!==item.title)d.title=title;
    if(kind!==item.kind)d.kind=kind;
    if(project)d.project_id=project;
    return d;});
  try{const res=await api('/api/capture/confirm','POST',
    {text:$('capture-text').value,decisions:decisions});
    notice('Capture: '+res.created_tasks+' task(s), '+
      res.created_projects+' project(s), skipped '+res.skipped+
      ', merged '+res.merged);
    closeCapture();await refresh();}catch(err){notice(err.message);}}

// --- outcome capture (planned vs actual) --------------------------------------
function openOutcomeForm(taskId){const o=obj('task:'+taskId);if(!o)return;
  selectObject('task:'+taskId);
  $('outcome-body').innerHTML='<h3 style="margin:0">Record outcome — '+
    esc(o.title)+'</h3><div class="muted">planned '+
    fmtMin(o.estimated_minutes)+(o.deadline?' · due '+esc(o.deadline):'')+
    '</div><label>Outcome</label><select id="oc-outcome">'+
    ['COMPLETED','DEFERRED','BLOCKED','ABANDONED'].map(function(v){
      return '<option>'+v+'</option>';}).join('')+'</select>'+
    '<label>Actual minutes (planned vs actual)</label>'+
    '<input id="oc-minutes" type="number" min="0" value="'+
    (o.actual_minutes==null?'':o.actual_minutes)+'">'+
    '<label>Result / blockers note</label><textarea id="oc-note" rows="3">'+
    '</textarea>';
  $('outcome-overlay').classList.add('open');}
function closeOutcomeForm(){$('outcome-overlay').classList.remove('open');}
async function saveOutcomeForm(){
  const id=S.selection&&S.selection.indexOf('task:')===0?
    S.selection.slice(5):null;
  if(!id)return;
  const body={outcome:$('oc-outcome').value};
  const raw=$('oc-minutes').value.trim();
  if(raw!=='')body.actual_minutes=Math.round(Number(raw));
  const note=$('oc-note').value;
  if(note)body.note=note;
  try{await api('/api/tasks/'+encodeURIComponent(id)+'/outcome','POST',body);
    closeOutcomeForm();await refresh();notice('Outcome recorded.');}
  catch(err){notice(err.message);}}

// --- boot --------------------------------------------------------------------
async function boot(){
  try{const c=await api('/api/cockpit');S.cockpit=c;}catch(e){/* ignore */}
  loadEngineCatalog();
  await refresh();}
window.addEventListener('DOMContentLoaded',boot);
"""


def render_page(cockpit: engine.Cockpit) -> str:
    """Render the full cockpit HTML page (shell + CSS + JS)."""
    from trajectory_os.mvp.render import _escape

    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>TrajectoryOS cockpit</title>",
        "<style>", PAGE_CSS, "</style></head><body>",
        "<header><h1>TrajectoryOS</h1>",
        "<span class='sub'>visual execution &amp; decision cockpit · ",
        _escape(cockpit.today), " · generated ", _escape(cockpit.generated_at),
        "</span>",
        "<div class='toolbar'>",
        "<button class='small' onclick='openProjectForm(null)'>New Project</button>",
        "<button class='small' onclick='openTaskForm(null)'>New Task</button>",
        "<button class='small' onclick='openImport()'>Import document</button>",
        "<button class='small' onclick='openCapture()'>Quick Capture</button>",
        "<button class='small primary' onclick='boot()'>Refresh</button>",
        "</div></header>",
        "<nav id='viewbar'></nav>",
        "<div id='controls'></div>",
        "<div class='notice' id='notice'></div>",
        "<section class='card' style='margin:.6rem 1rem'>",
        "<div class='grid3'>",
        "<div><h2>Today</h2><div id='today-summary' class='muted'>loading…</div></div>",
        "<div><h2>Blocked / waiting</h2><div id='blocked-summary' class='muted'>"
        "loading…</div></div>",
        "<div><h2>Projects</h2><div id='projects-summary' class='muted'>"
        "loading…</div></div>",
        "</div></section>",
        "<div id='layout'>",
        "<main id='view-root'><p class='muted'>loading…</p></main>",
        "<aside id='inspector'><h2>Inspector</h2>"
        "<p class='muted'>Select an object.</p></aside>",
        "</div>",
        # Project create/edit form
        "<div class='overlay' id='project-form-overlay'><div class='modal'>",
        "<h3 id='project-form-title'>Project</h3>",
        "<div id='project-form-body'></div>",
        "<div class='bar' style='justify-content:flex-end'>",
        "<button onclick='closeProjectForm()'>Cancel</button>",
        "<button class='primary' onclick='saveProject()'>Save</button></div>",
        "</div></div>",
        # Task edit form
        "<div class='overlay' id='task-overlay'><div class='modal'>",
        "<div id='task-body'></div>",
        "<div class='bar' style='justify-content:flex-end'>",
        "<button onclick='closeTask()'>Cancel</button>",
        "<button class='primary' onclick='saveTask()'>Save</button></div>",
        "</div></div>",
        # New task form
        "<div class='overlay' id='task-form-overlay'><div class='modal'>",
        "<div id='task-form-body'></div>",
        "<div class='bar' style='justify-content:flex-end'>",
        "<button onclick='closeTaskForm()'>Cancel</button>",
        "<button class='primary' onclick='saveNewTask()'>Create task</button>",
        "</div></div></div>",
        # Document import
        "<div class='overlay' id='import-overlay'><div class='modal' "
        "style='max-width:860px'><h3>Import document</h3>",
        "<div id='import-picker'>",
        "<input type='file' id='import-file' accept='.odt,.docx,.pdf,.md,"
        ".markdown,.txt,.text'>",
        "<div class='muted' style='margin-top:.5rem'>ODT, DOCX, PDF, Markdown "
        "or TXT. Factual import only mirrors what the document states; "
        "nothing is written until you confirm.</div>",
        "<div id='import-engine-box' style='margin-top:.5rem'></div>",
        "<div class='bar' style='justify-content:flex-start'>",
        "<button class='primary' onclick='analyzeFile()'>Analyze</button>",
        "<button onclick='closeImport()'>Cancel</button></div></div>",
        "<div id='import-progress' style='display:none;margin-top:.6rem'></div>",
        "<div id='import-report'></div>",
        "<div id='import-preview'></div>",
        "<div class='bar' id='import-actions' style='display:none'>",
        "<button class='primary' onclick='confirmImport()'>Confirm import</button>",
        "<button onclick='closeImport()'>Cancel</button></div></div></div>",
        # Quick Capture
        "<div class='overlay' id='capture-overlay'><div class='modal' "
        "style='max-width:760px'><h3>Quick Capture</h3>",
        "<div class='muted'>One or many lines. Nothing is written until you "
        "confirm the preview; matching is targeted, not a full re-analysis "
        "of the portfolio.</div>",
        "<textarea id='capture-text' rows='6' placeholder='water the "
        "garden plots&#10;sample data project&#10;check the budget transfer'></textarea>",
        "<div class='bar' style='justify-content:flex-start'>",
        "<button class='primary' onclick='analyzeCapture()'>Analyze</button>",
        "<button onclick='closeCapture()'>Cancel</button></div>",
        "<div id='capture-preview'></div>",
        "<div class='bar' id='capture-actions' style='display:none;"
        "justify-content:flex-end'>",
        "<button class='primary' onclick='confirmCapture()'>Confirm</button>",
        "<button onclick='closeCapture()'>Cancel</button></div>",
        "</div></div>",
        # Super Productivity dry-run export preview
        "<div class='overlay' id='export-overlay'><div class='modal' "
        "style='max-width:760px'><h3>Super Productivity export (preview)</h3>",
        "<div id='export-body'></div></div></div>",
        # Outcome capture
        "<div class='overlay' id='outcome-overlay'><div class='modal'>",
        "<div id='outcome-body'></div>",
        "<div class='bar' style='justify-content:flex-end'>",
        "<button onclick='closeOutcomeForm()'>Cancel</button>",
        "<button class='primary' onclick='saveOutcomeForm()'>Record</button>",
        "</div></div></div>",
        "<script>", PAGE_JS, "</script>",
        "</body></html>",
    ]
    return "".join(parts)


__all__ = ["PAGE_CSS", "PAGE_JS", "TABLE_JS_HELPERS", "render_page"]
