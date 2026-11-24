from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleet_a.config import AgentPool, FleetConfig  # noqa: E402
from fleet_a.model import Plan, Role, TaskSpec, usd_to_micros  # noqa: E402


def spec(task_id: str, scope=("src/",), deps=(), **kw) -> TaskSpec:
    return TaskSpec(
        id=task_id,
        title=f"task {task_id}",
        description=f"do {task_id}",
        write_scope=tuple(scope),
        deps=tuple(deps),
        **kw,
    )


def plan_of(*specs: TaskSpec, plan_id: str = "p1") -> Plan:
    return Plan(id=plan_id, goal="test goal", tasks=tuple(specs))


def config(**kw) -> FleetConfig:
    base = dict(
        fleet_budget=usd_to_micros("1000"),
        run_budget=usd_to_micros("1000"),
        review_budget=usd_to_micros("0.50"),
        lease_ttl_s={Role.WORKER: 100.0, Role.REVIEWER: 50.0, Role.INTEGRATOR: 50.0},
        agents=(AgentPool(Role.WORKER, 2), AgentPool(Role.REVIEWER, 1), AgentPool(Role.INTEGRATOR, 1)),
    )
    base.update(kw)
    return FleetConfig(**base)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with one commit on main; the primary checkout is detached."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "src").mkdir()
    (root / "src" / "base.py").write_text("BASE = 1\n")
    (root / "README.md").write_text("# demo\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    git(root, "checkout", "-q", "--detach")
    return root
