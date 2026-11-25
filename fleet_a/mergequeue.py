"""The integrator: the only component that writes the target branch. It runs no model.

For one approved attempt it:

1. Claims the task's merge fence ``refs/fleet/fence/<run>/<task>`` with its fencing token. The
   claim is a compare-and-swap that only moves the fence to a *higher* token, so once a newer
   lease claims it no older integrator can land anything for this task. Fences are namespaced
   by run because every coordinator starts counting tokens at 1.
2. Reconciles: if a commit whose parsed trailer block names this run, task and attempt is
   already on the target branch (a previous integrator landed it and then lost its lease), it
   reports MERGED without touching anything.
3. Rebases the attempt onto the current target in a scratch worktree.
4. Re-checks the write scope against the rebased change.
5. Runs the verification command on the rebased tree.
6. Lands with one atomic ``git update-ref --stdin`` transaction that verifies the fence still
   holds *its* claim and compare-and-swaps the target ref from the commit it rebased onto.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable, Sequence

from .git import Git
from .model import MergeOutcome, MergeStatus
from .scopes import scope_violations
from .workspaces import Workspaces

FENCE_PREFIX = "refs/fleet/fence/"
_TAIL = 4000
_FIELD, _VALUE, _RECORD = "\x1f", "\x1e", "\x1d"
_TRAILER_FORMAT = "%H" + "".join(
    f"%x1f%(trailers:key={key},valueonly,separator=%x1e)" for key in ("Fleet-Run", "Fleet-Task", "Fleet-Attempt")
) + "%x1d"


def _tail(text: str) -> str:
    text = text.strip()
    return text if len(text) <= _TAIL else "..." + text[-_TAIL:]


def fence_ref(run_id: str, task_id: str) -> str:
    return f"{FENCE_PREFIX}{run_id}/{task_id}"


class MergeQueue:
    def __init__(
        self,
        git: Git,
        workspaces: Workspaces,
        target_branch: str,
        verify_command: Sequence[str],
        verify_timeout_s: float,
    ) -> None:
        self.git = git
        self.workspaces = workspaces
        self.target_ref = f"refs/heads/{target_branch}"
        self.verify_command = tuple(verify_command)
        self.verify_timeout_s = verify_timeout_s

    # ------------------------------------------------------------------------------ public

    def integrate(
        self,
        run_id: str,
        task_id: str,
        attempt: int,
        token: int,
        commit: str,
        write_scope: Iterable[str],
    ) -> MergeOutcome:
        fence = self.claim_fence(run_id, task_id, token)
        if fence is None:
            return MergeOutcome(MergeStatus.FENCED, detail=f"a newer lease than {token} owns the merge fence")
        landed = self.find_landed(run_id, task_id, attempt)
        if landed is not None:
            return MergeOutcome(MergeStatus.MERGED, landed, "already on target (reconciled)")

        base = self.git.rev_parse(self.target_ref)
        scratch = self.workspaces.checkout_detached(commit, f"{run_id}/merge-{task_id}-a{attempt}-t{token}", base)
        try:
            rebase = self.git.run("rebase", "-q", base, cwd=scratch.path, check=False)
            if rebase.returncode != 0:
                self.git.run("rebase", "--abort", cwd=scratch.path, check=False)
                return MergeOutcome(MergeStatus.CONFLICT, detail=_tail(rebase.stdout + rebase.stderr))
            head = self.git.rev_parse("HEAD", cwd=scratch.path)
            changed = self.workspaces.changed_files(base, head)
            if not changed:
                return MergeOutcome(MergeStatus.CONFLICT, detail="rebase onto target left no changes")
            outside = scope_violations(list(write_scope), changed)
            if outside:
                return MergeOutcome(MergeStatus.SCOPE_VIOLATION, detail=f"outside write scope: {outside}")
            ok, output = self.verify(scratch.path)
            if not ok:
                return MergeOutcome(MergeStatus.VERIFY_FAILED, detail=output)
            ref = fence_ref(run_id, task_id)
            land = self.git.run(
                "update-ref",
                "--stdin",
                input=f"verify {ref} {fence}\nupdate {self.target_ref} {head} {base}\n",
                check=False,
            )
            if land.returncode != 0:
                if self.git.ref_oid(ref) != fence:
                    return MergeOutcome(MergeStatus.FENCED, detail="fence moved before landing")
                return MergeOutcome(MergeStatus.TARGET_MOVED, detail=_tail(land.stderr))
            return MergeOutcome(MergeStatus.MERGED, head, f"landed on {self.target_ref}")
        finally:
            self.workspaces.remove(scratch)

    def claim_fence(self, run_id: str, task_id: str, token: int) -> str | None:
        """Move the fence to ``token`` unless a higher token already holds it. Returns its oid."""
        ref = fence_ref(run_id, task_id)
        blob = self.git.run(
            "hash-object", "-w", "--stdin", input=f"fleet-fence {run_id} {task_id} {token}\n"
        ).stdout.strip()
        zero = self.git.zero_oid()
        for _ in range(16):
            current = self.git.ref_oid(ref)
            if current is not None:
                if current == blob:
                    return blob
                held = int(self.git.run("cat-file", "-p", current).stdout.split()[3])
                if held > token:
                    return None
            swap = self.git.run("update-ref", ref, blob, current or zero, check=False)
            if swap.returncode == 0:
                return blob
        return None

    def find_landed(self, run_id: str, task_id: str, attempt: int) -> str | None:
        """The target-branch commit whose trailer block is exactly this run, task and attempt.

        Only git's parsed trailers (the message's final paragraph) count, and values must match
        exactly, so text elsewhere in a message can never make a task look landed.
        """
        out = self.git.run(
            "log",
            self.target_ref,
            f"--format={_TRAILER_FORMAT}",
            "--fixed-strings",
            f"--grep=Fleet-Run: {run_id}",
        ).stdout
        want = ([run_id], [task_id], [str(attempt)])
        for record in out.split(_RECORD):
            fields = record.strip("\n").split(_FIELD)
            if len(fields) != 4:
                continue
            sha, runs, tasks, attempts = fields
            if (runs.split(_VALUE), tasks.split(_VALUE), attempts.split(_VALUE)) == want:
                return sha
        return None

    def landed_tasks(self, run_id: str) -> dict[str, dict[str, object]]:
        return landed_tasks(self.git, self.target_ref, run_id)


    def verify(self, path: Path) -> tuple[bool, str]:
        return run_verify(self.verify_command, self.verify_timeout_s, path)


def run_verify(command: Sequence[str], timeout_s: float, path: Path) -> tuple[bool, str]:
    """Run the verify command in ``path``: (passed, tail of its output). No command always passes."""
    if not command:
        return True, "no verify command configured"
    try:
        proc = subprocess.run(
            list(command),
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return False, f"verify command timed out after {timeout_s:.0f}s"
    except OSError as exc:
        return False, f"verify command could not start: {exc}"
    return proc.returncode == 0, _tail(proc.stdout + "\n" + proc.stderr)


def landed_tasks(git: Git, target_ref: str, run_id: str) -> dict[str, dict[str, object]]:
    """Every task of ``run_id`` whose change is on ``target_ref``: {task: {attempt, commit}}.

    This reads git, not the event log, so it is the authority after a coordinator crash: a merge
    can land in the instant between the atomic ``update-ref`` and the ``done`` transition being
    logged. Same exact-trailer rules as ``MergeQueue.find_landed``. Read-only.
    """
    out = git.run(
        "log",
        target_ref,
        f"--format={_TRAILER_FORMAT}",
        "--fixed-strings",
        f"--grep=Fleet-Run: {run_id}",
    ).stdout
    landed: dict[str, dict[str, object]] = {}
    for record in out.split(_RECORD):
        fields = record.strip("\n").split(_FIELD)
        if len(fields) != 4:
            continue
        sha, runs, tasks, attempts = (f.split(_VALUE) for f in fields)
        if runs != [run_id] or len(tasks) != 1 or len(attempts) != 1 or not attempts[0].isdigit():
            continue
        landed.setdefault(tasks[0], {"attempt": int(attempts[0]), "commit": sha[0]})
    return dict(sorted(landed.items()))
