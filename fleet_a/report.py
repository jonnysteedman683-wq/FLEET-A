"""Run report for FLEET-A: summarize one run's event log.

Usage: python3 -m fleet_a.report EVENTS.jsonl
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Optional, Sequence

from .events import Event, EventLog
from .errors import FleetError


def summarize(events: Sequence[Event]) -> dict:
    """Fold an event sequence into a per-run summary dict."""
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
        duration = float(events[-1].ts) - float(events[0].ts)

    total = 0.0
    stale = 0
    for ev in events:
        data = ev.data or {}
        if ev.kind == "spend":
            amount = float(data.get("charged_usd", 0.0) or 0.0)
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
    run_text = "-" if run is None else str(run)
    duration = float(summary.get("duration_s") or 0.0)
    total = float(summary.get("total_usd") or 0.0)
    lines = [
        "run %s  duration %ss  total $%.4f" % (run_text, round(duration, 3), total)
    ]
    for tid, rec in (summary.get("tasks") or {}).items():
        lines.append(
            "%s  %s  attempts=%s  $%.4f"
            % (tid, rec.get("state"), rec.get("attempts"), float(rec.get("spend_usd") or 0.0))
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
    except OSError as exc:
        print("error: cannot read %s: %s" % (args.events, exc), file=sys.stderr)
        return 2
    except (FleetError, TypeError) as exc:
        # TypeError: a line that is valid JSON but not an object, which EventLog.load does not wrap.
        print("error: %s: %s" % (args.events, exc), file=sys.stderr)
        return 1
    print(format_report(summarize(events)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
