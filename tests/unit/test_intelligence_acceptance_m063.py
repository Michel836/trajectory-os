"""M063 — intelligence acceptance matrix tests (fixtures, no Git)."""

from __future__ import annotations

from pathlib import Path

from trajectory_os.intelligence import acceptance


def test_acceptance_matrix_has_at_least_fifty_cases(tmp_path: Path) -> None:
    matrix = acceptance.run_acceptance(str(tmp_path))
    assert matrix.total >= 50
    assert matrix.failed == 0, [
        case.case_id for case in matrix.cases if not case.passed]
    assert matrix.passed == matrix.total


def test_acceptance_covers_all_categories(tmp_path: Path) -> None:
    matrix = acceptance.run_acceptance(str(tmp_path))
    categories = {case.category for case in matrix.cases}
    assert categories == set(acceptance.CATEGORIES)
    for category in acceptance.CATEGORIES:
        assert any(case.category == category for case in matrix.cases)


def test_acceptance_matrix_persists_and_renders(tmp_path: Path) -> None:
    matrix = acceptance.run_acceptance(str(tmp_path))
    assert (tmp_path / "acceptance" / "matrix.json").is_file()
    markdown = matrix.render_markdown()
    assert "acceptance matrix" in markdown
    assert f"{matrix.passed}/{matrix.total}" in markdown
