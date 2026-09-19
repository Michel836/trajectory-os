"""M060 — provenance-first retrieval unit tests (no Git, no network)."""

from __future__ import annotations

from trajectory_os.intelligence import knowledge


def _sources() -> tuple[knowledge.SourceAdapter, ...]:
    return (
        knowledge.TextSource(
            "notes/a.md", "Alpha",
            "The project must reduce cost and scale the data platform. "
            "Enrichment improved by 20 percent."),
        knowledge.TextSource(
            "notes/b.md", "Beta",
            "Enrichment improved by 35 percent. Regulatory compliance is a "
            "recurring concern."),
        knowledge.TextSource(
            "notes/c.md", "Secret", "Restricted pricing detail.",
            sensitivity=knowledge.SENS_RESTRICTED),
    )


def _index() -> knowledge.KnowledgeIndex:
    return knowledge.build_index(_sources(),
                                 generated_at="2026-01-01T00:00:00Z")


def test_index_carries_document_and_chunk_provenance() -> None:
    index = _index()
    assert index.document_count == 3
    assert index.chunk_count >= 3
    assert all(document.content_hash for document in index.documents)
    assert all(chunk.document_id and chunk.source_ref and chunk.content_hash
               for chunk in index.chunks)


def test_retrieval_is_ranked_and_cited() -> None:
    index = _index()
    trace = knowledge.retrieve(index, "enrichment improved",
                               generated_at="2026-01-01T00:00:00Z")
    assert trace.status == knowledge.OK
    assert trace.results
    assert [r.rank for r in trace.results] == list(
        range(1, len(trace.results) + 1))
    for result in trace.results:
        citation = result.to_dict()["citation"]
        assert citation["source_ref"] == result.chunk.source_ref
        assert citation["content_hash"] == result.chunk.content_hash


def test_no_evidence_does_not_substitute_model_knowledge() -> None:
    index = _index()
    trace = knowledge.retrieve(index, "zzzunmatchedquery",
                               generated_at="2026-01-01T00:00:00Z")
    assert trace.status == knowledge.NO_EVIDENCE
    assert "no model knowledge" in trace.caveat
    assert trace.canonical is False


def test_sensitivity_boundary_filters_results() -> None:
    index = _index()
    trace = knowledge.retrieve(index, "restricted pricing",
                               allowed_sensitivities=(
                                   knowledge.SENS_PUBLIC,),
                               generated_at="2026-01-01T00:00:00Z")
    assert all(result.chunk.sensitivity != knowledge.SENS_RESTRICTED
               for result in trace.results)


def test_stale_and_missing_statuses() -> None:
    index = _index()
    statuses = {entry.source_ref: entry.status
                for entry in knowledge.assess_staleness(
                    index, (knowledge.TextSource(
                        "notes/a.md", "Alpha", "changed content"),))}
    assert statuses["notes/a.md"] == knowledge.STALE
    assert statuses["notes/b.md"] == knowledge.MISSING
    fresh = knowledge.assess_staleness(index, _sources())
    assert all(entry.status == knowledge.FRESH for entry in fresh)


def test_new_source_is_detected() -> None:
    index = _index()
    statuses = knowledge.assess_staleness(
        index, (*_sources(),
                knowledge.TextSource("notes/d.md", "Delta", "new")))
    assert any(entry.status == knowledge.NEW for entry in statuses)


def test_contradiction_is_explicit_and_unresolved() -> None:
    index = _index()
    chunks = index.chunks
    contradiction = knowledge.record_contradiction(
        index, left_chunk_id=chunks[0].chunk_id,
        right_chunk_id=chunks[1].chunk_id, kind=knowledge.CT_NUMERIC,
        description="numeric mismatch")
    assert contradiction.kind == knowledge.CT_NUMERIC
    assert contradiction.resolution.startswith("UNRESOLVED")


def test_index_and_trace_persist(tmp_path) -> None:
    index = _index()
    knowledge.persist_index(str(tmp_path), index)
    reloaded = knowledge.load_index(str(tmp_path))
    assert reloaded is not None
    assert reloaded.index_id == index.index_id
    trace = knowledge.retrieve(index, "cost")
    knowledge.persist_trace(str(tmp_path), trace)
    assert trace.to_dict()["canonical"] is False
