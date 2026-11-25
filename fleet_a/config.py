"""Fleet configuration: budgets, lease TTLs, concurrency, agent pools and per-role model profiles.

``FleetConfig.from_dict`` rejects unknown keys so a typo can never silently fall back to a
default.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from typing import Any, Iterable, Mapping

from .errors import ConfigError
from .model import ID_PATTERN, Role, TaskSpec, usd_to_micros

EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
PERMISSION_MODES = frozenset({"acceptEdits", "auto", "manual", "dontAsk", "plan"})
PROFILE_NAMES = ("planner", "worker", "reviewer", "triage")


@dataclass(frozen=True)
class ModelProfile:
    """How one role's Claude Code session is launched."""

    model: str
    effort: str = "high"
    fallback_model: str | None = None
    permission_mode: str = "manual"
    tools: tuple[str, ...] | None = None  # None = Claude Code's default set; () = no tools
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    timeout_s: float = 1800.0

    def __post_init__(self) -> None:
        if not self.model:
            raise ConfigError("model profile: model must not be empty")
        if self.effort not in EFFORTS:
            raise ConfigError(f"model profile: effort must be one of {sorted(EFFORTS)}")
        if self.permission_mode not in PERMISSION_MODES:
            raise ConfigError(
                f"model profile: permission_mode must be one of {sorted(PERMISSION_MODES)} "
                "(bypassPermissions is never allowed)"
            )
        if self.timeout_s <= 0:
            raise ConfigError("model profile: timeout_s must be > 0")


_READ_ONLY_DENY = ("Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "Bash(git push *)")

DEFAULT_MODELS: Mapping[str, ModelProfile] = {
    "planner": ModelProfile(
        model="claude-opus-5-5",
        effort="high",
        fallback_model="claude-opus-5",
        permission_mode="manual",
        allowed_tools=("Read", "Glob", "Grep", "Bash(git log *)", "Bash(git ls-files *)"),
        disallowed_tools=_READ_ONLY_DENY,
        timeout_s=1200.0,
    ),
    "worker": ModelProfile(
        model="claude-sonnet-5-5",
        effort="medium",
        fallback_model="claude-sonnet-5",
        permission_mode="acceptEdits",
        allowed_tools=("Read", "Edit", "Write", "Glob", "Grep", "Bash"),
        disallowed_tools=(
            "WebFetch",
            "WebSearch",
            "Bash(git push *)",
            "Bash(git commit *)",
            "Bash(git checkout *)",
            "Bash(git switch *)",
            "Bash(git reset *)",
            "Bash(git rebase *)",
            "Bash(git worktree *)",
            "Bash(git update-ref *)",
        ),
        timeout_s=3600.0,
    ),
    "reviewer": ModelProfile(
        model="claude-opus-5-5",
        effort="high",
        fallback_model="claude-opus-5",
        permission_mode="manual",
        allowed_tools=(
            "Read",
            "Glob",
            "Grep",
            "Bash(git diff *)",
            "Bash(git log *)",
            "Bash(git show *)",
            "Bash(python -m pytest *)",
            "Bash(python3 -m pytest *)",
        ),
        disallowed_tools=_READ_ONLY_DENY,
        timeout_s=1200.0,
    ),
    "triage": ModelProfile(
        model="claude-haiku-5-5",
        effort="low",
        fallback_model=None,
        permission_mode="manual",
        tools=(),
        timeout_s=300.0,
    ),
}

DEFAULT_TTL: Mapping[Role, float] = {Role.WORKER: 900.0, Role.REVIEWER: 600.0, Role.INTEGRATOR: 600.0}
DEFAULT_CONCURRENCY: Mapping[Role, int] = {Role.WORKER: 8, Role.REVIEWER: 2, Role.INTEGRATOR: 1}


@dataclass(frozen=True)
class AgentPool:
    role: Role
    count: int
    capabilities: frozenset[str] = frozenset()
    prefix: str = ""

    def __post_init__(self) -> None:
        if self.role is Role.PLANNER:
            raise ConfigError("agent pools run workers, reviewers and integrators; plan with `fleet_a plan`")
        if self.count < 0:
            raise ConfigError("agent pool count must be >= 0")
        if self.prefix and not ID_PATTERN.match(self.prefix):
            raise ConfigError(f"agent pool prefix must match {ID_PATTERN.pattern}")

    def agent_ids(self) -> list[str]:
        base = self.prefix or self.role.value
        return [f"{base}-{i + 1}" for i in range(self.count)]


DEFAULT_AGENTS = (
    AgentPool(Role.WORKER, 4),
    AgentPool(Role.REVIEWER, 1),
    AgentPool(Role.INTEGRATOR, 1),
)


@dataclass(frozen=True)
class FleetConfig:
    target_branch: str = "main"
    fleet_budget: int = usd_to_micros("500")
    run_budget: int = usd_to_micros("100")
    review_budget: int = usd_to_micros("1.50")
    planner_budget: int = usd_to_micros("5.00")
    triage_budget: int = usd_to_micros("0.05")
    triage_threshold_chars: int = 6000
    lease_ttl_s: Mapping[Role, float] = field(default_factory=lambda: dict(DEFAULT_TTL))
    max_concurrent: Mapping[Role, int] = field(default_factory=lambda: dict(DEFAULT_CONCURRENCY))
    max_infra_retries: int = 3
    aging_interval_s: float = 600.0
    verify_command: tuple[str, ...] = ("python3", "-m", "pytest", "-q")
    verify_timeout_s: float = 1200.0
    # Run verify_command in the worker's worktree before review: a failing attempt goes straight
    # back to rework with the test output, and no paid review is spent on it.
    verify_before_review: bool = True
    # A review that hits review_budget is retried this many times, then the task is BLOCKED for
    # the operator to raise review_budget_usd: each retry at the same cap can spend all of it again.
    max_review_budget_retries: int = 1
    agents: tuple[AgentPool, ...] = DEFAULT_AGENTS
    models: Mapping[str, ModelProfile] = field(default_factory=lambda: dict(DEFAULT_MODELS))
    git_name: str = "FLEET-A"
    git_email: str = "fleet-a@localhost"
    poll_interval_s: float = 2.0

    def __post_init__(self) -> None:
        if not self.target_branch or self.target_branch.startswith("fleet/"):
            raise ConfigError("target_branch must be set and must not be under fleet/")
        for name in ("fleet_budget", "run_budget", "review_budget", "planner_budget", "triage_budget"):
            if getattr(self, name) < 0:
                raise ConfigError(f"{name} must be >= 0")
        if self.review_budget == 0:
            raise ConfigError("review_budget_usd must be > 0: every attempt is reviewed")
        if self.run_budget > self.fleet_budget:
            raise ConfigError("run_budget_usd must not exceed fleet_budget_usd")
        for role in (Role.WORKER, Role.REVIEWER, Role.INTEGRATOR):
            if self.lease_ttl_s.get(role, 0) <= 0:
                raise ConfigError(f"lease_ttl_s.{role.value} must be > 0")
            if self.max_concurrent.get(role, 0) < 1:
                raise ConfigError(f"max_concurrent.{role.value} must be >= 1")
        if self.max_concurrent[Role.INTEGRATOR] != 1:
            raise ConfigError("max_concurrent.integrator must be exactly 1: merges are single-flight")
        if self.max_infra_retries < 0:
            raise ConfigError("max_infra_retries must be >= 0")
        if self.aging_interval_s < 0:
            raise ConfigError("aging_interval_s must be >= 0")
        if self.verify_timeout_s <= 0:
            raise ConfigError("verify_timeout_s must be > 0")
        if not isinstance(self.verify_before_review, bool):
            raise ConfigError("verify_before_review must be true or false")
        if (
            isinstance(self.max_review_budget_retries, bool)
            or not isinstance(self.max_review_budget_retries, int)
            or self.max_review_budget_retries < 0
        ):
            raise ConfigError("max_review_budget_retries must be an integer >= 0")
        if self.triage_threshold_chars < 200:
            raise ConfigError("triage_threshold_chars must be >= 200")
        missing = [p for p in PROFILE_NAMES if p not in self.models]
        if missing:
            raise ConfigError(f"models: missing profiles {missing}")
        ids: set[str] = set()
        for pool in self.agents:
            for agent_id in pool.agent_ids():
                if agent_id in ids:
                    raise ConfigError(f"duplicate agent id {agent_id}; give pools distinct prefixes")
                ids.add(agent_id)

    # ------------------------------------------------------------------------------- loading

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FleetConfig":
        if not isinstance(data, Mapping):
            raise ConfigError("config must be a JSON object")
        money = {
            "fleet_budget_usd": "fleet_budget",
            "run_budget_usd": "run_budget",
            "review_budget_usd": "review_budget",
            "planner_budget_usd": "planner_budget",
            "triage_budget_usd": "triage_budget",
        }
        plain = {
            "target_branch",
            "triage_threshold_chars",
            "max_infra_retries",
            "aging_interval_s",
            "verify_timeout_s",
            "verify_before_review",
            "max_review_budget_retries",
            "git_name",
            "git_email",
            "poll_interval_s",
        }
        structured = {"lease_ttl_s", "max_concurrent", "verify_command", "agents", "models"}
        unknown = set(data) - set(money) - plain - structured
        if unknown:
            raise ConfigError(f"config: unknown keys {sorted(unknown)}")
        base = cls()
        kwargs: dict[str, Any] = {}
        try:
            for key, attr in money.items():
                if key in data:
                    kwargs[attr] = usd_to_micros(data[key])
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        for key in plain:
            if key in data:
                kwargs[key] = data[key]
        if "lease_ttl_s" in data:
            kwargs["lease_ttl_s"] = {**base.lease_ttl_s, **_role_map(data["lease_ttl_s"], float)}
        if "max_concurrent" in data:
            kwargs["max_concurrent"] = {**base.max_concurrent, **_role_map(data["max_concurrent"], int)}
        if "verify_command" in data:
            cmd = data["verify_command"]
            if not isinstance(cmd, list) or not all(isinstance(c, str) for c in cmd):
                raise ConfigError("verify_command must be a list of strings (argv), [] to skip")
            kwargs["verify_command"] = tuple(cmd)
        if "agents" in data:
            kwargs["agents"] = tuple(_agent_pool(p) for p in data["agents"])
        if "models" in data:
            kwargs["models"] = _models(data["models"], base.models)
        try:
            return replace(base, **kwargs)
        except TypeError as exc:
            raise ConfigError(str(exc)) from exc

    def ttl(self, role: Role) -> float:
        return self.lease_ttl_s[role]

    def default_task_budget(self, attempt_budget: int, max_attempts: int) -> int:
        return max_attempts * (attempt_budget + self.review_budget)

    def _pool_tags(self, role: Role) -> frozenset[str]:
        return frozenset().union(*(p.capabilities for p in self.agents if p.role is role and p.count > 0))

    def plannable_capabilities(self) -> frozenset[str]:
        """Tags carried by some worker pool and some reviewer pool: the only tags a plan may use."""
        return self._pool_tags(Role.WORKER) & self._pool_tags(Role.REVIEWER)

    def plannable_tag_sets(self) -> list[frozenset[str]]:
        """The largest tag combinations one task can carry and still be served.

        A task can be taken only if a single worker pool and a single reviewer pool each carry all
        of its tags, so its tags must be a subset of some ``worker & reviewer`` intersection. The
        union of these sets equals ``plannable_capabilities()``; the sets say which tags combine.
        """
        workers = [p.capabilities for p in self.agents if p.role is Role.WORKER and p.count > 0]
        reviewers = [p.capabilities for p in self.agents if p.role is Role.REVIEWER and p.count > 0]
        pairs = {frozenset(w & r) for w in workers for r in reviewers} - {frozenset()}
        maximal = [s for s in pairs if not any(s < other for other in pairs)]
        return sorted(maximal, key=lambda s: sorted(s))

    def capability_gaps(self, tasks: Iterable[TaskSpec]) -> list[str]:
        """One message per task that no configured worker pool, or no reviewer pool, can take.

        A task with a gap can never be dispatched, so a run containing one would stall at once.
        """
        gaps = []
        for task in tasks:
            for role in (Role.WORKER, Role.REVIEWER):
                if not any(
                    p.role is role and p.count > 0 and task.capabilities <= p.capabilities for p in self.agents
                ):
                    gaps.append(
                        f"task {task.id}: no {role.value} pool has capabilities {sorted(task.capabilities)}"
                    )
        return gaps


def _role_map(raw: Any, cast: type) -> dict[Role, Any]:
    if not isinstance(raw, Mapping):
        raise ConfigError("expected an object keyed by role")
    out: dict[Role, Any] = {}
    for key, value in raw.items():
        try:
            role = Role(key)
        except ValueError as exc:
            raise ConfigError(f"unknown role {key!r}") from exc
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{key}: expected a number")
        out[role] = cast(value)
    return out


def _agent_pool(raw: Any) -> AgentPool:
    if not isinstance(raw, Mapping) or set(raw) - {"role", "count", "capabilities", "prefix"}:
        raise ConfigError("agents entries take role, count, capabilities, prefix")
    try:
        role = Role(raw["role"])
    except (KeyError, ValueError) as exc:
        raise ConfigError(f"agents entry needs a valid role: {raw!r}") from exc
    count = raw.get("count", 1)
    caps = raw.get("capabilities", [])
    if isinstance(count, bool) or not isinstance(count, int):
        raise ConfigError("agents.count must be an integer")
    if not isinstance(caps, list) or not all(isinstance(c, str) for c in caps):
        raise ConfigError("agents.capabilities must be a list of strings")
    return AgentPool(role, count, frozenset(caps), str(raw.get("prefix", "")))


def _models(raw: Any, base: Mapping[str, ModelProfile]) -> dict[str, ModelProfile]:
    if not isinstance(raw, Mapping):
        raise ConfigError("models must be an object keyed by profile name")
    allowed = {f.name for f in fields(ModelProfile)}
    out = dict(base)
    for name, overrides in raw.items():
        if name not in PROFILE_NAMES:
            raise ConfigError(f"models: unknown profile {name!r}; expected {list(PROFILE_NAMES)}")
        if not isinstance(overrides, Mapping) or set(overrides) - allowed:
            raise ConfigError(f"models.{name}: keys must be a subset of {sorted(allowed)}")
        values = dict(overrides)
        for key in ("tools", "allowed_tools", "disallowed_tools"):
            if key in values and values[key] is not None:
                if not isinstance(values[key], list) or not all(isinstance(t, str) for t in values[key]):
                    raise ConfigError(f"models.{name}.{key} must be a list of strings")
                values[key] = tuple(values[key])
        out[name] = replace(base[name], **values)
    return out
