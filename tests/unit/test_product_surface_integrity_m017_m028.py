"""Regression guard for the M017–M028 product-surface integrity contract.

An external review reported BLOCKER findings claiming that
``trajectory_os.resources.arbiter`` began mid-expression (a truncated source
file) and that numerous product modules were missing from the change set,
which would make the unit/integration suites unrunnable.

Those findings were artifacts of evaluating a tracked-only diff: every cited
module is present, importable and passing. This test encodes the invariant the
review was trying to protect so that a *genuine* truncation or deletion fails
loudly and locally instead of surfacing as an opaque collection error.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "trajectory_os"
ARBITER_PATH = SRC_ROOT / "resources" / "arbiter.py"

# Modules named by the review findings whose absence would break the
# ``trajectory_os.resources`` package and every dependent product surface
# (web, events, lifeos, portfolio, daemon, artifacts, agents, graph).
IMPLICATED_MODULES = (
    "trajectory_os.resources.arbiter",
    "trajectory_os.events",
    "trajectory_os.events.engine",
    "trajectory_os.events.model",
    "trajectory_os.events.store",
    "trajectory_os.lifeos",
    "trajectory_os.lifeos.adapters",
    "trajectory_os.lifeos.engine",
    "trajectory_os.lifeos.model",
    "trajectory_os.lifeos.store",
    "trajectory_os.lifeos.summary",
    "trajectory_os.agents.telemetry",
    "trajectory_os.agents.cancellation",
    "trajectory_os.agents.capabilities",
    "trajectory_os.agents.lifecycle",
    "trajectory_os.agents.retry",
    "trajectory_os.agents.route",
    "trajectory_os.agents.identity",
    "trajectory_os.agents.model",
    "trajectory_os.agents.contract",
    "trajectory_os.agents.harness_qualification",
    "trajectory_os.missions.review_protocol",
    "trajectory_os.portfolio",
    "trajectory_os.portfolio.engine",
    "trajectory_os.portfolio.identity",
    "trajectory_os.portfolio.model",
    "trajectory_os.portfolio.summary",
    "trajectory_os.daemon",
    "trajectory_os.daemon.engine",
    "trajectory_os.daemon.model",
    "trajectory_os.daemon.store",
    "trajectory_os.artifacts",
    "trajectory_os.artifacts.engine",
    "trajectory_os.artifacts.identity",
    "trajectory_os.artifacts.model",
    "trajectory_os.artifacts.store",
    "trajectory_os.artifacts.summary",
    "trajectory_os.graph.proof",
    "trajectory_os.graph.replan",
    "trajectory_os.graph.scheduler",
)


def test_every_package_source_is_syntactically_complete() -> None:
    """No module in ``trajectory_os`` may be truncated or corrupted."""

    sources = sorted(SRC_ROOT.rglob("*.py"))
    assert sources, f"no package sources discovered under {SRC_ROOT}"
    for path in sources:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_arbiter_starts_with_a_docstring() -> None:
    """Guard finding #1 directly: the arbiter source is not a mid-expression."""

    tree = ast.parse(ARBITER_PATH.read_text(encoding="utf-8"), filename=str(ARBITER_PATH))
    assert ast.get_docstring(tree), "resources/arbiter.py must begin with a module docstring"
    assert isinstance(tree.body[0], ast.Expr)


@pytest.mark.parametrize("module", IMPLICATED_MODULES)
def test_implicated_product_module_imports(module: str) -> None:
    imported = importlib.import_module(module)
    assert imported.__name__ == module
