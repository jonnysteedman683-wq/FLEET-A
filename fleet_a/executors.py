"""Executors run one model session for one phase of one task.

``ClaudeCodeExecutor`` launches headless Claude Code (``claude -p``) in the lease's worktree with
the role's model, effort, tool allow/deny lists and a hard dollar cap equal to the phase's
reservation. ``CallableExecutor`` adapts a Python function (tests, deterministic tasks).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .config import ModelProfile


@dataclass(frozen=True)
class ExecRequest:
    cwd: Path
    prompt: str
    system_append: str
    profile: ModelProfile
    budget_usd: float
    json_schema: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ExecResult:
    ok: bool
    cost_usd: float = 0.0
    text: str = ""
    structured: dict[str, Any] | None = None
    error: str | None = None
    retryable: bool = True
    session_id: str | None = None


class Executor(Protocol):
    def run(self, request: ExecRequest, cancel: threading.Event) -> ExecResult: ...


class CallableExecutor:
    def __init__(self, fn: Callable[[ExecRequest], ExecResult]) -> None:
        self._fn = fn

    def run(self, request: ExecRequest, cancel: threading.Event) -> ExecResult:
        return self._fn(request)


class ClaudeCodeExecutor:
    """Runs ``claude -p`` with the prompt on stdin and parses its single JSON result."""

    def __init__(
        self,
        binary: str = "claude",
        *,
        env: Mapping[str, str] | None = None,
        poll_s: float = 0.5,
        kill_grace_s: float = 5.0,
    ) -> None:
        self.binary = binary
        self.env = dict(env) if env is not None else None
        self.poll_s = poll_s
        self.kill_grace_s = kill_grace_s

    def argv(self, request: ExecRequest) -> list[str]:
        p = request.profile
        argv = [
            self.binary,
            "-p",
            "--output-format", "json",
            "--model", p.model,
            "--effort", p.effort,
            "--max-budget-usd", f"{request.budget_usd:.4f}",
            "--permission-mode", p.permission_mode,
            "--permission-prompts", "none",
            "--no-session-persistence",
            "--setting-sources", "project",
            "--strict-mcp-config",
            "--exclude-dynamic-system-prompt-sections",
            "--append-system-prompt", request.system_append,
        ]
        if p.fallback_model:
            argv += ["--fallback-model", p.fallback_model]
        if p.tools is not None:
            argv += ["--tools", ",".join(p.tools)]
        if p.allowed_tools:
            argv += ["--allowedTools", ",".join(p.allowed_tools)]
        if p.disallowed_tools:
            argv += ["--disallowedTools", ",".join(p.disallowed_tools)]
        if request.json_schema is not None:
            argv += ["--json-schema", json.dumps(request.json_schema, separators=(",", ":"))]
        return argv

    def run(self, request: ExecRequest, cancel: threading.Event) -> ExecResult:
        if request.budget_usd <= 0:
            return ExecResult(ok=False, error="no budget reserved for this call", retryable=False)
        env = {**os.environ, **(self.env or {})}
        try:
            proc = subprocess.Popen(
                self.argv(request),
                cwd=str(request.cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            return ExecResult(ok=False, error=f"cannot start {self.binary}: {exc}", retryable=False)

        deadline = time.monotonic() + request.profile.timeout_s
        pending: str | None = request.prompt
        while True:
            try:
                out, err = proc.communicate(input=pending, timeout=self.poll_s)
                break
            except subprocess.TimeoutExpired:
                pending = None
                if cancel.is_set():
                    self._kill(proc)
                    return ExecResult(ok=False, cost_usd=request.budget_usd, error="cancelled: lease lost")
                if time.monotonic() >= deadline:
                    self._kill(proc)
                    return ExecResult(
                        ok=False,
                        cost_usd=request.budget_usd,
                        error=f"timed out after {request.profile.timeout_s:.0f}s",
                    )
        return self.parse(proc.returncode, out, err, request)

    def _kill(self, proc: subprocess.Popen[str]) -> None:
        for sig, wait in ((signal.SIGTERM, self.kill_grace_s), (signal.SIGKILL, None)):
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                break
            try:
                proc.wait(timeout=wait)
                break
            except subprocess.TimeoutExpired:
                continue
        try:
            proc.communicate(timeout=self.kill_grace_s)
        except (subprocess.TimeoutExpired, ValueError):
            pass

    @staticmethod
    def parse(returncode: int, stdout: str, stderr: str, request: ExecRequest) -> ExecResult:
        data: Any = None
        text = stdout.strip()
        if text:
            try:
                data = json.loads(text)
            except ValueError:
                last = text.splitlines()[-1]
                try:
                    data = json.loads(last)
                except ValueError:
                    data = None
        if not isinstance(data, dict):
            detail = (stderr.strip() or stdout.strip())[-1000:]
            return ExecResult(ok=False, error=f"exit {returncode}, no JSON result: {detail}")

        cost = data.get("total_cost_usd")
        cost_usd = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else 0.0
        result_text = data.get("result") if isinstance(data.get("result"), str) else ""
        session_id = data.get("session_id") if isinstance(data.get("session_id"), str) else None
        structured = data.get("structured_output")
        if structured is None and request.json_schema is not None and result_text:
            try:
                parsed = json.loads(result_text)
                structured = parsed if isinstance(parsed, dict) else None
            except ValueError:
                structured = None
        if not isinstance(structured, dict):
            structured = None

        if data.get("is_error") or returncode != 0:
            subtype = data.get("subtype", "error")
            return ExecResult(
                ok=False,
                cost_usd=cost_usd,
                text=result_text,
                structured=structured,
                error=f"{subtype}: {result_text[-1000:] or stderr.strip()[-1000:]}",
                session_id=session_id,
            )
        if request.json_schema is not None and structured is None:
            return ExecResult(
                ok=False,
                cost_usd=cost_usd,
                text=result_text,
                error="session ended without structured output matching the schema",
                session_id=session_id,
            )
        return ExecResult(ok=True, cost_usd=cost_usd, text=result_text, structured=structured, session_id=session_id)
