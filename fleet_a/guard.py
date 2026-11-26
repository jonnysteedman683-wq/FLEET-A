"""Detect a session that tampered with the shared repository instead of its own worktree.

Workers run ``Bash`` in a git worktree, and a worktree shares refs, config and hooks with the
repository the integrator lands in. The ``Bash(git update-ref *)``-style deny rules match command
text, so a session can still move the target branch (``sh -c 'git update-ref ...'``, a script, a
write under ``.git/``) or change what git runs (a hook, ``core.fsmonitor``, a filter driver in
``config``, ``info/attributes``). Either one would land code without the fence, review, scope
check or verify.

``Git`` already disables hooks and fsmonitor for every command the fleet runs. ``RepoGuard`` is the
second line: it snapshots the shared state when the fleet starts and keeps the one target tip the
integrator itself produced. The worker checks it after every session, and the merge queue checks it
under the same lock it lands with, so a change from any other source blocks the task for an
operator instead of being built on.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

from .git import Git

# Files in the common git directory that change which programs git runs or which objects it sees.
_WATCHED_FILES = ("config", "info/attributes", "objects/info/alternates")


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


class RepoGuard:
    def __init__(self, git: Git, target_branch: str) -> None:
        self.git = git
        self.target_ref = f"refs/heads/{target_branch}"
        common = git.run("rev-parse", "--git-common-dir").stdout.strip()
        self.git_dir = (git.repo / common).resolve()
        self.lock = threading.RLock()
        self._files = self._snapshot_files()
        self._hooks = self._snapshot_hooks()
        self._tip = git.ref_oid(self.target_ref)

    @property
    def expected_tip(self) -> str | None:
        return self._tip

    def _snapshot_files(self) -> dict[str, str | None]:
        return {name: _digest(self.git_dir / name) for name in _WATCHED_FILES}

    def _snapshot_hooks(self) -> dict[str, str | None]:
        hooks = self.git_dir / "hooks"
        if not hooks.is_dir():
            return {}
        return {p.name: _digest(p) for p in sorted(hooks.iterdir()) if p.is_file() or p.is_symlink()}

    def check(self) -> str | None:
        """Describe every change made outside the integrator since the snapshot, or None."""
        with self.lock:
            problems = []
            tip = self.git.ref_oid(self.target_ref)
            if tip != self._tip:
                problems.append(f"{self.target_ref} is {tip} but the integrator last left it at {self._tip}")
            files = self._snapshot_files()
            for name in _WATCHED_FILES:
                if files[name] != self._files[name]:
                    problems.append(f"{self.git_dir / name} changed")
            hooks = self._snapshot_hooks()
            for name in sorted(set(hooks) | set(self._hooks)):
                if hooks.get(name) != self._hooks.get(name):
                    problems.append(f"hook {name} added, changed or removed")
            if not problems:
                return None
            return "shared repository changed outside the integrator: " + "; ".join(problems)

    def landed(self, old: str, new: str) -> None:
        """Record a landing the integrator made. Call with ``lock`` held, right after the update."""
        with self.lock:
            if old == self._tip:
                self._tip = new
