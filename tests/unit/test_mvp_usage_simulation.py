"""Usage-simulation regression gate.

Runs `scripts/mvp_ui_simulation.py` against a small seeded portfolio and
requires a clean result. This keeps the harness honest: a new view, a new entry
surface or a new HTTP route that renders nothing, throws, or returns a server
error will fail here instead of being discovered by the user.

The simulation is the screen-level contract of the cockpit:
"the shipped page code, driven with the shipped payloads, produces something
visible". It does not replace a human looking at the browser.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

from trajectory_os.mvp import model, store

REPO = Path(__file__).resolve().parents[2]


def _load_harness() -> ModuleType:
    path = REPO / "scripts" / "mvp_ui_simulation.py"
    spec = importlib.util.spec_from_file_location("mvp_ui_simulation", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed(root: Path) -> None:
    store.save_portfolio(str(root), model.Portfolio(
        schema_version=model.SCHEMA_VERSION,
        name="simulation",
        projects=(
            model.Project(
                project_id="case-alpha",
                name="Dossier Alpha — régularisation",
                objective="Finaliser la régularisation des comptes.",
                domain="finance",
                status=model.PS_WAITING,
                aliases=("zephyr", "acronyme"),
            ),
            model.Project(project_id="case-beta", name="Dossier Beta",
                          objective="Obtenir les attestations.",
                          domain="admin"),
        ),
        tasks=(
            model.Task(task_id="alpha.pay", project_id="case-alpha",
                       title="Payer le cabinet", estimated_minutes=30),
            model.Task(task_id="alpha.send", project_id="case-alpha",
                       title="Envoyer les statuts", estimated_minutes=10),
            model.Task(task_id="beta.renew", project_id="case-beta",
                       title="Relancer le greffe", estimated_minutes=15),
        ),
    ))


def test_usage_simulation_reports_no_anomaly(tmp_path: Path,
                                             capsys: pytest.CaptureFixture[str]
                                             ) -> None:
    if shutil.which("node") is None:  # pragma: no cover - node is optional
        pytest.skip("node not available")
    root = tmp_path / "data"
    root.mkdir()
    _seed(root)

    harness = _load_harness()
    code = harness.main(["--root", str(root), "--json"])
    output = capsys.readouterr().out
    payload = json.loads(output)
    anomalies = [error for error in payload["ui"]["errors"]]
    for row in payload["api"]:
        if not row["ok"]:
            anomalies.append(f"{row['route']} → {row['detail']}")
    assert code == 0, "\n".join(anomalies)
    assert anomalies == []
    assert payload["surfaces"] > 100, payload["surfaces"]
    assert len(payload["ui"]["views"]) == 24
    assert len(payload["ui"]["entries"]) == 15
    assert len(payload["ui"]["interactions"]) >= 15


def test_usage_simulation_is_read_only_for_the_source_root(
        tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    _seed(root)
    before = (root / "portfolio.json").read_bytes()
    harness = _load_harness()
    harness.main(["--root", str(root), "--skip-ui"])
    assert (root / "portfolio.json").read_bytes() == before
    # sidecars written during the sweep live in the copy, never here
    assert not (root / "changes.jsonl").exists()
    assert not (root / "inbox").exists()


def test_usage_simulation_requires_a_declared_alias(tmp_path: Path,
                                                    capsys: pytest
                                                    .CaptureFixture[str]) -> None:
    root = tmp_path / "data"
    root.mkdir()
    store.save_portfolio(str(root), model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="no-alias",
        projects=(model.Project(project_id="p", name="P", objective="o"),),
        tasks=(model.Task(task_id="t", project_id="p", title="T",
                          estimated_minutes=5),),
    ))
    harness = _load_harness()
    code = harness.main(["--root", str(root), "--json", "--skip-api"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert any("alias" in error for error in payload["ui"]["errors"])


def test_usage_simulation_rejects_a_missing_root(tmp_path: Path,
                                                 capsys: pytest
                                                 .CaptureFixture[str]) -> None:
    harness = _load_harness()
    code = harness.main(["--root", str(tmp_path / "absent"), "--skip-ui"])
    assert code == 2
    assert "aucun portefeuille" in capsys.readouterr().err


def test_harness_exposes_its_plan_without_side_effects() -> None:
    module = _load_harness()
    assert len(module._API_PLAN) >= 50
    assert any(method == "POST" for method, *_ in module._API_PLAN)
    assert sys.version_info >= (3, 13)
