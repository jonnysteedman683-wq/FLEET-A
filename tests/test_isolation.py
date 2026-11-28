"""Workers and reviewers work in a clone the integrator never lands in.

``test_guard.py`` covers a worker whose worktree shares the landing repository, which the guard can
only detect. Here the worker clone separates them. A worker can rewrite that clone's config, hooks
and refs, and nothing it plants runs while the integrator merges. The landing repository and its
guard see no change.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import config, git, plan_of, spec

from fleet_a.cli import build_fleet
from fleet_a.config import AgentPool
from fleet_a.errors import FleetError
from fleet_a.executors import CallableExecutor, ExecRequest, ExecResult
from fleet_a.git import Git
from fleet_a.guard import RepoGuard
from fleet_a.mergequeue import MergeQueue
from fleet_a.model import Assignment, MergeStatus, Role, TaskState
from fleet_a.prompts import commit_message
from fleet_a.workspaces import Workspaces, create_worker_clone

RUN = "p1-run1"


def isolated(repo: Path, tmp_path: Path, verify=()):
    landing = Git(repo)
    clone = create_worker_clone(landing, tmp_path / "worker-clone.git")
    work = Workspaces(clone, tmp_path / "work", upstream=landing)
    guard = RepoGuard(landing, "main")
    mq = MergeQueue(
        landing, Workspaces(landing, tmp_path / "integrator"), "main", list(verify), 60, guard=guard, source=clone
    )
    return landing, clone, work, mq, guard


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


# -------------------------------------------------------------------------------- the clone


def test_worker_clone_is_bare_unlinked_and_names_no_remote(repo, tmp_path):
    landing = Git(repo)
    clone = create_worker_clone(landing, tmp_path / "c.git")
    assert git(clone.repo, "rev-parse", "--is-bare-repository") == "true"
    assert git(clone.repo, "remote") == ""
    assert str(repo) not in (clone.repo / "config").read_text()
    assert git(clone.repo, "rev-parse", "main") == git(repo, "rev-parse", "main")
    land_objs = {p.stat().st_ino for p in (repo / ".git" / "objects").rglob("*") if p.is_file()}
    clone_objs = {p.stat().st_ino for p in (clone.repo / "objects").rglob("*") if p.is_file()}
    assert not land_objs & clone_objs  # copied, not hard-linked


def test_worker_clone_is_rebuilt_from_scratch(repo, tmp_path):
    landing = Git(repo)
    clone = create_worker_clone(landing, tmp_path / "c.git")
    (clone.repo / "config").write_text((clone.repo / "config").read_text() + "[filter \"evil\"]\n\tclean = x\n")
    (clone.repo / "hooks" / "post-checkout").write_text("#!/bin/sh\n")
    clone = create_worker_clone(landing, tmp_path / "c.git")
    assert "evil" not in (clone.repo / "config").read_text()
    assert not (clone.repo / "hooks" / "post-checkout").exists()


@pytest.mark.parametrize("where", ["repo", "inside", "parent"])
def test_worker_clone_must_be_outside_the_landing_repository(repo, tmp_path, where):
    path = {"repo": repo, "inside": repo / "sub" / "c.git", "parent": repo.parent}[where]
    with pytest.raises(FleetError, match="outside the repository"):
        create_worker_clone(Git(repo), path)


def test_workers_branch_from_the_landing_tip_and_cannot_move_it(repo, tmp_path):
    landing, clone, work, mq, guard = isolated(repo, tmp_path)
    c1 = attempt(work, "A", 1, 1, {"src/a.py": "A = 1\n"})
    assert mq.integrate(RUN, "A", 1, 1, c1, ("src/",)).status is MergeStatus.MERGED
    tip = git(repo, "rev-parse", "main")

    wt = work.create_worktree(RUN, "B", 1, 2, "main")
    assert wt.base == tip  # fetched the integrator's landing before the attempt
    git(wt.path, "commit", "-q", "--allow-empty", "-m", "not reviewed")
    subprocess.run(["sh", "-c", "git update-ref refs/heads/main HEAD"], cwd=wt.path, check=True)
    work.remove(wt)

    assert git(repo, "rev-parse", "main") == tip  # the landing repository never moved
    assert guard.check() is None
    assert work.target_tip("main") == tip  # and the clone's main is reset from it


# ---------------------------------------------------------------------------- the integrator


def test_nothing_a_worker_configures_runs_while_the_integrator_merges(repo, tmp_path):
    """The window the guard could only detect: a filter driver, hook or fsmonitor in the shared
    config running during the integrator's checkout. In the worker clone it never reaches it."""
    landing, clone, work, mq, guard = isolated(repo, tmp_path)
    marker = tmp_path / "ran.txt"
    program = f"sh -c 'pwd >> {marker}; cat'"
    git(clone.repo, "config", "filter.evil.clean", program)
    git(clone.repo, "config", "filter.evil.smudge", program)
    git(clone.repo, "config", "core.fsmonitor", f"sh -c 'pwd >> {marker}'")
    hook = clone.repo / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\npwd >> '{marker}'\n")
    hook.chmod(0o755)

    c = attempt(work, "T", 1, 1, {"src/.gitattributes": "* filter=evil\n", "src/t.py": "T = 1\n"})
    out = mq.integrate(RUN, "T", 1, 1, c, ("src/",))

    assert out.status is MergeStatus.MERGED, out.detail
    ran = marker.read_text().splitlines() if marker.exists() else []
    assert ran, "the planted filter never ran at all, so this test proves nothing"
    integrator = str((tmp_path / "integrator").resolve())
    assert not [d for d in ran if d.startswith(integrator) or d.startswith(str(repo))], ran
    assert guard.check() is None
    assert git(repo, "show", "main:src/t.py") == "T = 1"


def test_a_commit_that_never_reached_the_worker_clone_is_not_landed(repo, tmp_path):
    landing, clone, work, mq, guard = isolated(repo, tmp_path)
    missing = "1" * 40
    out = mq.integrate(RUN, "T", 1, 1, missing, ("src/",))
    assert out.status is MergeStatus.CONFLICT and "could not be fetched" in out.detail
    assert guard.check() is None


VERIFY_EXPORT = [
    sys.executable,
    "-c",
    "import os, pathlib; p = pathlib.Path('.');"
    "assert not (p / '.git').exists(), 'git dir';"
    "assert (p / 'src' / 't.py').read_text() == 'T = 1\\n';"
    "assert (p / 'src' / 'skipped.py').exists(), 'export-ignore applied';"
    "assert os.access(p / 'run.sh', os.X_OK), 'exec bit';"
    "assert os.readlink(p / 'src' / 'link') == 't.py', 'symlink'",
]


def test_verify_runs_in_a_plain_export_of_the_rebased_tree(repo, tmp_path):
    landing, clone, work, mq, guard = isolated(repo, tmp_path, verify=VERIFY_EXPORT)
    wt = work.create_worktree(RUN, "T", 1, 1, "main")
    (wt.path / "src" / "t.py").write_text("T = 1\n")
    (wt.path / "src" / "skipped.py").write_text("S = 1\n")
    (wt.path / "src" / ".gitattributes").write_text("skipped.py export-ignore\n")
    (wt.path / "run.sh").write_text("#!/bin/sh\n")
    (wt.path / "run.sh").chmod(0o755)
    os.symlink("t.py", wt.path / "src" / "link")
    a = Assignment(spec("T", scope=("src/", "run.sh")), Role.WORKER, "w1", 1, 1, 60, 1.0, "main", run_id=RUN)
    c = work.squash_commit(wt, commit_message(a, "summary"))
    work.remove(wt)

    out = mq.integrate(RUN, "T", 1, 1, c, ("src/", "run.sh"))
    assert out.status is MergeStatus.MERGED, out.detail
    assert not list((tmp_path / "integrator").glob(f"{RUN}/verify-*"))  # export cleaned up


def test_a_failing_verify_in_the_export_still_blocks_the_landing(repo, tmp_path):
    fail = [sys.executable, "-c", "import sys; sys.exit(3)"]
    landing, clone, work, mq, guard = isolated(repo, tmp_path, verify=fail)
    before = git(repo, "rev-parse", "main")
    c = attempt(work, "T", 1, 1, {"src/t.py": "T = 1\n"})
    assert mq.integrate(RUN, "T", 1, 1, c, ("src/",)).status is MergeStatus.VERIFY_FAILED
    assert git(repo, "rev-parse", "main") == before


# --------------------------------------------------------------------------- the whole fleet


class TamperingModel:
    """Every worker writes src/<task>.py. The worker on EVIL also tampers with everything it can reach:
    it moves main, plants a hook and a filter driver, and commits a .gitattributes that uses the filter."""

    def __init__(self, marker: Path) -> None:
        self.marker = marker

    def __call__(self, req: ExecRequest) -> ExecResult:
        first = req.prompt.splitlines()[0]
        if first.startswith("REVIEW TASK"):
            return ExecResult(ok=True, cost_usd=0.01, structured={"approve": True, "summary": "ok", "findings": []})
        task_id = first.split()[1].rstrip(":")
        (req.cwd / "src" / f"{task_id.lower()}.py").write_text(f"VALUE_{task_id} = 1\n")
        if task_id == "EVIL":
            common = (req.cwd / git(req.cwd, "rev-parse", "--git-common-dir")).resolve()
            program = f"sh -c 'pwd >> {self.marker}; cat'"
            # Through sh -c, as a Bash session would; the program is an argument, not shell text.
            for key in ("filter.evil.clean", "filter.evil.smudge"):
                subprocess.run(["sh", "-c", 'git config "$1" "$2"', "sh", key, program], cwd=req.cwd, check=True)
            (common / "hooks").mkdir(exist_ok=True)
            (common / "hooks" / "post-checkout").write_text(f"#!/bin/sh\npwd >> '{self.marker}'\n")
            (common / "hooks" / "post-checkout").chmod(0o755)
            (req.cwd / "src" / ".gitattributes").write_text("* filter=evil\n")
            git(req.cwd, "add", "-A")
            git(req.cwd, "commit", "-q", "-m", "not reviewed")
            subprocess.run(["sh", "-c", "git update-ref refs/heads/main HEAD"], cwd=req.cwd, check=True)
        return ExecResult(ok=True, cost_usd=0.01, structured={"status": "done", "summary": "wrote it", "blocker": ""})


def test_fleet_lands_reviewed_work_while_a_worker_tampers_with_its_clone(repo, tmp_path):
    conf = config(
        verify_command=(sys.executable, "-c", "import sys; sys.exit(0)"),
        agents=(AgentPool(Role.WORKER, 1), AgentPool(Role.REVIEWER, 1), AgentPool(Role.INTEGRATOR, 1)),
        lease_ttl_s={Role.WORKER: 30.0, Role.REVIEWER: 30.0, Role.INTEGRATOR: 30.0},
    )
    plan = plan_of(spec("EVIL"), spec("OK", deps=["EVIL"]))
    marker = tmp_path / "ran.txt"
    landing_config = (repo / ".git" / "config").read_text()
    coord, fleet = build_fleet(
        repo,
        plan,
        conf,
        events_path=tmp_path / "events.jsonl",
        worktrees=tmp_path / "fleet",
        claude_binary="unused",
        executor=CallableExecutor(TamperingModel(marker)),
    )
    report = fleet.run(timeout_s=60)

    assert report.finished, report.stall_reason
    assert {t: r.state for t, r in coord.tasks.items()} == {"EVIL": TaskState.DONE, "OK": TaskState.DONE}
    subjects = git(repo, "log", "--format=%s", "main").splitlines()
    assert "not reviewed" not in subjects and sum(s.startswith("fleet(") for s in subjects) == 2
    assert (repo / ".git" / "config").read_text() == landing_config
    assert not (repo / ".git" / "hooks" / "post-checkout").exists()
    ran = marker.read_text().splitlines() if marker.exists() else []
    assert ran, "the planted filter never ran at all, so this test proves nothing"
    integrator = str((tmp_path / "fleet" / "integrator").resolve())
    assert not [d for d in ran if d.startswith(integrator) or d.startswith(str(repo))], ran
