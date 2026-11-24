"""End-to-end: real coordinator, real git worktrees and merge queue, scripted model sessions."""

from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path

from conftest import config, git, plan_of, spec

from fleet_a.config import AgentPool
from fleet_a.coordinator import Coordinator, SystemClock
from fleet_a.events import EventLog, project_states
from fleet_a.executors import CallableExecutor, ExecRequest, ExecResult
from fleet_a.git import Git
from fleet_a.mergequeue import MergeQueue
from fleet_a.model import AgentInfo, Role, TaskState, usd_to_micros
from fleet_a.runner import AgentRunner, Fleet, clip_feedback, parse_review
from fleet_a.workspaces import Workspaces

COMPILE_SRC = [sys.executable, "-m", "compileall", "-q", "src"]


class ScriptedModel:
    """Plays every role. Workers write src/<task>.py; behaviour per task is scripted."""

    def __init__(self, broken_first: set[str] = frozenset(), reject_first: set[str] = frozenset(), blocked: set[str] = frozenset()):
        self.broken_first = set(broken_first)
        self.reject_first = set(reject_first)
        self.blocked = set(blocked)
        self.calls: list[tuple[str, str, int]] = []
        self.lock = threading.Lock()

    def __call__(self, req: ExecRequest) -> ExecResult:
        first_line = req.prompt.splitlines()[0]
        if first_line.startswith("REVIEW TASK"):
            m = re.match(r"REVIEW TASK (\S+) \(attempt (\d+)\)", first_line)
            task_id, attempt = m.group(1), int(m.group(2))
            with self.lock:
                self.calls.append(("review", task_id, attempt))
            approve = not (task_id in self.reject_first and attempt == 1)
            findings = [] if approve else [{"severity": "major", "file": f"src/{task_id}.py", "detail": "needs a docstring"}]
            return ExecResult(ok=True, cost_usd=0.05, structured={"approve": True, "summary": "reviewed", "findings": findings})
        task_id = re.match(r"TASK (\S+):", first_line).group(1)
        with self.lock:
            n = sum(1 for kind, t, _ in self.calls if kind == "work" and t == task_id) + 1
            self.calls.append(("work", task_id, n))
        if task_id in self.blocked:
            return ExecResult(ok=True, cost_usd=0.01, structured={"status": "blocked", "summary": "", "blocker": "needs a schema decision"})
        target = req.cwd / "src" / f"{task_id.lower()}.py"
        if task_id in self.broken_first and n == 1:
            target.write_text("def broken(:\n")
        else:
            doc = '"""docstring"""\n' if task_id in self.reject_first and "needs a docstring" in req.prompt else ""
            target.write_text(f"{doc}VALUE_{task_id} = {n}\n")
        return ExecResult(ok=True, cost_usd=0.2, structured={"status": "done", "summary": f"wrote {target.name}", "blocker": ""})


def build(repo: Path, tmp_path: Path, plan, model: ScriptedModel, **cfg):
    cfg.setdefault("verify_command", tuple(COMPILE_SRC))
    cfg.setdefault(
        "agents",
        (AgentPool(Role.WORKER, 3), AgentPool(Role.REVIEWER, 2), AgentPool(Role.INTEGRATOR, 1)),
    )
    cfg.setdefault("lease_ttl_s", {Role.WORKER: 30.0, Role.REVIEWER: 30.0, Role.INTEGRATOR: 30.0})
    conf = config(**cfg)
    g = Git(repo)
    ws = Workspaces(g, tmp_path / "worktrees")
    mq = MergeQueue(g, ws, conf.target_branch, conf.verify_command, 60)
    events = EventLog(tmp_path / "events.jsonl")
    coord = Coordinator(plan, conf, SystemClock(), events)
    executor = CallableExecutor(model)
    runners = []
    for pool in conf.agents:
        for agent_id in pool.agent_ids():
            runners.append(
                AgentRunner(
                    coord,
                    AgentInfo(agent_id, pool.role, pool.capabilities),
                    conf,
                    executor=executor if pool.role is not Role.INTEGRATOR else None,
                    workspaces=ws,
                    merge_queue=mq if pool.role is Role.INTEGRATOR else None,
                )
            )
    return coord, Fleet(coord, runners, poll_interval_s=0.02), g


def test_fleet_lands_a_dag_with_rework(repo, tmp_path):
    plan = plan_of(
        spec("A", scope=["src/a.py"]),
        spec("B", scope=["src/b.py"]),
        spec("C", scope=["src/c.py"], deps=["A", "B"]),
        spec("D", scope=["src/d.py"], deps=["C"]),
    )
    model = ScriptedModel(broken_first={"B"}, reject_first={"C"})
    coord, fleet, g = build(repo, tmp_path, plan, model)
    report = fleet.run(timeout_s=120)
    assert report.finished and not report.errors and not report.timed_out, report
    assert all(r.state is TaskState.DONE for r in coord.tasks.values()), coord.summary()

    files = set(git(repo, "ls-tree", "-r", "--name-only", "main").split())
    assert {"src/a.py", "src/b.py", "src/c.py", "src/d.py"} <= files
    assert git(repo, "show", "main:src/c.py").startswith('"""docstring"""')
    log = git(repo, "log", "--format=%s", "main").splitlines()
    assert len(log) == 5 and all(line.startswith("fleet(") for line in log[:4])
    assert log.index("fleet(C): task C") < log.index("fleet(A): task A")  # newest first

    assert coord.tasks["B"].attempts == 2 and "verify_failed" in coord.tasks["B"].feedback[0]
    assert coord.tasks["C"].attempts == 2 and "needs a docstring" in coord.tasks["C"].feedback[0]
    for rec in coord.tasks.values():
        assert rec.reviewer is not None and rec.reviewer != rec.author

    coord.events.close()
    events = EventLog.load(tmp_path / "events.jsonl")
    EventLog.verify(events)
    assert set(project_states(events).values()) == {"done"}
    assert git(repo, "worktree", "list").count("\n") == 0


def test_blocked_worker_escalates_and_dependents_wait(repo, tmp_path):
    plan = plan_of(spec("A", scope=["src/a.py"]), spec("B", scope=["src/b.py"], deps=["A"]), spec("C", scope=["src/c.py"]))
    coord, fleet, _ = build(repo, tmp_path, plan, ScriptedModel(blocked={"A"}))
    report = fleet.run(timeout_s=60)
    assert not report.finished
    assert coord.tasks["A"].state is TaskState.BLOCKED and "schema decision" in coord.tasks["A"].reason
    assert coord.tasks["B"].state is TaskState.PENDING and coord.tasks["C"].state is TaskState.DONE
    assert "awaiting operator: A" in report.stall_reason


def test_triage_condenses_long_feedback(repo, tmp_path):
    seen: dict[str, object] = {}

    def triage(req: ExecRequest) -> ExecResult:
        seen["profile"] = req.profile.model
        seen["budget"] = req.budget_usd
        return ExecResult(ok=True, cost_usd=0.01, structured={"points": ["fix the import in src/a.py"]})

    def worker(req: ExecRequest) -> ExecResult:
        seen["prompt"] = req.prompt
        seen["worker_budget"] = req.budget_usd
        (req.cwd / "src" / "a.py").write_text("A = 1\n")
        return ExecResult(ok=True, cost_usd=0.1, structured={"status": "done", "summary": "ok", "blocker": ""})

    plan = plan_of(spec("A", scope=["src/a.py"]))
    conf = config(triage_threshold_chars=200)
    coord = Coordinator(plan, conf, SystemClock(), EventLog())
    first = coord.acquire(AgentInfo("w1", Role.WORKER))
    coord.fail("A", first.token, "pytest output: " + "E" * 5000)
    ws = Workspaces(Git(repo), tmp_path / "wt")
    runner = AgentRunner(
        coord, AgentInfo("w2", Role.WORKER), conf,
        executor=CallableExecutor(worker), triage_executor=CallableExecutor(triage), workspaces=ws,
    )
    assert runner.step()
    assert seen["profile"] == "claude-haiku-5-5" and seen["budget"] == 0.05
    assert "fix the import in src/a.py" in seen["prompt"] and "EEEE" not in seen["prompt"]
    assert seen["worker_budget"] == 1.99
    assert coord.tasks["A"].state is TaskState.AWAITING_REVIEW
    spent = coord.ledger.account("task:A").spent
    assert spent == usd_to_micros("0.11")  # first attempt reported $0; triage + work = $0.11


def test_lost_lease_suppresses_the_report(repo, tmp_path):
    plan = plan_of(spec("A", scope=["src/a.py"]))
    conf = config(lease_ttl_s={Role.WORKER: 0.3, Role.REVIEWER: 30.0, Role.INTEGRATOR: 30.0})
    coord = Coordinator(plan, conf, SystemClock(), EventLog())
    started = threading.Event()

    def slow_worker(req: ExecRequest) -> ExecResult:
        coord.block("A", "operator pulls the task")  # revoke the lease mid-session
        started.set()
        (req.cwd / "src" / "a.py").write_text("A = 1\n")
        time.sleep(0.5)  # heartbeat (every 0.1s) notices the lease is gone
        return ExecResult(ok=True, cost_usd=0.1, structured={"status": "done", "summary": "", "blocker": ""})

    ws = Workspaces(Git(repo), tmp_path / "wt")
    runner = AgentRunner(coord, AgentInfo("w1", Role.WORKER), conf, executor=CallableExecutor(slow_worker), workspaces=ws)
    assert runner.step() and started.is_set()
    rec = coord.tasks["A"]
    assert rec.state is TaskState.BLOCKED and rec.commit is None
    assert not any(e.kind == "stale_rejected" for e in coord.events.events)


def test_helpers():
    assert clip_feedback(["a" * 50, "b" * 50, "c" * 50], 120) == ("b" * 50, "c" * 50)
    clipped = clip_feedback(["x" * 1000], 200)
    assert len(clipped[0]) == 200 and clipped[0].startswith("...")
    verdict = parse_review(
        {"approve": True, "summary": "s", "findings": [{"severity": "major", "file": "a.py", "detail": "bug"}]}, 0.2
    )
    assert not verdict.approve and verdict.findings == ("[major] a.py: bug",)
    assert parse_review({"approve": True, "summary": "s", "findings": [{"severity": "minor", "file": "a", "detail": "n"}]}, 0).approve
