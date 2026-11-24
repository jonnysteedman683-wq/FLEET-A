"""Dispatch ordering. Pure functions: given candidate records, return them best-first.

* Workers: effective priority (priority + one point per ``aging_interval_s`` spent waiting, so
  low-priority work cannot starve), then critical-path depth (unblock the most downstream work
  first), then time waiting, then task id.
* Reviewers: effective priority, then time waiting, then task id.
* Integrator: strict FIFO by approval time, then task id.

Every key ends in the task id, so the order is total and deterministic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

from .dag import TaskGraph
from .model import Role

if TYPE_CHECKING:
    from .coordinator import TaskRecord


def age_bonus(waiting_s: float, aging_interval_s: float) -> int:
    if aging_interval_s <= 0:
        return 0
    return int(max(0.0, waiting_s) // aging_interval_s)


def rank(
    role: Role,
    records: Iterable["TaskRecord"],
    now: float,
    aging_interval_s: float,
    graph: TaskGraph,
) -> list["TaskRecord"]:
    def effective(rec: "TaskRecord") -> int:
        return rec.spec.priority + age_bonus(now - rec.state_since, aging_interval_s)

    if role is Role.INTEGRATOR:
        return sorted(records, key=lambda r: (r.state_since, r.spec.id))
    if role is Role.REVIEWER:
        return sorted(records, key=lambda r: (-effective(r), r.state_since, r.spec.id))
    if role is Role.WORKER:
        return sorted(
            records,
            key=lambda r: (-effective(r), -graph.depth(r.spec.id), r.state_since, r.spec.id),
        )
    raise ValueError(f"no dispatch order for role {role.value}")
