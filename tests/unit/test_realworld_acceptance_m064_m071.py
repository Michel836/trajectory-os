"""M064–M071 — acceptance matrix and trust-boundary tests."""

from __future__ import annotations

from pathlib import Path

from trajectory_os.realworld import acceptance


def test_acceptance_matrix_has_at_least_sixty_cases(tmp_path: Path) -> None:
    matrix = acceptance.run_acceptance(
        str(tmp_path), generated_at="2026-01-01T00:00:00Z")
    assert matrix.total >= 60
    assert matrix.failed == 0, [
        case.to_dict() for case in matrix.cases if not case.passed]
    assert set(matrix.category_counts()) == set(acceptance.CATEGORIES)


def test_acceptance_matrix_persists(tmp_path: Path) -> None:
    acceptance.run_acceptance(str(tmp_path),
                              generated_at="2026-01-01T00:00:00Z")
    assert (tmp_path / "acceptance" / "matrix.json").is_file()
    assert (tmp_path / "acceptance" / "matrix.md").is_file()
