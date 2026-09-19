"""M056–M063 — adaptive intelligence bundle integration tests (no Git)."""

from __future__ import annotations

import json
from pathlib import Path

from trajectory_os.intelligence import dogfood, projection


def test_dogfood_without_real_history_is_honest(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    missing = tmp_path / "no-history"
    evidence = dogfood.run_dogfood(
        str(runtime), real_runs_dir=str(missing), docs_dir=None,
        generated_at="2026-01-01T00:00:00Z")
    assert evidence.history_source == dogfood.HISTORY_UNAVAILABLE
    assert evidence.real_row_count == 0
    assert evidence.ml_report is None
    assert evidence.routing is None
    assert evidence.decision is None
    assert evidence.acceptance["failed"] == 0
    assert evidence.marker == dogfood.MARKER
    assert {workflow["family"] for workflow in evidence.workflows} == {
        "CAREER_CONSULTING", "LIFE_SCIENCES_PHARMA", "RESEARCH_STRATEGY"}


def test_dogfood_persists_durable_artifacts(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    docs = tmp_path / "docs"
    dogfood.run_dogfood(
        str(runtime), real_runs_dir=str(tmp_path / "absent"),
        docs_dir=str(docs), generated_at="2026-01-01T00:00:00Z")
    assert (docs / "m056-m063-dogfood.json").is_file()
    assert (docs / "m056-m063-dogfood.md").is_file()
    assert (runtime / "workflows" / "dogfood-career" /
            "deliverable.md").is_file()
    assert (runtime / "workflows" / "dogfood-research" /
            "deliverable.md").is_file()
    document = json.loads(
        (docs / "m056-m063-dogfood.json").read_text())
    assert document["marker"] == dogfood.MARKER


def test_projection_reads_persisted_intelligence(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    dogfood.run_dogfood(
        str(runtime), real_runs_dir=str(tmp_path / "absent"),
        docs_dir=None, generated_at="2026-01-01T00:00:00Z")
    projected = projection.build_intelligence_projection(
        str(runtime), clock=lambda: "2026-01-01T00:00:00Z")
    assert projected["read_only"] is True
    assert projected["dataset"]["source_kind"] == "REAL"
    assert projected["routing"] is None
    assert len(projected["workflows"]) == 3
    assert projected["ml"] is None


def test_lifeos_projection_refuses_target_inside_root(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    import pytest

    from trajectory_os.intelligence import model

    with pytest.raises(model.IntelligenceError):
        projection.write_lifeos_notes(str(runtime), str(runtime / "vault"))


def test_lifeos_projection_writes_outside_root(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    dogfood.run_dogfood(
        str(runtime), real_runs_dir=str(tmp_path / "absent"),
        docs_dir=None, generated_at="2026-01-01T00:00:00Z")
    vault = tmp_path / "vault"
    written = projection.write_lifeos_notes(str(runtime), str(vault))
    assert written
    assert all(Path(note["path"]).is_file() for note in written)


def test_platform_api_exposes_intelligence_projection(tmp_path: Path) -> None:
    from trajectory_os.platform import api as platform_api

    runtime = tmp_path / "runtime"
    dogfood.run_dogfood(
        str(runtime), real_runs_dir=str(tmp_path / "absent"),
        docs_dir=None, generated_at="2026-01-01T00:00:00Z")
    api = platform_api.LocalApi(str(runtime))
    response = api.handle("GET", "/api/intelligence")
    assert response.status == 200
    payload = response.json()
    assert payload["read_only"] is True
    decisions = api.handle("GET", "/api/intelligence/decisions")
    assert decisions.status == 200
    assert decisions.json()["read_only"] is True
