"""M056 — canonical learning dataset unit tests (fixtures only, no Git)."""

from __future__ import annotations

from pathlib import Path

import pytest

from trajectory_os.intelligence import dataset, fixtures, model


def _fixture_root(tmp_path: Path, count: int = 40) -> Path:
    root = tmp_path / "runs"
    fixtures.generate_fixture_runs(root, count=count)
    return root


def test_extraction_is_deterministic(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    first = dataset.build_learning_dataset(
        [root], source_kind=model.SOURCE_FIXTURE,
        built_at="2026-01-01T00:00:00Z")
    second = dataset.build_learning_dataset(
        [root], source_kind=model.SOURCE_FIXTURE,
        built_at="2026-01-01T00:00:00Z")
    assert first.dataset_id == second.dataset_id
    assert first.row_count == second.row_count == 40


def test_every_value_carries_provenance(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    learning = dataset.build_learning_dataset(
        [root], source_kind=model.SOURCE_FIXTURE,
        built_at="2026-01-01T00:00:00Z")
    for observation in learning.observations:
        for field_name in dataset.OBSERVATION_FIELDS:
            assert observation.provenance_for(field_name) is not None
        for entry in observation.provenance.entries:
            if entry.source == model.UNAVAILABLE:
                assert entry.reason
            else:
                assert entry.source_ref


def test_canonical_source_reference_and_read_only(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    before = {path: path.read_text() for path in root.rglob("*")
              if path.is_file()}
    learning = dataset.build_learning_dataset(
        [root], source_kind=model.SOURCE_FIXTURE,
        built_at="2026-01-01T00:00:00Z")
    after = {path: path.read_text() for path in root.rglob("*")
             if path.is_file()}
    assert before == after
    assert all(observation.source_ref for observation in learning.observations)


def test_quality_report_reports_missingness(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    learning = dataset.build_learning_dataset(
        [root], source_kind=model.SOURCE_FIXTURE,
        built_at="2026-01-01T00:00:00Z")
    quality = learning.quality
    assert quality.field_availability["cost_usd"] == 0
    assert quality.field_availability["duration_s"] == 40
    assert "cost_usd" in quality.untrainable_targets


def test_fixture_rows_are_never_real(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    learning = dataset.build_learning_dataset(
        [root], source_kind=model.SOURCE_FIXTURE,
        built_at="2026-01-01T00:00:00Z")
    assert learning.quality.real_rows == 0
    assert learning.quality.fixture_rows == learning.row_count


def test_splits_are_leakage_free_and_reproducible(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path, count=80)
    learning = dataset.build_learning_dataset(
        [root], source_kind=model.SOURCE_FIXTURE,
        built_at="2026-01-01T00:00:00Z")
    assert learning.split_metadata["leakage_free"] is True
    groups: dict[str, set[str]] = {}
    for observation in learning.observations:
        split = learning.splits[observation.observation_id]
        groups.setdefault(observation.group_key, set()).add(split)
    assert all(len(splits) == 1 for splits in groups.values())
    reassigned, _ = dataset.split_assignments(learning.observations)
    assert reassigned == dict(learning.splits)


def test_unsupported_schema_version_fails_closed(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path, count=5)
    learning = dataset.build_learning_dataset(
        [root], source_kind=model.SOURCE_FIXTURE,
        built_at="2026-01-01T00:00:00Z")
    document = learning.to_dict()
    document["schema_version"] = 999
    with pytest.raises(model.IntelligenceError) as error:
        dataset.LearningDataset.from_dict(document)
    assert error.value.code == model.E_UNSUPPORTED_VERSION


def test_missing_schema_version_fails_closed(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path, count=5)
    learning = dataset.build_learning_dataset(
        [root], source_kind=model.SOURCE_FIXTURE,
        built_at="2026-01-01T00:00:00Z")
    document = learning.to_dict()
    del document["schema_version"]
    with pytest.raises(model.IntelligenceError) as error:
        dataset.LearningDataset.from_dict(document)
    assert error.value.code == model.E_UNSUPPORTED_VERSION


def test_persistence_round_trip(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path, count=10)
    learning = dataset.build_learning_dataset(
        [root], source_kind=model.SOURCE_FIXTURE,
        built_at="2026-01-01T00:00:00Z")
    dataset.persist_dataset(str(tmp_path), learning)
    reloaded = dataset.load_dataset(str(tmp_path))
    assert reloaded is not None
    assert reloaded.dataset_id == learning.dataset_id
    assert reloaded.to_dict() == learning.to_dict()


def test_missing_source_is_skipped_not_fabricated(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    learning = dataset.build_learning_dataset(
        [empty], source_kind=model.SOURCE_REAL,
        built_at="2026-01-01T00:00:00Z")
    assert learning.row_count == 0
    assert learning.quality.real_rows == 0


def test_target_value_never_imputes(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path, count=3)
    learning = dataset.build_learning_dataset(
        [root], source_kind=model.SOURCE_FIXTURE,
        built_at="2026-01-01T00:00:00Z")
    observation = learning.observations[0]
    assert dataset.target_value(observation, "cost_usd") is None
    assert dataset.target_value(observation, "duration_s") is not None
