"""Isolated git worktrees, one per lease.

Each worker attempt gets a fresh worktree on a branch named after its run, task, attempt and
fencing token (``fleet/<run>/<task>/a<attempt>-t<token>``), under ``<root>/<run>/``. Two leases can
never share a branch or a directory, even across runs of the same plan in one repository: tokens
restart at 1 in every run, so without the run id a new run collides with branches an earlier run
left behind. Reviewers and the integrator check out detached worktrees of a specific commit.

Workers and reviewers run a model with ``Bash``, so their worktrees never belong to the repository
the integrator lands in: a worktree shares refs, config and hooks with its repository.
``create_worker_clone`` makes a separate bare clone for them, and their ``Workspaces`` has that
repository as its ``upstream``, fetching the target branch from it before each attempt. The
integrator keeps its own ``Workspaces`` on the landing repository, fetches each reviewed commit out
of the worker clone by id, and runs the verify command in a plain export of the rebased tree, so
no program a worker could configure ever runs while the integrator merges.
"""

from __future__ import annotations

import shutil
import stat
import threading
from dataclasses import dataclass
from pathlib import Path

from .errors import FleetError, GitError
from .git import Git
from .model import is_ref_safe_id


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str | None
    base: str


def create_worker_clone(landing: Git, path: Path) -> Git:
    """A fresh bare clone of ``landing`` at ``path`` for worker and reviewer worktrees.

    Anything already at ``path`` is replaced, so no config, hook or ref a previous run's workers
    left there carries over. ``--no-local`` copies objects instead of hard-linking them: a
    hard-linked object file is the same inode as the landing repository's, writable by anyone who
    can ``chmod`` it. The clone keeps no remote, so nothing in it names the landing repository.
    """
    path = Path(path).resolve()
    if path == landing.repo or path in landing.repo.parents or landing.repo in path.parents:
        raise FleetError(f"worker clone {path} must be outside the repository {landing.repo}")
    if path.exists():
        shutil.rmtree(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    landing.run("clone", "-q", "--bare", "--no-local", "--no-tags", str(landing.repo), str(path))
    clone = Git(path, name=landing.name, email=landing.email, timeout_s=landing.timeout_s)
    clone.run("remote", "remove", "origin")
    return clone


class Workspaces:
    def __init__(self, git: Git, root: Path, *, upstream: Git | None = None) -> None:
        self.git = git
        self.upstream = upstream
        self._sync = threading.Lock()  # runners fetch concurrently; one ref update at a time
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def target_tip(self, target_branch: str) -> str:
        """The target branch's tip, fetched from ``upstream`` first when there is one.

        The forced refspec also resets the branch if a worker moved it inside the worker clone.
        """
        ref = f"refs/heads/{target_branch}"
        if self.upstream is not None:
            with self._sync:
                self.git.run(
                    "fetch", "-q", "--no-tags", "--no-write-fetch-head", str(self.upstream.repo), f"+{ref}:{ref}"
                )
                return self.git.rev_parse(ref)
        return self.git.rev_parse(ref)

    def create_worktree(
        self, run_id: str, task_id: str, attempt: int, token: int, target_branch: str
    ) -> Worktree:
        if not is_ref_safe_id(run_id, max_len=128):
            raise FleetError(f"worktree needs a valid run id, got {run_id!r}")
        base = self.target_tip(target_branch)
        branch = f"fleet/{run_id}/{task_id}/a{attempt}-t{token}"
        path = self._inside_root(Path(run_id) / f"{task_id}-a{attempt}-t{token}")
        self._clear(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.git.run("worktree", "add", "-q", "-b", branch, str(path), base)
        return Worktree(path=path, branch=branch, base=base)

    def checkout_detached(self, commit: str, name: str, base: str | None = None) -> Worktree:
        """Check out ``commit`` at ``<root>/<name>``; callers put their run id in ``name``."""
        path = self._inside_root(Path(name))
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

    def import_commit(self, source: Git, commit: str) -> bool:
        """Fetch ``commit`` and its history from ``source`` by id. True once it is here as a commit.

        Fetching from a repository only runs ``upload-pack`` there, which git keeps safe against
        that repository's own config, and objects are addressed by hash, so the commit that
        arrives is exactly the one that was reviewed.
        """
        fetch = self.git.run(
            "fetch", "-q", "--no-tags", "--no-write-fetch-head", str(source.repo), commit, check=False
        )
        if fetch.returncode != 0:
            return False
        return self.git.run("cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode == 0

    def export_tree(self, commit: str, name: str) -> Path:
        """Write ``commit``'s files to ``<root>/<name>``: a plain directory with no ``.git``.

        Read straight from the object database, so no attribute, filter or export rule applies
        and the files are exactly the commit's. Gitlinks (submodules) are left out.
        """
        path = self._inside_root(Path(name))
        self._clear(path)
        path.mkdir(parents=True)
        listing = self.git.run("ls-tree", "-r", "-z", "--full-tree", commit).stdout
        entries = []
        for record in listing.split("\0"):
            if not record:
                continue
            meta, rel = record.split("\t", 1)
            mode, kind, oid = meta.split()
            if kind == "blob":
                entries.append((mode, oid, rel))
        if not entries:
            return path
        out, pos = self.git.read_blobs([oid for _, oid, _ in entries]), 0
        for mode, oid, rel in entries:
            header_end = out.index(b"\n", pos)
            size = int(out[pos:header_end].split()[2])
            data = out[header_end + 1 : header_end + 1 + size]
            pos = header_end + 1 + size + 1
            target = path / rel
            if path not in target.parent.resolve().parents and target.parent.resolve() != path:
                raise FleetError(f"tree entry {rel!r} of {commit} is outside the export")
            target.parent.mkdir(parents=True, exist_ok=True)
            if mode == "120000":
                target.symlink_to(data.decode())
            else:
                target.write_bytes(data)
                if mode == "100755":
                    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return path

    def changed_files(self, base: str, head: str) -> list[str]:
        out = self.git.run("diff", "--name-only", "--no-renames", "-z", base, head).stdout
        return sorted(p for p in out.split("\0") if p)

    def remove(self, worktree: Worktree) -> None:
        self.git.run("worktree", "remove", "--force", str(worktree.path), check=False)
        if worktree.path.exists():
            shutil.rmtree(worktree.path, ignore_errors=True)
        self.git.run("worktree", "prune", check=False)

    def _inside_root(self, rel: Path) -> Path:
        """``<root>/<rel>``, refusing anything that would land outside the worktrees root.

        An absolute ``rel`` (e.g. a name built from an empty run id, "/review-...") would replace
        the root entirely under pathlib's join, and ``..`` would climb out of it; _clear then
        deletes whatever is there, so both are rejected before any filesystem change.
        """
        path = (self.root / rel).resolve()
        if rel.is_absolute() or path == self.root or self.root not in path.parents:
            raise FleetError(f"worktree path {str(rel)!r} is outside the worktrees root")
        return path

    def _clear(self, path: Path) -> None:
        if path.exists():
            self.git.run("worktree", "remove", "--force", str(path), check=False)
            shutil.rmtree(path, ignore_errors=True)
            self.git.run("worktree", "prune", check=False)

    @staticmethod
    def _error(what: str, stderr: str) -> Exception:
        return GitError(f"{what} failed: {stderr.strip()[-2000:]}")
