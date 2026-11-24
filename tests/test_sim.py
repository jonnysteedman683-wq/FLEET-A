"""The simulator passes on the real control plane, and catches each safety mechanism's removal.

The second half is mutation testing: each test disables one mechanism and asserts the
simulator reports a violation. A checker that cannot see a broken system proves nothing.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from fleet_a import coordinator as coordinator_module
from fleet_a.budget import Ledger, Reservation
from fleet_a.dag import TaskGraph
from fleet_a.errors import StaleLease
from fleet_a.leases import LeaseTable
from fleet_a.model import ROLE_FOR_ACTIVE_STATE, TERMINAL_STATES, TaskState
from fleet_a.scopes import ScopeLocks
from fleet_a.sim import SimConfig, SimTarget, simulate


def first_violation(base: SimConfig, max_seeds: int = 60) -> str | None:
    for seed in range(max_seeds):
        report = simulate(replace(base, seed=seed))
        if report.violations:
            return report.violations[0]
    return None


@pytest.mark.parametrize("seed", range(40))
def test_invariants_hold_under_faults(seed):
    report = simulate(SimConfig(seed=seed))
    assert report.ok, report.violations
    assert report.finished
    assert report.merges == report.states.get("done", 0)


def test_invariants_hold_at_scale_with_heavy_faults():
    cfg = SimConfig(tasks=60, workers=10, reviewers=3, integrators=3, p_crash=0.1, p_stall=0.1, p_cancel=0.01)
    for seed in range(5):
        report = simulate(replace(cfg, seed=seed))
        assert report.ok, report.violations
        assert report.stale_rejected > 0


def test_budget_exhaustion_stalls_cleanly():
    for seed in range(5):
        report = simulate(SimConfig(seed=seed, run_budget_usd="15"))
        assert report.ok, report.violations
        assert not report.finished and "budget" in report.stall_reason
        assert report.spent_usd <= 15 * 1.25


def test_simulation_is_deterministic():
    a, b = simulate(SimConfig(seed=7)), simulate(SimConfig(seed=7))
    assert a.to_dict() == b.to_dict()


# ------------------------------------------------------------------------- mutation testing


def test_detects_stale_tokens_being_accepted(monkeypatch):
    def loose_check(self, task_id, token, now):
        lease = self._by_task.get(task_id)
        if lease is None:
            raise StaleLease("no lease")
        return lease

    monkeypatch.setattr(LeaseTable, "check", loose_check)
    assert first_violation(SimConfig()) is not None


def test_detects_missing_scope_locks(monkeypatch):
    monkeypatch.setattr(ScopeLocks, "blockers", lambda self, task_id, scope: [])
    assert "overlapping write scopes" in first_violation(SimConfig())


def test_detects_missing_cascade(monkeypatch):
    monkeypatch.setattr(TaskGraph, "descendants", lambda self, task_id: ())
    assert "can never land" in first_violation(SimConfig())


def test_detects_missing_budget_admission(monkeypatch):
    def reserve_unchecked(self, name, amount):
        for account in self._chain(name):
            account.reserved += amount
        res = Reservation(self._next_id, name, amount)
        self._next_id += 1
        self._open[res.id] = res
        return res

    monkeypatch.setattr(Ledger, "reserve", reserve_unchecked)
    assert "over limit" in first_violation(SimConfig(run_budget_usd="15"))


def test_detects_missing_merge_fence(monkeypatch):
    def land_without_fence(self, task, attempt, token, base_version):
        if self.version != base_version:
            return "moved"
        self.log.append((task, attempt, token))
        self.version += 1
        return "ok"

    monkeypatch.setattr(SimTarget, "land", land_without_fence)
    monkeypatch.setattr(SimTarget, "claim", lambda self, task, token: True)
    cfg = SimConfig(p_stall=0.3, p_conflict=0.3, p_verify_fail=0.2, integrators=3)
    assert "landed" in first_violation(cfg)


def test_detects_missing_deferred_cancel(monkeypatch):
    def cancel_immediately(self, rec, now, reason):
        if rec.state in TERMINAL_STATES:
            return False
        if rec.state in ROLE_FOR_ACTIVE_STATE:
            self._settle(rec, now, None)
        rec.merge_unresolved = False
        self._transition(rec, TaskState.CANCELLED, now, reason)
        return True

    monkeypatch.setattr(coordinator_module.Coordinator, "_cancel", cancel_immediately)
    assert "landed" in first_violation(SimConfig(p_cancel=0.2, p_stall=0.2))
