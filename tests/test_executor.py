"""ClaudeCodeExecutor against a stand-in `claude` binary that records what it was given."""

from __future__ import annotations

import json
import stat
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from fleet_a.config import DEFAULT_MODELS
from fleet_a.executors import ClaudeCodeExecutor, ExecRequest
from fleet_a.prompts import WORK_SCHEMA

FAKE = r'''#!PYTHON
import json, os, sys, time
args = sys.argv[1:]
prompt = sys.stdin.read()
with open(os.environ["FAKE_RECORD"], "w") as fh:
    json.dump({"argv": args, "prompt": prompt, "cwd": os.getcwd()}, fh)
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "sleep":
    time.sleep(60)
if mode == "garbage":
    print("Traceback: not json", file=sys.stderr)
    sys.exit(2)
if mode == "error":
    print(json.dumps({"type": "result", "subtype": "error_max_budget_usd", "is_error": True,
                      "result": "budget exceeded", "total_cost_usd": 0.75}))
    sys.exit(1)
out = {"type": "result", "subtype": "success", "is_error": False, "result": "done",
       "total_cost_usd": 0.25, "session_id": "sess-1"}
if "--json-schema" in args:
    payload = {"status": "done", "summary": "did it", "blocker": ""}
    if mode == "text-json":
        out["result"] = json.dumps(payload)
    elif mode != "no-structured":
        out["structured_output"] = payload
print("progress line that is not the result")
print(json.dumps(out))
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    path = tmp_path / "claude"
    path.write_text(FAKE.replace("PYTHON", sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_RECORD", str(record))
    return path, record


def request(tmp_path: Path, profile="worker", schema=WORK_SCHEMA, budget=1.5, timeout=None) -> ExecRequest:
    prof = DEFAULT_MODELS[profile]
    if timeout is not None:
        prof = replace(prof, timeout_s=timeout)
    return ExecRequest(cwd=tmp_path, prompt="TASK T1: do it", system_append="SYSTEM", profile=prof, budget_usd=budget, json_schema=schema)


def test_argv_and_structured_result(fake, tmp_path, monkeypatch):
    binary, record = fake
    monkeypatch.setenv("FAKE_MODE", "ok")
    result = ClaudeCodeExecutor(str(binary)).run(request(tmp_path), threading.Event())
    assert result.ok and result.cost_usd == 0.25 and result.session_id == "sess-1"
    assert result.structured == {"status": "done", "summary": "did it", "blocker": ""}
    seen = json.loads(record.read_text())
    argv = seen["argv"]
    assert seen["prompt"] == "TASK T1: do it" and Path(seen["cwd"]) == tmp_path
    flags = dict(zip(argv[1::1], argv[2::1]))
    assert argv[0] == "-p"
    assert flags["--model"] == "claude-sonnet-5-5" and flags["--effort"] == "medium"
    assert flags["--max-budget-usd"] == "1.5000"
    assert flags["--permission-mode"] == "acceptEdits" and flags["--permission-prompts"] == "none"
    assert flags["--fallback-model"] == "claude-sonnet-5"
    assert "Bash(git push *)" in flags["--disallowedTools"]
    assert json.loads(flags["--json-schema"]) == WORK_SCHEMA
    assert flags["--append-system-prompt"] == "SYSTEM"
    assert "--no-session-persistence" in argv and "--strict-mcp-config" in argv
    assert "--dangerously-skip-permissions" not in argv


def test_triage_profile_disables_tools(tmp_path):
    argv = ClaudeCodeExecutor().argv(request(tmp_path, profile="triage", schema=None))
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--model") + 1] == "claude-haiku-5-5"
    assert "--json-schema" not in argv


@pytest.mark.parametrize(
    "mode, ok, cost, error",
    [
        ("text-json", True, 0.25, None),
        ("no-structured", False, 0.25, "without structured output"),
        ("error", False, 0.75, "error_max_budget_usd"),
        ("garbage", False, 0.0, "no JSON result"),
    ],
)
def test_result_parsing(fake, tmp_path, monkeypatch, mode, ok, cost, error):
    binary, _ = fake
    monkeypatch.setenv("FAKE_MODE", mode)
    result = ClaudeCodeExecutor(str(binary)).run(request(tmp_path), threading.Event())
    assert result.ok is ok and result.cost_usd == cost
    if error:
        assert error in result.error


def test_timeout_kills_and_charges_full_budget(fake, tmp_path, monkeypatch):
    binary, _ = fake
    monkeypatch.setenv("FAKE_MODE", "sleep")
    start = time.monotonic()
    result = ClaudeCodeExecutor(str(binary), poll_s=0.05, kill_grace_s=1).run(request(tmp_path, timeout=0.5), threading.Event())
    assert time.monotonic() - start < 10
    assert not result.ok and "timed out" in result.error and result.cost_usd == 1.5


def test_cancel_kills_session(fake, tmp_path, monkeypatch):
    binary, _ = fake
    monkeypatch.setenv("FAKE_MODE", "sleep")
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    start = time.monotonic()
    result = ClaudeCodeExecutor(str(binary), poll_s=0.05, kill_grace_s=1).run(request(tmp_path), cancel)
    assert time.monotonic() - start < 10
    assert not result.ok and "lease lost" in result.error


def test_missing_binary_and_zero_budget(tmp_path):
    result = ClaudeCodeExecutor("/nonexistent/claude").run(request(tmp_path), threading.Event())
    assert not result.ok and not result.retryable
    result = ClaudeCodeExecutor("/nonexistent/claude").run(request(tmp_path, budget=0), threading.Event())
    assert not result.ok and "no budget" in result.error


def test_planner_and_reviewer_profiles_are_read_only():
    for name in ("planner", "reviewer"):
        prof = DEFAULT_MODELS[name]
        assert prof.model == "claude-opus-5-5" and prof.effort == "high"
        assert "Edit" in prof.disallowed_tools and "Write" in prof.disallowed_tools
        assert "Edit" not in prof.allowed_tools and "Write" not in prof.allowed_tools
