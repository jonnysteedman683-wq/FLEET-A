"""A worker shares refs, config and hooks with the landing repository through its worktree.

These tests play a worker that reaches past its worktree, the way a ``Bash`` session can
(``sh -c 'git update-ref ...'`` gets past the ``Bash(git update-ref *)`` deny rule), and check that
nothing it planted runs and nothing it moved gets built on.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from conftest import config, git, plan_of, spec

from fleet_a.config import AgentPool
from fleet_a.coordinator import Coordinator, SystemClock
from fleet_a.events import EventLog
from fleet_a.executors import CallableExecutor, ExecRequest, ExecResult
from fleet_a.git import Git
from fleet_a.guard import RepoGuard
from fleet_a.mergequeue import MergeQueue
from fleet_a.model import AgentInfo, Assignment, MergeStatus, Role, TaskState
from fleet_a.prompts import commit_message
from fleet_a.runner import AgentRunner, Fleet
from fleet_a.workspaces import Workspaces

OK = [sys.executable, "-c", "import sys; sys.exit(0)"]
RUN = "p1-run1"


def common_dir(repo: Path) -> Path:
    return (repo / git(repo, "rev-parse", "--git-common-dir")).resolve()


def plant_hook(repo: Path, name: str, marker: Path) -> None:
    hook = common_dir(repo) / "hooks" / name
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f"#!/bin/sh\necho ran >> '{marker}'\n")
    hook.chmod(0o755)


def setup(repo: Path, tmp_path: Path):
    g = Git(repo)
    ws = Workspaces(g, tmp_path / "wt")
    guard = RepoGuard(g, "main")
    mq = MergeQueue(g, ws, "main", OK, 60, guard=guard)
    return g, ws, mq, guard


def attempt(ws: Workspaces, task_id: str, n: int, token: int, files: dict[str, str]) -> str:
    wt = ws.create_worktree(RUN, task_id, n, token, "main")
    for rel, text in files.items():
        (wt.path / rel).parent.mkdir(parents=True, exist_ok=True)
        (wt.path / rel).write_text(text)
    a = Assignment(spec(task_id), Role.WORKER, "w1", token, n, 60, 1.0, "main", run_id=RUN)
    commit = ws.squash_commit(wt, commit_message(a, "summary"))
    ws.remove(wt)
    assert commit is not None
    return commit


def move_main_from_a_worktree(ws: Workspaces, tmp_path: Path) -> str:
    """What a worker can do with Bash: commit in its worktree and point main at it via sh -c."""
    wt = ws.create_worktree(RUN, "EVIL", 1, 99, "main")
    (wt.path / "src" / "evil.py").write_text("EVIL = 1\n")
    git(wt.path, "add", "-A")
    git(wt.path, "commit", "-q", "-m", "not reviewed")
    subprocess.run(["sh", "-c", "git update-ref refs/heads/main HEAD"], cwd=wt.path, check=True)
    head = git(wt.path, "rev-parse", "HEAD")
    ws.remove(wt)
    return head


# ------------------------------------------------------------------ hooks and fsmonitor never run


def test_fleet_git_never_runs_a_planted_hook(repo, tmp_path):
    marker = tmp_path / "hook-ran"
    for name in ("post-checkout", "post-rewrite", "reference-transaction", "post-commit", "pre-commit"):
        plant_hook(repo, name, marker)
    g, ws, mq, _ = setup(repo, tmp_path)
    c = attempt(ws, "T", 1, 1, {"src/t.py": "T = 1\n"})
    assert mq.integrate(RUN, "T", 1, 1, c, ("src/",)).status is MergeStatus.MERGED
    assert not marker.exists(), "a hook from the shared hooks directory ran"
    # Plain git, without FLEET-A's flags, does run it: the test plants a working hook.
    subprocess.run(["git", "worktree", "add", "-q", "--detach", str(tmp_path / "plain"), "main"], cwd=repo, check=True)
    assert marker.exists()


def test_fleet_git_never_runs_a_planted_fsmonitor(repo, tmp_path):
    marker = tmp_path / "fsmonitor-ran"
    script = tmp_path / "fsmonitor.sh"
    script.write_text(f"#!/bin/sh\necho ran >> '{marker}'\n")
    script.chmod(0o755)
    git(repo, "config", "core.fsmonitor", str(script))
    g = Git(repo)
    wt = Workspaces(g, tmp_path / "wt").create_worktree(RUN, "T", 1, 1, "main")
    g.run("status", "--porcelain", cwd=wt.path)
    g.run("diff", "--name-only", cwd=wt.path)
    assert not marker.exists()
    subprocess.run(["git", "status", "--porcelain"], cwd=wt.path, capture_output=True)
    assert marker.exists(), "control: plain git runs the planted fsmonitor"


# --------------------------------------------------------------------------- the merge queue


def test_integrator_refuses_to_build_on_a_target_a_worker_moved(repo, tmp_path):
    g, ws, mq, guard = setup(repo, tmp_path)
    c = attempt(ws, "T", 1, 1, {"src/t.py": "T = 1\n"})
    evil = move_main_from_a_worktree(ws, tmp_path)

    out = mq.integrate(RUN, "T", 1, 1, c, ("src/",))
    assert out.status is MergeStatus.TAMPERED
    assert "refs/heads/main" in out.detail and evil in out.detail
    assert g.rev_parse("refs/heads/main") == evil, "the integrator must not land on top of it"
    assert mq.find_landed(RUN, "T", 1) is None
    assert git(repo, "worktree", "list").count("\n") == 0  # no scratch worktree was made


def test_integrator_refuses_after_config_or_hook_changes(repo, tmp_path):
    g, ws, mq, _ = setup(repo, tmp_path)
    c = attempt(ws, "T", 1, 1, {"src/t.py": "T = 1\n"})
    git(repo, "config", "filter.x.smudge", "touch /tmp/should-not-run")
    out = mq.integrate(RUN, "T", 1, 1, c, ("src/",))
    assert out.status is MergeStatus.TAMPERED and "config changed" in out.detail

    g, ws, mq, _ = setup(repo, tmp_path / "second")
    plant_hook(repo, "post-merge", tmp_path / "m")
    out = mq.integrate(RUN, "T", 1, 2, c, ("src/",))
    assert out.status is MergeStatus.TAMPERED and "hook post-merge" in out.detail
    attrs = common_dir(repo) / "info" / "attributes"
    attrs.parent.mkdir(exist_ok=True)
    attrs.write_text("* filter=x\n")
    assert "info/attributes changed" in mq.integrate(RUN, "T", 1, 3, c, ("src/",)).detail


def test_guard_follows_the_integrators_own_landings(repo, tmp_path):
    g, ws, mq, guard = setup(repo, tmp_path)
    for i, task in enumerate(("A", "B", "C"), start=1):
        c = attempt(ws, task, 1, i, {f"src/{task.lower()}.py": f"{task} = 1\n"})
        assert mq.integrate(RUN, task, 1, i, c, ("src/",)).status is MergeStatus.MERGED
    assert guard.check() is None
    assert guard.expected_tip == g.rev_parse("refs/heads/main")


def test_a_landing_by_another_integrator_is_a_race_not_tampering(repo, tmp_path, monkeypatch):
    g, ws, mq, guard = setup(repo, tmp_path)
    other_mq = MergeQueue(g, ws, "main", OK, 60, guard=guard)
    c = attempt(ws, "T", 1, 1, {"src/t.py": "T = 1\n"})
    u = attempt(ws, "U", 1, 2, {"src/u.py": "U = 1\n"})
    real_verify = mq.verify

    def verify_while_another_integrator_lands(path):
        assert other_mq.integrate(RUN, "U", 1, 2, u, ("src/",)).status is MergeStatus.MERGED
        return real_verify(path)

    monkeypatch.setattr(mq, "verify", verify_while_another_integrator_lands)
    assert mq.integrate(RUN, "T", 1, 1, c, ("src/",)).status is MergeStatus.TARGET_MOVED
    monkeypatch.setattr(mq, "verify", real_verify)
    assert guard.check() is None
    assert mq.integrate(RUN, "T", 1, 3, c, ("src/",)).status is MergeStatus.MERGED


# --------------------------------------------------------------------------- the whole fleet


class TamperingModel:
    """Workers write src/<task>.py; the worker on EVIL also points main at an unreviewed commit."""

    def __call__(self, req: ExecRequest) -> ExecResult:
        first = req.prompt.splitlines()[0]
        if first.startswith("REVIEW TASK"):
            return ExecResult(ok=True, cost_usd=0.01, structured={"approve": True, "summary": "ok", "findings": []})
        task_id = first.split()[1].rstrip(":")
        (req.cwd / "src" / f"{task_id.lower()}.py").write_text(f"VALUE_{task_id} = 1\n")
        if task_id == "EVIL":
            git(req.cwd, "add", "-A")
            git(req.cwd, "commit", "-q", "-m", "not reviewed")
            subprocess.run(["sh", "-c", "git update-ref refs/heads/main HEAD"], cwd=req.cwd, check=True)
        return ExecResult(ok=True, cost_usd=0.01, structured={"status": "done", "summary": "wrote it", "blocker": ""})


@pytest.mark.parametrize("caught_by", ["worker", "integrator"])
def test_fleet_blocks_a_worker_that_moves_the_target_and_lands_nothing_after(repo, tmp_path, caught_by):
    conf = config(
        verify_command=tuple(OK),
        agents=(AgentPool(Role.WORKER, 1), AgentPool(Role.REVIEWER, 1), AgentPool(Role.INTEGRATOR, 1)),
        lease_ttl_s={Role.WORKER: 30.0, Role.REVIEWER: 30.0, Role.INTEGRATOR: 30.0},
    )
    plan = plan_of(spec("EVIL", scope=["src/evil.py"]), spec("OK", scope=["src/ok.py"], deps=["EVIL"]))
    g = Git(repo)
    ws = Workspaces(g, tmp_path / "worktrees")
    guard = RepoGuard(g, "main")
    mq = MergeQueue(g, ws, "main", conf.verify_command, 60, guard=guard)
    coord = Coordinator(plan, conf, SystemClock(), EventLog(tmp_path / "events.jsonl"))
    executor = CallableExecutor(TamperingModel())
    runners = [
        AgentRunner(
            coord,
            AgentInfo(agent_id, pool.role, pool.capabilities),
            conf,
            executor=executor if pool.role is not Role.INTEGRATOR else None,
            workspaces=ws,
            merge_queue=mq if pool.role is Role.INTEGRATOR else None,
            guard=guard if caught_by == "worker" or pool.role is Role.INTEGRATOR else None,
        )
        for pool in conf.agents
        for agent_id in pool.agent_ids()
    ]
    report = Fleet(coord, runners, poll_interval_s=0.02).run(timeout_s=60)

    assert not report.finished
    evil = coord.tasks["EVIL"]
    assert evil.state is TaskState.BLOCKED and "shared repository changed" in evil.reason, evil.reason
    if caught_by == "integrator":
        assert evil.reason.startswith("merge refused, repository tampered")
    assert coord.tasks["OK"].state is TaskState.PENDING
    assert "awaiting operator: EVIL" in report.stall_reason
    subjects = git(repo, "log", "--format=%s", "main").splitlines()
    assert subjects[0] == "not reviewed" and not any(s.startswith("fleet(") for s in subjects)


def test_replace_refs_cannot_swap_a_reviewed_commit(repo, tmp_path):
    g, ws, mq, _ = setup(repo, tmp_path)
    reviewed = attempt(ws, "T", 1, 1, {"src/t.py": "T = 1\n"})
    evil = attempt(ws, "T", 2, 2, {"src/t.py": "T = 'evil'\n"})
    git(repo, "replace", reviewed, evil)
    assert git(repo, "show", f"{reviewed}:src/t.py") == "T = 'evil'"  # plain git is fooled
    assert g.run("show", f"{reviewed}:src/t.py").stdout.strip() == "T = 1"  # FLEET-A's git is not

    out = mq.integrate(RUN, "T", 1, 1, reviewed, ("src/",))
    assert out.status is MergeStatus.TAMPERED and "refs/replace changed" in out.detail

    _, _, fresh_mq, _ = setup(repo, tmp_path / "after")  # a guard that started with the replace ref
    assert fresh_mq.integrate(RUN, "T", 1, 3, reviewed, ("src/",)).status is MergeStatus.MERGED
    assert g.run("show", "refs/heads/main:src/t.py").stdout.strip() == "T = 1"


def test_grafts_file_changes_are_caught(repo, tmp_path):
    g, ws, mq, _ = setup(repo, tmp_path)
    c = attempt(ws, "T", 1, 1, {"src/t.py": "T = 1\n"})
    (common_dir(repo) / "info").mkdir(exist_ok=True)
    (common_dir(repo) / "info" / "grafts").write_text(f"{c}\n")
    out = mq.integrate(RUN, "T", 1, 1, c, ("src/",))
    assert out.status is MergeStatus.TAMPERED and "info/grafts changed" in out.detail
