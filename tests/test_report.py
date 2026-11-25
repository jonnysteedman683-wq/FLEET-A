"""Tests for fleet_a.report."""
from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fleet_a import report
from fleet_a.events import EventLog


def add_plan(log, ts, tasks=("t1", "t2")):
    return log.append(
        "plan",
        None,
        {
            "run": "run-1",
            "plan": "plan-1",
            "goal": "do things",
            "tasks": list(tasks),
            "run_budget_usd": 5.0,
            "target_branch": "main",
        },
        ts,
    )


def add_transition(log, task, src, dst, attempt, ts, commit=None):
    data = {"from": src, "to": dst, "attempt": attempt, "reason": "test"}
    if dst == "done":
        data["commit"] = commit
    return log.append("transition", task, data, ts)


def add_spend(log, task, amount, ts):
    return log.append(
        "spend",
        task,
        {"charged_usd": amount, "reserved_usd": amount, "overrun_usd": 0.0},
        ts,
    )


def clean_log():
    log = EventLog()
    add_plan(log, 100.0)
    add_transition(log, "t1", "pending", "running", 1, 101.0)
    add_spend(log, "t1", 0.0821, 102.0)
    add_transition(log, "t1", "running", "done", 1, 103.0, commit="abc123")
    add_transition(log, "t2", "pending", "running", 1, 104.0)
    add_spend(log, "t2", 0.0821, 105.0)
    add_transition(log, "t2", "running", "done", 1, 112.5, commit="def456")
    return log


class CleanRunTests(unittest.TestCase):
    def test_two_tasks_land(self):
        s = report.summarize(clean_log().events)
        self.assertEqual(s["run"], "run-1")
        self.assertEqual(s["plan"], "plan-1")
        self.assertAlmostEqual(s["duration_s"], 12.5)
        self.assertEqual(s["tasks"]["t1"]["state"], "done")
        self.assertEqual(s["tasks"]["t2"]["state"], "done")
        self.assertEqual(s["tasks"]["t1"]["commit"], "abc123")
        self.assertEqual(s["tasks"]["t2"]["commit"], "def456")
        self.assertEqual(s["tasks"]["t1"]["attempts"], 1)
        self.assertAlmostEqual(s["total_usd"], 0.1642)
        self.assertEqual(s["stale_rejected"], 0)
        self.assertEqual(
            set(s), {"run", "plan", "duration_s", "total_usd", "stale_rejected", "tasks"}
        )

    def test_single_event_duration_zero(self):
        log = EventLog()
        add_plan(log, 50.0)
        s = report.summarize(log.events)
        self.assertEqual(s["duration_s"], 0.0)

    def test_empty(self):
        s = report.summarize([])
        self.assertIsNone(s["run"])
        self.assertIsNone(s["plan"])
        self.assertEqual(s["tasks"], {})
        self.assertEqual(s["duration_s"], 0.0)
        self.assertEqual(s["total_usd"], 0.0)

    def test_plan_order_preserved(self):
        log = EventLog()
        add_plan(log, 1.0, tasks=("zeta", "alpha", "mid"))
        s = report.summarize(log.events)
        self.assertEqual(list(s["tasks"]), ["zeta", "alpha", "mid"])


class RetryFailTests(unittest.TestCase):
    def test_retried_then_failed(self):
        log = EventLog()
        add_plan(log, 10.0, tasks=("t1",))
        add_transition(log, "t1", "pending", "running", 1, 11.0)
        add_transition(log, "t1", "running", "rework", 1, 12.0)
        add_transition(log, "t1", "rework", "running", 2, 13.0)
        log.append("stale_rejected", "t1", {"reason": "late"}, 13.5)
        add_transition(log, "t1", "running", "failed", 2, 14.0)
        s = report.summarize(log.events)
        t = s["tasks"]["t1"]
        self.assertEqual(t["state"], "failed")
        self.assertEqual(t["attempts"], 2)
        self.assertIsNone(t["commit"])
        self.assertEqual(s["stale_rejected"], 1)


class SpendTests(unittest.TestCase):
    def test_spend_sums(self):
        log = EventLog()
        add_plan(log, 1.0, tasks=("a", "b", "c"))
        add_spend(log, "a", 0.1, 2.0)
        add_spend(log, "a", 0.25, 3.0)
        add_spend(log, "b", 0.5, 4.0)
        add_spend(log, None, 0.05, 5.0)
        add_spend(log, "ghost", 0.2, 6.0)
        s = report.summarize(log.events)
        self.assertAlmostEqual(s["tasks"]["a"]["spend_usd"], 0.35)
        self.assertAlmostEqual(s["tasks"]["b"]["spend_usd"], 0.5)
        self.assertAlmostEqual(s["total_usd"], 1.1)
        self.assertNotIn("ghost", s["tasks"])
        c = s["tasks"]["c"]
        self.assertEqual(c["state"], "pending")
        self.assertEqual(c["attempts"], 0)
        self.assertEqual(c["spend_usd"], 0.0)
        self.assertIsNone(c["commit"])

    def test_unknown_task_transition_ignored(self):
        log = EventLog()
        add_plan(log, 1.0, tasks=("a",))
        add_transition(log, "ghost", "pending", "running", 1, 2.0)
        s = report.summarize(log.events)
        self.assertEqual(list(s["tasks"]), ["a"])


class FormatTests(unittest.TestCase):
    def test_format(self):
        s = report.summarize(clean_log().events)
        text = report.format_report(s)
        lines = text.split("\n")
        self.assertEqual(len(lines), 3)
        self.assertIn("run-1", lines[0])
        self.assertIn("12.5", lines[0])
        self.assertIn("$0.1642", lines[0])
        self.assertTrue(lines[1].startswith("t1"))
        self.assertTrue(lines[2].startswith("t2"))
        self.assertIn("done", lines[1])
        self.assertIn("attempts=1", lines[1])
        self.assertIn("$0.0821", lines[1])
        self.assertIn("$0.0821", lines[2])

    def test_format_none_run(self):
        text = report.format_report(report.summarize([]))
        self.assertEqual(len(text.split("\n")), 1)
        self.assertIn("$0.0000", text)


def write_log(path):
    log = EventLog(path)
    add_plan(log, 100.0)
    add_transition(log, "t1", "pending", "running", 1, 101.0)
    add_spend(log, "t1", 0.0821, 102.0)
    add_transition(log, "t1", "running", "done", 1, 103.0, commit="abc123")
    log.close()


class MainTests(unittest.TestCase):
    def run_main(self, path):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = report.main([str(path)])
        return code, out.getvalue(), err.getvalue()

    def test_good_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "events.jsonl"
            write_log(path)
            code, out, err = self.run_main(path)
        self.assertEqual(code, 0)
        self.assertIn("run-1", out)
        self.assertIn("t1", out)
        self.assertIn("t2", out)

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, err = self.run_main(Path(d) / "nope.jsonl")
        self.assertEqual(code, 2)
        self.assertTrue(err.strip())

    def test_tampered_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "events.jsonl"
            write_log(path)
            text = path.read_text()
            self.assertIn("0.0821", text)
            path.write_text(text.replace("0.0821", "0.0999", 1))
            code, out, err = self.run_main(path)
        self.assertEqual(code, 1)
        self.assertTrue(err.strip())

    def test_directory_is_not_found(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, err = self.run_main(Path(d))
        self.assertEqual(code, 2)
        self.assertIn("not found", err)

    def test_unreadable_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "events.jsonl"
            write_log(path)
            with mock.patch.object(report.EventLog, "load", side_effect=PermissionError("denied")):
                code, out, err = self.run_main(path)
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)

    def test_non_object_line(self):
        for line in ("[]", "null", "5"):
            with self.subTest(line=line), tempfile.TemporaryDirectory() as d:
                path = Path(d) / "events.jsonl"
                path.write_text(line + "\n")
                code, out, err = self.run_main(path)
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertTrue(err.strip())


if __name__ == "__main__":
    unittest.main()
