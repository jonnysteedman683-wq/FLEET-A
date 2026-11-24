"""The coordinator: the single source of truth for task state, leases, write scopes and money.

Every public method is atomic (one re-entrant lock), first expires overdue leases, and appends
every state change to the event log before returning. Agents never change task state directly:
they present a fencing token, and a token that is not the task's current lease is rejected with
StaleLease without side effects.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

from . import scheduler
from .budget import Ledger, Reservation
from .config import FleetConfig
from .dag import TaskGraph
from .errors import BudgetExceeded, FleetError, StaleLease
from .events import EventLog
from .fsm import check_transition
from .leases import Lease, LeaseTable
from .model import (
    ACTIVE_STATE_FOR_ROLE,
    QUEUE_STATE_FOR_ROLE,
    ROLE_FOR_ACTIVE_STATE,
    ROLE_FOR_QUEUE_STATE,
    SCOPE_HELD_STATES,
    TERMINAL_STATES,
    AgentInfo,
    Assignment,
    MergeOutcome,
    MergeStatus,
    Plan,
    ReviewVerdict,
    Role,
    TaskSpec,
    TaskState,
    WorkResult,
    format_usd,
    micros_to_usd,
    usd_to_micros,
)
from .scopes import ScopeLocks, scope_violations

S = TaskState
FLEET_ACCOUNT = "fleet"


def run_account(plan_id: str) -> str:
    return f"run:{plan_id}"


def task_account(task_id: str) -> str:
    return f"task:{task_id}"


class Clock(Protocol):
    def now(self) -> float: ...


class SystemClock:
    """Monotonic seconds: immune to wall-clock jumps, which matters for lease expiry."""

    def now(self) -> float:
        return time.monotonic()


class ManualClock:
    """Deterministic clock for tests and simulation."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> float:
        if seconds < 0:
            raise ValueError("time does not go backwards")
        self._now += seconds
        return self._now


@dataclass
class TaskRecord:
    """Mutable runtime state of one task. Owned by the coordinator; read it under its lock."""

    spec: TaskSpec
    max_attempts: int
    state: TaskState = S.PENDING
    state_since: float = 0.0
    attempts: int = 0
    infra_failures: int = 0
    author: str | None = None
    reviewer: str | None = None
    branch: str | None = None
    commit: str | None = None
    base_commit: str | None = None
    changed_files: tuple[str, ...] = ()
    summary: str = ""
    feedback: list[str] = field(default_factory=list)
    reservation: Reservation | None = None
    resume_to: TaskState | None = None
    cancel_requested: bool = False
    merge_unresolved: bool = False
    merged_commit: str | None = None
    reason: str = ""


class Coordinator:
    def __init__(
        self,
        plan: Plan,
        config: FleetConfig,
        clock: Clock | None = None,
        events: EventLog | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self.plan = plan
        self.config = config
        self.clock: Clock = clock if clock is not None else SystemClock()
        self.events = events if events is not None else EventLog()
        self.graph = TaskGraph(plan.tasks)
        self.leases = LeaseTable()
        self.ledger = Ledger()
        self.scopes = ScopeLocks()
        self.tasks: dict[str, TaskRecord] = {}
        self._agent_task: dict[str, str] = {}
        self.run_account = run_account(plan.id)

        now = self.clock.now()
        self.ledger.open(FLEET_ACCOUNT, config.fleet_budget)
        self.ledger.open(self.run_account, config.run_budget, parent=FLEET_ACCOUNT)
        for task_id in self.graph.topo_order:
            spec = self.graph.spec(task_id)
            limit = (
                spec.budget
                if spec.budget is not None
                else config.default_task_budget(spec.attempt_budget, spec.max_attempts)
            )
            self.ledger.open(task_account(task_id), limit, parent=self.run_account)
            self.tasks[task_id] = TaskRecord(spec=spec, max_attempts=spec.max_attempts, state_since=now)
        self.events.append(
            "plan",
            None,
            {
                "plan": plan.id,
                "goal": plan.goal,
                "tasks": list(self.graph.topo_order),
                "fleet_budget_usd": micros_to_usd(config.fleet_budget),
                "run_budget_usd": micros_to_usd(config.run_budget),
                "target_branch": config.target_branch,
            },
            now,
        )
        for task_id in self.graph.topo_order:
            self._promote(self.tasks[task_id], now)

    # ======================================================================== agent-facing API

    def acquire(self, agent: AgentInfo) -> Assignment | None:
        """Grant ``agent`` the best eligible task for its role, or None."""
        with self._lock:
            now = self.clock.now()
            self._sweep(now)
            if agent.id in self._agent_task:
                raise FleetError(f"agent {agent.id} already holds the lease on {self._agent_task[agent.id]}")
            if agent.role not in QUEUE_STATE_FOR_ROLE:
                raise FleetError(f"role {agent.role.value} does not take leases")
            active = sum(1 for r in self.tasks.values() if r.state is ACTIVE_STATE_FOR_ROLE[agent.role])
            if active >= self.config.max_concurrent[agent.role]:
                return None
            queue = [r for r in self.tasks.values() if r.state is QUEUE_STATE_FOR_ROLE[agent.role]]
            ordered = scheduler.rank(agent.role, queue, now, self.config.aging_interval_s, self.graph)
            for rec in ordered:
                if rec.state is not QUEUE_STATE_FOR_ROLE[agent.role]:
                    continue  # moved by a cascade earlier in this loop
                if not self._eligible(rec, agent):
                    continue
                if agent.role is Role.WORKER and self.scopes.blockers(rec.spec.id, rec.spec.write_scope):
                    continue
                reservation = self._reserve(rec, agent.role, now)
                if reservation is None and agent.role is not Role.INTEGRATOR:
                    continue
                return self._grant(rec, agent, reservation, now)
            return None

    def heartbeat(self, task_id: str, token: int) -> float:
        """Extend a live lease. Returns the new expiry. Raises StaleLease if it is not live."""
        with self._lock:
            now = self.clock.now()
            self._sweep(now)
            lease = self.leases.check(task_id, token, now)
            renewed = self.leases.renew(task_id, token, now, self.config.ttl(lease.role))
            return renewed.expires_at

    def submit_work(self, task_id: str, token: int, result: WorkResult) -> None:
        with self._lock:
            now = self.clock.now()
            rec, lease = self._check(task_id, token, Role.WORKER, now)
            self._settle(rec, now, usd_to_micros(result.cost_usd))
            outside = scope_violations(rec.spec.write_scope, result.changed_files)
            if outside:
                self._rework(
                    rec,
                    now,
                    f"write-scope violation: changed {outside}; allowed {list(rec.spec.write_scope)}",
                )
                return
            if not result.changed_files:
                self._rework(rec, now, "attempt produced no changes")
                return
            rec.author = lease.agent_id
            rec.branch = result.branch
            rec.commit = result.commit
            rec.base_commit = result.base_commit
            rec.changed_files = tuple(sorted(result.changed_files))
            rec.summary = result.summary
            rec.infra_failures = 0
            self._transition(
                rec,
                S.AWAITING_REVIEW,
                now,
                "attempt submitted",
                commit=result.commit,
                files=len(rec.changed_files),
            )

    def submit_review(self, task_id: str, token: int, verdict: ReviewVerdict) -> None:
        with self._lock:
            now = self.clock.now()
            rec, lease = self._check(task_id, token, Role.REVIEWER, now)
            if lease.agent_id == rec.author:
                raise FleetError(f"agent {lease.agent_id} cannot review its own attempt")
            self._settle(rec, now, usd_to_micros(verdict.cost_usd))
            if verdict.approve:
                rec.reviewer = lease.agent_id
                rec.infra_failures = 0
                self._transition(rec, S.AWAITING_MERGE, now, "approved", reviewer=lease.agent_id)
            else:
                detail = "; ".join(verdict.findings)
                self._rework(rec, now, f"review rejected: {verdict.summary}" + (f" | {detail}" if detail else ""))

    def submit_merge(self, task_id: str, token: int, outcome: MergeOutcome) -> None:
        with self._lock:
            now = self.clock.now()
            rec, _ = self._check(task_id, token, Role.INTEGRATOR, now)
            rec.merge_unresolved = False
            status = outcome.status
            if status is MergeStatus.MERGED:
                rec.merged_commit = outcome.commit
                if rec.cancel_requested:
                    self.events.append("cancel_superseded", rec.spec.id, {"reason": "change already landed"}, now)
                    rec.cancel_requested = False
                self._transition(rec, S.DONE, now, outcome.detail or "merged", commit=outcome.commit)
            elif rec.cancel_requested:
                self._transition(rec, S.CANCELLED, now, f"cancel applied after merge outcome {status.value}")
            elif status in (MergeStatus.TARGET_MOVED, MergeStatus.FENCED):
                self._infra_retry(rec, now, S.AWAITING_MERGE, f"merge {status.value}: {outcome.detail}")
            elif status in (MergeStatus.CONFLICT, MergeStatus.VERIFY_FAILED):
                self._rework(rec, now, f"merge {status.value}: {outcome.detail}")
            elif status is MergeStatus.SCOPE_VIOLATION:
                rec.resume_to = S.READY
                self._transition(rec, S.BLOCKED, now, f"scope violation at merge: {outcome.detail}")
            else:
                raise FleetError(f"unhandled merge status {status}")

    def fail(
        self,
        task_id: str,
        token: int,
        reason: str,
        *,
        cost_usd: float = 0.0,
        retryable: bool = True,
        escalate: bool = False,
    ) -> None:
        """Report that the current phase could not complete."""
        with self._lock:
            now = self.clock.now()
            self._sweep(now)
            current = self.leases.get(task_id)
            role = current.role if current is not None and current.token == token else Role.WORKER
            rec, lease = self._check(task_id, token, role, now)
            self._settle(rec, now, usd_to_micros(cost_usd))
            back = QUEUE_STATE_FOR_ROLE[lease.role]
            if lease.role is Role.INTEGRATOR:
                rec.merge_unresolved = True  # the integrator cannot prove nothing landed
            if escalate:
                rec.resume_to = back
                self._transition(rec, S.BLOCKED, now, f"escalated: {reason}")
            elif lease.role is Role.WORKER:
                if retryable:
                    self._rework(rec, now, reason)
                else:
                    self._transition(rec, S.FAILED, now, f"non-retryable: {reason}")
            elif retryable:
                self._infra_retry(rec, now, back, reason)
            else:
                rec.resume_to = back
                self._transition(rec, S.BLOCKED, now, f"non-retryable {lease.role.value} failure: {reason}")

    # ======================================================================= operator-facing API

    def tick(self) -> None:
        """Expire overdue leases now (every other public call does this too)."""
        with self._lock:
            self._sweep(self.clock.now())

    def cancel(self, task_id: str, reason: str = "cancelled by operator") -> bool:
        """Cancel a task and its descendants. Returns False if it was already terminal.

        A task whose merge may have landed (MERGING, or a lost integrator lease) is not cancelled
        immediately: the cancel is deferred until the integrator reconciles with the target
        branch. If the change landed, the task ends DONE; otherwise CANCELLED.
        """
        with self._lock:
            now = self.clock.now()
            self._sweep(now)
            return self._cancel(self.tasks[task_id], now, reason)

    def block(self, task_id: str, reason: str = "blocked by operator") -> bool:
        """Park a task for a human decision. Returns False if it was already blocked."""
        with self._lock:
            now = self.clock.now()
            self._sweep(now)
            rec = self.tasks[task_id]
            if rec.state in TERMINAL_STATES:
                raise FleetError(f"task {task_id} is {rec.state.value}")
            if rec.state is S.BLOCKED:
                return False
            if rec.state is S.MERGING or rec.merge_unresolved:
                raise FleetError(f"task {task_id} has a merge in flight; cancel it or wait")
            resume_to = {
                S.PENDING: S.PENDING,
                S.READY: S.READY,
                S.RUNNING: S.READY,
                S.AWAITING_REVIEW: S.AWAITING_REVIEW,
                S.REVIEWING: S.AWAITING_REVIEW,
                S.AWAITING_MERGE: S.AWAITING_MERGE,
            }[rec.state]
            if rec.state in ROLE_FOR_ACTIVE_STATE:
                self._settle(rec, now, None)
            rec.resume_to = resume_to
            self._transition(rec, S.BLOCKED, now, reason)
            return True

    def resume(
        self,
        task_id: str,
        note: str = "",
        *,
        extra_attempts: int = 0,
        extra_budget_usd: float = 0.0,
    ) -> TaskState:
        """Return a BLOCKED task to its queue, optionally with more attempts and budget."""
        with self._lock:
            now = self.clock.now()
            self._sweep(now)
            rec = self.tasks[task_id]
            if rec.state is not S.BLOCKED:
                raise FleetError(f"task {task_id} is {rec.state.value}, not blocked")
            if extra_attempts < 0:
                raise FleetError("extra_attempts must be >= 0")
            extra = usd_to_micros(extra_budget_usd)
            target = rec.resume_to or S.READY
            if target in (S.PENDING, S.READY):
                if rec.attempts >= rec.max_attempts + extra_attempts:
                    raise FleetError(
                        f"task {task_id} used {rec.attempts}/{rec.max_attempts} attempts; pass extra_attempts"
                    )
                deps_done = all(self.tasks[d].state is S.DONE for d in self.graph.deps(task_id))
                target = S.READY if deps_done else S.PENDING
            if extra:
                self.ledger.raise_limit(task_account(task_id), extra)
            rec.max_attempts += extra_attempts
            if note:
                rec.feedback.append(f"operator: {note}")
            rec.infra_failures = 0
            rec.resume_to = None
            self._transition(
                rec,
                target,
                now,
                "resumed by operator",
                extra_attempts=extra_attempts,
                extra_budget_usd=micros_to_usd(extra),
            )
            return target

    # ============================================================================ introspection

    def record(self, task_id: str) -> TaskRecord:
        return self.tasks[task_id]

    def is_finished(self) -> bool:
        with self._lock:
            return all(r.state in TERMINAL_STATES for r in self.tasks.values())

    def lease_alive(self, task_id: str, token: int) -> bool:
        with self._lock:
            lease = self.leases.get(task_id)
            return lease is not None and lease.token == token and lease.alive(self.clock.now())

    def stall_reason(self, agents: Iterable[AgentInfo]) -> str | None:
        """Why no progress is possible, or None if some agent can still make progress.

        A stall is stable: with no live lease and nothing dispatchable, only an operator call
        can change any task's state.
        """
        with self._lock:
            now = self.clock.now()
            self._sweep(now)
            if self.is_finished() or self.leases.all():
                return None
            pool = list(agents)
            reasons: list[str] = []
            for rec in self.tasks.values():
                role = ROLE_FOR_QUEUE_STATE.get(rec.state)
                if role is None:
                    continue
                candidates = [a for a in pool if a.role is role and self._eligible(rec, a)]
                if not candidates:
                    reasons.append(f"{rec.spec.id}: no eligible {role.value}")
                    continue
                if role is Role.WORKER:
                    holders = self.scopes.blockers(rec.spec.id, rec.spec.write_scope)
                    if holders:
                        reasons.append(f"{rec.spec.id}: write scope held by {holders}")
                        continue
                if role is Role.INTEGRATOR:
                    return None
                amount = self._phase_amount(rec, role)
                if self.ledger.account(task_account(rec.spec.id)).available < amount:
                    return None  # acquire() would block the task: that is progress
                if self.ledger.available(self.run_account) < amount:
                    reasons.append(f"{rec.spec.id}: run/fleet budget cannot fund {format_usd(amount)}")
                    continue
                return None
            blocked = sorted(r.spec.id for r in self.tasks.values() if r.state is S.BLOCKED)
            if blocked:
                reasons.append(f"awaiting operator: {', '.join(blocked)}")
            return "; ".join(reasons) or "no dispatchable work"

    def summary(self) -> dict[str, Any]:
        with self._lock:
            counts: dict[str, int] = {}
            for rec in self.tasks.values():
                counts[rec.state.value] = counts.get(rec.state.value, 0) + 1
            run = self.ledger.account(self.run_account)
            return {
                "plan": self.plan.id,
                "finished": self.is_finished(),
                "states": counts,
                "spent_usd": micros_to_usd(run.spent),
                "reserved_usd": micros_to_usd(run.reserved),
                "run_budget_usd": micros_to_usd(run.limit),
                "events": len(self.events),
                "tasks": {
                    tid: {
                        "state": rec.state.value,
                        "attempts": rec.attempts,
                        "reason": rec.reason,
                        "merged_commit": rec.merged_commit,
                        "spent_usd": micros_to_usd(self.ledger.account(task_account(tid)).spent),
                    }
                    for tid, rec in self.tasks.items()
                },
            }

    # ================================================================================ internals

    def _eligible(self, rec: TaskRecord, agent: AgentInfo) -> bool:
        if agent.role is Role.INTEGRATOR:
            return True
        if not rec.spec.capabilities <= agent.capabilities:
            return False
        if agent.role is Role.REVIEWER and agent.id == rec.author:
            return False  # separation of duties
        return True

    def _phase_amount(self, rec: TaskRecord, role: Role) -> int:
        if role is Role.WORKER:
            return rec.spec.attempt_budget
        if role is Role.REVIEWER:
            return self.config.review_budget
        return 0

    def _reserve(self, rec: TaskRecord, role: Role, now: float) -> Reservation | None:
        """Reserve the phase budget. Integrator phases are deterministic and cost nothing."""
        if role is Role.INTEGRATOR:
            return None
        if role is Role.WORKER and rec.attempts >= rec.max_attempts:
            self._transition(rec, S.FAILED, now, f"attempts exhausted ({rec.attempts}/{rec.max_attempts})")
            return None
        amount = self._phase_amount(rec, role)
        try:
            return self.ledger.reserve(task_account(rec.spec.id), amount)
        except BudgetExceeded as exc:
            if exc.account == task_account(rec.spec.id):
                rec.resume_to = rec.state
                self._transition(
                    rec,
                    S.BLOCKED,
                    now,
                    f"task budget exhausted: needs {format_usd(amount)}, has {format_usd(exc.available)}",
                )
            return None

    def _grant(self, rec: TaskRecord, agent: AgentInfo, reservation: Reservation | None, now: float) -> Assignment:
        role = agent.role
        if role is Role.WORKER:
            if not self.scopes.try_acquire(rec.spec.id, rec.spec.write_scope):
                raise FleetError("internal: scope became unavailable under the lock")
            rec.attempts += 1
        lease = self.leases.grant(rec.spec.id, agent.id, role, now, self.config.ttl(role))
        rec.reservation = reservation
        self._agent_task[agent.id] = rec.spec.id
        amount = reservation.amount if reservation else 0
        self._transition(
            rec,
            ACTIVE_STATE_FOR_ROLE[role],
            now,
            f"leased to {agent.id}",
            agent=agent.id,
            token=lease.token,
            expires_at=lease.expires_at,
            reserved_usd=micros_to_usd(amount),
        )
        return Assignment(
            task=rec.spec,
            role=role,
            agent_id=agent.id,
            token=lease.token,
            attempt=rec.attempts,
            ttl_s=self.config.ttl(role),
            budget_usd=micros_to_usd(amount),
            target_branch=self.config.target_branch,
            feedback=tuple(rec.feedback),
            branch=rec.branch if role is not Role.WORKER else None,
            commit=rec.commit if role is not Role.WORKER else None,
            base_commit=rec.base_commit if role is not Role.WORKER else None,
            summary=rec.summary if role is not Role.WORKER else "",
        )

    def _check(self, task_id: str, token: int, role: Role, now: float) -> tuple[TaskRecord, Lease]:
        self._sweep(now)
        try:
            lease = self.leases.check(task_id, token, now)
        except StaleLease:
            self.events.append("stale_rejected", task_id, {"token": token, "role": role.value}, now)
            raise
        if lease.role is not role:
            raise FleetError(f"lease {token} is a {lease.role.value} lease, not {role.value}")
        rec = self.tasks[task_id]
        if rec.state is not ACTIVE_STATE_FOR_ROLE[role]:
            raise FleetError(f"internal: task {task_id} is {rec.state.value} under a live {role.value} lease")
        return rec, lease

    def _sweep(self, now: float) -> None:
        for lease in self.leases.expired(now):
            rec = self.tasks[lease.task_id]
            self._settle(rec, now, None)
            self.events.append(
                "lease_expired",
                lease.task_id,
                {"token": lease.token, "agent": lease.agent_id, "role": lease.role.value},
                now,
            )
            if lease.role is Role.WORKER:
                self._rework(rec, now, f"lease expired: {lease.agent_id} stopped heartbeating")
            elif lease.role is Role.REVIEWER:
                self._infra_retry(rec, now, S.AWAITING_REVIEW, f"reviewer {lease.agent_id} lease expired")
            else:
                rec.merge_unresolved = True
                self._infra_retry(rec, now, S.AWAITING_MERGE, f"integrator {lease.agent_id} lease expired")

    def _settle(self, rec: TaskRecord, now: float, actual: int | None) -> None:
        """Commit the open reservation. ``actual=None`` charges the full reservation (unknown cost)."""
        reservation = rec.reservation
        if reservation is None:
            return
        rec.reservation = None
        charged = reservation.amount if actual is None else actual
        overrun = self.ledger.commit(reservation, charged)
        self.events.append(
            "spend",
            rec.spec.id,
            {
                "reserved_usd": micros_to_usd(reservation.amount),
                "charged_usd": micros_to_usd(charged),
                "overrun_usd": micros_to_usd(overrun),
                "estimated": actual is None,
            },
            now,
        )

    def _rework(self, rec: TaskRecord, now: float, reason: str) -> None:
        rec.feedback.append(reason)
        if rec.attempts >= rec.max_attempts:
            self._transition(rec, S.FAILED, now, f"attempts exhausted ({rec.attempts}/{rec.max_attempts}): {reason}")
        else:
            self._transition(rec, S.READY, now, f"rework: {reason}")

    def _infra_retry(self, rec: TaskRecord, now: float, back: TaskState, reason: str) -> None:
        rec.infra_failures += 1
        if rec.infra_failures > self.config.max_infra_retries:
            rec.resume_to = back
            self._transition(rec, S.BLOCKED, now, f"infrastructure retries exhausted: {reason}")
        else:
            self._transition(rec, back, now, f"retry: {reason}")

    def _cancel(self, rec: TaskRecord, now: float, reason: str) -> bool:
        if rec.state in TERMINAL_STATES:
            return False
        if rec.state is S.MERGING or rec.merge_unresolved:
            if not rec.cancel_requested:
                rec.cancel_requested = True
                self.events.append("cancel_deferred", rec.spec.id, {"reason": reason}, now)
            if rec.state is S.BLOCKED:
                rec.resume_to = None
                self._transition(rec, S.AWAITING_MERGE, now, "reconcile merge before cancelling")
            return True
        if rec.state in ROLE_FOR_ACTIVE_STATE:
            self._settle(rec, now, None)
        self._transition(rec, S.CANCELLED, now, reason)
        return True

    def _promote(self, rec: TaskRecord, now: float) -> None:
        if rec.state is S.PENDING and all(self.tasks[d].state is S.DONE for d in self.graph.deps(rec.spec.id)):
            self._transition(rec, S.READY, now, "dependencies done")

    def _transition(self, rec: TaskRecord, dst: TaskState, now: float, reason: str, **extra: Any) -> None:
        src = rec.state
        check_transition(src, dst)
        task_id = rec.spec.id
        if src in ROLE_FOR_ACTIVE_STATE:
            if rec.reservation is not None:
                raise FleetError(f"internal: task {task_id} leaves {src.value} with an open reservation")
            lease = self.leases.drop(task_id)
            if lease is not None:
                self._agent_task.pop(lease.agent_id, None)
        rec.state = dst
        rec.state_since = now
        rec.reason = reason
        keep_scope = dst in SCOPE_HELD_STATES or (
            dst is S.BLOCKED and rec.resume_to in (S.AWAITING_REVIEW, S.AWAITING_MERGE)
        )
        if not keep_scope:
            self.scopes.release(task_id)
        data: dict[str, Any] = {"from": src.value, "to": dst.value, "reason": reason, "attempt": rec.attempts}
        data.update(extra)
        self.events.append("transition", task_id, data, now)
        if dst is S.DONE:
            for child in self.graph.dependents(task_id):
                self._promote(self.tasks[child], now)
        elif dst in (S.FAILED, S.CANCELLED):
            for descendant in self.graph.descendants(task_id):
                self._cancel(self.tasks[descendant], now, f"upstream task {task_id} {dst.value}")
