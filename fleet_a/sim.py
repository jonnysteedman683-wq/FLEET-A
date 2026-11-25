"""Fault-injecting discrete-event simulation of a whole fleet, with invariant checks.

The simulator drives the real Coordinator with a manual clock and simulated agents that crash
(stop heartbeating and never report), stall (stop heartbeating, then report long after their
lease expired), fail, escalate, overrun their budget, touch files outside their scope, produce
empty attempts, get rejected in review, hit merge conflicts and verification failures, and race
each other on the merge fence. A simulated operator cancels, blocks and resumes tasks at random.

After every step it checks the safety invariants listed in ``docs/ARCHITECTURE.md`` section 9;
at the end it checks the log, exactly-once landing, dependency order and termination.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from itertools import combinations
from typing import Any

from .config import AgentPool, FleetConfig
from .coordinator import Coordinator, ManualClock
from .errors import FleetError, StaleLease
from .events import EventLog, project_states
from .model import (
    ACTIVE_STATE_FOR_ROLE,
    ROLE_FOR_ACTIVE_STATE,
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
    micros_to_usd,
    usd_to_micros,
)
from .scopes import scopes_overlap

S = TaskState
CAPABILITIES = ("python", "typescript", "sql")
SCOPE_POOL = (
    "src/",
    "src/api/",
    "src/api/routes.py",
    "src/core/",
    "src/core/models.py",
    "src/ui/",
    "docs/",
    "tests/",
    "migrations/",
    "README.md",
)
AHEAD_OF_DEPS = frozenset(
    {S.READY, S.RUNNING, S.AWAITING_REVIEW, S.REVIEWING, S.AWAITING_MERGE, S.MERGING, S.DONE}
)


@dataclass(frozen=True)
class SimConfig:
    seed: int = 0
    tasks: int = 25
    workers: int = 5
    reviewers: int = 2
    integrators: int = 2
    max_steps: int = 20_000
    p_crash: float = 0.04
    p_stall: float = 0.04
    p_fail: float = 0.08
    p_escalate: float = 0.02
    p_scope_violation: float = 0.03
    p_empty: float = 0.02
    p_overrun: float = 0.02
    p_reject: float = 0.20
    p_review_error: float = 0.03
    p_conflict: float = 0.08
    p_verify_fail: float = 0.08
    p_integrator_error: float = 0.03
    p_cancel: float = 0.004
    p_block: float = 0.004
    run_budget_usd: str = "10000"
    attempt_budget_usd: str = "2.00"
    review_budget_usd: str = "0.50"


@dataclass
class SimReport:
    seed: int
    steps: int
    finished: bool
    stall_reason: str | None
    violations: list[str]
    states: dict[str, int]
    stale_rejected: int
    leases_lost: int
    merges: int
    spent_usd: float
    events: int

    @property
    def ok(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "ok": self.ok}


class SimTarget:
    """Model of the target branch with the merge-fence and compare-and-swap semantics of git."""

    def __init__(self) -> None:
        self.version = 0
        self.log: list[tuple[str, int, int]] = []  # (task, attempt, token)
        self.fence: dict[str, int] = {}

    def claim(self, task: str, token: int) -> bool:
        if self.fence.get(task, 0) > token:
            return False
        self.fence[task] = token
        return True

    def landed(self, task: str, attempt: int) -> bool:
        return any(t == task and a == attempt for t, a, _ in self.log)

    def land(self, task: str, attempt: int, token: int, base_version: int) -> str:
        if self.fence.get(task) != token:
            return "fenced"
        if self.version != base_version:
            return "moved"
        self.log.append((task, attempt, token))
        self.version += 1
        return "ok"


@dataclass
class SimAgent:
    info: AgentInfo
    assignment: Assignment | None = None
    mode: str = "normal"
    finish_at: float = 0.0
    idle_until: float = 0.0
    fence_ok: bool = True
    already_landed: bool = False
    base_version: int = 0


def random_plan(rng: random.Random, cfg: SimConfig) -> Plan:
    tasks = []
    ids: list[str] = []
    for i in range(cfg.tasks):
        tid = f"T{i:03d}"
        k = min(len(ids), rng.choice((0, 0, 1, 1, 2, 3)))
        tasks.append(
            TaskSpec(
                id=tid,
                title=f"simulated task {i}",
                description="simulated",
                write_scope=tuple(rng.sample(SCOPE_POOL, rng.choice((1, 1, 2)))),
                deps=tuple(sorted(rng.sample(ids, k))),
                capabilities=frozenset(rng.choice(((), ("python",), ("typescript",), ("python", "sql")))),
                priority=rng.randint(0, 3),
                max_attempts=rng.choice((2, 3, 4)),
                attempt_budget=usd_to_micros(cfg.attempt_budget_usd),
            )
        )
        ids.append(tid)
    return Plan(id=f"sim-{cfg.seed}", goal="simulated goal", tasks=tuple(tasks))


class Simulator:
    def __init__(self, cfg: SimConfig) -> None:
        self.cfg = cfg
        self.rng = random.Random(cfg.seed)
        self.clock = ManualClock()
        self.plan = random_plan(self.rng, cfg)
        run_budget = usd_to_micros(cfg.run_budget_usd)
        self.config = FleetConfig(
            fleet_budget=run_budget,
            run_budget=run_budget,
            review_budget=usd_to_micros(cfg.review_budget_usd),
            lease_ttl_s={Role.WORKER: 300.0, Role.REVIEWER: 200.0, Role.INTEGRATOR: 200.0},
            max_concurrent={Role.WORKER: max(1, cfg.workers - 1), Role.REVIEWER: cfg.reviewers, Role.INTEGRATOR: 1},
            max_infra_retries=3,
            aging_interval_s=120.0,
            agents=(
                AgentPool(Role.WORKER, cfg.workers, frozenset(CAPABILITIES)),
                AgentPool(Role.REVIEWER, cfg.reviewers, frozenset(CAPABILITIES)),
                AgentPool(Role.INTEGRATOR, cfg.integrators),
            ),
        )
        self.coord = Coordinator(self.plan, self.config, self.clock, EventLog(), run_id=f"sim-{cfg.seed}")
        self.target = SimTarget()
        self.agents: list[SimAgent] = []
        for pool in self.config.agents:
            for n, agent_id in enumerate(pool.agent_ids()):
                caps = pool.capabilities
                if pool.role is Role.WORKER and n > 0:
                    caps = frozenset(c for c in CAPABILITIES if self.rng.random() < 0.6)
                self.agents.append(SimAgent(AgentInfo(agent_id, pool.role, caps)))
        self.violations: list[str] = []
        self.max_token = 0
        self.stale_rejected = 0
        self.leases_lost = 0

    # --------------------------------------------------------------------------------- run

    def run(self) -> SimReport:
        infos = [a.info for a in self.agents]
        step = 0
        for step in range(1, self.cfg.max_steps + 1):
            self.clock.advance(self.rng.uniform(1.0, 15.0))
            self.coord.tick()
            self._operator()
            order = list(self.agents)
            self.rng.shuffle(order)
            for agent in order:
                try:
                    self._act(agent)
                except FleetError as exc:  # StaleLease is handled inside _deliver
                    self._violate(f"coordinator raised {type(exc).__name__}: {exc}")
                    agent.assignment = None
            self._check_step()
            if self.violations or self.coord.is_finished():
                break
            if all(a.assignment is None for a in self.agents):
                reason = self.coord.stall_reason(infos)
                blocked = any(r.state is S.BLOCKED for r in self.coord.tasks.values())
                if reason is not None and not blocked:
                    break
        self._check_final(infos)
        states: dict[str, int] = {}
        for rec in self.coord.tasks.values():
            states[rec.state.value] = states.get(rec.state.value, 0) + 1
        return SimReport(
            seed=self.cfg.seed,
            steps=step,
            finished=self.coord.is_finished(),
            stall_reason=None if self.coord.is_finished() else self.coord.stall_reason(infos),
            violations=self.violations,
            states=states,
            stale_rejected=self.stale_rejected,
            leases_lost=self.leases_lost,
            merges=len(self.target.log),
            spent_usd=micros_to_usd(self.coord.ledger.account(self.coord.run_account).spent),
            events=len(self.coord.events),
        )

    # ---------------------------------------------------------------------------- actors

    def _operator(self) -> None:
        rng, coord = self.rng, self.coord
        open_tasks = sorted(t for t, r in coord.tasks.items() if r.state not in TERMINAL_STATES)
        if open_tasks and rng.random() < self.cfg.p_cancel:
            coord.cancel(rng.choice(open_tasks), "sim operator cancel")
        if open_tasks and rng.random() < self.cfg.p_block:
            try:
                coord.block(rng.choice(open_tasks), "sim operator block")
            except FleetError:
                pass
        now = self.clock.now()
        for task_id, rec in sorted(coord.tasks.items()):
            if rec.state is S.BLOCKED and now - rec.state_since > 60.0:
                if rng.random() < 0.6:
                    try:
                        capped = "review_budget_usd" in rec.reason
                        coord.resume(
                            task_id,
                            "sim operator resume",
                            extra_attempts=1,
                            extra_budget_usd=2 * micros_to_usd(rec.spec.attempt_budget),
                            review_budget_usd=2 * micros_to_usd(rec.review_budget or coord.config.review_budget)
                            if capped
                            else None,
                        )
                    except FleetError:
                        coord.cancel(task_id, "sim operator gave up")
                else:
                    coord.cancel(task_id, "sim operator cancel blocked")

    def _act(self, agent: SimAgent) -> None:
        now = self.clock.now()
        a = agent.assignment
        if a is None:
            if now < agent.idle_until:
                return
            granted = self.coord.acquire(agent.info)
            if granted is not None:
                self._on_grant(agent, granted)
            return
        if agent.mode == "crash":
            if not self.coord.lease_alive(a.task.id, a.token):
                agent.assignment = None
                agent.idle_until = now + self.rng.uniform(0.0, 30.0)
            return
        if agent.mode == "normal":
            try:
                self.coord.heartbeat(a.task.id, a.token)
            except StaleLease:
                self.leases_lost += 1
                agent.assignment = None
                return
        if now >= agent.finish_at:
            self._deliver(agent, a)

    def _on_grant(self, agent: SimAgent, a: Assignment) -> None:
        if a.token <= self.max_token:
            self._violate(f"token {a.token} not greater than previous {self.max_token}")
        self.max_token = a.token
        rec = self.coord.tasks[a.task.id]
        if a.role is Role.REVIEWER and rec.author == agent.info.id:
            self._violate(f"{agent.info.id} assigned to review its own attempt of {a.task.id}")
        r = self.rng.random()
        if r < self.cfg.p_crash:
            agent.mode = "crash"
        elif r < self.cfg.p_crash + self.cfg.p_stall:
            agent.mode = "stall"
        else:
            agent.mode = "normal"
        now = self.clock.now()
        if agent.mode == "stall":
            agent.finish_at = now + a.ttl_s * self.rng.uniform(1.2, 2.0)
        else:
            agent.finish_at = now + self.rng.uniform(5.0, a.ttl_s * 0.6)
        if a.role is Role.INTEGRATOR:
            agent.fence_ok = self.target.claim(a.task.id, a.token)
            agent.already_landed = self.target.landed(a.task.id, a.attempt)
            agent.base_version = self.target.version
        agent.assignment = a

    def _deliver(self, agent: SimAgent, a: Assignment) -> None:
        coord = self.coord
        coord.tick()
        rec = coord.tasks[a.task.id]
        live = coord.lease_alive(a.task.id, a.token)
        before = (rec.state, rec.attempts, rec.commit)
        n_events = len(coord.events)
        try:
            self._submit(agent, a)
            if not live:
                self._violate(f"stale token {a.token} accepted on {a.task.id}")
        except StaleLease:
            self.stale_rejected += 1
            if live:
                self._violate(f"live token {a.token} rejected on {a.task.id}")
            if (rec.state, rec.attempts, rec.commit) != before:
                self._violate(f"rejected stale report changed {a.task.id}")
            extra = [e.kind for e in coord.events.events[n_events:]]
            if extra != ["stale_rejected"]:
                self._violate(f"rejected stale report logged {extra}")
        finally:
            agent.assignment = None

    def _submit(self, agent: SimAgent, a: Assignment) -> None:
        rng, cfg, coord = self.rng, self.cfg, self.coord
        task_id, token = a.task.id, a.token
        cost = rng.uniform(0.05, 1.0) * a.budget_usd
        if rng.random() < cfg.p_overrun:
            cost = a.budget_usd * 1.25
        if a.role is Role.WORKER:
            r = rng.random()
            if r < cfg.p_fail:
                coord.fail(task_id, token, "sim worker failure", cost_usd=cost, retryable=rng.random() < 0.85)
            elif r < cfg.p_fail + cfg.p_escalate:
                coord.fail(task_id, token, "sim worker needs a human", cost_usd=cost, escalate=True)
            else:
                coord.submit_work(
                    task_id,
                    token,
                    WorkResult(
                        branch=f"fleet/{coord.run_id}/{task_id}/a{a.attempt}-t{token}",
                        commit=f"c{token}",
                        base_commit=f"v{self.target.version}",
                        changed_files=self._files(a.task),
                        cost_usd=cost,
                        summary="sim",
                    ),
                )
        elif a.role is Role.REVIEWER:
            if rng.random() < cfg.p_review_error:
                coord.fail(task_id, token, "sim reviewer error", cost_usd=cost, budget_exhausted=rng.random() < 0.5)
            else:
                coord.submit_review(task_id, token, ReviewVerdict(rng.random() >= cfg.p_reject, "sim", (), cost))
        else:
            outcome = self._merge(agent, a)
            if outcome is None:
                coord.fail(task_id, token, "sim integrator error")
            else:
                coord.submit_merge(task_id, token, outcome)

    def _merge(self, agent: SimAgent, a: Assignment) -> MergeOutcome | None:
        if not agent.fence_ok:
            return MergeOutcome(MergeStatus.FENCED)
        if agent.already_landed:
            return MergeOutcome(MergeStatus.MERGED, f"landed-{a.task.id}", "reconciled")
        r = self.rng.random()
        cfg = self.cfg
        if r < cfg.p_integrator_error:
            return None
        if r < cfg.p_integrator_error + cfg.p_conflict:
            return MergeOutcome(MergeStatus.CONFLICT, detail="sim conflict")
        if r < cfg.p_integrator_error + cfg.p_conflict + cfg.p_verify_fail:
            return MergeOutcome(MergeStatus.VERIFY_FAILED, detail="sim verify failure")
        result = self.target.land(a.task.id, a.attempt, a.token, agent.base_version)
        if result == "ok":
            return MergeOutcome(MergeStatus.MERGED, f"m{a.token}", "landed")
        if result == "moved":
            return MergeOutcome(MergeStatus.TARGET_MOVED)
        return MergeOutcome(MergeStatus.FENCED)

    def _files(self, task: TaskSpec) -> tuple[str, ...]:
        r = self.rng.random()
        if r < self.cfg.p_empty:
            return ()
        files = [e + f"f{self.rng.randint(0, 9)}.py" if e.endswith("/") else e for e in task.write_scope]
        if r < self.cfg.p_empty + self.cfg.p_scope_violation:
            files.append("outside/escape.py")
        return tuple(files)

    # ------------------------------------------------------------------------ invariants

    def _violate(self, message: str) -> None:
        self.violations.append(f"t={self.clock.now():.1f}: {message}")

    def _check_step(self) -> None:
        c = self.coord
        leases = c.leases.all()
        if len({l.agent_id for l in leases}) != len(leases):
            self._violate("an agent holds more than one lease")
        for lease in leases:
            rec = c.tasks[lease.task_id]
            if rec.state is not ACTIVE_STATE_FOR_ROLE[lease.role]:
                self._violate(f"{lease.role.value} lease on {lease.task_id} in state {rec.state.value}")
        held = c.scopes.held()
        for (t1, s1), (t2, s2) in combinations(sorted(held.items()), 2):
            if scopes_overlap(s1, s2):
                self._violate(f"overlapping write scopes held by {t1} and {t2}")
        merging = 0
        for task_id, rec in c.tasks.items():
            if rec.state in ROLE_FOR_ACTIVE_STATE and c.leases.get(task_id) is None:
                self._violate(f"{task_id} is {rec.state.value} without a lease")
            if (rec.reservation is not None) != (rec.state in (S.RUNNING, S.REVIEWING)):
                self._violate(f"{task_id} reservation/state mismatch in {rec.state.value}")
            if rec.state in SCOPE_HELD_STATES and task_id not in held:
                self._violate(f"{task_id} is {rec.state.value} without its write scope")
            if task_id in held and rec.state not in SCOPE_HELD_STATES and rec.state is not S.BLOCKED:
                self._violate(f"{task_id} holds its write scope in {rec.state.value}")
            if rec.state is S.MERGING:
                merging += 1
            if rec.state not in TERMINAL_STATES:
                dead = [d for d in c.graph.deps(task_id) if c.tasks[d].state in (S.FAILED, S.CANCELLED)]
                if dead:
                    self._violate(f"{task_id} is {rec.state.value} but dependencies {dead} can never land")
            if rec.state is not S.DONE:
                for d in c.graph.descendants(task_id):
                    if c.tasks[d].state in AHEAD_OF_DEPS:
                        self._violate(f"{d} is {c.tasks[d].state.value} but ancestor {task_id} is {rec.state.value}")
        if merging > 1:
            self._violate(f"{merging} tasks merging at once")
        for account in c.ledger.accounts():
            if account.spent + account.reserved > account.limit + account.overrun:
                self._violate(f"account {account.name} over limit without recorded overrun")
        landed = [t for t, _, _ in self.target.log]
        if len(landed) != len(set(landed)):
            self._violate("a task landed on the target more than once")

    def _check_final(self, infos: list[AgentInfo]) -> None:
        c = self.coord
        events = c.events.events
        try:
            EventLog.verify(events)
            projected = project_states(events)
        except FleetError as exc:
            self._violate(f"event log: {exc}")
            return
        live = {t: r.state.value for t, r in c.tasks.items()}
        if projected != live:
            diff = {t: (projected.get(t), s) for t, s in live.items() if projected.get(t) != s}
            self._violate(f"log projection differs from live state: {diff}")
        landed = [t for t, _, _ in self.target.log]
        for task_id, rec in c.tasks.items():
            n = landed.count(task_id)
            if rec.state is S.DONE and n != 1:
                self._violate(f"{task_id} is done but landed {n} times")
            if rec.state is not S.DONE and rec.state in TERMINAL_STATES and n != 0:
                self._violate(f"{task_id} is {rec.state.value} but its change landed")
        first_run: dict[str, int] = {}
        done_at: dict[str, int] = {}
        for e in events:
            if e.kind != "transition":
                continue
            if e.data["to"] == "running" and e.task not in first_run:
                first_run[e.task or ""] = e.seq
            if e.data["to"] == "done":
                done_at[e.task or ""] = e.seq
        for task_id, seq in first_run.items():
            for dep in c.graph.deps(task_id):
                if dep not in done_at or done_at[dep] > seq:
                    self._violate(f"{task_id} started before dependency {dep} was done")
        if c.is_finished():
            if c.leases.all() or c.ledger.open_reservations:
                self._violate("finished with live leases or open reservations")
        elif c.stall_reason(infos) is None:
            self._violate(f"did not terminate within {self.cfg.max_steps} steps")


def simulate(cfg: SimConfig) -> SimReport:
    return Simulator(cfg).run()


def simulate_many(base: SimConfig, seeds: range) -> list[SimReport]:
    return [simulate(replace(base, seed=s)) for s in seeds]
