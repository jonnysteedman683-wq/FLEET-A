"""Thin, strict wrapper around the git CLI."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .errors import GitError


class Git:
    def __init__(self, repo: Path, *, name: str = "FLEET-A", email: str = "fleet-a@localhost", timeout_s: float = 300.0) -> None:
        self.repo = Path(repo).resolve()
        self.name = name
        self.email = email
        self.timeout_s = timeout_s
        self._env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "GIT_EDITOR": "true"}

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
                env=self._env,
                timeout=timeout or self.timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {' '.join(args)} timed out") from exc
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()[-2000:]}")
        return proc

    def rev_parse(self, ref: str, cwd: Path | None = None) -> str:
        return self.run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=cwd).stdout.strip()

    def ref_oid(self, ref: str) -> str | None:
        proc = self.run("rev-parse", "--verify", "--quiet", ref, check=False)
        return proc.stdout.strip() if proc.returncode == 0 else None

    def zero_oid(self) -> str:
        fmt = self.run("rev-parse", "--show-object-format", check=False).stdout.strip()
        return "0" * (64 if fmt == "sha256" else 40)
