"""Exception hierarchy. Every error FLEET-A raises on purpose derives from FleetError."""

from __future__ import annotations


class FleetError(Exception):
    """Base class for FLEET-A errors."""


class PlanError(FleetError):
    """A plan or task specification is invalid."""


class ConfigError(FleetError):
    """A fleet configuration is invalid."""


class IllegalTransition(FleetError):
    """A task state change that the state machine forbids."""


class StaleLease(FleetError):
    """A lease token is not (or is no longer) the current lease for its task."""


class GitError(FleetError):
    """A git command failed."""


class BudgetExceeded(FleetError):
    """A reservation does not fit in an account (or one of its ancestors)."""

    def __init__(self, account: str, requested: int, available: int) -> None:
        super().__init__(
            f"account {account!r} cannot fund {requested} micro-USD (available {available})"
        )
        self.account = account
        self.requested = requested
        self.available = available
