"""Task dependency graph: validation, topological order, descendants and critical-path depth."""

from __future__ import annotations

import heapq
from typing import Iterable

from .errors import PlanError
from .model import TaskSpec


class TaskGraph:
    """Immutable DAG over task ids. Edges point from a dependency to its dependents."""

    def __init__(self, specs: Iterable[TaskSpec]) -> None:
        self._specs: dict[str, TaskSpec] = {}
        for spec in specs:
            if spec.id in self._specs:
                raise PlanError(f"duplicate task id {spec.id!r}")
            self._specs[spec.id] = spec
        if not self._specs:
            raise PlanError("a plan needs at least one task")

        self._deps: dict[str, tuple[str, ...]] = {}
        dependents: dict[str, list[str]] = {tid: [] for tid in self._specs}
        for tid, spec in self._specs.items():
            if len(set(spec.deps)) != len(spec.deps):
                raise PlanError(f"task {tid}: duplicate dependencies")
            for dep in spec.deps:
                if dep == tid:
                    raise PlanError(f"task {tid}: depends on itself")
                if dep not in self._specs:
                    raise PlanError(f"task {tid}: unknown dependency {dep!r}")
                dependents[dep].append(tid)
            self._deps[tid] = tuple(sorted(spec.deps))
        self._dependents = {tid: tuple(sorted(ch)) for tid, ch in dependents.items()}

        self.topo_order: tuple[str, ...] = self._toposort()
        self._position = {tid: i for i, tid in enumerate(self.topo_order)}

        depth: dict[str, int] = {}
        for tid in reversed(self.topo_order):
            depth[tid] = 1 + max((depth[c] for c in self._dependents[tid]), default=0)
        self._depth = depth

    def _toposort(self) -> tuple[str, ...]:
        indegree = {tid: len(deps) for tid, deps in self._deps.items()}
        heap = [tid for tid, k in indegree.items() if k == 0]
        heapq.heapify(heap)
        order: list[str] = []
        while heap:
            tid = heapq.heappop(heap)
            order.append(tid)
            for child in self._dependents[tid]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    heapq.heappush(heap, child)
        if len(order) != len(self._specs):
            remaining = set(self._specs) - set(order)
            cycle = self._find_cycle(remaining)
            raise PlanError("dependency cycle: " + " -> ".join(cycle))
        return tuple(order)

    def _find_cycle(self, nodes: set[str]) -> list[str]:
        # Every node left over by Kahn's algorithm has a dependency inside the leftover set, so a
        # depth-first walk along dependency edges must revisit a node on its own path.
        color = dict.fromkeys(nodes, 0)
        for start in sorted(nodes):
            if color[start]:
                continue
            color[start] = 1
            path = [start]
            stack = [iter(d for d in self._deps[start] if d in nodes)]
            while stack:
                nxt = next(stack[-1], None)
                if nxt is None:
                    color[path.pop()] = 2
                    stack.pop()
                    continue
                if color[nxt] == 1:
                    return path[path.index(nxt) :] + [nxt]
                if color[nxt] == 0:
                    color[nxt] = 1
                    path.append(nxt)
                    stack.append(iter(d for d in self._deps[nxt] if d in nodes))
        raise PlanError("dependency cycle among: " + ", ".join(sorted(nodes)))

    # ----------------------------------------------------------------------------- queries

    def __contains__(self, task_id: object) -> bool:
        return task_id in self._specs

    def __len__(self) -> int:
        return len(self._specs)

    def spec(self, task_id: str) -> TaskSpec:
        return self._specs[task_id]

    def deps(self, task_id: str) -> tuple[str, ...]:
        return self._deps[task_id]

    def dependents(self, task_id: str) -> tuple[str, ...]:
        return self._dependents[task_id]

    def descendants(self, task_id: str) -> tuple[str, ...]:
        """All transitive dependents, in topological order."""
        seen: set[str] = set()
        frontier = list(self._dependents[task_id])
        while frontier:
            tid = frontier.pop()
            if tid not in seen:
                seen.add(tid)
                frontier.extend(self._dependents[tid])
        return tuple(sorted(seen, key=self._position.__getitem__))

    def ancestors(self, task_id: str) -> tuple[str, ...]:
        """All transitive dependencies, in topological order."""
        seen: set[str] = set()
        frontier = list(self._deps[task_id])
        while frontier:
            tid = frontier.pop()
            if tid not in seen:
                seen.add(tid)
                frontier.extend(self._deps[tid])
        return tuple(sorted(seen, key=self._position.__getitem__))

    def depth(self, task_id: str) -> int:
        """Number of tasks on the longest chain that starts at this task (itself included)."""
        return self._depth[task_id]

    def critical_path(self) -> tuple[str, ...]:
        """One longest dependency chain (ties broken by task id)."""
        start = min(self._specs, key=lambda t: (-self._depth[t], t))
        path = [start]
        while self._depth[path[-1]] > 1:
            want = self._depth[path[-1]] - 1
            path.append(min(c for c in self._dependents[path[-1]] if self._depth[c] == want))
        return tuple(path)
