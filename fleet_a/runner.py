"""Agent runtime: the loop each worker, reviewer and integrator runs, and the Fleet that runs them.

One ``AgentRunner.step()`` = acquire one lease -> heartbeat while working -> report exactly once.
If the heartbeat discovers the lease is gone, the runner kills the session and reports nothing:
the coordinator has already reclaimed the task, and any report would be rejected anyway.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .config import FleetConfig
from .coordinator import Coordinator
from .errors import FleetError, StaleLease
from .executors import ExecRequest, Executor
from .guard import RepoGuard
from .mergequeue import MergeQueue, run_verify
from .model import AgentInfo, Assignment, ReviewVerdict, Role, WorkResult, micros_to_usd
from .prompts import (
    REVIEW_SCHEMA,
    REVIEWER_SYSTEM,
    TRIAGE_SCHEMA,
    TRIAGE_SYSTEM,
    WORK_SCHEMA,
    WORKER_SYSTEM,
    commit_message,
    reviewer_prompt,
    triage_prompt,
    worker_prompt,
)
from .workspaces import Workspaces

log = logging.getLogger("fleet_a")

BLOCKING_SEVERITIES = frozenset({"blocker", "major"})


class Heartbeat:
    """Renews a lease every ``interval`` seconds; sets ``lost`` when renewal is refused."""

    def __init__(self, renew: Callable[[], Any], interval: float) -> None:
        self.lost = threading.Event()
        self._renew = renew
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="fleet-heartbeat")

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._renew()
            except StaleLease:
                self.lost.set()
                return

    def __enter__(self) -> "Heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=self._interval + 5)


def clip_feedback(items: Sequence[str], limit: int) -> tuple[str, ...]:
    """Keep the most recent feedback that fits in ``limit`` characters."""
    kept: list[str] = []
    used = 0
    for item in reversed(items):
        if used + len(item) <= limit:
            kept.append(item)
            used += len(item)
        else:
            room = limit - used
            if room > 80:
                kept.append("..." + item[-(room - 3):])
            break
    return tuple(reversed(kept))


def parse_review(structured: dict[str, Any], cost_usd: float) -> ReviewVerdict:
    findings = structured.get("findings") or []
    lines: list[str] = []
    blocking = False
    for f in findings:
        severity = str(f.get("severity", "major"))
        blocking = blocking or severity in BLOCKING_SEVERITIES
        lines.append(f"[{severity}] {f.get('file', '?')}: {f.get('detail', '')}")
    approve = bool(structured.get("approve")) and not blocking
    return ReviewVerdict(
        approve=approve,
        summary=str(structured.get("summary", "")),
        findings=tuple(lines),
        cost_usd=cost_usd,
    )


class AgentRunner:
    def __init__(
        self,
        coordinator: Coordinator,
        agent: AgentInfo,
        config: FleetConfig,
        *,
        executor: Executor | None = None,
        triage_executor: Executor | None = None,
        workspaces: Workspaces | None = None,
        merge_queue: MergeQueue | None = None,
        guard: RepoGuard | None = None,
    ) -> None:
        if agent.role in (Role.WORKER, Role.REVIEWER) and (executor is None or workspaces is None):
            raise FleetError(f"{agent.role.value} runner needs an executor and workspaces")
        if agent.role is Role.INTEGRATOR and merge_queue is None:
            raise FleetError("integrator runner needs a merge queue")
        if agent.role is Role.PLANNER:
            raise FleetError("the planner is not a long-running agent")
        self.coord = coordinator
        self.agent = agent
        self.config = config
        self.executor = executor
        self.triage_executor = triage_executor
        self.workspaces = workspaces
        self.merge_queue = merge_queue
        self.guard = guard

    def step(self) -> bool:
        """Do one phase of one task. Returns False if there was nothing to do."""
        assignment = self.coord.acquire(self.agent)
        if assignment is None:
            return False
        task_id, token = assignment.task.id, assignment.token
        interval = max(assignment.ttl_s / 3.0, 0.05)
        log.info("%s leased %s (%s, token %d)", self.agent.id, task_id, assignment.role.value, token)
        with Heartbeat(lambda: self.coord.heartbeat(task_id, token), interval) as hb:
            try:
                if assignment.role is Role.WORKER:
                    self._work(assignment, hb.lost)
                elif assignment.role is Role.REVIEWER:
                    self._review(assignment, hb.lost)
                else:
                    self._integrate(assignment, hb.lost)
            except StaleLease as exc:
                log.warning("%s lost lease %d on %s: %s", self.agent.id, token, task_id, exc)
        return True

    # ----------------------------------------------------------------------------- phases

    def _work(self, a: Assignment, lost: threading.Event) -> None:
        assert self.executor is not None and self.workspaces is not None
        cost = 0.0
        worktree = None
        try:
            worktree = self.workspaces.create_worktree(a.run_id, a.task.id, a.attempt, a.token, a.target_branch)
            feedback, triage_cost = self._condense(a, lost)
            cost += triage_cost
            remaining = round(a.budget_usd - triage_cost, 4)
            if remaining <= 0:
                self.coord.fail(a.task.id, a.token, "budget spent on feedback triage", cost_usd=cost)
                return
            result = self.executor.run(
                ExecRequest(
                    cwd=worktree.path,
                    prompt=worker_prompt(a.task, worktree.base, feedback),
                    system_append=WORKER_SYSTEM,
                    profile=self.config.models["worker"],
                    budget_usd=remaining,
                    json_schema=WORK_SCHEMA,
                ),
                lost,
            )
            cost += result.cost_usd
            if self.guard is not None:
                problem = self.guard.check()
                if problem is not None:
                    # Possibly this session, possibly another: either way nothing more runs on it.
                    self.coord.fail(a.task.id, a.token, problem, cost_usd=cost, escalate=True)
                    return
            if lost.is_set():
                return
            if not result.ok:
                self.coord.fail(a.task.id, a.token, f"worker session failed: {result.error}", cost_usd=cost, retryable=result.retryable)
                return
            out = result.structured or {}
            summary = str(out.get("summary", "")).strip()
            if out.get("status") == "blocked":
                self.coord.fail(a.task.id, a.token, f"worker blocked: {out.get('blocker', '')}", cost_usd=cost, escalate=True)
                return
            commit = self.workspaces.squash_commit(worktree, commit_message(a, summary))
            if commit is None:
                self.coord.fail(a.task.id, a.token, "attempt produced no changes", cost_usd=cost)
                return
            if self.config.verify_before_review:
                passed, output = run_verify(
                    self.config.verify_command, self.config.verify_timeout_s, worktree.path, cancel=lost
                )
                if lost.is_set():
                    return
                if not passed:
                    tip = self.workspaces.target_tip(a.target_branch)
                    if tip == worktree.base:
                        # Rework with the test output; nothing newer on the target could fix it.
                        self.coord.fail(a.task.id, a.token, f"verify_failed before review: {output}", cost_usd=cost)
                        return
                    # The target moved since this attempt started, and the change might pass on
                    # top of it: don't spend an attempt. Review it; the merge queue verifies the
                    # rebased tree before anything lands.
                    log.info("%s: %s failed verify on a stale base; sending it to review", self.agent.id, a.task.id)
            self.coord.submit_work(
                a.task.id,
                a.token,
                WorkResult(
                    branch=worktree.branch or "",
                    commit=commit,
                    base_commit=worktree.base,
                    changed_files=tuple(self.workspaces.changed_files(worktree.base, commit)),
                    cost_usd=cost,
                    summary=summary,
                ),
            )
        except StaleLease:
            raise
        except Exception as exc:  # report our own runtime errors as a retryable failure
            log.exception("%s: worker error on %s", self.agent.id, a.task.id)
            self.coord.fail(a.task.id, a.token, f"worker runtime error: {exc!r}", cost_usd=cost)
        finally:
            if worktree is not None:
                self.workspaces.remove(worktree)

    def _review(self, a: Assignment, lost: threading.Event) -> None:
        assert self.executor is not None and self.workspaces is not None
        if not a.commit or not a.base_commit:
            raise FleetError(f"review assignment for {a.task.id} has no commit")
        cost = 0.0
        worktree = None
        try:
            worktree = self.workspaces.checkout_detached(
                a.commit, f"{a.run_id}/review-{a.task.id}-a{a.attempt}-t{a.token}", a.base_commit
            )
            result = self.executor.run(
                ExecRequest(
                    cwd=worktree.path,
                    prompt=reviewer_prompt(a),
                    system_append=REVIEWER_SYSTEM,
                    profile=self.config.models["reviewer"],
                    budget_usd=a.budget_usd,
                    json_schema=REVIEW_SCHEMA,
                ),
                lost,
            )
            cost = result.cost_usd
            if lost.is_set():
                return
            if not result.ok or result.structured is None:
                self.coord.fail(
                    a.task.id,
                    a.token,
                    f"review session failed: {result.error}",
                    cost_usd=cost,
                    retryable=result.retryable,
                    budget_exhausted=result.budget_exhausted,
                )
                return
            self.coord.submit_review(a.task.id, a.token, parse_review(result.structured, cost))
        except StaleLease:
            raise
        except Exception as exc:
            log.exception("%s: reviewer error on %s", self.agent.id, a.task.id)
            self.coord.fail(a.task.id, a.token, f"reviewer runtime error: {exc!r}", cost_usd=cost)
        finally:
            if worktree is not None:
                self.workspaces.remove(worktree)

    def _integrate(self, a: Assignment, lost: threading.Event | None = None) -> None:
        assert self.merge_queue is not None
        if not a.commit:
            raise FleetError(f"merge assignment for {a.task.id} has no commit")
        try:
            outcome = self.merge_queue.integrate(
                a.run_id, a.task.id, a.attempt, a.token, a.commit, a.task.write_scope, cancel=lost
            )
        except Exception as exc:
            log.exception("%s: integrator error on %s", self.agent.id, a.task.id)
            self.coord.fail(a.task.id, a.token, f"integrator runtime error: {exc!r}")
            return
        if lost is not None and lost.is_set():
            # The coordinator already reclaimed the task; whatever happened here is reconciled by
            # the next integrator through the fence and find_landed, so report nothing.
            return
        log.info("%s: %s -> %s %s", self.agent.id, a.task.id, outcome.status.value, outcome.detail[:200])
        self.coord.submit_merge(a.task.id, a.token, outcome)

    def _condense(self, a: Assignment, lost: threading.Event) -> tuple[tuple[str, ...], float]:
        """Fit accumulated feedback into the prompt; use the triage model when it is too long."""
        limit = self.config.triage_threshold_chars
        if sum(len(f) for f in a.feedback) <= limit:
            return a.feedback, 0.0
        if self.triage_executor is None or self.workspaces is None:
            return clip_feedback(a.feedback, limit), 0.0
        budget = min(micros_to_usd(self.config.triage_budget), a.budget_usd / 10.0)
        result = self.triage_executor.run(
            ExecRequest(
                cwd=self.workspaces.root,
                prompt=triage_prompt(a.task, "\n\n".join(a.feedback)),
                system_append=TRIAGE_SYSTEM,
                profile=self.config.models["triage"],
                budget_usd=round(budget, 4),
                json_schema=TRIAGE_SCHEMA,
            ),
            lost,
        )
        points = (result.structured or {}).get("points") if result.ok else None
        if isinstance(points, list) and points and all(isinstance(p, str) for p in points):
            return clip_feedback(points, limit), result.cost_usd
        return clip_feedback(a.feedback, limit), result.cost_usd


@dataclass
class FleetReport:
    finished: bool
    stall_reason: str | None
    timed_out: bool
    summary: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


class Fleet:
    """Runs every agent in its own thread until the plan finishes, stalls, or times out."""

    def __init__(self, coordinator: Coordinator, runners: Sequence[AgentRunner], poll_interval_s: float = 2.0) -> None:
        roles = {r.agent.role for r in runners}
        missing = {Role.WORKER, Role.REVIEWER, Role.INTEGRATOR} - roles
        if missing:
            raise FleetError(f"a fleet needs at least one agent per role; missing {sorted(r.value for r in missing)}")
        self.coord = coordinator
        self.runners = list(runners)
        self.poll_interval_s = poll_interval_s
        self._stop = threading.Event()
        self._errors: list[str] = []
        self._errors_lock = threading.Lock()

    def _loop(self, runner: AgentRunner) -> None:
        agents = [r.agent for r in self.runners]
        while not self._stop.is_set():
            try:
                worked = runner.step()
            except Exception as exc:
                with self._errors_lock:
                    self._errors.append(f"{runner.agent.id}: {exc!r}")
                log.exception("%s: step failed", runner.agent.id)
                worked = False
            if worked:
                continue
            if self.coord.is_finished() or self.coord.stall_reason(agents) is not None:
                return
            self._stop.wait(self.poll_interval_s)

    def run(self, timeout_s: float | None = None) -> FleetReport:
        threads = [
            threading.Thread(target=self._loop, args=(r,), name=f"fleet-{r.agent.id}", daemon=True)
            for r in self.runners
        ]
        timer: threading.Timer | None = None
        if timeout_s is not None:
            # Runners finish their current phase (bounded by the executor timeout) before exiting.
            timer = threading.Timer(timeout_s, self._stop.set)
            timer.daemon = True
            timer.start()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if timer is not None:
            timer.cancel()
        timed_out = self._stop.is_set() and not self.coord.is_finished()
        agents = [r.agent for r in self.runners]
        return FleetReport(
            finished=self.coord.is_finished(),
            stall_reason=self.coord.stall_reason(agents),
            timed_out=timed_out,
            summary=self.coord.summary(),
            errors=list(self._errors),
        )

    def stop(self) -> None:
        self._stop.set()
