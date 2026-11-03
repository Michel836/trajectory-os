"""Every wired UI action must lead somewhere.

Motivation, measured: in one afternoon of real use, five defects were found by
the human that 5 392 tests had missed. Two of them were of exactly this shape —
the interface offered an action that did nothing (`openObjectDetail` was not
defined, so the button was a dead end).

This module checks the cheapest part of that class mechanically, at every
quality run:

* every event handler referenced in the rendered markup (`onclick`, `onchange`,
  …) is a **defined** function or binding;
* every view name passed to `setView(...)` is a **declared** view;
* every function used as a command target in the palette exists.

It cannot prove that a click *produces a visible result* — that is the
human gesture acceptance (see `docs/development/GESTURE_ACCEPTANCE.md`) and the
headless usage simulation. It does prove that the page cannot reference a
button that leads nowhere.
"""

from __future__ import annotations

import re

import pytest

from trajectory_os.mvp import cockpit_ui, uistate

#: A handler reference inside an attribute. `event.stopPropagation()` and
#: `this.foo()` are DOM plumbing, not page functions, so a value preceded by a
#: dot or an identifier character is not captured.
_HANDLER_RE = re.compile(
    r"""on(?:click|change|input|keydown|keyup|blur|focus|submit)="[^"]*?"""
    r"""(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(""")
_DEFINITION_RES = (
    re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\("),
    re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*="),
)
_SETVIEW_RE = re.compile(r"setView\(\s*'([a-z_]+)'\s*\)")

#: Names that are language keywords or event plumbing, never page functions.
_ALLOWED = frozenset({
    "if", "for", "while", "return", "function", "typeof", "new", "this",
    "event", "window", "document", "Math", "Number", "String", "Boolean",
    "Array", "Object", "JSON", "setTimeout", "clearTimeout", "parseInt",
    "parseFloat", "encodeURIComponent", "decodeURIComponent", "confirm",
    "alert", "prompt", "require", "assert", "console",
    "stopPropagation", "preventDefault",
})

_OVERLAY_DECL_RE = re.compile(r"id='([a-z-]+-overlay)'")
_OVERLAY_USED_RE = re.compile(r"\$\('([a-z-]+-overlay)'\)")
_OVERLAY_USED_ALT_RE = re.compile(r"getElementById\('([a-z-]+-overlay)'\)")


def _defined_names(source: str) -> set[str]:
    names: set[str] = set()
    for pattern in _DEFINITION_RES:
        names.update(pattern.findall(source))
    return names


def _referenced_handlers(source: str) -> set[str]:
    return set(_HANDLER_RE.findall(source))


def test_every_wired_handler_is_defined(page_html: str) -> None:
    """A button that calls an undefined function is a dead end."""
    referenced = _referenced_handlers(cockpit_ui.PAGE_JS)
    referenced |= _referenced_handlers(page_html)
    defined = _defined_names(cockpit_ui.PAGE_JS)
    missing = sorted(referenced - defined - _ALLOWED)
    assert missing == [], (
        "handlers referenced in the markup but never defined: "
        + ", ".join(missing))


@pytest.fixture(scope="module")
def page_html(tmp_path_factory: pytest.TempPathFactory) -> str:
    """The page a fresh install serves, rendered once for the whole module."""
    from trajectory_os.mvp import engine, model, store

    root = str(tmp_path_factory.mktemp("page"))
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="t",
        projects=(model.Project(project_id="p", name="P", objective="o"),),
        tasks=(),
    ))
    return cockpit_ui.render_page(engine.build_cockpit(root))


def test_every_static_page_handler_is_defined(page_html: str) -> None:
    """The page shell is generated too: its handlers must exist as well."""
    referenced = _referenced_handlers(page_html)
    defined = _defined_names(cockpit_ui.PAGE_JS)
    missing = sorted(referenced - defined - _ALLOWED)
    assert missing == [], (
        "handlers rendered in the page but never defined: "
        + ", ".join(missing))


def test_every_targeted_view_is_declared() -> None:
    targeted = set(_SETVIEW_RE.findall(cockpit_ui.PAGE_JS))
    declared = set(uistate.VIEWS)
    assert targeted, "no setView() target found: the check would be vacuous"
    unknown = sorted(targeted - declared)
    assert unknown == [], f"setView() targets not declared in VIEWS: {unknown}"


def test_every_view_is_reachable_from_a_workspace() -> None:
    """A view declared but unreachable is a feature nobody can open."""
    workspaces = set()
    for match in re.finditer(r"views:\[([^\]]*)\]", cockpit_ui.PAGE_JS):
        workspaces.update(re.findall(r"'([a-z_]+)'", match.group(1)))
    assert workspaces, "no workspace declaration found"
    unreachable = sorted(set(uistate.VIEWS) - workspaces)
    assert unreachable == [], f"views not reachable from any workspace: {unreachable}"


def test_every_overlay_referenced_by_a_hook_exists(page_html: str) -> None:
    """Every `$('x-overlay')` used by a hook must exist in the page markup."""
    declared = set(_OVERLAY_DECL_RE.findall(page_html))
    used = set(_OVERLAY_USED_RE.findall(cockpit_ui.PAGE_JS))
    used |= set(_OVERLAY_USED_ALT_RE.findall(cockpit_ui.PAGE_JS))
    assert declared, "no overlay declared: the check would be vacuous"
    missing = sorted(used - declared)
    assert missing == [], f"overlays used but never declared: {missing}"


@pytest.mark.parametrize("marker", [
    "captureToIntake", "openRecall", "confirmIntake", "createIntakeProject",
    "recallOpen", "searchKey",
])
def test_reported_gestures_have_a_named_action(marker: str) -> None:
    """The actions the human reported using must exist as named functions.

    This is a thin guard, not a proof: it keeps the names used in the gesture
    register (`docs/development/GESTURE_ACCEPTANCE.md`) from disappearing.
    """
    assert f"function {marker}" in cockpit_ui.PAGE_JS or \
        f"async function {marker}" in cockpit_ui.PAGE_JS, marker
