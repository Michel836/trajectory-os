"""Integration tests for the public JSON portfolio file import boundary."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from trajectory_os.domain.entities import SourceKind
from trajectory_os.domain.portfolio import Portfolio
from trajectory_os.importers import PortfolioImportError, import_portfolio_file
from trajectory_os.importers.identity import canonicalize_import_id


def _valid_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_namespace": "acme",
        "portfolio": {"external_id": "portfolio-1", "name": "Acme Portfolio"},
        "entities": [
            {
                "external_id": "project-1",
                "entity_type": "project",
                "title": "Project One",
                "created_at": "2023-01-01T12:00:00+05:00",
                "updated_at": "2023-01-02T00:00:00Z",
            },
            {
                "external_id": "task-1",
                "entity_type": "task",
                "title": "Task One",
                "confidence": 0.7,
            },
        ],
        "relations": [
            {
                "external_id": "rel-1",
                "source_external_id": "task-1",
                "target_external_id": "project-1",
                "relation_type": "belongs_to",
                "confidence": 0.5,
            }
        ],
    }


def _write(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "portfolio.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_str_path_accepted(tmp_path: Path):
    path = _write(tmp_path, _valid_payload())
    portfolio = import_portfolio_file(str(path))
    assert isinstance(portfolio, Portfolio)


def test_path_object_accepted(tmp_path: Path):
    path = _write(tmp_path, _valid_payload())
    portfolio = import_portfolio_file(path)
    assert isinstance(portfolio, Portfolio)


def test_valid_file_returns_canonical_portfolio(tmp_path: Path):
    portfolio = import_portfolio_file(_write(tmp_path, _valid_payload()))
    assert portfolio.name == "Acme Portfolio"
    assert len(portfolio.entities) == 2
    assert len(portfolio.relations) == 1
    assert portfolio.relations[0].source_id in {e.id for e in portfolio.entities}
    assert portfolio.relations[0].target_id in {e.id for e in portfolio.entities}


def test_deterministic_ids_preserved_through_public_importer(tmp_path: Path):
    path = _write(tmp_path, _valid_payload())
    first = import_portfolio_file(path)
    second = import_portfolio_file(path)

    assert first.id == second.id
    assert first.id == canonicalize_import_id("portfolio", "acme", "portfolio-1")
    assert [e.id for e in first.entities] == [e.id for e in second.entities]
    assert canonicalize_import_id("entity", "acme", "project-1") in {
        e.id for e in first.entities
    }
    assert canonicalize_import_id("relation", "acme", "rel-1") in {
        r.id for r in first.relations
    }
    assert first.relations[0].id == second.relations[0].id


def test_source_imported_preserved(tmp_path: Path):
    portfolio = import_portfolio_file(_write(tmp_path, _valid_payload()))
    assert all(e.source == SourceKind.IMPORTED for e in portfolio.entities)
    assert all(r.source == SourceKind.IMPORTED for r in portfolio.relations)


def test_explicit_timestamps_preserved(tmp_path: Path):
    portfolio = import_portfolio_file(_write(tmp_path, _valid_payload()))
    project = next(e for e in portfolio.entities if e.title == "Project One")
    offset = timezone(timedelta(hours=5))
    assert project.created_at == datetime(2023, 1, 1, 12, 0, 0, tzinfo=offset)
    assert project.created_at.utcoffset() == timedelta(hours=5)
    assert project.updated_at == datetime(2023, 1, 2, tzinfo=UTC)


def test_missing_file_raises_portfolio_import_error(tmp_path: Path):
    with pytest.raises(PortfolioImportError, match="unable to read"):
        import_portfolio_file(tmp_path / "does-not-exist.json")


def test_malformed_json_raises_portfolio_import_error(tmp_path: Path):
    path = tmp_path / "portfolio.json"
    path.write_text('{"schema_version": 1, "source_namespace": "acme"', encoding="utf-8")
    with pytest.raises(PortfolioImportError, match="invalid JSON"):
        import_portfolio_file(path)


def test_invalid_utf8_raises_portfolio_import_error(tmp_path: Path):
    path = tmp_path / "portfolio.json"
    path.write_bytes(b"\xff\xfe\xfa{\xff invalid")
    with pytest.raises(PortfolioImportError, match="unable to read"):
        import_portfolio_file(path)


@pytest.mark.parametrize(
    "root",
    [
        [{"external_id": "p"}],
        "payload",
        42,
        True,
        None,
    ],
    ids=["array", "string", "number", "boolean", "null"],
)
def test_non_object_json_root_raises_portfolio_import_error(tmp_path: Path, root: object):
    path = _write(tmp_path, root)
    with pytest.raises(PortfolioImportError):
        import_portfolio_file(path)


def test_unsupported_schema_version_raises_portfolio_import_error(tmp_path: Path):
    payload = dict(_valid_payload())
    payload["schema_version"] = 2  # type: ignore[typeddict-item]
    with pytest.raises(PortfolioImportError, match="schema"):
        import_portfolio_file(_write(tmp_path, payload))


def test_invalid_enum_raises_portfolio_import_error(tmp_path: Path):
    payload = json.loads(json.dumps(_valid_payload()))
    payload["entities"][0]["entity_type"] = "warp_drive"
    with pytest.raises(PortfolioImportError, match="schema"):
        import_portfolio_file(_write(tmp_path, payload))


def test_duplicate_entity_ids_raise_portfolio_import_error(tmp_path: Path):
    payload = json.loads(json.dumps(_valid_payload()))
    payload["entities"].append(
        {"external_id": "project-1", "entity_type": "task", "title": "Clone"}
    )
    with pytest.raises(PortfolioImportError, match="duplicate entity external_id"):
        import_portfolio_file(_write(tmp_path, payload))


def test_duplicate_relation_ids_raise_portfolio_import_error(tmp_path: Path):
    payload = json.loads(json.dumps(_valid_payload()))
    payload["relations"].append(
        {
            "external_id": "rel-1",
            "source_external_id": "project-1",
            "target_external_id": "task-1",
            "relation_type": "blocks",
        }
    )
    with pytest.raises(PortfolioImportError, match="duplicate relation external_id"):
        import_portfolio_file(_write(tmp_path, payload))


def test_unknown_relation_endpoint_raises_portfolio_import_error(tmp_path: Path):
    payload = json.loads(json.dumps(_valid_payload()))
    payload["relations"][0]["target_external_id"] = "ghost"
    with pytest.raises(PortfolioImportError, match="unknown target entity"):
        import_portfolio_file(_write(tmp_path, payload))


def test_self_relation_rejected_by_domain_validation(tmp_path: Path):
    payload = json.loads(json.dumps(_valid_payload()))
    payload["relations"][0]["source_external_id"] = "project-1"
    with pytest.raises(PortfolioImportError, match="self-relations are not allowed"):
        import_portfolio_file(_write(tmp_path, payload))


def test_original_exception_preserved_as_cause(tmp_path: Path):
    with pytest.raises(PortfolioImportError) as exc_info:
        import_portfolio_file(tmp_path / "does-not-exist.json")
    assert exc_info.value.__cause__ is not None
    assert isinstance(exc_info.value.__cause__, (OSError, UnicodeDecodeError))

    path = tmp_path / "portfolio.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(PortfolioImportError) as info:
        import_portfolio_file(path)
    assert isinstance(info.value.__cause__, json.JSONDecodeError)


def test_failure_returns_no_portfolio(tmp_path: Path):
    payload = json.loads(json.dumps(_valid_payload()))
    payload["entities"][0]["entity_type"] = "warp_drive"
    path = _write(tmp_path, payload)

    outcome: object = None
    try:
        outcome = import_portfolio_file(path)
    except PortfolioImportError:
        pass
    else:
        pytest.fail("expected PortfolioImportError")
    assert not isinstance(outcome, Portfolio)
