"""M064 — real input adapter unit tests (deterministic, no network)."""

from __future__ import annotations

from pathlib import Path

from trajectory_os.intelligence import knowledge
from trajectory_os.realworld import ingest


def _write(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "a.md").write_text("Acme faces a data problem.",
                                    encoding="utf-8")
    (directory / "a-copy.md").write_text("Acme faces a data problem.",
                                         encoding="utf-8")
    (directory / "secret.md").write_text("password = hunter2\n",
                                         encoding="utf-8")
    (directory / "roles.csv").write_text("role,level\nData,senior\n",
                                         encoding="utf-8")
    (directory / "raw.pdf").write_bytes(b"%PDF-1.4 binary")


def test_manifest_is_deterministic_and_provenanced(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    _write(inputs)
    first = ingest.ingest_paths(
        [str(inputs)], root=str(tmp_path / "one"),
        generated_at="2026-01-01T00:00:00Z")
    second = ingest.ingest_paths(
        [str(inputs)], root=str(tmp_path / "two"),
        generated_at="2026-01-02T00:00:00Z")
    assert first.manifest_id == second.manifest_id
    assert first.canonical is False and first.runtime_truth is False
    assert all(record.origin and record.content_hash and record.location
               for record in first.records)


def test_duplicate_detection(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    _write(inputs)
    manifest = ingest.ingest_paths(
        [str(inputs)], root=str(tmp_path / "root"),
        generated_at="2026-01-01T00:00:00Z")
    duplicates = [record for record in manifest.records
                  if record.ingestion_status == ingest.STATUS_DUPLICATE]
    assert duplicates
    assert all(record.duplicate_of for record in duplicates)


def test_changed_source_detection(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    _write(inputs)
    prior = ingest.ingest_paths(
        [str(inputs)], root=str(tmp_path / "prior"),
        generated_at="2026-01-01T00:00:00Z")
    (inputs / "a.md").write_text("Acme faces a scaling challenge.",
                                 encoding="utf-8")
    changed = ingest.ingest_paths(
        [str(inputs)], root=str(tmp_path / "changed"), prior_manifest=prior,
        generated_at="2026-01-02T00:00:00Z")
    changed_records = [record for record in changed.records
                       if record.ingestion_status == ingest.STATUS_CHANGED]
    assert changed_records
    assert all(record.changed_from for record in changed_records)


def test_unsupported_format_is_explicit_and_no_ocr(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    _write(inputs)
    manifest = ingest.ingest_paths(
        [str(inputs)], root=str(tmp_path / "root"),
        generated_at="2026-01-01T00:00:00Z")
    unsupported = ingest.unsupported_records(manifest)
    assert unsupported
    assert all(record.ingestion_status == ingest.STATUS_UNSUPPORTED
               for record in unsupported)
    assert any(ingest.LIM_NO_OCR in record.conversion_limitations
               for record in unsupported)


def test_secret_redaction_and_restricted_not_persisted(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    _write(inputs)
    manifest = ingest.ingest_paths(
        [str(inputs)], root=str(tmp_path / "root"),
        generated_at="2026-01-01T00:00:00Z")
    secret = next(record for record in manifest.records
                  if record.location.endswith("secret.md"))
    assert secret.sensitivity == knowledge.SENS_CONFIDENTIAL
    content = manifest.content_by_source[secret.source_id]
    assert "hunter2" not in content
    assert "[REDACTED_SECRET]" in content

    restricted_file = inputs / "restricted.md"
    restricted_file.write_text("restricted pricing", encoding="utf-8")
    restricted = ingest.ingest_paths(
        [str(restricted_file)], root=str(tmp_path / "restricted"),
        generated_at="2026-01-01T00:00:00Z",
        sensitivity_overrides={str(restricted_file):
                               knowledge.SENS_RESTRICTED})
    assert restricted.contents
    assert all(item.persisted is False for item in restricted.contents)


def test_remote_source_records_network_limitation(tmp_path: Path) -> None:
    manifest = ingest.ingest_remote(
        (ingest.RemoteSource("https://example.invalid/x", "Evidence text"),
         ), root=str(tmp_path), generated_at="2026-01-01T00:00:00Z")
    assert manifest.records[0].origin == ingest.ORIGIN_URL
    assert ingest.LIM_NETWORK_NOT_FETCHED in (
        manifest.records[0].conversion_limitations)


def test_manifest_round_trip(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    _write(inputs)
    manifest = ingest.ingest_paths(
        [str(inputs)], root=str(tmp_path / "root"),
        generated_at="2026-01-01T00:00:00Z")
    loaded = ingest.load_manifest(str(tmp_path / "root"))
    assert loaded is not None
    assert loaded.manifest_id == manifest.manifest_id
