"""FLEET-A: a fleet of autonomous coding agents with a provably safe control plane."""

from .config import AgentPool, FleetConfig, ModelProfile
from .coordinator import Coordinator, ManualClock, SystemClock, TaskRecord
from .errors import (
    BudgetExceeded,
    ConfigError,
    FleetError,
    GitError,
    IllegalTransition,
    PlanError,
    StaleLease,
)
from .events import EventLog, project_states
from .model import (
    AgentInfo,
    Assignment,
    MergeOutcome,
    MergeStatus,
    Plan,
    ReviewVerdict,
    Role,
    TaskSpec,
    TaskState,
    WorkResult,
)

__version__ = "1.0.0"

__all__ = [
    "AgentInfo",
    "AgentPool",
    "Assignment",
    "BudgetExceeded",
    "ConfigError",
    "Coordinator",
    "EventLog",
    "FleetConfig",
    "FleetError",
    "GitError",
    "IllegalTransition",
    "ManualClock",
    "MergeOutcome",
    "MergeStatus",
    "ModelProfile",
    "Plan",
    "PlanError",
    "ReviewVerdict",
    "Role",
    "StaleLease",
    "SystemClock",
    "TaskRecord",
    "TaskSpec",
    "TaskState",
    "WorkResult",
    "project_states",
]
