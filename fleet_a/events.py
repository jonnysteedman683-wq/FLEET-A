"""Append-only, hash-chained event log.

Every state change the coordinator makes is appended here before the call that caused it
returns. Each event commits to its predecessor's hash, so truncation, reordering or editing of
any past event is detected by ``EventLog.verify``. Task states can be re-derived from the log
alone with ``project_states``.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .errors import FleetError

GENESIS = "0" * 64


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(body: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Event:
    seq: int
    ts: float
    kind: str
    task: str | None
    data: Mapping[str, Any]
    prev: str
    hash: str

    def body(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "kind": self.kind,
            "task": self.task,
            "data": dict(self.data),
            "prev": self.prev,
        }

    def to_json(self) -> str:
        return canonical_json({**self.body(), "hash": self.hash})

    @classmethod
    def from_json(cls, line: str) -> "Event":
        raw = json.loads(line)
        return cls(
            seq=raw["seq"],
            ts=raw["ts"],
            kind=raw["kind"],
            task=raw["task"],
            data=raw["data"],
            prev=raw["prev"],
            hash=raw["hash"],
        )


class EventLog:
    """In-memory event chain, optionally mirrored line-by-line to a JSONL file."""

    def __init__(self, path: Path | None = None, *, fsync: bool = False) -> None:
        self._events: list[Event] = []
        self._lock = threading.Lock()
        self._fsync = fsync
        self._fh = None
        if path is not None:
            path = Path(path)
            if path.exists() and path.stat().st_size > 0:
                raise FleetError(f"event log {path} already exists; each run writes a fresh log")
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8")

    def append(self, kind: str, task: str | None, data: Mapping[str, Any], ts: float) -> Event:
        with self._lock:
            body = {
                "seq": len(self._events),
                "ts": ts,
                "kind": kind,
                "task": task,
                "data": dict(data),
                "prev": self._events[-1].hash if self._events else GENESIS,
            }
            event = Event(hash=_digest(body), **body)
            self._events.append(event)
            if self._fh is not None:
                self._fh.write(event.to_json() + "\n")
                self._fh.flush()
                if self._fsync:
                    os.fsync(self._fh.fileno())
            return event

    @property
    def events(self) -> tuple[Event, ...]:
        with self._lock:
            return tuple(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    @staticmethod
    def verify(events: Sequence[Event]) -> None:
        """Raise FleetError at the first event that breaks the chain."""
        prev = GENESIS
        for index, event in enumerate(events):
            if event.seq != index:
                raise FleetError(f"event {index}: sequence number {event.seq}, expected {index}")
            if event.prev != prev:
                raise FleetError(f"event {index}: prev hash does not match event {index - 1}")
            if _digest(event.body()) != event.hash:
                raise FleetError(f"event {index}: content does not match its hash")
            prev = event.hash

    @staticmethod
    def load(path: Path) -> list[Event]:
        events = []
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                try:
                    events.append(Event.from_json(line))
                except (ValueError, KeyError) as exc:
                    raise FleetError(f"{path}:{lineno}: malformed event: {exc}") from exc
        return events


def project_states(events: Iterable[Event]) -> dict[str, str]:
    """Rebuild every task's state from ``plan`` and ``transition`` events alone."""
    states: dict[str, str] = {}
    for event in events:
        if event.kind == "plan":
            for task_id in event.data["tasks"]:
                states[task_id] = "pending"
        elif event.kind == "transition":
            current = states.get(event.task or "")
            if current != event.data["from"]:
                raise FleetError(
                    f"event {event.seq}: task {event.task} moves from {event.data['from']} "
                    f"but its projected state is {current}"
                )
            states[event.task or ""] = event.data["to"]
    return states
