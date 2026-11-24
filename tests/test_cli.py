"""The command line, the shipped example plan and config, and `run` against a stand-in claude."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

from conftest import git

from fleet_a.cli import main

ROOT = Path(__file__).resolve().parents[1]

FAKE_CLAUDE = r'''#!PYTHON
import json, os, re, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
schema = json.loads(args[args.index("--json-schema") + 1]) if "--json-schema" in args else {}
props = schema.get("properties", {})
if "approve" in props:
    out = {"approve": True, "summary": "looks right", "findings": []}
elif "status" in props:
    task = re.match(r"TASK (\S+):", prompt).group(1)
    os.makedirs("src", exist_ok=True)
    with open(f"src/{task.lower()}.py", "w") as fh:
        fh.write(f"NAME = {task!r}\n")
    out = {"status": "done", "summary": f"added src/{task.lower()}.py", "blocker": ""}
else:
    out = {"points": []}
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "ok",
                  "total_cost_usd": 0.01, "structured_output": out}))
'''


def test_validate_example_plan(capsys):
    assert main(["validate", str(ROOT / "examples" / "plan.json"), "--config", str(ROOT / "examples" / "fleet.json")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["plan"] == "rate-limiter" and out["fits_run_budget"]
    assert out["critical_path"][0] == out["topological_order"][0]


def test_validate_rejects_bad_plan(tmp_path, capsys):
    bad = tmp_path / "plan.json"
    bad.write_text(json.dumps({"id": "p", "tasks": [{"id": "a", "title": "t", "description": "d", "write_scope": ["src/"], "deps": ["a"]}]}))
    assert main(["validate", str(bad)]) == 1
    assert "depends on itself" in capsys.readouterr().err


def test_simulate_command(capsys):
    assert main(["simulate", "--runs", "3", "--tasks", "10"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["failed"] == 0 and len(out["results"]) == 3


def test_run_end_to_end_with_stand_in_claude(repo, tmp_path, capsys):
    binary = tmp_path / "claude"
    binary.write_text(FAKE_CLAUDE.replace("PYTHON", sys.executable))
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "id": "demo",
                "goal": "two modules and a consumer",
                "tasks": [
                    {"id": "a", "title": "A", "description": "add a", "write_scope": ["src/a.py"]},
                    {"id": "b", "title": "B", "description": "add b", "write_scope": ["src/b.py"]},
                    {"id": "c", "title": "C", "description": "add c", "write_scope": ["src/c.py"], "deps": ["a", "b"]},
                ],
            }
        )
    )
    conf = tmp_path / "fleet.json"
    conf.write_text(
        json.dumps(
            {
                "run_budget_usd": "20",
                "verify_command": [sys.executable, "-m", "compileall", "-q", "src"],
                "poll_interval_s": 0.05,
                "agents": [{"role": "worker", "count": 2}, {"role": "reviewer"}, {"role": "integrator"}],
            }
        )
    )
    events = tmp_path / "events.jsonl"
    code = main(
        [
            "run", "--repo", str(repo), "--plan", str(plan), "--config", str(conf),
            "--events", str(events), "--worktrees", str(tmp_path / "wt"),
            "--claude", str(binary), "--timeout", "120",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["states"] == {"done": 3} and out["spent_usd"] > 0
    assert {"src/a.py", "src/b.py", "src/c.py"} <= set(git(repo, "ls-tree", "-r", "--name-only", "main").split())
    assert main(["verify-log", str(events)]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["chain"] == "ok" and verified["tasks"] == {"done": 3}


def test_plan_command_with_stand_in_planner(repo, tmp_path, capsys):
    binary = tmp_path / "claude"
    binary.write_text(
        "#!" + sys.executable + "\n"
        "import json, sys\n"
        "sys.stdin.read()\n"
        "plan = {'id': 'x', 'goal': 'g', 'tasks': [{'id': 't1', 'title': 'T', 'description': 'D',"
        " 'acceptance': ['works'], 'deps': [], 'write_scope': ['src/']}]}\n"
        "print(json.dumps({'is_error': False, 'result': '', 'total_cost_usd': 0.5, 'structured_output': plan}))\n"
    )
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    out_path = tmp_path / "plan.json"
    assert main(["plan", "--repo", str(repo), "--goal", "Add a thing", "--id", "thing", "--out", str(out_path), "--claude", str(binary)]) == 0
    plan = json.loads(out_path.read_text())
    assert plan["id"] == "thing" and plan["goal"] == "Add a thing" and plan["tasks"][0]["write_scope"] == ["src/"]
    assert "planned 1 tasks" in capsys.readouterr().err
