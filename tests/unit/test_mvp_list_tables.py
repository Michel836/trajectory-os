"""Focused tests for the List-view data-table layer.

The cockpit List view renders two independent data tables (Projects and
Tasks). Sorting and column filtering are presentation-only: they are pure
functions in the browser and a bounded, validated slice of ``ui_state.json``.
These tests pin the deterministic semantics (never alphabetical) and the
persistence contract, and they confirm the portfolio is never mutated.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from trajectory_os.mvp import cockpit_ui, dataset, model, store, uistate


def _node_assert(tmp_path: Path, body: str) -> None:
    """Run ``body`` after the pure table helpers inside Node."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    script = tmp_path / "list_table_helpers.js"
    script.write_text(cockpit_ui.TABLE_JS_HELPERS + "\n" + body,
                      encoding="utf-8")
    result = subprocess.run([node, str(script)], capture_output=True,
                            text=True, check=False)
    assert result.returncode == 0, result.stderr


# --- semantic sort order (pure JS) -------------------------------------------


def test_urgency_sort_is_semantic(tmp_path: Path) -> None:
    _node_assert(tmp_path, r"""
const assert=require('assert');
const rows=[{title:'A',urgency:'LOW'},{title:'B',urgency:'HIGH'},
  {title:'C',urgency:'MEDIUM'},{title:'D',urgency:'CRITICAL'}];
const asc=tableSortRows('project',rows,
  [{column:'urgency',dir:'asc'}]).map(o=>o.title);
assert.deepStrictEqual(asc,['A','C','B','D']);
const desc=tableSortRows('project',rows,
  [{column:'urgency',dir:'desc'}]).map(o=>o.title);
assert.deepStrictEqual(desc,['D','B','C','A']);
""")


def test_impact_sort_is_semantic(tmp_path: Path) -> None:
    _node_assert(tmp_path, r"""
const assert=require('assert');
const rows=[{title:'A',impact:'LOW'},{title:'B',impact:'HIGH'},
  {title:'C',impact:'MEDIUM'}];
assert.deepStrictEqual(tableSortRows('project',rows,
  [{column:'impact',dir:'asc'}]).map(o=>o.title),['A','C','B']);
assert.deepStrictEqual(tableSortRows('project',rows,
  [{column:'impact',dir:'desc'}]).map(o=>o.title),['B','C','A']);
""")


def test_effort_sort_is_numeric(tmp_path: Path) -> None:
    _node_assert(tmp_path, r"""
const assert=require('assert');
const rows=[{title:'nine',estimated_minutes:9},
  {title:'hundred',estimated_minutes:100},
  {title:'twenty',estimated_minutes:20}];
assert.deepStrictEqual(tableSortRows('task',rows,
  [{column:'effort',dir:'asc'}]).map(o=>o.title),
  ['nine','twenty','hundred']);
// missing effort sorts last regardless of direction
const withNull=rows.concat([{title:'unknown',estimated_minutes:null}]);
assert.strictEqual(tableSortRows('task',withNull,
  [{column:'effort',dir:'desc'}]).slice(-1)[0].title,'unknown');
// projects use effort_minutes
const projects=[{title:'big',effort_minutes:600},
  {title:'small',effort_minutes:30}];
assert.deepStrictEqual(tableSortRows('project',projects,
  [{column:'effort',dir:'asc'}]).map(o=>o.title),['small','big']);
""")


def test_progress_sort_is_numeric(tmp_path: Path) -> None:
    _node_assert(tmp_path, r"""
const assert=require('assert');
const rows=[{title:'half',progress:0.5},{title:'low',progress:0.1},
  {title:'high',progress:0.9}];
assert.deepStrictEqual(tableSortRows('project',rows,
  [{column:'progress',dir:'asc'}]).map(o=>o.title),['low','half','high']);
assert.deepStrictEqual(tableSortRows('project',rows,
  [{column:'progress',dir:'desc'}]).map(o=>o.title),['high','half','low']);
""")


def test_status_sort_is_workflow_aware(tmp_path: Path) -> None:
    _node_assert(tmp_path, r"""
const assert=require('assert');
const rows=[{title:'done',status:'DONE'},{title:'blocked',status:'BLOCKED'},
  {title:'inprog',status:'IN_PROGRESS'},{title:'ready',status:'READY'},
  {title:'todo',status:'TODO'},{title:'waiting',status:'WAITING'},
  {title:'deferred',status:'DEFERRED'},{title:'active',status:'ACTIVE'}];
assert.deepStrictEqual(tableSortRows('task',rows,
  [{column:'status',dir:'asc'}]).map(o=>o.title),
  ['inprog','ready','todo','waiting','blocked','deferred','done','active']);
// COMPLETED renders and sorts as DONE
assert.strictEqual(tableDisplayStatus('COMPLETED'),'DONE');
assert.strictEqual(tableStatusRank('COMPLETED'),tableStatusRank('DONE'));
""")


def test_multi_column_sort(tmp_path: Path) -> None:
    _node_assert(tmp_path, r"""
const assert=require('assert');
const rows=[
  {title:'u1',urgency:'HIGH',impact:'LOW',effort_minutes:30},
  {title:'u2',urgency:'HIGH',impact:'HIGH',effort_minutes:90},
  {title:'u3',urgency:'HIGH',impact:'HIGH',effort_minutes:10},
  {title:'u4',urgency:'LOW',impact:'HIGH',effort_minutes:5}];
assert.deepStrictEqual(tableSortRows('project',rows,[
  {column:'urgency',dir:'desc'},
  {column:'impact',dir:'desc'},
  {column:'effort',dir:'asc'}]).map(o=>o.title),['u3','u2','u1','u4']);
""")


def test_tri_state_and_shift_click_sort_state(tmp_path: Path) -> None:
    _node_assert(tmp_path, r"""
const assert=require('assert');
// normal click cycles asc -> desc -> none
let s=tableNextSort([],'urgency',false);
assert.deepStrictEqual(s,[{column:'urgency',dir:'asc'}]);
s=tableNextSort(s,'urgency',false);
assert.deepStrictEqual(s,[{column:'urgency',dir:'desc'}]);
s=tableNextSort(s,'urgency',false);
assert.deepStrictEqual(s,[]);
// shift adds a secondary term and toggles/removes it
s=tableNextSort([{column:'urgency',dir:'desc'}],'impact',true);
assert.deepStrictEqual(s,[{column:'urgency',dir:'desc'},
  {column:'impact',dir:'asc'}]);
s=tableNextSort(s,'impact',true);
assert.deepStrictEqual(s,[{column:'urgency',dir:'desc'},
  {column:'impact',dir:'desc'}]);
s=tableNextSort(s,'impact',true);
assert.deepStrictEqual(s,[{column:'urgency',dir:'desc'}]);
// a normal click always resets to a single column
s=tableNextSort([{column:'urgency',dir:'desc'},{column:'impact',dir:'desc'}],
  'title',false);
assert.deepStrictEqual(s,[{column:'title',dir:'asc'}]);
""")


# --- filters (pure JS) -------------------------------------------------------


def test_text_filter(tmp_path: Path) -> None:
    _node_assert(tmp_path, r"""
const assert=require('assert');
const rows=[{title:'Community Garden Planner'},{title:'Finance review'},
  {title:'AI reading list'}];
assert.strictEqual(tableFilterRows('project',rows,{title:'ai'}).length,1);
assert.strictEqual(tableFilterRows('project',rows,{title:'PLANNER'}).length,1);
assert.strictEqual(tableFilterRows('project',rows,{title:''}).length,3);
""")


def test_select_filter(tmp_path: Path) -> None:
    _node_assert(tmp_path, r"""
const assert=require('assert');
const rows=[{title:'a',domain:'work',status:'READY',urgency:'HIGH',
  impact:'HIGH',ready:true,project_id:'p1'},
  {title:'b',domain:'home',status:'TODO',urgency:'LOW',
  impact:'LOW',ready:false,project_id:'p2'}];
assert.strictEqual(tableFilterRows('project',rows,{domain:'work'}).length,1);
assert.strictEqual(tableFilterRows('task',rows,{status:'READY'}).length,1);
assert.strictEqual(tableFilterRows('task',rows,{ready:'yes'}).length,1);
assert.strictEqual(tableFilterRows('task',rows,{ready:'no'}).length,1);
assert.strictEqual(
  tableFilterRows('task',rows,{project:'p2'})[0].title,'b');
""")


def test_combined_filters_are_and(tmp_path: Path) -> None:
    _node_assert(tmp_path, r"""
const assert=require('assert');
const rows=[
  {title:'a',domain:'work',urgency:'HIGH',impact:'HIGH',progress:0.5,
    estimated_minutes:30,ready:true,status:'TODO'},
  {title:'b',domain:'work',urgency:'LOW',impact:'HIGH',progress:0.2,
    estimated_minutes:200,ready:true,status:'TODO'},
  {title:'c',domain:'home',urgency:'HIGH',impact:'HIGH',progress:0.8,
    estimated_minutes:10,ready:false,status:'DONE'}];
const onlyA=tableFilterRows('task',rows,{domain:'work',urgency:'HIGH',
  progress_min:'40',progress_max:'60',effort_max:'60'});
assert.deepStrictEqual(onlyA.map(o=>o.title),['a']);
assert.strictEqual(tableFilterRows('task',rows,
  {domain:'work',urgency:'HIGH'}).length,1);
assert.strictEqual(tableFilterRows('task',rows,{ready:'no'}).length,1);
// progress and effort ranges exclude rows with unknown values
assert.strictEqual(tableFilterRows('project',
  [{title:'x',progress:null,effort_minutes:null}],
  {progress_min:'1'}).length,0);
assert.strictEqual(tableFilterRows('project',
  [{title:'x',progress:0.5,effort_minutes:10}],
  {effort_max:'5'}).length,0);
""")


def test_normalise_tables_bounds_and_drops_invalid(tmp_path: Path) -> None:
    _node_assert(tmp_path, r"""
const assert=require('assert');
const n=normaliseTables({projects:{sort:[
  {column:'urgency',dir:'desc'},{column:'urgency',dir:'asc'},
  {column:'bogus',dir:'asc'},{column:'impact',dir:'sideways'}],
  filters:{domain:'work',bogus:'x',title:'  keep  '}},
  tasks:{sort:[{column:'ready',dir:'desc'}],filters:{ready:'yes'}}});
assert.deepStrictEqual(n.projects.sort,[{column:'urgency',dir:'desc'},
  {column:'impact',dir:'asc'}]);
assert.deepStrictEqual(n.projects.filters,{domain:'work',title:'  keep  '});
assert.deepStrictEqual(n.tasks.sort,[{column:'ready',dir:'desc'}]);
assert.deepStrictEqual(n.tasks.filters,{ready:'yes'});
// malformed input degrades to the empty, shape-stable default
assert.deepStrictEqual(normaliseTables(null),
  {projects:{sort:[],filters:{}},tasks:{sort:[],filters:{}}});
""")


# --- persistence (Python) ----------------------------------------------------


def test_table_state_persistence_roundtrip(tmp_path: Path) -> None:
    root = str(tmp_path)
    prefs = uistate.apply_update(uistate.default_preferences(), {
        "tables": {
            "projects": {
                "sort": [{"column": "urgency", "dir": "desc"},
                         {"column": "impact", "dir": "desc"}],
                "filters": {"domain": "career", "progress_min": "25"},
            },
            "tasks": {
                "sort": [{"column": "effort", "dir": "asc"}],
                "filters": {"ready": "yes", "status": "READY"},
            },
        },
    })
    uistate.save_preferences(root, prefs)
    reloaded = uistate.load_preferences(root)
    assert reloaded.tables["projects"]["sort"] == [
        {"column": "urgency", "dir": "desc"},
        {"column": "impact", "dir": "desc"},
    ]
    assert reloaded.tables["projects"]["filters"] == {
        "domain": "career", "progress_min": "25"}
    assert reloaded.tables["tasks"]["sort"] == [
        {"column": "effort", "dir": "asc"}]
    assert reloaded.tables["tasks"]["filters"] == {
        "ready": "yes", "status": "READY"}


def test_table_state_normalises_hostile_input() -> None:
    prefs = uistate.apply_update(uistate.default_preferences(), {
        "tables": {
            "projects": {
                "sort": [{"column": "urgency", "dir": "sideways"},
                         {"column": "urgency", "dir": "desc"},
                         {"column": "not-a-column", "dir": "asc"}],
                "filters": {"bogus": "x", "title": "  hello  "},
            },
        },
    })
    assert prefs.tables["projects"]["sort"] == [
        {"column": "urgency", "dir": "asc"}]
    assert prefs.tables["projects"]["filters"] == {"title": "hello"}
    assert prefs.tables["tasks"] == {"sort": [], "filters": {}}


def test_table_state_fails_closed_on_invalid_direct_object() -> None:
    prefs = uistate.UIPreferences(tables={
        "projects": {"sort": [{"column": "bogus", "dir": "asc"}],
                     "filters": {}},
        "tasks": {"sort": [], "filters": {}},
    })
    with pytest.raises(model.MvpError):
        prefs.validate()


def test_table_state_serialisation_is_json_safe(tmp_path: Path) -> None:
    prefs = uistate.apply_update(uistate.default_preferences(), {
        "tables": {"tasks": {"filters": {"title": "needle"}}},
    })
    encoded = json.dumps(prefs.to_dict())
    decoded = uistate.UIPreferences.from_dict(json.loads(encoded)).validate()
    assert decoded.tables["tasks"]["filters"] == {"title": "needle"}


def test_table_state_does_not_touch_portfolio(tmp_path: Path) -> None:
    root = str(tmp_path)
    dataset.write_seed(root)
    path = store.default_portfolio_path(root)
    before = path.read_bytes()
    prefs = uistate.apply_update(uistate.default_preferences(), {
        "tables": {
            "projects": {"sort": [{"column": "urgency", "dir": "desc"}],
                         "filters": {"domain": "career"}},
            "tasks": {"sort": [{"column": "effort", "dir": "asc"}],
                      "filters": {"ready": "yes"}},
        },
    })
    uistate.save_preferences(root, prefs)
    uistate.load_preferences(root)
    assert path.read_bytes() == before
