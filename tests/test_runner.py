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
from fleet_a.model import AgentInfo, MergeOutcome, MergeStatus, Role, TaskState, usd_to_micros
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

    assert coord.tasks["B"].attempts == 2 and "verify_failed before review" in coord.tasks["B"].feedback[0]
    assert ("review", "B", 1) not in model.calls  # the broken attempt never reached a paid review
    assert coord.tasks["C"].attempts == 2 and "needs a docstring" in coord.tasks["C"].feedback[0]
    for rec in coord.tasks.values():
        assert rec.reviewer is not None and rec.reviewer != rec.author

    coord.events.close()
    events = EventLog.load(tmp_path / "events.jsonl")
    EventLog.verify(events)
    assert set(project_states(events).values()) == {"done"}
    assert git(repo, "worktree", "list").count("\n") == 0


def test_stale_base_verify_failure_goes_to_review_not_rework(repo, tmp_path):
    """The target moves while the attempt runs: a failure there is left to the merge queue."""

    class MovesTarget(ScriptedModel):
        def __call__(self, req):
            result = super().__call__(req)
            if req.prompt.startswith("TASK B:") and len([c for c in self.calls if c[0] == "work"]) == 1:
                tree = git(repo, "rev-parse", "main^{tree}")
                moved = git(repo, "commit-tree", tree, "-p", "main", "-m", "someone else lands")
                git(repo, "update-ref", "refs/heads/main", moved)
            return result

    model = MovesTarget(broken_first={"B"})
    coord, fleet, _ = build(repo, tmp_path, plan_of(spec("B", scope=["src/b.py"])), model)
    report = fleet.run(timeout_s=60)
    assert report.finished and coord.tasks["B"].state is TaskState.DONE, coord.summary()
    assert ("review", "B", 1) in model.calls  # stale base: reviewed instead of reworked
    assert coord.tasks["B"].feedback[0].startswith("merge verify_failed")  # the merge queue caught it


def test_run_verify_stops_on_cancel_and_timeout(tmp_path):
    from fleet_a.mergequeue import run_verify

    # The command starts a grandchild; killing the process group must stop both.
    sleeper = [sys.executable, "-c", "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); time.sleep(60)"]
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    ok, out = run_verify(sleeper, 60, tmp_path, cancel=cancel)
    assert not ok and "cancelled" in out and time.monotonic() - started < 10
    started = time.monotonic()
    ok, out = run_verify(sleeper, 0.5, tmp_path)
    assert not ok and "timed out" in out and time.monotonic() - started < 10
    assert run_verify([sys.executable, "-c", "print('fine')"], 30, tmp_path) == (True, "fine")
    assert run_verify([], 30, tmp_path)[0] is True


def test_run_verify_kills_group_members_that_ignore_sigterm(tmp_path):
    import os

    from fleet_a.mergequeue import run_verify

    pidfile = tmp_path / "grandchild.pid"
    grandchild = (
        "import os,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"open({str(pidfile)!r},'w').write(str(os.getpid())); time.sleep(60)"
    )
    # The leader dies on SIGTERM (default action) while the grandchild ignores it.
    leader = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{grandchild!r}]); time.sleep(60)"
    )
    ok, out = run_verify([sys.executable, "-c", leader], 1.5, tmp_path)
    assert not ok and "timed out" in out
    pid = int(pidfile.read_text())

    def running(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        try:  # a zombie has exited; only its parent (or PID 1) has not reaped it yet
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except (OSError, IndexError):
            return True

    deadline = time.monotonic() + 5
    while running(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not running(pid)


def test_integrator_passes_its_lease_loss_event_to_the_merge_queue(repo, tmp_path):
    """The integrator's heartbeat event reaches integrate(); once it is set, nothing is reported."""
    from fleet_a.coordinator import ManualClock

    plan = plan_of(spec("A", scope=["src/a.py"]))
    conf = config(verify_command=(), lease_ttl_s={Role.WORKER: 30.0, Role.REVIEWER: 30.0, Role.INTEGRATOR: 0.3})
    clock = ManualClock()
    coord = Coordinator(plan, conf, clock, EventLog())
    ws = Workspaces(Git(repo), tmp_path / "wt")
    for agent in (AgentInfo("w1", Role.WORKER), AgentInfo("r1", Role.REVIEWER)):
        runner = AgentRunner(coord, agent, conf, executor=CallableExecutor(ScriptedModel()), workspaces=ws)
        assert runner.step()
    assert coord.tasks["A"].state is TaskState.AWAITING_MERGE

    seen = {}

    class LosesLease:
        def integrate(self, *args, cancel=None):
            seen["cancel"] = cancel
            clock.advance(60)  # the lease expires; the next heartbeat is refused and sets the event
            cancel.wait(5)
            return MergeOutcome(MergeStatus.VERIFY_FAILED, detail="verify command cancelled: lease lost")

    integrator = AgentRunner(coord, AgentInfo("i1", Role.INTEGRATOR), conf, merge_queue=LosesLease())
    assert integrator.step()
    assert isinstance(seen["cancel"], threading.Event) and seen["cancel"].is_set()
    assert not any(e.kind == "stale_rejected" for e in coord.events.events)  # nothing was reported
    assert coord.tasks["A"].merge_unresolved  # the next integrator reconciles through the fence


def test_without_verify_before_review_the_merge_queue_catches_it(repo, tmp_path):
    plan = plan_of(spec("B", scope=["src/b.py"]))
    model = ScriptedModel(broken_first={"B"})
    coord, fleet, _ = build(repo, tmp_path, plan, model, verify_before_review=False)
    report = fleet.run(timeout_s=60)
    assert report.finished and coord.tasks["B"].state is TaskState.DONE, coord.summary()
    assert ("review", "B", 1) in model.calls  # the broken attempt was reviewed (and paid for)
    assert coord.tasks["B"].feedback[0].startswith("merge verify_failed")


def test_budget_capped_review_blocks_after_one_retry(repo, tmp_path):
    reviews: list[float] = []

    def model(req: ExecRequest) -> ExecResult:
        if req.prompt.startswith("REVIEW TASK"):
            reviews.append(req.budget_usd)
            return ExecResult(
                ok=False, cost_usd=req.budget_usd, error="error_max_budget_usd: cap", budget_exhausted=True
            )
        (req.cwd / "src" / "a.py").write_text("A = 1\n")
        return ExecResult(ok=True, cost_usd=0.1, structured={"status": "done", "summary": "ok", "blocker": ""})

    coord, fleet, _ = build(repo, tmp_path, plan_of(spec("A", scope=["src/a.py"])), model)
    report = fleet.run(timeout_s=60)
    rec = coord.tasks["A"]
    assert not report.finished and rec.state is TaskState.BLOCKED, coord.summary()
    assert len(reviews) == 2 and "resume with a higher review_budget_usd" in rec.reason


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
    conf = config(triage_threshold_chars=200, verify_command=())  # the repo fixture has no tests
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
