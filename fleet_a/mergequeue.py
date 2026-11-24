"""The integrator: the only component that writes the target branch. It runs no model.

For one approved attempt it:

1. Claims the task's merge fence ``refs/fleet/fence/<task>`` with its fencing token. The claim
   is a compare-and-swap that only moves the fence to a *higher* token, so once a newer lease
   claims it no older integrator can land anything for this task.
2. Reconciles: if a commit carrying this task's ``Fleet-Task``/``Fleet-Attempt`` trailers is
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


def _tail(text: str) -> str:
    text = text.strip()
    return text if len(text) <= _TAIL else "..." + text[-_TAIL:]


def _bre_escape(text: str) -> str:
    return "".join("\\" + c if c in ".[]*^$\\" else c for c in text)


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
        task_id: str,
        attempt: int,
        token: int,
        commit: str,
        write_scope: Iterable[str],
    ) -> MergeOutcome:
        fence = self.claim_fence(task_id, token)
        if fence is None:
            return MergeOutcome(MergeStatus.FENCED, detail=f"a newer lease than {token} owns the merge fence")
        landed = self.find_landed(task_id, attempt)
        if landed is not None:
            return MergeOutcome(MergeStatus.MERGED, landed, "already on target (reconciled)")

        base = self.git.rev_parse(self.target_ref)
        scratch = self.workspaces.checkout_detached(commit, f"merge-{task_id}-a{attempt}-t{token}", base)
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
            land = self.git.run(
                "update-ref",
                "--stdin",
                input=f"verify {FENCE_PREFIX}{task_id} {fence}\nupdate {self.target_ref} {head} {base}\n",
                check=False,
            )
            if land.returncode != 0:
                if self.git.ref_oid(FENCE_PREFIX + task_id) != fence:
                    return MergeOutcome(MergeStatus.FENCED, detail="fence moved before landing")
                return MergeOutcome(MergeStatus.TARGET_MOVED, detail=_tail(land.stderr))
            return MergeOutcome(MergeStatus.MERGED, head, f"landed on {self.target_ref}")
        finally:
            self.workspaces.remove(scratch)

    def claim_fence(self, task_id: str, token: int) -> str | None:
        """Move the fence to ``token`` unless a higher token already holds it. Returns its oid."""
        ref = FENCE_PREFIX + task_id
        blob = self.git.run(
            "hash-object", "-w", "--stdin", input=f"fleet-fence {task_id} {token}\n"
        ).stdout.strip()
        zero = self.git.zero_oid()
        for _ in range(16):
            current = self.git.ref_oid(ref)
            if current is not None:
                if current == blob:
                    return blob
                held = int(self.git.run("cat-file", "-p", current).stdout.split()[2])
                if held > token:
                    return None
            swap = self.git.run("update-ref", ref, blob, current or zero, check=False)
            if swap.returncode == 0:
                return blob
        return None

    def find_landed(self, task_id: str, attempt: int) -> str | None:
        out = self.git.run(
            "log",
            self.target_ref,
            "--format=%H",
            "--all-match",
            f"--grep=^Fleet-Task: {_bre_escape(task_id)}$",
            f"--grep=^Fleet-Attempt: {attempt}$",
            "-n",
            "1",
        ).stdout.strip()
        return out or None

    def verify(self, path: Path) -> tuple[bool, str]:
        if not self.verify_command:
            return True, "no verify command configured"
        try:
            proc = subprocess.run(
                list(self.verify_command),
                cwd=str(path),
                capture_output=True,
                text=True,
                timeout=self.verify_timeout_s,
            )
        except subprocess.TimeoutExpired:
            return False, f"verify command timed out after {self.verify_timeout_s:.0f}s"
        except OSError as exc:
            return False, f"verify command could not start: {exc}"
        output = _tail(proc.stdout + "\n" + proc.stderr)
        return proc.returncode == 0, output
