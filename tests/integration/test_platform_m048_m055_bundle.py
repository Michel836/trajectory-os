"""M048–M055 integration — the persistent autonomous operator platform."""

from __future__ import annotations

from pathlib import Path

from trajectory_os.platform import acceptance as platform_acceptance
from trajectory_os.platform import dogfood as platform_dogfood
from trajectory_os.platform import hardening as hardening_module


def test_platform_acceptance_matrix_passes(tmp_path: Path) -> None:
    report = platform_acceptance.run_acceptance(tmp_path)
    assert report.status == "PASS", report.render()
    assert len(report.cases) == 57
    assert all(case.ok for case in report.cases)


def test_platform_dogfood_evidence(tmp_path: Path) -> None:
    payload = platform_dogfood.run_platform_dogfood(
        tmp_path, write_evidence=True)
    assert payload["status"] == "PASS", payload
    assert payload["real_evidence"]["projects"] >= 3
    assert payload["real_evidence"]["missions"] >= 5
    assert payload["fixture_evidence"]["simulated_process_death"][
        "crash_detected"]
    assert payload["fixture_evidence"]["simulated_machine_restart"]["restored"]
    # Dogfood objectives must be a structured collection of meaningful
    # strings, never a character-by-character serialization of one string.
    assert isinstance(platform_dogfood.OBJECTIVES, tuple)
    objectives = payload["objectives"]
    assert isinstance(objectives, list)
    assert objectives == list(platform_dogfood.OBJECTIVES)
    assert len(objectives) == len(platform_dogfood.OBJECTIVES) >= 2
    assert all(isinstance(o, str) and len(o) > 1 for o in objectives)
    assert all(o.strip() for o in objectives)
    stored = platform_dogfood.load_dogfood_evidence(str(tmp_path))
    assert stored is not None and stored["status"] == "PASS"
    assert stored["objectives"] == objectives


def test_platform_disaster_recovery(tmp_path: Path) -> None:
    report = hardening_module.disaster_recovery(
        tmp_path,
        backup_dir=tmp_path / "backup",
        fixture_root=tmp_path / "fixture")
    assert report["status"] == "PASS"
    assert report["reconstruction"]["reconstructed"]
    assert report["trust_gates_unchanged"]
    assert report["git_writes"] == 0
