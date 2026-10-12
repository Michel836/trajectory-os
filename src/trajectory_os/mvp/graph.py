"""MVP — deterministic dependency graph over tasks.

The graph has two explicit edge kinds, both supplied (never inferred):

* ``dependency`` — the target must be completed before the source may start;
* ``blocker`` — the source is hard-blocked until the target is resolved.

This module answers the cross-portfolio questions the cockpit needs:
topological order, downstream unlock impact ("what does this task unblock?")
and the transitive closure used for prioritisation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from trajectory_os.mvp import model


@dataclass(frozen=True)
class DependencyGraph:
    """Deterministic dependency/blocker graph over one portfolio."""

    successors: dict[str, tuple[str, ...]]
    predecessors: dict[str, tuple[str, ...]]

    def downstream(self, task_id: str) -> tuple[str, ...]:
        return self.successors.get(task_id, ())

    def upstream(self, task_id: str) -> tuple[str, ...]:
        return self.predecessors.get(task_id, ())

    def direct_unblocks(self, task_id: str) -> int:
        """Count of tasks directly unblocked by completing ``task_id``."""
        return len(self.downstream(task_id))

    def transitive_unblocks(self, task_id: str) -> int:
        """Count of distinct tasks transitively unblocked (excludes self)."""
        seen: set[str] = set()
        stack = list(self.downstream(task_id))
        while stack:
            node = stack.pop()
            if node in seen or node == task_id:
                continue
            seen.add(node)
            stack.extend(self.downstream(node))
        return len(seen)

    def topological_order(self, priority: dict[str, int] | None = None,
                          seed_order: Sequence[str] = ()) -> tuple[str, ...]:
        """Deterministic topological order (priority DESC, id ASC).

        ``priority`` is an optional task-id -> score map used to break ties;
        ``seed_order`` supplies the initial ready-set ordering before ties.
        """
        priority = priority or {}
        ranks = {task_id: (priority.get(task_id, 0), task_id)
                 for task_id in self.predecessors}
        ready = sorted(
            (task_id for task_id, deps in self.predecessors.items()
             if not deps),
            key=lambda task_id: (-ranks[task_id][0], ranks[task_id][1]),
        )
        if seed_order:
            ready = _order_by_seed(ready, seed_order)
        indegree = {task_id: len(deps)
                    for task_id, deps in self.predecessors.items()}
        order: list[str] = []
        while ready:
            node = ready.pop(0)
            order.append(node)
            for child in self.successors.get(node, ()):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
                    ready.sort(
                        key=lambda task_id: (-ranks[task_id][0],
                                             ranks[task_id][1]))
        return tuple(order)

    def to_dict(self) -> dict[str, Any]:
        return {
            "successors": {k: list(v) for k, v in sorted(
                self.successors.items())},
            "predecessors": {k: list(v) for k, v in sorted(
                self.predecessors.items())},
        }


def _order_by_seed(items: list[str],
                   seed: Sequence[str]) -> list[str]:
    rank = {task_id: index for index, task_id in enumerate(seed)}
    return sorted(items, key=lambda task_id: (rank.get(task_id, 1 << 30),
                                              task_id))


def build_graph(portfolio: model.Portfolio) -> DependencyGraph:
    """Build the dependency/blocker graph (the portfolio is already acyclic)."""
    successors: dict[str, list[str]] = {}
    predecessors: dict[str, list[str]] = {}
    for task in portfolio.tasks:
        successors.setdefault(task.task_id, [])
        predecessors.setdefault(task.task_id, [])
        for dep in task.dependencies:
            successors.setdefault(dep, []).append(task.task_id)
            predecessors[task.task_id].append(dep)
        for blocker in task.blocked_by:
            successors.setdefault(blocker, []).append(task.task_id)
            predecessors[task.task_id].append(blocker)
    return DependencyGraph(
        successors={k: tuple(sorted(set(v)))
                    for k, v in sorted(successors.items())},
        predecessors={k: tuple(sorted(set(v)))
                      for k, v in sorted(predecessors.items())},
    )


__all__ = ["DependencyGraph", "build_graph"]
