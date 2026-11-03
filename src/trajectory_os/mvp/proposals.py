"""MVP — human review and application of inbound proposals (Phase 3).

Analysis creates proposals; this module is the only place where a proposal
can change the portfolio. Every application goes through the validated
mutation/outcome layer, writes an evidence record, and leaves the immutable
change journal as the audit trail.

Acceptance is per proposal. There is deliberately no "accept all" shortcut:
the human reviews each before/after pair.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trajectory_os.intelligence import model as intel_model
from trajectory_os.mvp import changes, model, mutations, outcomes, signals

EVIDENCE_SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z")


def _find_proposal(root: str | Path, signal_id: str,
                   proposal_id: str) -> signals.Proposal:
    for proposal in signals.load_proposals(root, signal_id):
        if proposal.proposal_id == proposal_id:
            return proposal
    raise signals.SignalError(model.E_UNKNOWN_ID, proposal_id)


def _apply(root: str | Path, proposal: signals.Proposal) -> dict[str, Any]:
    after = proposal.after
    op = proposal.op
    if op == signals.OP_CREATE_TASK:
        return mutations.create_task(root, after)
    if op == signals.OP_CREATE_PROJECT:
        return mutations.create_project(root, after)
    if op == signals.OP_UPDATE_TASK:
        return mutations.update_task(root, proposal.target_id, after)
    if op == signals.OP_UPDATE_PROJECT:
        return mutations.update_project(root, proposal.target_id, after)
    if op == signals.OP_SET_DEADLINE:
        if proposal.target_kind == signals.TARGET_TASK:
            return mutations.update_task(
                root, proposal.target_id,
                {"deadline": after.get("deadline")})
        if proposal.target_kind == signals.TARGET_PROJECT:
            return mutations.update_project(
                root, proposal.target_id,
                {"deadline": after.get("deadline")})
    if op == signals.OP_SET_STATUS:
        if proposal.target_kind == signals.TARGET_TASK:
            return mutations.update_task(
                root, proposal.target_id,
                {"status": after.get("status")})
        if proposal.target_kind == signals.TARGET_PROJECT:
            return mutations.update_project(
                root, proposal.target_id,
                {"status": after.get("status")})
    if op == signals.OP_SET_EFFORT:
        return mutations.update_task(
            root, proposal.target_id,
            {"estimated_minutes": after.get("estimated_minutes")})
    if op == signals.OP_RESOLVE_WAITING:
        return mutations.update_task(root, proposal.target_id, {
            "status": after.get("status", model.TS_TODO),
            "waiting_for": after.get("waiting_for", []),
        })
    if op == signals.OP_ADD_BLOCKER:
        portfolio = _load_portfolio(root)
        task = portfolio.task_map()[proposal.target_id]
        blocker = str(after.get("blocker", ""))
        return mutations.update_task(root, proposal.target_id, {
            "blocked_by": list(task.blocked_by) + ([blocker]
                                                  if blocker else []),
        })
    if op == signals.OP_RECORD_OUTCOME:
        return outcomes.record_and_save(
            root, task_id=proposal.target_id,
            outcome=str(after.get("outcome", model.O_COMPLETED)),
            actual_minutes=after.get("actual_minutes"),
            note=str(after.get("note", "")))
    raise signals.SignalError(model.E_MALFORMED,
                              f"op {proposal.op!r} cannot be applied")


def _load_portfolio(root: str | Path) -> model.Portfolio:
    from trajectory_os.mvp import store

    portfolio = store.load_portfolio(root)
    if portfolio is None:
        raise signals.SignalError(model.E_MALFORMED, "no portfolio loaded")
    return portfolio


def _target_identity(proposal: signals.Proposal,
                     result: Mapping[str, Any]) -> tuple[str, str]:
    if proposal.target_kind == signals.TARGET_TASK:
        return (changes.OBJECT_TASK,
                str(result.get("task_id", proposal.target_id)))
    if proposal.target_kind == signals.TARGET_PROJECT:
        return (changes.OBJECT_PROJECT,
                str(result.get("project_id", proposal.target_id)))
    if "task_id" in result:
        return changes.OBJECT_TASK, str(result["task_id"])
    if "project_id" in result:
        return changes.OBJECT_PROJECT, str(result["project_id"])
    return changes.OBJECT_BATCH, proposal.signal_id


def _record_evidence(root: str | Path, signal: signals.InboundSignal,
                     proposal: signals.Proposal, decision: str,
                     result: Mapping[str, Any], at: str) -> None:
    intel_model.append_jsonl(signals.evidence_path(root), {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "signal_id": signal.signal_id,
        "proposal_id": proposal.proposal_id,
        "op": proposal.op,
        "target_kind": proposal.target_kind,
        "target_id": proposal.target_id,
        "decision": decision,
        "quote": proposal.evidence_quote,
        "source_ref": signal.source_ref,
        "at": at,
        "result": dict(result),
    })


def _refresh_signal_status(root: str | Path, signal_id: str) -> str:
    proposals = signals.load_proposals(root, signal_id)
    if any(proposal.state == signals.STATE_PENDING
           for proposal in proposals):
        return signals.STATUS_ANALYZED
    if any(proposal.state == signals.STATE_ACCEPTED
           for proposal in proposals):
        return signals.STATUS_APPLIED
    return signals.STATUS_DISMISSED


def _journal_signal(root: str | Path, signal: signals.InboundSignal,
                    proposal: signals.Proposal, result: Mapping[str, Any],
                    at: str) -> None:
    object_kind, object_id = _target_identity(proposal, result)
    with contextlib.suppress(OSError, changes.ChangeError):
        changes.append_change(
            root, kind=changes.KIND_SIGNAL, object_kind=object_kind,
            object_id=object_id, title=proposal.title,
            summary=f"proposition acceptée · {proposal.op}",
            before=proposal.before, after={**proposal.after,
                                           "proposal_id":
                                               proposal.proposal_id},
            source="signal", undoable=False, references=signal.signal_id,
            at=at)


def apply_proposal(root: str | Path, signal_id: str,
                   proposal_id: str) -> dict[str, Any]:
    """Accept and apply one pending proposal through the validated layer."""
    signal = signals.get_signal(root, signal_id)
    proposal = _find_proposal(root, signal_id, proposal_id)
    if proposal.state != signals.STATE_PENDING:
        raise signals.SignalError(model.E_MALFORMED,
                                  "proposal is not pending")
    result = _apply(root, proposal)
    at = _now()
    updated = signals.update_proposal(
        root, signal_id, proposal_id, state=signals.STATE_ACCEPTED,
        applied_at=at)
    status = _refresh_signal_status(root, signal_id)
    signals.update_signal_status(root, signal_id, status)
    _record_evidence(root, signal, proposal, "ACCEPTED", result, at)
    _journal_signal(root, signal, proposal, result, at)
    return {
        "proposal": updated.to_dict(),
        "result": dict(result),
        "signal_status": status,
    }


def reject_proposal(root: str | Path, signal_id: str,
                    proposal_id: str,
                    *, reason: str = "") -> dict[str, Any]:
    """Reject one proposal; the portfolio is never touched."""
    signal = signals.get_signal(root, signal_id)
    proposal = _find_proposal(root, signal_id, proposal_id)
    if proposal.state != signals.STATE_PENDING:
        raise signals.SignalError(model.E_MALFORMED,
                                  "proposal is not pending")
    at = _now()
    updated = signals.update_proposal(
        root, signal_id, proposal_id, state=signals.STATE_REJECTED)
    status = _refresh_signal_status(root, signal_id)
    signals.update_signal_status(root, signal_id, status)
    _record_evidence(root, signal, proposal, "REJECTED",
                     {"reason": reason}, at)
    return {"proposal": updated.to_dict(), "signal_status": status}


def defer_proposal(root: str | Path, signal_id: str,
                   proposal_id: str) -> dict[str, Any]:
    """Keep a proposal for later without applying it."""
    signal = signals.get_signal(root, signal_id)
    proposal = _find_proposal(root, signal_id, proposal_id)
    if proposal.state != signals.STATE_PENDING:
        raise signals.SignalError(model.E_MALFORMED,
                                  "proposal is not pending")
    at = _now()
    updated = signals.update_proposal(
        root, signal_id, proposal_id, state=signals.STATE_DEFERRED)
    status = _refresh_signal_status(root, signal_id)
    signals.update_signal_status(root, signal_id, status)
    _record_evidence(root, signal, proposal, "DEFERRED", {}, at)
    return {"proposal": updated.to_dict(), "signal_status": status}


__all__ = [
    "EVIDENCE_SCHEMA_VERSION", "apply_proposal", "defer_proposal",
    "reject_proposal",
]
