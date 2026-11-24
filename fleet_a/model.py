"""Data model: money units, roles, task states, task specs, plans and the messages agents exchange
with the coordinator."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from typing import Any, Mapping

from .errors import PlanError
from .scopes import normalize_scope

MICROS_PER_USD = 1_000_000
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


# --------------------------------------------------------------------------------------------
# Money: all accounting is in integer micro-dollars so sums are exact.
# --------------------------------------------------------------------------------------------


def usd_to_micros(value: object) -> int:
    """Convert a non-negative dollar amount to integer micro-dollars (half-up rounding)."""
    if isinstance(value, bool):
        raise ValueError(f"not a dollar amount: {value!r}")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"not a dollar amount: {value!r}") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"dollar amount must be finite and >= 0: {value!r}")
    return int((amount * MICROS_PER_USD).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def micros_to_usd(micros: int) -> float:
    return micros / MICROS_PER_USD


def format_usd(micros: int) -> str:
    return f"${micros / MICROS_PER_USD:,.4f}"


# --------------------------------------------------------------------------------------------
# Roles and task states
# --------------------------------------------------------------------------------------------


class Role(str, Enum):
    PLANNER = "planner"
    WORKER = "worker"
    REVIEWER = "reviewer"
    INTEGRATOR = "integrator"


class TaskState(str, Enum):
    PENDING = "pending"  # waiting for dependencies
    READY = "ready"  # dependencies done; waiting for a worker
    RUNNING = "running"  # a worker holds the lease
    AWAITING_REVIEW = "awaiting_review"  # attempt committed; waiting for a reviewer
    REVIEWING = "reviewing"  # a reviewer holds the lease
    AWAITING_MERGE = "awaiting_merge"  # approved; in the merge queue
    MERGING = "merging"  # the integrator holds the lease
    DONE = "done"  # landed on the target branch (terminal)
    FAILED = "failed"  # attempts exhausted or non-retryable failure (terminal)
    CANCELLED = "cancelled"  # by an operator or an upstream failure (terminal)
    BLOCKED = "blocked"  # needs a human decision


TERMINAL_STATES = frozenset({TaskState.DONE, TaskState.FAILED, TaskState.CANCELLED})

QUEUE_STATE_FOR_ROLE: Mapping[Role, TaskState] = {
    Role.WORKER: TaskState.READY,
    Role.REVIEWER: TaskState.AWAITING_REVIEW,
    Role.INTEGRATOR: TaskState.AWAITING_MERGE,
}
ACTIVE_STATE_FOR_ROLE: Mapping[Role, TaskState] = {
    Role.WORKER: TaskState.RUNNING,
    Role.REVIEWER: TaskState.REVIEWING,
    Role.INTEGRATOR: TaskState.MERGING,
}
ROLE_FOR_QUEUE_STATE: Mapping[TaskState, Role] = {v: k for k, v in QUEUE_STATE_FOR_ROLE.items()}
ROLE_FOR_ACTIVE_STATE: Mapping[TaskState, Role] = {v: k for k, v in ACTIVE_STATE_FOR_ROLE.items()}

# A task holds its write-scope lock from the moment a worker is dispatched until it leaves the
# implement -> review -> merge band. (BLOCKED keeps it only when resuming to an AWAITING_* state.)
SCOPE_HELD_STATES = frozenset(
    {
        TaskState.RUNNING,
        TaskState.AWAITING_REVIEW,
        TaskState.REVIEWING,
        TaskState.AWAITING_MERGE,
        TaskState.MERGING,
    }
)


# --------------------------------------------------------------------------------------------
# Plans
# --------------------------------------------------------------------------------------------

_TASK_KEYS = frozenset(
    {
        "id",
        "title",
        "description",
        "acceptance",
        "deps",
        "write_scope",
        "capabilities",
        "priority",
        "max_attempts",
        "attempt_budget_usd",
        "budget_usd",
    }
)
_TASK_REQUIRED = frozenset({"id", "title", "description", "write_scope"})
_PLAN_KEYS = frozenset({"id", "goal", "tasks"})


def _str_list(value: object, field_name: str, owner: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise PlanError(f"{owner}: {field_name} must be a list of strings")
    return tuple(value)


def _check_id(value: object, what: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.match(value):
        raise PlanError(f"{what} must match {ID_PATTERN.pattern}: {value!r}")
    return value


@dataclass(frozen=True)
class TaskSpec:
    """One unit of work: a change that one worker can implement, review and land on its own."""

    id: str
    title: str
    description: str
    write_scope: tuple[str, ...]
    acceptance: tuple[str, ...] = ()
    deps: tuple[str, ...] = ()
    capabilities: frozenset[str] = frozenset()
    priority: int = 0
    max_attempts: int = 3
    attempt_budget: int = 2 * MICROS_PER_USD  # micro-USD reserved per worker attempt
    budget: int | None = None  # micro-USD cap for the task; None = derived from config

    def __post_init__(self) -> None:
        _check_id(self.id, "task id")
        if not self.title.strip():
            raise PlanError(f"task {self.id}: title must not be empty")
        if not self.description.strip():
            raise PlanError(f"task {self.id}: description must not be empty")
        object.__setattr__(self, "write_scope", normalize_scope(self.write_scope))
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise PlanError(f"task {self.id}: priority must be an integer")
        if not isinstance(self.max_attempts, int) or not 1 <= self.max_attempts <= 20:
            raise PlanError(f"task {self.id}: max_attempts must be an integer in [1, 20]")
        if self.attempt_budget <= 0:
            raise PlanError(f"task {self.id}: attempt_budget_usd must be > 0")
        if self.budget is not None and self.budget < self.attempt_budget:
            raise PlanError(f"task {self.id}: budget_usd must be >= attempt_budget_usd")
        for dep in self.deps:
            _check_id(dep, f"task {self.id} dependency")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskSpec":
        if not isinstance(data, Mapping):
            raise PlanError(f"task must be an object, got {type(data).__name__}")
        owner = f"task {data.get('id', '?')!r}"
        unknown = set(data) - _TASK_KEYS
        if unknown:
            raise PlanError(f"{owner}: unknown keys {sorted(unknown)}")
        missing = _TASK_REQUIRED - set(data)
        if missing:
            raise PlanError(f"{owner}: missing keys {sorted(missing)}")
        for key in ("title", "description"):
            if not isinstance(data[key], str):
                raise PlanError(f"{owner}: {key} must be a string")
        try:
            attempt_budget = usd_to_micros(data.get("attempt_budget_usd", "2.00"))
            budget = data.get("budget_usd")
            budget_micros = None if budget is None else usd_to_micros(budget)
        except ValueError as exc:
            raise PlanError(f"{owner}: {exc}") from exc
        priority = data.get("priority", 0)
        max_attempts = data.get("max_attempts", 3)
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise PlanError(f"{owner}: priority must be an integer")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise PlanError(f"{owner}: max_attempts must be an integer")
        return cls(
            id=_check_id(data["id"], "task id"),
            title=data["title"],
            description=data["description"],
            write_scope=_str_list(data["write_scope"], "write_scope", owner),
            acceptance=_str_list(data.get("acceptance", []), "acceptance", owner),
            deps=_str_list(data.get("deps", []), "deps", owner),
            capabilities=frozenset(_str_list(data.get("capabilities", []), "capabilities", owner)),
            priority=priority,
            max_attempts=max_attempts,
            attempt_budget=attempt_budget,
            budget=budget_micros,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "acceptance": list(self.acceptance),
            "deps": list(self.deps),
            "write_scope": list(self.write_scope),
            "capabilities": sorted(self.capabilities),
            "priority": self.priority,
            "max_attempts": self.max_attempts,
            "attempt_budget_usd": str(Decimal(self.attempt_budget) / MICROS_PER_USD),
        }
        if self.budget is not None:
            out["budget_usd"] = str(Decimal(self.budget) / MICROS_PER_USD)
        return out


@dataclass(frozen=True)
class Plan:
    """A goal decomposed into a DAG of tasks."""

    id: str
    goal: str
    tasks: tuple[TaskSpec, ...]

    def __post_init__(self) -> None:
        _check_id(self.id, "plan id")
        if not self.tasks:
            raise PlanError("plan must contain at least one task")
        seen: set[str] = set()
        for task in self.tasks:
            if task.id in seen:
                raise PlanError(f"duplicate task id {task.id!r}")
            seen.add(task.id)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Plan":
        if not isinstance(data, Mapping):
            raise PlanError("plan must be a JSON object")
        unknown = set(data) - _PLAN_KEYS
        if unknown:
            raise PlanError(f"plan: unknown keys {sorted(unknown)}")
        if not isinstance(data.get("tasks"), list):
            raise PlanError("plan: tasks must be a list")
        goal = data.get("goal", "")
        if not isinstance(goal, str):
            raise PlanError("plan: goal must be a string")
        return cls(
            id=_check_id(data.get("id"), "plan id"),
            goal=goal,
            tasks=tuple(TaskSpec.from_dict(t) for t in data["tasks"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "goal": self.goal, "tasks": [t.to_dict() for t in self.tasks]}

    def task(self, task_id: str) -> TaskSpec:
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise KeyError(task_id)


# --------------------------------------------------------------------------------------------
# Messages between agents and the coordinator
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentInfo:
    id: str
    role: Role
    capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        _check_id(self.id, "agent id")


@dataclass(frozen=True)
class Assignment:
    """What an agent receives when the coordinator grants it a lease."""

    task: TaskSpec
    role: Role
    agent_id: str
    token: int  # fencing token: strictly increasing across all leases ever granted
    attempt: int  # worker attempt number this phase belongs to (1-based)
    ttl_s: float
    budget_usd: float  # reserved for this phase; the executor must not spend more
    target_branch: str
    feedback: tuple[str, ...] = ()
    branch: str | None = None
    commit: str | None = None
    base_commit: str | None = None
    summary: str = ""


@dataclass(frozen=True)
class WorkResult:
    branch: str
    commit: str
    base_commit: str
    changed_files: tuple[str, ...]
    cost_usd: float
    summary: str = ""


@dataclass(frozen=True)
class ReviewVerdict:
    approve: bool
    summary: str
    findings: tuple[str, ...] = ()
    cost_usd: float = 0.0


class MergeStatus(str, Enum):
    MERGED = "merged"  # landed (or found already landed) on the target branch
    CONFLICT = "conflict"  # rebase onto the target failed or emptied the change
    VERIFY_FAILED = "verify_failed"  # the verification command failed on the rebased tree
    SCOPE_VIOLATION = "scope_violation"  # rebased change touches files outside the write scope
    TARGET_MOVED = "target_moved"  # compare-and-swap on the target ref lost a race
    FENCED = "fenced"  # a newer integrator lease owns this task's merge fence


@dataclass(frozen=True)
class MergeOutcome:
    status: MergeStatus
    commit: str | None = None
    detail: str = ""
