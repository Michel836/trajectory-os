"""Persistent bounded FIFO queue (V1.89) — store-level unit tests.

Covers: bounded capacity (MAX_QUEUE_ENTRIES, deterministic QueueFullError);
strict first-in-first-out ordering via monotonic sequence numbers; duplicate
job-id rejection while queued or reserved (active); re-entry after terminal;
malformed/missing evidence fail closed; atomic writes leave no partial
file or tmp remnants; closed-record append is bounded (last
MAX_CLOSED_RECORDS retained, deterministic order); round-trip stability.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trajectory_os.runs import model, store


class TestQueueBounded:
    def test_enqueue_up_to_capacity(self, tmp_path: Path) -> None:
        doc = store.QueueDoc(seq=0, entries=[])
        for i in range(model.MAX_QUEUE_ENTRIES):
            entry = doc.enqueue(job_id=f"j{i:03d}", command=["true"], max_attempts=1)
            assert entry.seq == i + 1
        doc.save(tmp_path / "queue.json")
        assert len(store.QueueDoc.load(tmp_path / "queue.json").entries) == model.MAX_QUEUE_ENTRIES

    def test_queue_full_rejects_deterministically(self, tmp_path: Path) -> None:
        doc = store.QueueDoc(seq=0, entries=[])
        while len(doc.entries) < model.MAX_QUEUE_ENTRIES:
            doc.enqueue(job_id=f"fill-{len(doc.entries)}", command=["true"], max_attempts=1)
        doc.save(tmp_path / "queue.json")
        with pytest.raises(store.QueueFullError):
            store.QueueDoc.load(tmp_path / "queue.json").enqueue(
                job_id="overflow", command=["true"], max_attempts=1
            )

    def test_fifo_ordering_by_seq(self, tmp_path: Path) -> None:
        doc = store.QueueDoc(seq=0, entries=[])
        for name in ("zeta", "alpha", "mid"):
            doc.enqueue(job_id=name, command=["true"], max_attempts=1)
        assert [e.job_id for e in doc.entries] == ["zeta", "alpha", "mid"]
        doc.save(tmp_path / "queue.json")
        assert store.QueueDoc.load(tmp_path / "queue.json").head().job_id == "zeta"

    def test_remove_preserves_order(self) -> None:
        doc = store.QueueDoc(seq=0, entries=[])
        for name in ("a", "b", "c"):
            doc.enqueue(job_id=name, command=["true"], max_attempts=1)
        removed = doc.remove("b")
        assert removed.job_id == "b"
        assert [e.job_id for e in doc.entries] == ["a", "c"]
        with pytest.raises(store.QueueEmptyError):
            doc.remove("missing")

    def test_head_empty_is_none(self) -> None:
        doc = store.QueueDoc(seq=0, entries=[])
        assert doc.head() is None


class TestQueueDuplicate:
    def test_duplicate_queued_rejected(self) -> None:
        doc = store.QueueDoc(seq=0, entries=[])
        doc.enqueue(job_id="dup", command=["true"], max_attempts=1)
        with pytest.raises(store.DuplicateIdentityError):
            doc.enqueue(job_id="dup", command=["true"], max_attempts=1)

    def test_duplicate_with_active_rejected(self) -> None:
        doc = store.QueueDoc(seq=0, entries=[])
        with pytest.raises(store.DuplicateIdentityError):
            doc.enqueue(job_id="j1", command=["true"], max_attempts=1,
                        reserved_ids=frozenset({"j1"}))

    def test_reentry_allowed_after_terminal(self) -> None:
        """A job may be re-enqueued once it is no longer queued/active."""
        doc = store.QueueDoc(seq=0, entries=[])
        doc.enqueue(job_id="j1", command=["true"], max_attempts=2)
        doc.remove("j1")  # terminal handling consumed it
        again = doc.enqueue(job_id="j1", command=["true"], max_attempts=2, attempts=1)
        assert again.job_id == "j1" and again.attempts == 1


class TestMalformedFailClosed:
    def test_missing_file_is_empty_not_error(self, tmp_path: Path) -> None:
        doc = store.QueueDoc.load(tmp_path / "queue.json")
        assert doc.entries == [] and doc.seq == 0

    def test_garbage_queue_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "queue.json"
        p.write_text("{ this is not: valid json", encoding="utf-8")
        with pytest.raises(store.MalformedStoreError):
            store.QueueDoc.load(p)

    def test_wrong_schema_version_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "queue.json"
        p.write_text('{"schema_version": 999, "seq": 0, "entries": []}', encoding="utf-8")
        with pytest.raises(store.MalformedStoreError):
            store.QueueDoc.load(p)

    def test_invalid_known_field_rejected(self, tmp_path: Path) -> None:
        """Fail-closed applies to missing/invalid known fields."""
        doc = store.QueueDoc(seq=0, entries=[])
        doc.enqueue(job_id="x", command=["true"], max_attempts=1)
        p = tmp_path / "q.json"
        doc.save(p)
        data = json.loads(p.read_text(encoding="utf-8"))
        data["seq"] = "not-an-int"  # invalid known field => reject
        p.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(store.MalformedStoreError):
            store.QueueDoc.load(p)

    def test_unknown_extra_field_tolerated_forward_compatible(self, tmp_path: Path) -> None:
        """Extra keys must not break older readers (schema evolution)."""
        doc = store.QueueDoc(seq=0, entries=[])
        doc.enqueue(job_id="x", command=["true"], max_attempts=1)
        p = tmp_path / "q.json"
        doc.save(p)
        data = json.loads(p.read_text(encoding="utf-8"))
        data["schema_hint"] = "future-field"
        p.write_text(json.dumps(data), encoding="utf-8")
        loaded = store.QueueDoc.load(p)  # must not raise
        assert len(loaded.entries) == 1


class TestAtomicWrite:
    def _full_doc(self) -> store.QueueDoc:
        doc = store.QueueDoc(seq=0, entries=[])
        for i in range(model.MAX_QUEUE_ENTRIES):
            doc.enqueue(job_id=f"j{i:03d}", command=["true"], max_attempts=1)
        return doc

    def test_save_leaves_no_tmp_remnants(self, tmp_path: Path) -> None:
        self._full_doc().save(tmp_path / "queue.json")
        names = [f.name for f in tmp_path.iterdir()]
        assert names == ["queue.json"]
        reloaded = store.QueueDoc.load(tmp_path / "queue.json")
        assert len(reloaded.entries) == model.MAX_QUEUE_ENTRIES

    def test_round_trip_stable(self, tmp_path: Path) -> None:
        self._full_doc().save(tmp_path / "queue.json")
        a = store.QueueDoc.load(tmp_path / "queue.json")
        b = store.QueueDoc.load(tmp_path / "queue.json")
        assert [e.job_id for e in a.entries] == [e.job_id for e in b.entries]
        assert a.seq == b.seq


class TestActiveRecords:
    def test_round_trip_and_live_proven_default(self, tmp_path: Path) -> None:
        rec = store.ActiveRecord(
            job_id="j1", slot="s1", pgid=111, pid=111, token="tok",
            workspace="/tmp/ws", command=["true"], seq=1, max_attempts=1,
            attempts=1, started_at="t0", log_stdout="o.log", log_stderr="e.log",
        )
        path = tmp_path / "active.json"
        store.save_active_records(path, [rec])
        loaded = store.load_active_records(path)
        assert len(loaded) == 1
        assert loaded[0].job_id == "j1"
        assert loaded[0].live_proven is False  # proven only at observation time

    def test_missing_active_is_empty(self, tmp_path: Path) -> None:
        assert store.load_active_records(tmp_path / "active.json") == []


class TestClosedRecords:
    def _record(self, i: int) -> store.ClosedRecord:
        return store.ClosedRecord(
            job_id=f"j{i:03d}",
            seq=i + 1,
            observed_at=f"2026-01-01T00:00:{i % 60:02d}+00:00",
            terminal="done",
            attempts=1,
            exit_code=0,
            signal=None,
        )

    def test_append_bounded_to_cap(self, tmp_path: Path) -> None:
        p = tmp_path / "closed.json"
        for i in range(model.MAX_CLOSED_RECORDS + 5):  # overfill on purpose
            store.append_closed_record(p, self._record(i))
        records = store.load_closed_records(p)
        assert len(records) == model.MAX_CLOSED_RECORDS
        # newest retained (bounded tail), deterministic ascending order
        seqs = [r.seq for r in records]
        assert seqs == sorted(seqs)
        assert seqs[-1] == model.MAX_CLOSED_RECORDS + 5

    def test_missing_closed_is_empty(self, tmp_path: Path) -> None:
        assert store.load_closed_records(tmp_path / "closed.json") == []

    def test_malformed_closed_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "closed.json"
        p.write_text("not json at all", encoding="utf-8")
        with pytest.raises(store.MalformedStoreError):
            store.load_closed_records(p)
