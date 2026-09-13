"""V1.98 — consolidated, deterministic observability (strict, evidence-bounded).

One typed document plus an optional human rendering, derived purely from
canonical state.  Stable ``schema_version``.  All functions are *read-only*:
they derive facts only from already-persisted state (``build_ops_document``
reads persisted state files and performs no writes) — no mutations, no
network, no provider coupling.  Strict parsing is reused from the
persistence layer (``store``); malformed state fails closed (the typed
error propagates) instead of rendering partial data.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from trajectory_os.runs import dependencies, execution, model, spec, store
from trajectory_os.runs import orchestration as orch


def _as_mapping(value: object) -> Mapping[str, object]:
    """Narrow an untrusted document value to a string mapping (fail closed).

    A non-Mapping value — including ``None`` and scalars — is *rejected* with
    a deterministic ``TypeError``.  It is never silently coerced into a
    valid-looking empty mapping: malformed evidence must surface as an error,
    not as a plausible empty state.
    """
    if isinstance(value, Mapping):
        return value
    raise TypeError(
        f"OBSERVABILITY_MALFORMED: expected Mapping, got {type(value).__name__}"
    )


def _job_core(js: object) -> dict[str, object]:
    command = getattr(js, "command", None)
    command = command if isinstance(command, (list, tuple)) else []
    return {
        "runner": getattr(js, "runner", None),
        "execution_class": getattr(js, "execution_class", None),
        "command_truncated": [
            str(part) if len(str(part)) <= 100 else str(part)[:100] + "…"
            for part in list(command)[:12]
        ],
    }


def _active_doc(
    record: orch.ActiveRecord,
    terminal_map: Mapping[str, str],
    reasons: tuple[str, ...] = (),
) -> dict[str, object]:
    doc = _job_core(record)
    doc.update(
        {
            "job_id": record.job_id,
            "pid": record.pid if isinstance(record.pid, int) else None,
            "pgid": record.pgid if isinstance(record.pgid, int) else None,
            "attempt": record.attempts if isinstance(record.attempts, int) else None,
            "max_attempts": record.max_attempts
            if isinstance(record.max_attempts, int)
            else None,
            "workspace_slot": str(record.workspace) if record.workspace else None,
            "ownership": {
                "live": bool(record.live_proven),
                "reasons": list(reasons),
            },
            "terminal": terminal_map.get(record.job_id),
        }
    )
    return doc


def _closed_doc(record: orch.ClosedRecord) -> dict[str, object]:
    doc = _job_core(record)
    doc.update(
        {
            "job_id": record.job_id,
            "terminal": record.terminal,
            "attempts_used": record.attempts if isinstance(record.attempts, int) else None,
            "workspace_slot": store.slot_name(record.seq),
            "observed_at": record.observed_at,
        }
    )
    return doc


def _resource_evidence(active: list[orch.ActiveRecord]) -> dict[str, object]:
    # Evidence only: declared resource usage when provenance exists on the
    # record (absent on legacy records -> status UNAVAILABLE, never guessed).
    declared: dict[str, object] = {}
    for record in active:
        js = getattr(record, "spec", None)
        if not isinstance(js, spec.JobSpec):
            continue
        declared[record.job_id] = execution.usage_for_specs((js,)).to_dict()
    return {
        "status": (
            model.RESOURCE_EVIDENCE_STATUS_AVAILABLE
            if declared
            else model.RESOURCE_EVIDENCE_STATUS_UNAVAILABLE
        ),
        "evidence": declared,
    }


def snapshot(state: orch.StateBundle) -> dict[str, object]:
    terminal_map: Mapping[str, str] = dict(state.terminal_map)
    started_by: Mapping[str, tuple[str, ...]] = orch.started_by_jobs(state)
    queue = list(state.queue.entries)
    by_terminal: dict[str, int] = {}
    by_exec: dict[str, int] = {}

    active_docs: list[dict[str, object]] = []
    for record in state.active:
        proof = state.active_proven.get(record.job_id)
        active_docs.append(
            _active_doc(
                record,
                terminal_map,
                proof.reasons if proof is not None else tuple(),
            )
        )
        by_exec[str(record.execution_class)] += 1

    def entry_doc(entry: store.QueueEntry) -> dict[str, object]:
        js = entry.spec
        core = _job_core(js) if js is not None else {"runner": None, "command_truncated": []}
        core.update(
            {
                "state": "queued",
                "retry_wait": entry.retry_wait if isinstance(entry.retry_wait, int) else 0,
            }
        )
        if js is not None:
            status = model.DEP_STATUS_SATISFIED
            for dep in js.depends_on:
                code, reason = dependencies.prerequisite_state(
                    dep, terminal_map, bool(js.permit_failed_prereqs)
                )
                if reason is not None:
                    status = code
                    break
            core.update({"dependency_status": status})
        return core

    queue_docs = [entry_doc(entry) for entry in queue]

    closed_docs = [_closed_doc(record) for record in state.closed]
    for document in closed_docs:
        terminal = str(document.get("terminal"))
        by_terminal[terminal] = by_terminal.get(terminal, 0) + 1

    proven_active = sum(1 for record in state.active if record.live_proven)
    capacity_section = {
        "active_records": len(state.active),
        "active_proven": proven_active,
        "active_unproven": len(state.active) - proven_active,
    }

    return {
        "schema_version": model.OPS_SCHEMA_VERSION,
        "counts": {
            "active": len(active_docs),
            "queued": len(queue_docs),
            "closed": len(closed_docs),
            "active_proven": proven_active,
        },
        "active": active_docs,
        "queue": queue_docs,
        "closed": closed_docs,
        "by_execution_class": by_exec,
        "dependencies": {
            "edges": {
                job: [
                    {
                        "prerequisite": dep,
                        "status": dependencies.prerequisite_state(
                            dep, terminal_map, False
                        )[0],
                    }
                    for dep in deps
                ]
                for job, deps in started_by.items()
            },
        },
        "terminal_map": dict(terminal_map),
        "started_by": {job: list(deps) for job, deps in started_by.items()},
        "capacity": capacity_section,
        "resource_evidence": _resource_evidence(list(state.active)),
    }


def render_human(doc: Mapping[str, object]) -> str:
    lines: list[str] = []

    def section(key: str) -> list[Mapping[str, object]]:
        raw = doc.get(key)
        out: list[Mapping[str, object]] = []
        if isinstance(raw, (list, tuple)):
            for item in raw:
                if isinstance(item, Mapping):
                    out.append(item)
        return out

    schema = doc.get("schema_version")
    counts = _as_mapping(doc.get("counts"))
    lines.append(
        "ops:"
        + str(schema)
        + " active=" + str(counts.get("active"))
        + " queued=" + str(counts.get("queued"))
        + " closed=" + str(counts.get("closed"))
    )
    def _reasons(owner: Mapping[str, object]) -> str:
        raw = owner.get("reasons")
        return ",".join(str(r) for r in raw) if isinstance(raw, (list, tuple)) else ""

    for record in section("active"):
        owner = _as_mapping(record.get("ownership"))
        lines.append(
            f"A {record.get('job_id')} pid={record.get('pid')} "
            f"exec={record.get('execution_class')} live={owner.get('live')} "
            f"reasons={_reasons(owner) or '-'}"
        )
    for record in section("queue"):
        dep = record.get("dependency_status")
        lines.append(
            f"Q {record.get('job_id')} retry_wait={record.get('retry_wait')} dep={dep}"
        )
    for record in section("closed"):
        lines.append(
            f"X {record.get('job_id')} terminal={record.get('terminal')} "
            f"runs={record.get('attempts_used')}"
        )
    return "\n".join(lines)


def build_ops_document(state_root: Path, runs_root: object = None) -> dict[str, object]:
    """Read the single source of truth and derive one stable document.

    ``runs_root`` is accepted for call-site compatibility; state reconstruction
    is strictly local to ``state_root`` in the current persistence layout.
    Malformed state fails closed (typed exception propagates; no partial render).
    """
    state = orch.rebuild_state(state_root)
    return snapshot(state)


def render_json(doc: Mapping[str, object]) -> str:
    return json.dumps(dict(doc), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
