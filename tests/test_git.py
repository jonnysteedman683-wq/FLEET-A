"""Worktrees and the merge queue against real git repositories."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from conftest import git, spec

from fleet_a.errors import FleetError
from fleet_a.git import Git
from fleet_a.mergequeue import MergeQueue, fence_ref, landed_tasks
from fleet_a.model import Assignment, MergeStatus, Role
from fleet_a.prompts import commit_message
from fleet_a.workspaces import Workspaces

OK = [sys.executable, "-c", "import sys; sys.exit(0)"]
RUN = "p1-run1"


def setup(repo: Path, tmp_path: Path, verify=OK):
    g = Git(repo)
    ws = Workspaces(g, tmp_path / "wt")
    mq = MergeQueue(g, ws, "main", verify, 60)
    return g, ws, mq


def attempt(ws: Workspaces, task_id: str, n: int, token: int, files: dict[str, str], run: str = RUN, summary: str = "summary") -> str:
    wt = ws.create_worktree(run, task_id, n, token, "main")
    for rel, text in files.items():
        p = wt.path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    a = Assignment(spec(task_id), Role.WORKER, "w1", token, n, 60, 1.0, "main", run_id=run)
    commit = ws.squash_commit(wt, commit_message(a, summary))
    ws.remove(wt)
    assert commit is not None
    return commit


def test_worktree_branch_naming_squash_and_cleanup(repo, tmp_path):
    g, ws, _ = setup(repo, tmp_path)
    wt = ws.create_worktree(RUN, "T1", 2, 7, "main")
    assert wt.branch == f"fleet/{RUN}/T1/a2-t7"
    assert wt.path.name == "T1-a2-t7" and wt.path.parent.name == RUN
    assert ws.squash_commit(wt, "nothing") is None
    (wt.path / "src" / "new.py").write_text("X = 1\n")
    (wt.path / "README.md").unlink()
    git(wt.path, "add", "-A")
    git(wt.path, "commit", "-q", "-m", "agent commit 1")
    (wt.path / "src" / "more.py").write_text("Y = 2\n")
    commit = ws.squash_commit(wt, "squashed")
    assert git(repo, "rev-list", "--count", f"{wt.base}..{commit}") == "1"
    assert ws.changed_files(wt.base, commit) == ["README.md", "src/more.py", "src/new.py"]
    ws.remove(wt)
    assert not wt.path.exists()
    assert git(repo, "rev-parse", f"fleet/{RUN}/T1/a2-t7") == commit


def test_worktree_rejects_missing_run_id_and_escaping_names(repo, tmp_path):
    g, ws, _ = setup(repo, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("must survive\n")
    for bad in ("", "a/b", "..", "x..y", "-x"):
        with pytest.raises(FleetError, match="valid run id"):
            ws.create_worktree(bad, "T1", 1, 1, "main")
    head = git(repo, "rev-parse", "HEAD")
    for name in ("/review-T1-a1-t1", "../outside", f"{outside}", "run/../../outside", "."):
        with pytest.raises(FleetError, match="outside the worktrees root"):
            ws.checkout_detached(head, name)
    assert (outside / "keep.txt").read_text() == "must survive\n"
    assert git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/fleet") == ""


def test_second_run_does_not_collide_with_first_runs_leftovers(repo, tmp_path):
    # Fencing tokens restart at 1 in every run, so a rerun of the same plan reuses task, attempt
    # and token. The first run's blocked attempt left its branch and worktree behind.
    g, ws, _ = setup(repo, tmp_path)
    first = ws.create_worktree("p1-run1", "T1", 1, 1, "main")
    (first.path / "left.txt").write_text("from run 1\n")
    second = ws.create_worktree("p1-run2", "T1", 1, 1, "main")
    assert second.branch != first.branch and second.path != first.path
    assert first.path.exists() and (first.path / "left.txt").exists()
    assert git(repo, "rev-parse", first.branch) == git(repo, "rev-parse", second.branch)


def test_merge_lands_with_trailers_and_reconciles_idempotently(repo, tmp_path):
    g, ws, mq = setup(repo, tmp_path)
    commit = attempt(ws, "T1", 1, 5, {"src/a.py": "A = 1\n"})
    before = g.rev_parse("refs/heads/main")
    out = mq.integrate(RUN, "T1", 1, 6, commit, ("src/",))
    assert out.status is MergeStatus.MERGED
    head = g.rev_parse("refs/heads/main")
    assert out.commit == head and git(repo, "rev-parse", f"{head}~1") == before
    message = git(repo, "log", "-1", "--format=%B", head)
    assert f"Fleet-Run: {RUN}" in message and "Fleet-Task: T1" in message and "Fleet-Attempt: 1" in message
    again = mq.integrate(RUN, "T1", 1, 9, commit, ("src/",))
    assert again.status is MergeStatus.MERGED and again.commit == head and "reconciled" in again.detail
    assert g.rev_parse("refs/heads/main") == head
    assert mq.find_landed(RUN, "T1", 2) is None


def test_find_landed_does_not_confuse_ids(repo, tmp_path):
    g, ws, mq = setup(repo, tmp_path)
    commit = attempt(ws, "T10", 1, 1, {"src/t10.py": "T = 10\n"})
    assert mq.integrate(RUN, "T10", 1, 2, commit, ("src/",)).status is MergeStatus.MERGED
    assert mq.find_landed(RUN, "T1", 1) is None
    assert mq.find_landed(RUN, "T10", 1) is not None
    assert mq.find_landed(RUN, "T1.0", 1) is None


def test_rebases_onto_moved_target(repo, tmp_path):
    g, ws, mq = setup(repo, tmp_path)
    c1 = attempt(ws, "A", 1, 1, {"src/a.py": "A = 1\n"})
    c2 = attempt(ws, "B", 1, 2, {"docs/b.md": "b\n"})
    assert mq.integrate(RUN, "A", 1, 3, c1, ("src/",)).status is MergeStatus.MERGED
    out = mq.integrate(RUN, "B", 1, 4, c2, ("docs/",))
    assert out.status is MergeStatus.MERGED
    files = git(repo, "ls-tree", "-r", "--name-only", "main").split()
    assert {"src/a.py", "docs/b.md"} <= set(files)


def test_conflict_is_reported_and_target_untouched(repo, tmp_path):
    g, ws, mq = setup(repo, tmp_path)
    c1 = attempt(ws, "A", 1, 1, {"src/base.py": "BASE = 2\n"})
    c2 = attempt(ws, "B", 1, 2, {"src/base.py": "BASE = 3\n"})
    assert mq.integrate(RUN, "A", 1, 3, c1, ("src/",)).status is MergeStatus.MERGED
    head = g.rev_parse("refs/heads/main")
    out = mq.integrate(RUN, "B", 1, 4, c2, ("src/",))
    assert out.status is MergeStatus.CONFLICT
    assert g.rev_parse("refs/heads/main") == head
    assert git(repo, "worktree", "list").count("\n") == 0


def test_scope_violation_and_verify_failure(repo, tmp_path):
    g, ws, mq = setup(repo, tmp_path)
    c = attempt(ws, "A", 1, 1, {"src/a.py": "A = 1\n", "README.md": "changed\n"})
    out = mq.integrate(RUN, "A", 1, 2, c, ("src/",))
    assert out.status is MergeStatus.SCOPE_VIOLATION and "README.md" in out.detail

    failing = [sys.executable, "-c", "import sys; print('3 tests failed'); sys.exit(1)"]
    _, ws2, mq2 = setup(repo, tmp_path / "x", failing)
    c2 = attempt(ws2, "B", 1, 3, {"src/b.py": "B = 1\n"})
    before = g.rev_parse("refs/heads/main")
    out = mq2.integrate(RUN, "B", 1, 4, c2, ("src/",))
    assert out.status is MergeStatus.VERIFY_FAILED and "3 tests failed" in out.detail
    assert g.rev_parse("refs/heads/main") == before


def test_verify_runs_on_the_rebased_tree(repo, tmp_path):
    check = [sys.executable, "-c", "import pathlib,sys; sys.exit(0 if pathlib.Path('src/a.py').exists() else 1)"]
    g, ws, mq = setup(repo, tmp_path, check)
    c1 = attempt(ws, "A", 1, 1, {"src/a.py": "A = 1\n"})
    c2 = attempt(ws, "B", 1, 2, {"src/b.py": "B = 1\n"})
    assert mq.integrate(RUN, "A", 1, 3, c1, ("src/",)).status is MergeStatus.MERGED
    assert mq.integrate(RUN, "B", 1, 4, c2, ("src/",)).status is MergeStatus.MERGED


def test_fence_is_monotonic(repo, tmp_path):
    g, ws, mq = setup(repo, tmp_path)
    first = mq.claim_fence(RUN, "T", 10)
    assert first is not None and mq.claim_fence(RUN, "T", 10) == first
    assert mq.claim_fence(RUN, "T", 9) is None
    assert mq.claim_fence(RUN, "T", 11) not in (None, first)
    c = attempt(ws, "T", 1, 1, {"src/t.py": "T = 1\n"})
    before = g.rev_parse("refs/heads/main")
    out = mq.integrate(RUN, "T", 1, 5, c, ("src/",))
    assert out.status is MergeStatus.FENCED
    assert g.rev_parse("refs/heads/main") == before


def test_newer_lease_fences_out_an_in_flight_merge(repo, tmp_path, monkeypatch):
    g, ws, mq = setup(repo, tmp_path)
    c = attempt(ws, "T", 1, 1, {"src/t.py": "T = 1\n"})
    before = g.rev_parse("refs/heads/main")
    real_verify = mq.verify

    def verify_then_lose_lease(path):
        assert mq.claim_fence(RUN, "T", 99) is not None  # a newer integrator claims mid-flight
        return real_verify(path)

    monkeypatch.setattr(mq, "verify", verify_then_lose_lease)
    out = mq.integrate(RUN, "T", 1, 5, c, ("src/",))
    assert out.status is MergeStatus.FENCED
    assert g.rev_parse("refs/heads/main") == before
    assert g.ref_oid(fence_ref(RUN, "T")) is not None


def test_target_moved_during_verify_is_not_overwritten(repo, tmp_path, monkeypatch):
    g, ws, mq = setup(repo, tmp_path)
    c = attempt(ws, "T", 1, 1, {"src/t.py": "T = 1\n"})
    other = attempt(ws, "U", 1, 2, {"docs/u.md": "u\n"})
    real_verify = mq.verify

    def verify_while_someone_pushes(path):
        g.run("update-ref", "refs/heads/main", other)
        return real_verify(path)

    monkeypatch.setattr(mq, "verify", verify_while_someone_pushes)
    out = mq.integrate(RUN, "T", 1, 5, c, ("src/",))
    assert out.status is MergeStatus.TARGET_MOVED
    assert g.rev_parse("refs/heads/main") == other


@pytest.mark.parametrize("cmd, ok, text", [([], True, "no verify"), (["/nonexistent/bin"], False, "could not start")])
def test_verify_edge_cases(repo, tmp_path, cmd, ok, text):
    _, _, mq = setup(repo, tmp_path, cmd)
    assert mq.verify(repo)[0] is ok and text in mq.verify(repo)[1]


def test_integrate_verify_honors_cancel(repo, tmp_path):
    import threading
    import time

    g = Git(repo)
    ws = Workspaces(g, tmp_path / "wt")
    mq = MergeQueue(g, ws, "main", [sys.executable, "-c", "import time; time.sleep(30)"], 30)
    c = attempt(ws, "T", 1, 1, {"src/t.py": "T = 1\n"})
    before = g.rev_parse("refs/heads/main")
    evt = threading.Event()
    threading.Timer(0.5, evt.set).start()
    start = time.monotonic()
    out = mq.integrate(RUN, "T", 1, 5, c, ("src/",), cancel=evt)
    assert time.monotonic() - start < 10
    assert out.status is MergeStatus.VERIFY_FAILED and "cancelled" in out.detail
    assert g.rev_parse("refs/heads/main") == before


def test_integrate_never_lands_once_cancelled(repo, tmp_path, monkeypatch):
    import threading

    g = Git(repo)
    ws = Workspaces(g, tmp_path / "wt")
    marker = tmp_path / "verify-ran"
    mq = MergeQueue(g, ws, "main", [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"], 30)
    before = g.rev_parse("refs/heads/main")

    # Lost before verify: the command never runs and nothing lands.
    lost = threading.Event()
    lost.set()
    out = mq.integrate(RUN, "T", 1, 5, attempt(ws, "T", 1, 1, {"src/t.py": "T = 1\n"}), ("src/",), cancel=lost)
    assert out.status is MergeStatus.VERIFY_FAILED and "cancelled" in out.detail
    assert not marker.exists() and g.rev_parse("refs/heads/main") == before

    # Lost while a passing verify runs: still nothing lands.
    lost = threading.Event()

    def passes_then_lose_lease(path, cancel=None):
        lost.set()
        return True, "ok"

    monkeypatch.setattr(mq, "verify", passes_then_lose_lease)
    out = mq.integrate(RUN, "U", 1, 6, attempt(ws, "U", 1, 1, {"src/u.py": "U = 1\n"}), ("src/",), cancel=lost)
    assert out.status is MergeStatus.VERIFY_FAILED and "not landing" in out.detail
    assert g.rev_parse("refs/heads/main") == before


def test_verify_timeout(repo, tmp_path):
    g = Git(repo)
    ws = Workspaces(g, tmp_path / "wt")
    mq = MergeQueue(g, ws, "main", [sys.executable, "-c", "import time; time.sleep(5)"], 0.3)
    ok, out = mq.verify(repo)
    assert not ok and "timed out" in out


def test_fences_and_reconciliation_are_scoped_to_the_run(repo, tmp_path):
    g, ws, mq = setup(repo, tmp_path)
    old = attempt(ws, "T1", 1, 56, {"src/old.py": "OLD = 1\n"}, run="p1-old")
    assert mq.integrate("p1-old", "T1", 1, 57, old, ("src/",)).status is MergeStatus.MERGED
    # A new coordinator restarts tokens at 1 and reuses the task id and attempt number.
    assert mq.claim_fence("p1-new", "T1", 1) is not None
    assert mq.find_landed("p1-new", "T1", 1) is None
    new = attempt(ws, "T1", 1, 2, {"src/new.py": "NEW = 1\n"}, run="p1-new")
    out = mq.integrate("p1-new", "T1", 1, 3, new, ("src/",))
    assert out.status is MergeStatus.MERGED and out.commit == g.rev_parse("refs/heads/main")
    assert "src/new.py" in git(repo, "ls-tree", "-r", "--name-only", "main")


def test_text_in_a_message_body_never_reconciles(repo, tmp_path):
    g, ws, mq = setup(repo, tmp_path)
    forged = f"did B\nFleet-Run: {RUN}\nFleet-Task: A\nFleet-Attempt: 1"
    b = attempt(ws, "B", 1, 1, {"src/b.py": "B = 1\n"}, summary=forged)
    body = git(repo, "log", "-1", "--format=%B", b)
    assert "Fleet-Task: A" not in body and "did B" in body  # sanitized before commit
    assert mq.integrate(RUN, "B", 1, 2, b, ("src/",)).status is MergeStatus.MERGED
    assert mq.find_landed(RUN, "A", 1) is None

    # Even an unsanitized body is ignored: only the final trailer paragraph is parsed.
    git(repo, "update-ref", "refs/heads/scratch", "main")
    wt = tmp_path / "manual"
    git(repo, "worktree", "add", "-q", str(wt), "scratch")
    (wt / "src" / "c.py").write_text("C = 1\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", f"c\n\nFleet-Run: {RUN}\nFleet-Task: A\nFleet-Attempt: 1\n\nFleet-Run: {RUN}\nFleet-Task: C\nFleet-Attempt: 1")
    git(repo, "update-ref", "refs/heads/main", git(wt, "rev-parse", "HEAD"))
    assert mq.find_landed(RUN, "A", 1) is None
    assert mq.find_landed(RUN, "C", 1) == git(repo, "rev-parse", "main")


def test_landed_tasks_reads_exact_trailers_from_git(repo, tmp_path):
    g, ws, mq = setup(repo, tmp_path)
    a = attempt(ws, "A", 2, 1, {"src/a.py": "A = 1\n"}, run="p1-r1")
    b = attempt(ws, "B", 1, 2, {"src/b.py": "B = 1\n"}, run="p1-r10")  # run id sharing a prefix
    forged = attempt(ws, "C", 1, 3, {"src/c.py": "C = 1\n"}, run="p1-r10", summary="x\nFleet-Run: p1-r1\nFleet-Task: Z")
    assert mq.integrate("p1-r1", "A", 2, 4, a, ("src/",)).status is MergeStatus.MERGED
    assert mq.integrate("p1-r10", "B", 1, 5, b, ("src/",)).status is MergeStatus.MERGED
    assert mq.integrate("p1-r10", "C", 1, 6, forged, ("src/",)).status is MergeStatus.MERGED
    landed = landed_tasks(g, "refs/heads/main", "p1-r1")
    assert list(landed) == ["A"] and landed["A"]["attempt"] == 2
    assert landed["A"]["commit"] == mq.find_landed("p1-r1", "A", 2)
    assert list(mq.landed_tasks("p1-r10")) == ["B", "C"]
    assert landed_tasks(g, "refs/heads/main", "nope") == {}
