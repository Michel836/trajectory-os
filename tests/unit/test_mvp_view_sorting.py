"""Focused tests for presentation-only sorting across cockpit views.

Kanban / Today / Ready share a single pure ordering helper
(``presentationSortRows``) whose baseline key (``default`` / ``recommended``)
always preserves the deterministic server order. These tests pin the semantic
comparisons, the persistence contract in ``ui_state.json`` and the guarantee
that no business logic, status membership or portfolio data is touched.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from trajectory_os.mvp import cockpit_ui, dataset, model, store, uistate


def _node_assert(tmp_path: Path, body: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    script = tmp_path / "view_sort_helpers.js"
    script.write_text(cockpit_ui.TABLE_JS_HELPERS + "\n" + body,
                      encoding="utf-8")
    result = subprocess.run([node, str(script)], capture_output=True,
                            text=True, check=False)
    assert result.returncode == 0, result.stderr


_ROWS = r"""
const rows=[
  {title:'Charlie',project:'Zeta',priority_rank:3,urgency:'LOW',
    impact:'LOW',estimated_minutes:30,effort_minutes:30},
  {title:'Alpha',project:'Yankee',priority_rank:null,urgency:'CRITICAL',
    impact:'HIGH',estimated_minutes:90,effort_minutes:90},
  {title:'Bravo',project:'Xray',priority_rank:1,urgency:'HIGH',
    impact:'MEDIUM',estimated_minutes:10,effort_minutes:10}];
const titles=(sort,dir)=>presentationSortRows('task',rows,sort,dir)
  .map(o=>o.title);
"""


# --- A — Kanban priority sort ------------------------------------------------


def test_kanban_priority_sort_highest_first(tmp_path: Path) -> None:
    _node_assert(tmp_path, _ROWS + r"""
const assert=require('assert');
// priority_rank 1 is the highest priority -> ascending rank
assert.deepStrictEqual(titles('priority'),['Bravo','Charlie','Alpha']);
// unknown priority always sorts last, regardless of direction
assert.deepStrictEqual(titles('priority','desc'),
  ['Charlie','Bravo','Alpha']);
""")


# --- B — Kanban effort sort --------------------------------------------------


def test_kanban_effort_sort_lowest_first(tmp_path: Path) -> None:
    _node_assert(tmp_path, _ROWS + r"""
const assert=require('assert');
assert.deepStrictEqual(titles('effort'),['Bravo','Charlie','Alpha']);
assert.deepStrictEqual(titles('effort','desc'),
  ['Alpha','Charlie','Bravo']);
""")


# --- C — Kanban name sort ----------------------------------------------------


def test_kanban_name_sort_az(tmp_path: Path) -> None:
    _node_assert(tmp_path, _ROWS + r"""
const assert=require('assert');
assert.deepStrictEqual(titles('name'),['Alpha','Bravo','Charlie']);
assert.deepStrictEqual(titles('name','desc'),['Charlie','Bravo','Alpha']);
""")


# --- D — Kanban default restores baseline ------------------------------------


def test_kanban_default_restores_baseline_order(tmp_path: Path) -> None:
    _node_assert(tmp_path, _ROWS + r"""
const assert=require('assert');
assert.deepStrictEqual(titles('default'),['Charlie','Alpha','Bravo']);
assert.deepStrictEqual(titles('recommended'),['Charlie','Alpha','Bravo']);
assert.deepStrictEqual(titles('bogus-key'),['Charlie','Alpha','Bravo']);
// the input array is never mutated
assert.deepStrictEqual(rows.map(o=>o.title),['Charlie','Alpha','Bravo']);
""")


# --- E/F — Today Recommended unchanged + alternate sort ----------------------


def test_today_and_ready_shared_ordering(tmp_path: Path) -> None:
    _node_assert(tmp_path, _ROWS + r"""
const assert=require('assert');
// Recommended preserves the deterministic baseline exactly (a copy).
const rec=presentationSortRows('task',rows,'recommended');
assert.deepStrictEqual(rec.map(o=>o.title),['Charlie','Alpha','Bravo']);
assert.notStrictEqual(rec,rows);
// Alternate sorts work on the same helper used by Today and Ready.
assert.deepStrictEqual(titles('urgency'),['Alpha','Bravo','Charlie']);
assert.deepStrictEqual(titles('impact'),['Alpha','Bravo','Charlie']);
assert.deepStrictEqual(titles('project'),['Bravo','Alpha','Charlie']);
""")


# --- K — drag/drop status logic is unaffected --------------------------------


def test_kanban_column_membership_unaffected_by_sorting(tmp_path: Path) -> None:
    _node_assert(tmp_path, r"""
const assert=require('assert');
const krows=[
  {id:'t1',title:'T1',status:'TODO',ready:false,readiness:'READY',
    priority_rank:2},
  {id:'t2',title:'T2',status:'IN_PROGRESS',ready:true,readiness:'READY',
    priority_rank:3},
  {id:'t3',title:'T3',status:'TODO',ready:true,readiness:'READY',
    priority_rank:1},
  {id:'t4',title:'T4',status:'TODO',ready:true,readiness:'BLOCKED',
    priority_rank:4},
  {id:'t5',title:'T5',status:'TODO',ready:true,readiness:'WAITING',
    priority_rank:5},
  {id:'t6',title:'T6',status:'DEFERRED',ready:true,readiness:'READY',
    priority_rank:6},
  {id:'t7',title:'T7',status:'COMPLETED',ready:true,readiness:'READY',
    priority_rank:7}];
assert.strictEqual(kanbanColumn(krows[0],'tasks'),'TODO');
assert.strictEqual(kanbanColumn(krows[1],'tasks'),'IN_PROGRESS');
assert.strictEqual(kanbanColumn(krows[2],'tasks'),'READY');
assert.strictEqual(kanbanColumn(krows[3],'tasks'),'BLOCKED');
assert.strictEqual(kanbanColumn(krows[4],'tasks'),'WAITING');
assert.strictEqual(kanbanColumn(krows[5],'tasks'),'DEFERRED');
assert.strictEqual(kanbanColumn(krows[6],'tasks'),'DONE');
// projects mode is still a plain status projection
assert.strictEqual(kanbanColumn({status:'BLOCKED'},'projects'),'BLOCKED');
const cols=['TODO','IN_PROGRESS','READY','WAITING','BLOCKED','DEFERRED','DONE'];
cols.forEach(function(c){
  const baseline=krows.filter(function(o){
    return kanbanColumn(o,'tasks')===c;}).map(function(o){return o.id;});
  const sorted=presentationSortRows('task',krows.filter(function(o){
    return kanbanColumn(o,'tasks')===c;}),'priority')
    .map(function(o){return o.id;});
  assert.deepStrictEqual(sorted.slice().sort(),baseline.slice().sort());});
""")


# --- L — List sorting regression smoke ---------------------------------------


def test_list_table_sorting_helpers_unchanged(tmp_path: Path) -> None:
    _node_assert(tmp_path, r"""
const assert=require('assert');
const rows=[{title:'A',urgency:'LOW'},{title:'B',urgency:'CRITICAL'},
  {title:'C',urgency:'MEDIUM'}];
assert.deepStrictEqual(tableSortRows('project',rows,
  [{column:'urgency',dir:'desc'}]).map(o=>o.title),['B','C','A']);
let s=tableNextSort([],'urgency',false);
assert.deepStrictEqual(s,[{column:'urgency',dir:'asc'}]);
""")


# --- I — persistence (Python) ------------------------------------------------


def test_view_sort_preferences_roundtrip(tmp_path: Path) -> None:
    root = str(tmp_path)
    prefs = uistate.apply_update(uistate.default_preferences(), {
        "kanban_sort": "urgency",
        "kanban_sort_direction": "asc",
        "today_sort": "effort",
        "ready_sort": "name",
    })
    uistate.save_preferences(root, prefs)
    reloaded = uistate.load_preferences(root)
    assert reloaded.kanban_sort == "urgency"
    assert reloaded.kanban_sort_direction == "asc"
    assert reloaded.today_sort == "effort"
    assert reloaded.ready_sort == "name"
    # persisted in ui_state.json only
    raw = json.loads((Path(root) / uistate.UI_STATE_NAME).read_text())
    assert raw["kanban_sort"] == "urgency"
    assert raw["today_sort"] == "effort"
    assert raw["ready_sort"] == "name"


def test_view_sort_defaults_when_absent(tmp_path: Path) -> None:
    root = str(tmp_path)
    prefs = uistate.load_preferences(root)
    assert prefs.kanban_sort == "default"
    assert prefs.kanban_sort_direction == "default"
    assert prefs.today_sort == "recommended"
    assert prefs.ready_sort == "recommended"


def test_view_sort_normalises_hostile_input() -> None:
    prefs = uistate.apply_update(uistate.default_preferences(), {
        "kanban_sort": "bogus",
        "kanban_sort_direction": "sideways",
        "today_sort": 123,
        "ready_sort": None,
    })
    assert prefs.kanban_sort == "default"
    assert prefs.kanban_sort_direction == "default"
    assert prefs.today_sort == "recommended"
    assert prefs.ready_sort == "recommended"
    # from_dict also degrades safely
    degraded = uistate.UIPreferences.from_dict({
        "kanban_sort": "nope", "today_sort": ["x"]}).validate()
    assert degraded.kanban_sort == "default"
    assert degraded.today_sort == "recommended"


def test_view_sort_fails_closed_on_invalid_direct_object() -> None:
    with pytest.raises(model.MvpError):
        uistate.UIPreferences(kanban_sort="bogus").validate()
    with pytest.raises(model.MvpError):
        uistate.UIPreferences(today_sort="bogus").validate()


# --- J — no portfolio mutation -----------------------------------------------


def test_view_sort_does_not_touch_portfolio(tmp_path: Path) -> None:
    root = str(tmp_path)
    dataset.write_seed(root)
    path = store.default_portfolio_path(root)
    before = path.read_bytes()
    prefs = uistate.apply_update(uistate.default_preferences(), {
        "kanban_sort": "priority",
        "kanban_sort_direction": "desc",
        "today_sort": "urgency",
        "ready_sort": "project",
    })
    uistate.save_preferences(root, prefs)
    uistate.load_preferences(root)
    assert path.read_bytes() == before


# --- wiring / embedded JS ----------------------------------------------------


def test_sort_controls_are_wired_in_all_three_views() -> None:
    assert "Sort cards by" in cockpit_ui.PAGE_JS
    assert "Order by" in cockpit_ui.PAGE_JS
    for handler in ("kanbanSortChange", "kanbanDirChange", "todaySortChange",
                    "readySortChange"):
        assert handler in cockpit_ui.PAGE_JS
    # Kanban sorts inside the existing column membership filter.
    assert "kanbanColumn(o,mode)===col" in cockpit_ui.PAGE_JS
    assert "presentationSortRows" in cockpit_ui.TABLE_JS_HELPERS
    # Today/Ready share the same wrapper and never re-derive order.
    assert "sortViewItems" in cockpit_ui.PAGE_JS
    assert "kanban_sort" in cockpit_ui.PAGE_JS
    assert "today_sort" in cockpit_ui.PAGE_JS
    assert "ready_sort" in cockpit_ui.PAGE_JS


def test_drop_kanban_transitions_are_intact() -> None:
    for token in ("function dropKanban", "if(col==='TODO')",
                  "if(col==='BLOCKED')", "if(col==='DEFERRED')",
                  "if(col==='DONE')", "recordOutcome(o.ref_id,'COMPLETED')"):
        assert token in cockpit_ui.PAGE_JS


def test_today_and_ready_views_are_persistable() -> None:
    assert "today" in uistate.VIEWS
    assert "ready" in uistate.VIEWS
    prefs = uistate.apply_update(uistate.default_preferences(),
                                 {"view": "today"})
    assert prefs.view == "today"
    prefs = uistate.apply_update(prefs, {"view": "ready"})
    assert prefs.view == "ready"


def test_embedded_js_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    script = tmp_path / "cockpit.js"
    script.write_text(cockpit_ui.PAGE_JS, encoding="utf-8")
    result = subprocess.run([node, "--check", str(script)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
