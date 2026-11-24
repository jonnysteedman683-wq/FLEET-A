"""Write scopes.

A write scope is the declared set of repository paths a task may create, modify or delete.
Entries are repository-relative POSIX paths:

* ``"src/api/"``  - a directory prefix (trailing slash): every path under it.
* ``"src/app.py"`` - exactly one file.
* ``"*"``          - the whole repository.

Globs are rejected on purpose: overlap between two prefix sets is decidable in O(n*m) string
comparisons, overlap between two glob sets is not cheap or obvious, and scopes exist to make
conflicts *provably* impossible.
"""

from __future__ import annotations

from typing import Iterable

from .errors import FleetError, PlanError

WHOLE_REPO = "*"
_GLOB_CHARS = frozenset("*?[]")


def normalize_entry(raw: object) -> str:
    """Return the canonical form of one scope entry, or raise PlanError."""
    if not isinstance(raw, str):
        raise PlanError(f"write-scope entries must be strings, got {raw!r}")
    text = raw.strip()
    if text in ("*", "/", "./", "."):
        return WHOLE_REPO
    if not text:
        raise PlanError("write-scope entries must not be empty")
    if text.startswith("/"):
        raise PlanError(f"write-scope entry must be repository-relative: {raw!r}")
    if "\\" in text:
        raise PlanError(f"write-scope entry must use '/' separators: {raw!r}")
    if _GLOB_CHARS & set(text):
        raise PlanError(
            f"globs are not supported in write scopes ({raw!r}); "
            "use a directory prefix ending in '/'"
        )
    is_dir = text.endswith("/")
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts:
        return WHOLE_REPO
    if ".." in parts:
        raise PlanError(f"write-scope entry must not contain '..': {raw!r}")
    if parts[0] == ".git":
        raise PlanError(f"write-scope entry must not touch .git: {raw!r}")
    joined = "/".join(parts)
    return joined + "/" if is_dir else joined


def covers(entry: str, path: str) -> bool:
    """True if scope entry ``entry`` includes ``path`` (a file path or a directory entry)."""
    if entry == WHOLE_REPO:
        return True
    if entry == path:
        return True
    return entry.endswith("/") and path.startswith(entry)


def entries_overlap(a: str, b: str) -> bool:
    """True if some path could be inside both entries."""
    return covers(a, b) or covers(b, a) or a.rstrip("/") == b.rstrip("/")


def scopes_overlap(a: Iterable[str], b: Iterable[str]) -> bool:
    b_list = list(b)
    return any(entries_overlap(x, y) for x in a for y in b_list)


def normalize_scope(entries: Iterable[object]) -> tuple[str, ...]:
    """Canonical, sorted, de-duplicated scope with entries covered by a directory removed."""
    if isinstance(entries, (str, bytes)):
        raise PlanError("write_scope must be a list of paths, not a single string")
    normalized = sorted({normalize_entry(e) for e in entries})
    if not normalized:
        raise PlanError("write_scope must not be empty")
    if WHOLE_REPO in normalized:
        return (WHOLE_REPO,)
    reduced = [
        e
        for e in normalized
        if not any(o != e and o.endswith("/") and covers(o, e) for o in normalized)
    ]
    return tuple(reduced)


def scope_violations(scope: Iterable[str], paths: Iterable[str]) -> list[str]:
    """Paths not covered by any entry of ``scope`` (sorted, unique)."""
    scope_list = list(scope)
    return sorted({p for p in paths if not any(covers(e, p) for e in scope_list)})


class ScopeLocks:
    """Exclusive locks on write scopes.

    A task acquires its whole scope atomically (all-or-nothing) and never waits while holding a
    lock, so the hold-and-wait condition for deadlock cannot arise.
    """

    def __init__(self) -> None:
        self._held: dict[str, tuple[str, ...]] = {}

    def blockers(self, task_id: str, scope: Iterable[str]) -> list[str]:
        scope_list = list(scope)
        return sorted(
            holder
            for holder, held in self._held.items()
            if holder != task_id and scopes_overlap(scope_list, held)
        )

    def try_acquire(self, task_id: str, scope: Iterable[str]) -> bool:
        scope_tuple = tuple(scope)
        if task_id in self._held:
            raise FleetError(f"task {task_id} already holds its write scope")
        if self.blockers(task_id, scope_tuple):
            return False
        self._held[task_id] = scope_tuple
        return True

    def release(self, task_id: str) -> bool:
        return self._held.pop(task_id, None) is not None

    def holds(self, task_id: str) -> bool:
        return task_id in self._held

    def held(self) -> dict[str, tuple[str, ...]]:
        return dict(self._held)
