"""Run report for FLEET-A: summarize one run's event log.

Usage: python3 -m fleet_a.report EVENTS.jsonl

Before reporting, the CLI checks the log's hash chain and replays its task states. The chain
detects corruption, truncation, reordering and any edit that does not recompute the hashes; it is
unkeyed, so it does not prove who wrote the log. Tasks are listed in the order the plan event
records them, which is the plan's dependency (topological) order. A log that is still being written
is reported as it stands; the header's "finished" count shows how many tasks have ended.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from typing import Any, Optional, Sequence

from .events import Event, EventLog, project_states
from .errors import FleetError

FINISHED_STATES = frozenset({"done", "failed", "cancelled"})


def _printable(value: Any) -> str:
    """Log text with control characters escaped, so a crafted log cannot drive the terminal."""
    text = str(value)
    return "".join(c if c.isprintable() else c.encode("unicode_escape").decode("ascii") for c in text)


def _number(value: Any, what: str) -> float:
    """A finite real number from the log, or ValueError.

    Bools, numeric strings, NaN, infinities and integers too large for a float are malformed.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("malformed %s: %r is not a number" % (what, value))
    try:
        number = float(value)
    except OverflowError:
        raise ValueError("malformed %s: integer too large" % what) from None
    if not math.isfinite(number):
        raise ValueError("malformed %s: %r is not finite" % (what, value))
    return number


def summarize(events: Sequence[Event]) -> dict:
    """Fold an event sequence into a per-run summary dict, tasks in the plan event's order.

    Raises ValueError on malformed event data (non-object data, a charged_usd or ts that is not a
    finite number).
    """
    for ev in events:
        if not isinstance(ev.data, dict):
            raise ValueError("malformed event %s: data is not an object" % ev.seq)
        _number(ev.ts, "ts")  # every event, not only the first and last that set the duration
    run = None
    plan = None
    tasks: dict[str, dict[str, Any]] = {}
    for ev in events:
        if ev.kind == "plan":
            data = ev.data or {}
            run = data.get("run")
            plan = data.get("plan")
            tasks = {
                str(tid): {
                    "state": "pending",
                    "attempts": 0,
                    "spend_usd": 0.0,
                    "commit": None,
                }
                for tid in (data.get("tasks") or [])
            }
            break

    duration = 0.0
    if len(events) > 1:
        duration = _number(events[-1].ts, "ts") - _number(events[0].ts, "ts")

    total = 0.0
    stale = 0
    for ev in events:
        data = ev.data or {}
        if ev.kind == "spend":
            amount = _number(data.get("charged_usd", 0.0), "charged_usd")  # null, false, "" are malformed
            total += amount
            rec = tasks.get(ev.task) if ev.task is not None else None
            if rec is not None:
                rec["spend_usd"] += amount
        elif ev.kind == "stale_rejected":
            stale += 1
        elif ev.kind == "transition":
            rec = tasks.get(ev.task) if ev.task is not None else None
            if rec is None:
                continue
            rec["state"] = data.get("to")
            attempt = data.get("attempt")
            if isinstance(attempt, int) and attempt > rec["attempts"]:
                rec["attempts"] = attempt
            if data.get("to") == "done":
                rec["commit"] = data.get("commit")

    return {
        "run": run,
        "plan": plan,
        "duration_s": float(duration),
        "total_usd": float(total),
        "stale_rejected": stale,
        "tasks": tasks,
    }


def format_report(summary: dict) -> str:
    """Render a summary as a header line plus one line per task."""
    run = summary.get("run")
    run_text = "-" if run is None else _printable(run)
    duration = float(summary.get("duration_s") or 0.0)
    total = float(summary.get("total_usd") or 0.0)
    tasks = summary.get("tasks") or {}
    finished = sum(1 for rec in tasks.values() if rec.get("state") in FINISHED_STATES)
    lines = [
        "run %s  duration %ss  total $%.4f  finished %d/%d"
        % (run_text, round(duration, 3), total, finished, len(tasks))
    ]
    for tid, rec in tasks.items():
        lines.append(
            "%s  %s  attempts=%s  $%.4f"
            % (
                _printable(tid),
                _printable(rec.get("state")),
                _printable(rec.get("attempts")),
                float(rec.get("spend_usd") or 0.0),
            )
        )
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fleet_a.report", description="Summarize a FLEET-A run's event log."
    )
    parser.add_argument("events", help="path to the events JSONL file")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.events):
        print("error: events file not found: %s" % args.events, file=sys.stderr)
        return 2
    try:
        events = EventLog.load(args.events)
        EventLog.verify(events)
        plans = [ev for ev in events if ev.kind == "plan"]
        if len(plans) > 1:
            raise ValueError(
                "repeated plan event: %d plan events (seq %s), expected exactly one"
                % (len(plans), ", ".join(str(ev.seq) for ev in plans))
            )
        project_states(events)  # a well-hashed log can still hold a transition from the wrong state
        text = format_report(summarize(events))
    except OSError as exc:
        print("error: cannot read %s: %s" % (args.events, exc), file=sys.stderr)
        return 2
    except (FleetError, TypeError, KeyError, ValueError, AttributeError) as exc:
        # TypeError: a line that is valid JSON but not an object, which EventLog.load does not wrap.
        # KeyError: a well-hashed plan or transition event missing a field project_states reads.
        print("error: %s: %s" % (args.events, exc), file=sys.stderr)
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
