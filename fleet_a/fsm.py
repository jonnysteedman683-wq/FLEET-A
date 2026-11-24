"""The task state machine: the complete set of legal transitions.

Any transition not listed here raises IllegalTransition. ``docs/ARCHITECTURE.md`` explains the
trigger for each edge.
"""

from __future__ import annotations

from typing import Mapping

from .errors import IllegalTransition
from .model import TaskState as S

TRANSITIONS: Mapping[S, frozenset[S]] = {
    S.PENDING: frozenset({S.READY, S.BLOCKED, S.CANCELLED}),
    S.READY: frozenset({S.RUNNING, S.BLOCKED, S.CANCELLED}),
    S.RUNNING: frozenset({S.AWAITING_REVIEW, S.READY, S.FAILED, S.BLOCKED, S.CANCELLED}),
    S.AWAITING_REVIEW: frozenset({S.REVIEWING, S.BLOCKED, S.CANCELLED}),
    S.REVIEWING: frozenset(
        {S.AWAITING_MERGE, S.AWAITING_REVIEW, S.READY, S.FAILED, S.BLOCKED, S.CANCELLED}
    ),
    S.AWAITING_MERGE: frozenset({S.MERGING, S.BLOCKED, S.CANCELLED}),
    S.MERGING: frozenset({S.DONE, S.AWAITING_MERGE, S.READY, S.FAILED, S.BLOCKED, S.CANCELLED}),
    S.BLOCKED: frozenset({S.PENDING, S.READY, S.AWAITING_REVIEW, S.AWAITING_MERGE, S.CANCELLED}),
    S.DONE: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
}


def check_transition(src: S, dst: S) -> None:
    if dst not in TRANSITIONS[src]:
        raise IllegalTransition(f"illegal transition {src.value} -> {dst.value}")
