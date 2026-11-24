"""Planner: one read-only model session that turns a goal into a validated Plan."""

from __future__ import annotations

import re
import threading
from pathlib import Path

from .config import FleetConfig
from .dag import TaskGraph
from .errors import FleetError, PlanError
from .executors import ExecRequest, Executor
from .model import Plan, micros_to_usd
from .prompts import PLANNER_SYSTEM, plan_schema, planner_prompt


def slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()[:limit].strip("-")
    return slug or "plan"


def plan_goal(
    goal: str,
    repo: Path,
    executor: Executor,
    config: FleetConfig,
    plan_id: str | None = None,
) -> tuple[Plan, float]:
    """Return (plan, cost_usd). Raises PlanError if the model's plan does not validate."""
    if not goal.strip():
        raise PlanError("goal must not be empty")
    tags = config.plannable_capabilities()
    result = executor.run(
        ExecRequest(
            cwd=Path(repo),
            prompt=planner_prompt(goal, tags),
            system_append=PLANNER_SYSTEM,
            profile=config.models["planner"],
            budget_usd=micros_to_usd(config.planner_budget),
            json_schema=plan_schema(tags),
        ),
        threading.Event(),
    )
    if not result.ok or result.structured is None:
        raise FleetError(f"planner session failed: {result.error}")
    data = dict(result.structured)
    data["id"] = plan_id or data.get("id") or slugify(goal)
    data["goal"] = goal
    plan = Plan.from_dict(data)
    TaskGraph(plan.tasks)
    gaps = config.capability_gaps(plan.tasks)
    if gaps:
        raise PlanError("planner produced tasks no configured agent can take: " + "; ".join(gaps))
    return plan, result.cost_usd
