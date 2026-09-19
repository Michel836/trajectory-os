"""M028 — LifeOS exchange engine (scoped, idempotent, failure-isolated).

One exchange builds a bounded, scoped projection of the owning goal's
canonical state, hands it to each explicitly enabled adapter and records the
exact provenance of what left the boundary. Adapter failures are captured as
``FAILED`` records; the other adapters still run and canonical state is
verified unchanged before and after.

The engine is read-only over the canonical stores and never performs a Git
trust-boundary write.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trajectory_os.artifacts import store as artifact_store
from trajectory_os.events import engine as event_engine
from trajectory_os.goals import snapshot as goal_snapshot
from trajectory_os.lifeos import adapters as lifeos_adapters
from trajectory_os.lifeos import identity as lifeos_identity
from trajectory_os.lifeos import model
from trajectory_os.lifeos import store as lifeos_store

#: Truncation bound for a captured adapter error string.
MAX_ERROR_LEN = 256


@dataclass(frozen=True)
class ExchangeReport:
    """Outcome of one scoped LifeOS exchange across all enabled adapters."""

    goal_id: str
    exchange_id: str
    generated_at: str
    canonical_unchanged: bool
    records: tuple[model.ExchangeRecord, ...]

    @property
    def status(self) -> str:
        if not self.canonical_unchanged:
            return "CANONICAL_MUTATED"
        if any(record.status == model.XS_FAILED for record in self.records):
            return "PARTIAL"
        if all(record.status in (model.XS_SKIPPED,)
               for record in self.records) and self.records:
            return "UNCHANGED"
        return "OK"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "schema_version": model.SCHEMA_VERSION,
            "lifeos_version": model.LIFEOS_VERSION,
            "goal_id": self.goal_id,
            "exchange_id": self.exchange_id,
            "generated_at": self.generated_at,
            "canonical_unchanged": self.canonical_unchanged,
            "records": [record.to_dict() for record in self.records],
        }


def _canonical_fingerprint(root: str, goal_id: str) -> dict[str, Any]:
    """Read-only identity of the canonical state an exchange must not move."""
    snapshot = goal_snapshot.build_snapshot(root, goal_id)
    final = snapshot.get("final", {})
    proof = snapshot.get("proof", {})
    generation = snapshot.get("generation") or {}
    return {
        "goal_id": goal_id,
        "graph_id": snapshot.get("goal", {}).get("graph_id"),
        "generation_id": generation.get("generation_id"),
        "final_state": final.get("state"),
        "final_reason": final.get("reason"),
        "proof_id": proof.get("proof_id"),
    }


def _scope_events(root: str, goal_id: str,
                  scope: model.ExchangeScope) -> list[Mapping[str, Any]]:
    records = event_engine.project_events(root, goal_id)
    if scope.event_categories:
        allowed = set(scope.event_categories)
        records = [record for record in records
                   if record.category in allowed]
    selected = records[-scope.max_events:] if scope.max_events else []
    return [record.to_dict() for record in selected]


def _scope_artifacts(root: str, goal_id: str,
                     scope: model.ExchangeScope) -> list[Mapping[str, Any]]:
    if not scope.include_artifacts:
        return []
    try:
        artifacts = artifact_store.list_artifacts(root, goal_id)
    except Exception:  # noqa: BLE001 - scoped projection must not crash
        return []
    selected = artifacts[:scope.max_artifacts]
    return [{
        "artifact_id": artifact.artifact_id,
        "kind": artifact.kind,
        "name": artifact.name,
        "mission_id": artifact.mission_id,
        "content_sha256": artifact.content_sha256,
        "size_bytes": artifact.size_bytes,
    } for artifact in selected]


def _inside(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
        base = root.resolve()
    except OSError:  # pragma: no cover - defensive
        return True
    return resolved == base or base in resolved.parents


def _target_is_safe(target: str | None, root: str) -> bool:
    if not target:
        return False
    return not _inside(Path(target), Path(root))


def _failed(adapter: str, exchange_id: str, goal_id: str, created_at: str,
            event_ids: Sequence[str], artifact_ids: Sequence[str],
            code: str) -> model.ExchangeRecord:
    return model.ExchangeRecord.build(
        adapter=adapter, exchange_id=exchange_id, goal_id=goal_id,
        status=model.XS_FAILED, event_ids=event_ids,
        artifact_ids=artifact_ids, outputs=(), created_at=created_at,
        error=code[:MAX_ERROR_LEN])


def run_exchange(
    root: str | Path,
    goal_id: str,
    config: model.LifeOSConfig,
    *,
    clock: Callable[[], str] | None = None,
) -> ExchangeReport:
    """Run one scoped, idempotent, failure-isolated LifeOS exchange."""
    config.validate()
    root_str = str(root)
    stamp = (clock or goal_snapshot.utc_now_iso)()
    before = _canonical_fingerprint(root_str, goal_id)

    events = _scope_events(root_str, goal_id, config.scope)
    artifacts = _scope_artifacts(root_str, goal_id, config.scope)
    event_ids = [str(event.get("event_id")) for event in events]
    artifact_ids = [str(artifact.get("artifact_id"))
                    for artifact in artifacts]
    enabled = config.enabled
    exchange_id = lifeos_identity.exchange_id({
        "schema_version": model.SCHEMA_VERSION,
        "goal_id": goal_id,
        "scope": config.scope.identity_payload(),
        "event_ids": event_ids,
        "artifact_ids": artifact_ids,
        "adapters": [adapter.kind for adapter in enabled],
    })

    payload = lifeos_adapters.ExchangePayload(
        goal_id=goal_id, exchange_id=exchange_id, generated_at=stamp,
        snapshot=before, events=tuple(events), artifacts=tuple(artifacts))

    records: list[model.ExchangeRecord] = []
    for adapter_config in enabled:
        existing = lifeos_store.find_record(
            root_str, adapter_config.kind, exchange_id)
        if existing is not None and existing.status in (
                model.XS_OK, model.XS_SKIPPED):
            records.append(model.ExchangeRecord.build(
                adapter=adapter_config.kind, exchange_id=exchange_id,
                goal_id=goal_id, status=model.XS_SKIPPED,
                event_ids=event_ids, artifact_ids=artifact_ids,
                outputs=existing.outputs, created_at=stamp,
                error=None))
            continue
        if not _target_is_safe(adapter_config.target, root_str):
            records.append(_failed(
                adapter_config.kind, exchange_id, goal_id, stamp,
                event_ids, artifact_ids, model.E_TARGET_INSIDE_ROOT))
            lifeos_store.append_record(root_str, records[-1])
            continue
        assert adapter_config.target is not None
        try:
            adapter = lifeos_adapters.get_adapter(adapter_config.kind)
            outputs = adapter.exchange(
                payload, target=adapter_config.target,
                options=adapter_config.options)
            record = model.ExchangeRecord.build(
                adapter=adapter_config.kind, exchange_id=exchange_id,
                goal_id=goal_id, status=model.XS_OK, event_ids=event_ids,
                artifact_ids=artifact_ids, outputs=outputs,
                created_at=stamp, error=None)
        except Exception as exc:  # noqa: BLE001 - failure isolation
            record = _failed(
                adapter_config.kind, exchange_id, goal_id, stamp,
                event_ids, artifact_ids,
                f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LEN])
        lifeos_store.append_record(root_str, record)
        records.append(record)

    after = _canonical_fingerprint(root_str, goal_id)
    unchanged = before == after
    if not unchanged:  # pragma: no cover - structural guard should prevent
        raise model.LifeOSError(
            model.E_CANONICAL_MUTATED,
            f"exchange {exchange_id} moved canonical state for {goal_id}")
    return ExchangeReport(
        goal_id=goal_id, exchange_id=exchange_id, generated_at=stamp,
        canonical_unchanged=True, records=tuple(records))
