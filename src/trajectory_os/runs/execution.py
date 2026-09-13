"""V2.01 (with the V2.03/V2.04 layers) — candidate eligibility composition.

Pure, deterministic, read-only composition of the per-layer rules for ONE
candidate job spec:

* dependency rule (V2.03): no blocking cycle reachable from the candidate,
  and all prerequisites resolved against authoritative terminal evidence;
* resource rule (V2.04): the requirement fits capacity - current usage, and
  unknown evidence DEFERS (never over-commits);
* same-worktree rule (V1.92/V2.01): jobs that read a SHARED source checkout
  conflict with a concurrent record that mutates that checkout; mutating jobs
  work in per-job isolated copies, so isolation is PROVEN by the canonical
  spec (mutating => isolated workspace) and two isolated jobs on the same
  origin checkout do not conflict.

No side effects. No provider coupling. No inference of live facts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trajectory_os.runs import dependencies, model, resources, spec


@dataclass(frozen=True)
class ActiveFacts:
    """Minimal, verifiable facts about one concurrently active record."""

    job_id: str
    execution_class: str | None      # None when the record predates canonical specs
    source_checkout: str | None      # absolute origin checkout as recorded


@dataclass(frozen=True)
class CandidateContext:
    """Authoritative, read-only context for evaluating one candidate."""

    terminal_map: Mapping[str, str] = field(default_factory=dict)  # job_id -> latest terminal
    dependency_graph: Mapping[str, Sequence[str]] = field(
        default_factory=dict
    )  # job_id -> prereqs
    capacity: resources.ResourceCapacity | None = None
    usage: resources.ResourceUsage | None = None
    active_facts: tuple[ActiveFacts, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CandidateDecision:
    eligible: bool
    reasons: tuple[str, ...]
    dependency_status: dependencies.DependencyStatus
    resource_decision: resources.ResourceDecision | None
    worktree_conflicts: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "dependency": self.dependency_status.to_dict(),
            "resource": (
                self.resource_decision.to_dict()
                if self.resource_decision is not None
                else None
            ),
            "worktree_conflicts": list(self.worktree_conflicts),
        }


def resolve_tree(path: str | Path | None) -> str | None:
    """Canonicalize a checkout path (no filesystem access, no side effects)."""
    if path is None:
        return None
    return Path(path).resolve().as_posix()


def usage_for_specs(specs: Sequence[spec.JobSpec]) -> resources.ResourceUsage:
    """Deterministically sum declared requirements of active specs."""
    total = resources.ResourceUsage()
    for candidate in specs:
        requirement = candidate.resources
        if requirement is None or not requirement.declared:
            continue
        dims = requirement.dimensions()
        total = total.add(
            resources.ResourceUsage(
                dims.get("cpu_slots", 0),
                dims.get("ram_bytes", 0),
                dims.get("gpu_slots", 0),
                dims.get("gpu_mem_bytes", 0),
            )
        )
    return total


def worktree_conflicts(
    candidate: spec.JobSpec,
    active_facts: Sequence[ActiveFacts],
) -> tuple[str, ...]:
    """Deterministic same-worktree conflict check (fail closed).

    A candidate that reads a shared checkout (``shared_read_only``) conflicts
    with a concurrent record that mutates the SAME checkout.  A candidate
    with the isolated policy works in its own copy — its origin checkout is
    not what it executes in, so no conflict is attributed to it.
    """
    if candidate.workspace_policy != spec.WORKSPACE_SHARED_READ_ONLY:
        return ()
    candidate_tree = resolve_tree(candidate.source_checkout)
    if candidate_tree is None:
        return ()
    conflicts: list[str] = []
    for record in active_facts:
        if record.execution_class != spec.EXEC_MUTATING:
            continue
        if record.source_checkout is None:
            # Cannot prove isolation for that record: fail closed.
            conflicts.append(f"{record.job_id}:{model.REASON_RESOURCE_UNKNOWN}:unproven")
            continue
        if resolve_tree(record.source_checkout) == candidate_tree:
            conflicts.append(f"{record.job_id}:{model.ERR_SAME_WORKTREE_CONFLICT}")
    return tuple(conflicts)


def evaluate_candidate(
    candidate: spec.JobSpec,
    context: CandidateContext | None = None,
) -> CandidateDecision:
    """Compose all per-layer rules for one candidate (pure, deterministic)."""
    ctx = context or CandidateContext()
    reasons: list[str] = []
    dep_status = dependencies.DependencyStatus(
        candidate.job_id, True, None, None, candidate.permit_failed_prereqs
    )
    conflict_ids: tuple[str, ...] = ()

    # 1) dependency rule — a blocking cycle anywhere in the candidate's
    #    reachable prerequisite subgraph rejects the candidate.
    graph = dict(ctx.dependency_graph)
    graph[candidate.job_id] = tuple(candidate.depends_on)
    cycle = dependencies.find_dependency_cycle(candidate.job_id, graph)
    if cycle is not None:
        reasons.append(
            f"{model.ERR_DEPENDENCY_CYCLE}:{'->'.join(cycle)}"
        )
        dep_status = dependencies.DependencyStatus(
            candidate.job_id, False, model.ERR_DEPENDENCY_CYCLE,
            cycle[1] if len(cycle) > 1 else None, candidate.permit_failed_prereqs,
        )
    else:
        dep_status = dependencies.resolve_dependencies(
            candidate.job_id,
            candidate.depends_on,
            dict(ctx.terminal_map),
            candidate.permit_failed_prereqs,
        )
        if not dep_status.satisfied:
            reasons.append(
                f"{dep_status.blocking_reason}:{dep_status.blocking_prereq}"
            )

    # 2) resource rule (unknown evidence defers; no over-commit).
    resource_decision: resources.ResourceDecision | None = None
    if candidate.resources is not None and candidate.resources.declared:
        capacity = ctx.capacity
        if capacity is None:
            resource_decision = resources.ResourceDecision(
                model.RES_DECISION_DEFERRED,
                tuple(
                    f"{model.REASON_RESOURCE_UNKNOWN}:{dim}"
                    for dim in candidate.resources.dimensions()
                ),
            )
        else:
            resource_decision = resources.evaluate(
                candidate.resources, capacity, ctx.usage
            )
        if not resource_decision.allowed:
            reasons.extend(resource_decision.reasons)

    # 3) same-worktree rule.
    conflict_ids = worktree_conflicts(candidate, ctx.active_facts)
    if conflict_ids:
        reasons.extend(conflict_ids)

    return CandidateDecision(
        eligible=not reasons,
        reasons=tuple(reasons),
        dependency_status=dep_status,
        resource_decision=resource_decision,
        worktree_conflicts=conflict_ids,
    )
