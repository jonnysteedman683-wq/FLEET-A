"""Thin, strict wrapper around the git CLI."""

from __future__ import annotations

import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Iterator, Sequence

from .errors import GitError


class Git:
    def __init__(
        self,
        repo: Path,
        *,
        name: str = "FLEET-A",
        email: str = "fleet-a@localhost",
        timeout_s: float = 300.0,
        tree_attributes: bool = True,
    ) -> None:
        self.repo = Path(repo).resolve()
        self.name = name
        self.email = email
        self.timeout_s = timeout_s
        self.env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
            "GIT_EDITOR": "true",
            # refs/replace and info/grafts are shared with worker worktrees and would let one swap
            # the content or parents of a reviewed commit: read every object as it really is.
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_GRAFT_FILE": os.devnull,
        }
        if not tree_attributes:
            self._ignore_tree_attributes()

    def run(
        self,
        *args: str,
        cwd: Path | None = None,
        input: str | None = None,
        check: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        argv = [
            "git",
            "-c", f"user.name={self.name}",
            "-c", f"user.email={self.email}",
            "-c", "commit.gpgsign=false",
            "-c", "core.quotePath=false",
            # Worker sessions share this repository's hooks and config through their worktrees.
            # Never run a hook or an fsmonitor program one of them may have planted.
            "-c", "core.hooksPath=/dev/null",
            "-c", "core.fsmonitor=false",
            *args,
        ]
        try:
            proc = subprocess.run(
                argv,
                cwd=str(cwd or self.repo),
                input=input,
                capture_output=True,
                text=True,
                # Paths are bytes to git; keep any that aren't valid UTF-8 round-trippable.
                errors="surrogateescape",
                env=self.env,
                timeout=timeout or self.timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {' '.join(args)} timed out") from exc
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()[-2000:]}")
        return proc

    def _ignore_tree_attributes(self) -> None:
        """Read ``.gitattributes`` from the empty tree instead of the files being merged.

        A worker's ``.gitattributes`` could otherwise select a ``filter``, ``merge`` or ``diff``
        driver that the landing repository's or the user's global config defines (``filter.lfs``
        is common), and that program would then run while the integrator rebases. With
        ``GIT_ATTR_SOURCE`` set to the empty tree no in-tree attribute applies; files are merged
        and checked out as their blobs. It needs git 2.42 or later, which is checked here because
        older versions ignore the variable silently.
        """
        version = self.run("version").stdout.strip()
        if git_version(version) < (2, 42):
            raise GitError(f"the integrator needs git 2.42 or later to ignore in-tree attributes, found {version!r}")
        empty_tree = self.run("hash-object", "-t", "tree", "--stdin", input="").stdout.strip()
        self.env["GIT_ATTR_SOURCE"] = empty_tree

    def iter_blobs(self, oids: Sequence[str]) -> Iterator[bytes]:
        """Each object's content for ``oids``, in order, streamed from one ``git cat-file --batch``.

        Only one object is held in memory at a time. A feeder thread writes the ids so a large
        request can't deadlock against a full stdout pipe.
        """
        proc = subprocess.Popen(
            ["git", "cat-file", "--batch"],
            cwd=str(self.repo),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=self.env,
        )
        assert proc.stdin is not None and proc.stdout is not None
        stdin = proc.stdin

        def feed() -> None:
            try:
                for oid in oids:
                    stdin.write(f"{oid}\n".encode())
            except (BrokenPipeError, ValueError):
                pass
            finally:
                try:
                    stdin.close()
                except (BrokenPipeError, ValueError):
                    pass

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        try:
            for oid in oids:
                header = proc.stdout.readline().split()
                if len(header) != 3 or header[0].decode() != oid:
                    raise GitError(f"git cat-file --batch: unexpected answer for {oid}: {header!r}")
                size = int(header[2])
                data = proc.stdout.read(size)
                if len(data) != size or proc.stdout.read(1) != b"\n":
                    raise GitError(f"git cat-file --batch: short read for {oid}")
                yield data
        finally:
            if proc.poll() is None and feeder.is_alive():
                proc.kill()  # stopped early: unblock the feeder
            feeder.join(timeout=self.timeout_s)
            proc.stdout.close()
            try:
                proc.wait(timeout=self.timeout_s)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def rev_parse(self, ref: str, cwd: Path | None = None) -> str:
        return self.run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=cwd).stdout.strip()

    def ref_oid(self, ref: str) -> str | None:
        proc = self.run("rev-parse", "--verify", "--quiet", ref, check=False)
        return proc.stdout.strip() if proc.returncode == 0 else None

    def zero_oid(self) -> str:
        fmt = self.run("rev-parse", "--show-object-format", check=False).stdout.strip()
        return "0" * (64 if fmt == "sha256" else 40)


def git_version(output: str) -> tuple[int, int]:
    """(major, minor) from ``git version`` output, or (0, 0) if it can't be read.

    Read from the start: Apple's git appends "(Apple Git-154)", whose number is not git's version.
    """
    match = re.match(r"git version (\d+)\.(\d+)", output.strip())
    return (int(match[1]), int(match[2])) if match else (0, 0)
