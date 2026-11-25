"""Isolated git worktrees, one per lease.

Each worker attempt gets a fresh worktree on a branch named after its run, task, attempt and
fencing token (``fleet/<run>/<task>/a<attempt>-t<token>``), under ``<root>/<run>/``. Two leases can
never share a branch or a directory, even across runs of the same plan in one repository: tokens
restart at 1 in every run, so without the run id a new run collides with branches an earlier run
left behind. Reviewers and the integrator check out detached worktrees of a specific commit.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .errors import GitError
from .git import Git


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str | None
    base: str


class Workspaces:
    def __init__(self, git: Git, root: Path) -> None:
        self.git = git
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def create_worktree(
        self, run_id: str, task_id: str, attempt: int, token: int, target_branch: str
    ) -> Worktree:
        base = self.git.rev_parse(f"refs/heads/{target_branch}")
        branch = f"fleet/{run_id}/{task_id}/a{attempt}-t{token}"
        path = self.root / run_id / f"{task_id}-a{attempt}-t{token}"
        self._clear(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.git.run("worktree", "add", "-q", "-b", branch, str(path), base)
        return Worktree(path=path, branch=branch, base=base)

    def checkout_detached(self, commit: str, name: str, base: str | None = None) -> Worktree:
        """Check out ``commit`` at ``<root>/<name>``; callers put their run id in ``name``."""
        path = self.root / name
        self._clear(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.git.run("worktree", "add", "-q", "--detach", str(path), commit)
        return Worktree(path=path, branch=None, base=base or commit)

    def squash_commit(self, worktree: Worktree, message: str) -> str | None:
        """Collapse everything in the worktree into one commit on top of its base.

        Returns the new commit, or None if the tree is identical to the base.
        """
        cwd = worktree.path
        self.git.run("reset", "-q", "--soft", worktree.base, cwd=cwd)
        self.git.run("add", "-A", cwd=cwd)
        diff = self.git.run("diff", "--cached", "--quiet", cwd=cwd, check=False)
        if diff.returncode == 0:
            return None
        if diff.returncode != 1:
            raise self._error("git diff --cached --quiet", diff.stderr)
        self.git.run("commit", "-q", "--no-verify", "-m", message, cwd=cwd)
        return self.git.rev_parse("HEAD", cwd=cwd)

    def changed_files(self, base: str, head: str) -> list[str]:
        out = self.git.run("diff", "--name-only", "--no-renames", "-z", base, head).stdout
        return sorted(p for p in out.split("\0") if p)

    def remove(self, worktree: Worktree) -> None:
        self.git.run("worktree", "remove", "--force", str(worktree.path), check=False)
        if worktree.path.exists():
            shutil.rmtree(worktree.path, ignore_errors=True)
        self.git.run("worktree", "prune", check=False)

    def _clear(self, path: Path) -> None:
        if path.exists():
            self.git.run("worktree", "remove", "--force", str(path), check=False)
            shutil.rmtree(path, ignore_errors=True)
            self.git.run("worktree", "prune", check=False)

    @staticmethod
    def _error(what: str, stderr: str) -> Exception:
        return GitError(f"{what} failed: {stderr.strip()[-2000:]}")
