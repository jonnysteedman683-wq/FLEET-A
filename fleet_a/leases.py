"""Time-bounded leases with fencing tokens.

At most one lease exists per task. Every grant takes the next value of a single counter, so
tokens are strictly increasing across the whole run. A holder proves ownership by presenting its
token; once a lease expires or is revoked, the old token is rejected forever (StaleLease), which
makes every late write from a crashed or partitioned agent harmless.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .errors import FleetError, StaleLease
from .model import Role


@dataclass(frozen=True)
class Lease:
    task_id: str
    token: int
    agent_id: str
    role: Role
    granted_at: float
    expires_at: float

    def alive(self, now: float) -> bool:
        return now < self.expires_at


class LeaseTable:
    def __init__(self) -> None:
        self._by_task: dict[str, Lease] = {}
        self._last_token = 0

    @property
    def last_token(self) -> int:
        return self._last_token

    def grant(self, task_id: str, agent_id: str, role: Role, now: float, ttl: float) -> Lease:
        if ttl <= 0:
            raise FleetError("lease ttl must be > 0")
        if task_id in self._by_task:
            raise FleetError(f"task {task_id} already has a lease")
        self._last_token += 1
        lease = Lease(task_id, self._last_token, agent_id, role, now, now + ttl)
        self._by_task[task_id] = lease
        return lease

    def get(self, task_id: str) -> Lease | None:
        return self._by_task.get(task_id)

    def check(self, task_id: str, token: int, now: float) -> Lease:
        lease = self._by_task.get(task_id)
        if lease is None or lease.token != token:
            raise StaleLease(f"token {token} is not the current lease on task {task_id}")
        if not lease.alive(now):
            raise StaleLease(f"lease {token} on task {task_id} expired at {lease.expires_at:.3f}")
        return lease

    def renew(self, task_id: str, token: int, now: float, ttl: float) -> Lease:
        lease = replace(self.check(task_id, token, now), expires_at=now + ttl)
        self._by_task[task_id] = lease
        return lease

    def drop(self, task_id: str) -> Lease | None:
        return self._by_task.pop(task_id, None)

    def expired(self, now: float) -> list[Lease]:
        return sorted((l for l in self._by_task.values() if not l.alive(now)), key=lambda l: l.token)

    def all(self) -> list[Lease]:
        return sorted(self._by_task.values(), key=lambda l: l.token)
