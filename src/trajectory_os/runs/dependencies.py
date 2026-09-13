"""V2.03 — simple, bounded prerequisite dependencies for multi-run jobs.

Deliberately minimal (no DAG solver, no scheduler engine):

* a job MAY declare up to ``MAX_DEPENDENCIES`` job id prerequisites
  (``depends_on``), captured on the canonical ``JobSpec``;
* a prerequisite is **satisfied** only when an authoritative closed record
  shows the prerequisite job reached ``done``;
* a prerequisite that reached a non-done terminal (``failed`` / ``crashed``
  / ``cancelled`` / ...) BLOCKS the dependent unless the spec explicitly
  sets ``permit_failed_prereqs`` (and is still reported, never hidden);
* a prerequisite that never reached a terminal state BLOCKS the dependent
  (``DEPENDENCY_UNKNOWN``) — no speculative starts (fail closed);
* self-references and malformed ids are rejected with stable codes;
* cycles are detected by a **bounded** deterministic DFS and reported with
  the cycle path — no infinite loops are possible on any input.

All functions are pure and side-effect free (read-only).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from trajectory_os.runs import model, spec

TERMINAL_SATISFYING = {spec.TERMINAL_DONE}
TERMINAL_BLOCKING = {
    spec.TERMINAL_FAILED,
    spec.TERMINAL_CRASHED,
    spec.TERMINAL_CANCELLED,
    spec.TERMINAL_CANCEL_PENDING,
    spec.TERMINAL_UNKNOWN,
}


class DependencyError(Exception):
    """Malformed / cyclic dependency declaration (fail closed)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class DependencyStatus:
    """Deterministic prerequisite resolution outcome for one job."""

    job_id: str
    satisfied: bool
    blocking_reason: str | None          # stable code (None when satisfied)
    blocking_prereq: str | None          # the first unsatisfied prerequisite
    permit_failed_prereqs: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "satisfied": self.satisfied,
            "blocking_reason": self.blocking_reason,
            "blocking_prereq": self.blocking_prereq,
            "permit_failed_prereqs": self.permit_failed_prereqs,
        }


def validate_depends_on(job_id: str, depends_on: Sequence[str] | object) -> tuple[str, ...]:
    """Strict, bounded validation of a prerequisite list for one job."""
    if isinstance(depends_on, bool) or not isinstance(depends_on, (tuple, list)):
        raise DependencyError(
            model.ERR_DEPENDENCY_MALFORMED,
            f"malformed depends_on: {repr(depends_on)[:160]}",
        )
    deps = tuple(depends_on)
    if len(deps) > model.MAX_DEPENDENCIES:
        raise DependencyError(
            model.ERR_DEPENDENCY_MALFORMED,
            f"more than {model.MAX_DEPENDENCIES} prerequisites",
        )
    seen: set[str] = set()
    for dep in deps:
        if (
            not isinstance(dep, str)
            or not (1 <= len(dep) <= spec.MAX_JOB_ID_SPEC_LEN)
            or spec.JOB_ID_SPEC_RE.fullmatch(dep) is None
        ):
            raise DependencyError(
                model.ERR_DEPENDENCY_MALFORMED, f"invalid prerequisite: {repr(dep)[:160]}"
            )
        if dep == job_id:
            raise DependencyError(model.ERR_DEPENDENCY_SELF, dep)
        if dep in seen:
            raise DependencyError(model.ERR_DEPENDENCY_MALFORMED, f"duplicate: {dep}")
        seen.add(dep)
    return deps


class _CycleFound(Exception):
    def __init__(self, cycle: tuple[str, ...]) -> None:
        super().__init__()
        self.cycle = cycle


_CYCLE_UNCOLORED = -1


def _dfs_from(
    start: str,
    graph: Mapping[str, Sequence[str]],
    color: dict[str, int],
) -> tuple[str, ...] | None:
    """One colored DFS from ``start`` against a caller-owned color map.

    The traversal is fully iterative (an explicit ``frames`` stack of edge
    iterators, ``next``-driven; no recursive helper), so it cannot raise
    ``RecursionError`` on any input, no matter how deep the dependency
    chain.

    Honest bounds:

    * cycle-reporting position lookup is O(1) — the ``pos`` map keys node
      id -> current path index, no linear path scan;
    * ``path`` and ``pos`` each store at most the current DFS depth, i.e.
      O(depth), where depth <= number of reachable nodes;
    * a node is pushed only while uncolored (gray on push, black on pop),
      so each node enters the frame stack at most once per color map and
      the whole walk is O(nodes + edges) steps — no input can drive an
      infinite loop or unbounded stack growth.
    """
    path: list[str] = []
    pos: dict[str, int] = {}
    frames: list[Iterator[str]] = []

    def push(node: str) -> None:
        color.setdefault(node, 0)
        color[node] = 0
        pos[node] = len(path)
        path.append(node)
        frames.append(iter(tuple(graph.get(node, ()))))

    try:
        push(start)
        while frames:
            dep = next(frames[-1], None)
            if dep is None:  # frame exhausted: pop and mark black
                frames.pop()
                node = path.pop()
                del pos[node]
                color[node] = 1
                continue
            if dep not in graph:
                continue  # unknown prerequisite: resolved elsewhere (UNKNOWN)
            state = color.get(dep, _CYCLE_UNCOLORED)
            if state == _CYCLE_UNCOLORED:
                push(dep)
            elif state == 0:  # on the current path: closed cycle found
                raise _CycleFound(tuple(path[pos[dep]:]) + (dep,))
    except _CycleFound as found:
        return found.cycle
    return None


def find_dependency_cycle(
    job_id: str,
    graph: Mapping[str, Sequence[str]],
) -> tuple[str, ...] | None:
    """Find one blocking dependency cycle in the subgraph reachable from
    ``job_id`` (bounded, deterministic, read-only).

    A cycle is *blocking* for ``job_id`` when the job's prerequisite chain
    loops (the loop may or may not include the job itself): any job whose
    prerequisites transitively re-enter themselves can never be satisfied, so
    it must be rejected rather than started.  Returns the cycle as a closed id
    path (first id repeated at the end), or ``None``.
    """
    if job_id not in graph:
        return None
    return _dfs_from(job_id, graph, {})


def detect_any_cycle(graph: Mapping[str, Sequence[str]]) -> tuple[str, ...] | None:
    """Bounded scan of the whole graph for ANY dependency cycle (deterministic
    node order: sorted ids).  Returns a closed id path, or ``None``.

    Colored state is shared across node starts, so each node is colored at
    most once and the whole scan is O(nodes + edges).
    """
    color: dict[str, int] = {}
    for node in sorted(graph):
        if color.get(node, _CYCLE_UNCOLORED) == 1:
            continue
        cycle = _dfs_from(node, graph, color)
        if cycle is not None:
            return cycle
    return None


def prerequisite_state(
    prereq_id: str,
    terminal_map: Mapping[str, str],
    permit_failed_prereqs: bool,
) -> tuple[str, str | None]:
    """Resolve one prerequisite against authoritative terminal evidence.

    Returns ``(status_code, blocking_reason)``:

    * ``done`` terminal                -> satisfied;
    * other terminal + no permission   -> blocked (``DEPENDENCY_BLOCKED``);
    * other terminal + permission      -> satisfied (reported, never hidden);
    * no terminal evidence             -> blocked (``DEPENDENCY_UNKNOWN``).
    """
    terminal = terminal_map.get(prereq_id)
    if terminal is None:
        return model.DEP_STATUS_BLOCKED_UNKNOWN, model.ERR_DEPENDENCY_UNKNOWN
    if terminal in TERMINAL_SATISFYING:
        return model.DEP_STATUS_SATISFIED, None
    if permit_failed_prereqs:
        return model.DEP_STATUS_SATISFIED, None
    return model.DEP_STATUS_BLOCKED_FAILED, model.ERR_DEPENDENCY_BLOCKED


def resolve_dependencies(
    job_id: str,
    depends_on: Sequence[str] | object,
    terminal_map: Mapping[str, str],
    permit_failed_prereqs: bool = False,
) -> DependencyStatus:
    """Deterministically resolve all prerequisites of one job (read only)."""
    deps = validate_depends_on(job_id, depends_on)
    if not deps:
        return DependencyStatus(job_id, True, None, None, permit_failed_prereqs)
    for dep in deps:
        status, reason = prerequisite_state(dep, terminal_map, permit_failed_prereqs)
        if reason is not None:
            return DependencyStatus(job_id, False, reason, dep, permit_failed_prereqs)
    return DependencyStatus(job_id, True, None, None, permit_failed_prereqs)


def candidate_eligible(
    candidate_spec: spec.JobSpec, terminal_map: Mapping[str, str]
) -> DependencyStatus:
    """V1.97/V2.01 composition helper: spec-level dependency eligibility."""
    return resolve_dependencies(
        candidate_spec.job_id,
        candidate_spec.depends_on,
        terminal_map,
        candidate_spec.permit_failed_prereqs,
    )
