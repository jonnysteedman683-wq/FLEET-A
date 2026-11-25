"""Coordinator semantics, driven with a manual clock."""

from __future__ import annotations

import pytest

from conftest import config, plan_of, spec

from fleet_a.coordinator import Coordinator, ManualClock, task_account
from fleet_a.errors import FleetError, StaleLease
from fleet_a.events import EventLog, project_states
from fleet_a.model import (
    AgentInfo,
    MergeOutcome,
    MergeStatus,
    ReviewVerdict,
    Role,
    TaskState as S,
    WorkResult,
    usd_to_micros,
)

W1 = AgentInfo("w1", Role.WORKER)
W2 = AgentInfo("w2", Role.WORKER)
R1 = AgentInfo("r1", Role.REVIEWER)
I1 = AgentInfo("i1", Role.INTEGRATOR)
I2 = AgentInfo("i2", Role.INTEGRATOR)


def make(*specs, **cfg):
    clock = ManualClock()
    coord = Coordinator(plan_of(*specs), config(**cfg), clock, EventLog())
    return coord, clock


def work(task_id, files=("src/x.py",), cost=0.5):
    return WorkResult(
        branch=f"fleet/{task_id}", commit=f"c-{task_id}", base_commit="b", changed_files=tuple(files), cost_usd=cost
    )


def land(coord, task_id, worker=W1, reviewer=R1, integrator=I1, files=("src/x.py",)):
    a = coord.acquire(worker)
    assert a is not None and a.task.id == task_id
    coord.submit_work(task_id, a.token, work(task_id, files))
    r = coord.acquire(reviewer)
    assert r is not None and r.task.id == task_id
    coord.submit_review(task_id, r.token, ReviewVerdict(True, "lgtm", cost_usd=0.1))
    m = coord.acquire(integrator)
    assert m is not None and m.task.id == task_id
    coord.submit_merge(task_id, m.token, MergeOutcome(MergeStatus.MERGED, f"m-{task_id}"))
    return a, r, m


def states(coord):
    return {t: r.state for t, r in coord.tasks.items()}


def test_initial_promotion_and_happy_path_unblocks_dependents():
    coord, _ = make(spec("a"), spec("b", deps=["a"]), spec("c", scope=["docs/"]))
    assert states(coord) == {"a": S.READY, "b": S.PENDING, "c": S.READY}
    a, r, m = land(coord, "a")
    assert a.attempt == 1 and r.commit == "c-a" and r.base_commit == "b"
    assert coord.tasks["a"].state is S.DONE and coord.tasks["a"].merged_commit == "m-a"
    assert coord.tasks["b"].state is S.READY
    assert a.token < r.token < m.token
    run = coord.ledger.account(coord.run_account)
    assert run.spent == usd_to_micros("0.6") and run.reserved == 0
    assert project_states(coord.events.events) == {t: s.value for t, s in states(coord).items()}


def test_assignment_carries_review_context_and_budgets():
    coord, _ = make(spec("a", attempt_budget=usd_to_micros("3")))
    a = coord.acquire(W1)
    assert a.budget_usd == 3.0 and a.ttl_s == 100.0 and a.target_branch == "main"
    assert coord.ledger.account(task_account("a")).reserved == usd_to_micros("3")
    coord.submit_work("a", a.token, WorkResult("br", "c1", "b0", ("src/x.py",), 1.0, "summary"))
    r = coord.acquire(R1)
    assert (r.branch, r.commit, r.base_commit, r.summary, r.budget_usd) == ("br", "c1", "b0", "summary", 0.5)


def test_reviewer_cannot_review_own_attempt():
    coord, _ = make(spec("a"))
    a = coord.acquire(W1)
    coord.submit_work("a", a.token, work("a"))
    assert coord.acquire(AgentInfo("w1", Role.REVIEWER)) is None
    assert coord.acquire(R1) is not None


def test_capabilities_gate_dispatch():
    coord, _ = make(spec("a", capabilities=frozenset({"rust"})))
    assert coord.acquire(W1) is None
    assert "no eligible worker" in coord.stall_reason([W1, R1, I1])
    assert coord.acquire(AgentInfo("w9", Role.WORKER, frozenset({"rust", "go"}))) is not None


def test_overlapping_scopes_serialize_until_merge():
    coord, _ = make(spec("a", scope=["src/api/"]), spec("b", scope=["src/"]), spec("c", scope=["docs/"]))
    first = coord.acquire(W1)
    assert first.task.id == "a"
    second = coord.acquire(W2)
    assert second.task.id == "c"  # b overlaps a's scope
    coord.submit_work("a", first.token, work("a", ["src/api/x.py"]))
    assert coord.scopes.holds("a")  # held through review and merge
    r = coord.acquire(R1)
    coord.submit_review("a", r.token, ReviewVerdict(True, "ok"))
    m = coord.acquire(I1)
    coord.submit_merge("a", m.token, MergeOutcome(MergeStatus.MERGED, "m"))
    assert not coord.scopes.holds("a")
    assert coord.acquire(AgentInfo("w3", Role.WORKER)).task.id == "b"


def test_worker_concurrency_limit():
    coord, _ = make(spec("a", scope=["a/"]), spec("b", scope=["b/"]), max_concurrent={Role.WORKER: 1, Role.REVIEWER: 1, Role.INTEGRATOR: 1})
    assert coord.acquire(W1) is not None
    assert coord.acquire(W2) is None


def test_priority_aging_and_depth_order():
    coord, clock = make(
        spec("low", scope=["l/"], priority=0),
        spec("high", scope=["h/"], priority=2),
        spec("root", scope=["r/"], priority=0),
        spec("leaf", scope=["x/"], deps=["root"], priority=0),
        aging_interval_s=10.0,
    )
    assert coord.acquire(W1).task.id == "high"
    # root outranks low on critical-path depth
    assert coord.acquire(W2).task.id == "root"


def test_one_lease_per_agent():
    coord, _ = make(spec("a", scope=["a/"]), spec("b", scope=["b/"]))
    coord.acquire(W1)
    with pytest.raises(FleetError, match="already holds"):
        coord.acquire(W1)


def test_heartbeat_extends_and_expiry_reclaims_with_full_charge():
    coord, clock = make(spec("a"))
    a = coord.acquire(W1)
    clock.advance(90)
    assert coord.heartbeat("a", a.token) == 190
    clock.advance(99)
    coord.tick()
    assert coord.tasks["a"].state is S.RUNNING
    clock.advance(2)
    coord.tick()
    rec = coord.tasks["a"]
    assert rec.state is S.READY and rec.attempts == 1 and "lease expired" in rec.feedback[-1]
    assert coord.ledger.account(task_account("a")).spent == usd_to_micros("2")
    with pytest.raises(StaleLease):
        coord.heartbeat("a", a.token)
    n = len(coord.events)
    with pytest.raises(StaleLease):
        coord.submit_work("a", a.token, work("a"))
    assert [e.kind for e in coord.events.events[n:]] == ["stale_rejected"]
    b = coord.acquire(W2)
    assert b.attempt == 2 and b.token > a.token and "lease expired" in b.feedback[-1]


def test_review_rejection_reworks_with_feedback_then_attempts_exhaust_and_cascade():
    coord, _ = make(spec("a", max_attempts=2), spec("b", deps=["a"]), spec("c", deps=["b"]))
    for attempt in (1, 2):
        a = coord.acquire(W1)
        assert a.attempt == attempt
        coord.submit_work("a", a.token, work("a"))
        r = coord.acquire(R1)
        coord.submit_review("a", r.token, ReviewVerdict(False, "missing tests", ("[major] src/x.py: no test",)))
    assert coord.tasks["a"].state is S.FAILED
    assert "attempts exhausted" in coord.tasks["a"].reason
    assert coord.tasks["b"].state is S.CANCELLED and coord.tasks["c"].state is S.CANCELLED
    assert coord.is_finished()


def test_rework_feedback_reaches_next_attempt():
    coord, _ = make(spec("a"))
    a = coord.acquire(W1)
    coord.submit_work("a", a.token, work("a"))
    r = coord.acquire(R1)
    coord.submit_review("a", r.token, ReviewVerdict(False, "wrong", ("[blocker] src/x.py: crashes",)))
    nxt = coord.acquire(W2)
    assert nxt.attempt == 2 and "crashes" in nxt.feedback[-1]


def test_scope_violation_and_empty_attempt_are_reworked():
    coord, _ = make(spec("a", scope=["src/"], max_attempts=3))
    a = coord.acquire(W1)
    coord.submit_work("a", a.token, work("a", ["src/ok.py", "secrets/leak.txt"]))
    assert coord.tasks["a"].state is S.READY and "write-scope violation" in coord.tasks["a"].feedback[-1]
    a = coord.acquire(W1)
    coord.submit_work("a", a.token, work("a", []))
    assert coord.tasks["a"].state is S.READY and "no changes" in coord.tasks["a"].feedback[-1]


def test_fail_paths_per_role():
    coord, _ = make(spec("a"), spec("b", scope=["b/"]), max_infra_retries=1)
    a = coord.acquire(W1)
    coord.fail("a", a.token, "tests broke", cost_usd=0.3)
    assert coord.tasks["a"].state is S.READY
    a = coord.acquire(W1)
    coord.fail("a", a.token, "needs product decision", escalate=True)
    assert coord.tasks["a"].state is S.BLOCKED and coord.tasks["a"].resume_to is S.READY
    b = coord.acquire(W1)
    coord.fail("b", b.token, "impossible", retryable=False)
    assert coord.tasks["b"].state is S.FAILED


def test_reviewer_infra_retries_then_blocks():
    coord, _ = make(spec("a"), max_infra_retries=1)
    a = coord.acquire(W1)
    coord.submit_work("a", a.token, work("a"))
    r = coord.acquire(R1)
    coord.fail("a", r.token, "api 529")
    assert coord.tasks["a"].state is S.AWAITING_REVIEW
    r = coord.acquire(R1)
    coord.fail("a", r.token, "api 529")
    assert coord.tasks["a"].state is S.BLOCKED and coord.scopes.holds("a")
    assert coord.resume("a") is S.AWAITING_REVIEW


def test_budget_capped_review_retries_once_then_blocks():
    coord, _ = make(spec("a"), max_infra_retries=5, max_review_budget_retries=1)
    a = coord.acquire(W1)
    coord.submit_work("a", a.token, work("a"))
    r = coord.acquire(R1)
    coord.fail("a", r.token, "review session failed: error_max_budget_usd", cost_usd=0.5, budget_exhausted=True)
    rec = coord.tasks["a"]
    assert rec.state is S.AWAITING_REVIEW and rec.review_budget_hits == 1
    r = coord.acquire(R1)
    coord.fail("a", r.token, "review session failed: error_max_budget_usd", cost_usd=0.5, budget_exhausted=True)
    assert rec.state is S.BLOCKED and "resume with a higher review_budget_usd" in rec.reason
    assert rec.infra_failures == 0 and coord.scopes.holds("a")
    assert coord.resume("a") is S.AWAITING_REVIEW and rec.review_budget_hits == 0
    r = coord.acquire(R1)
    coord.fail("a", r.token, "api 529")  # an ordinary infra error still takes the infra path
    assert rec.state is S.AWAITING_REVIEW and rec.infra_failures == 1 and rec.review_budget_hits == 0


def test_resume_raises_the_review_cap_for_that_task():
    coord, _ = make(spec("a"), spec("b", scope=["docs/"]), max_review_budget_retries=0)
    a = coord.acquire(W1)
    coord.submit_work("a", a.token, work("a"))
    r = coord.acquire(R1)
    assert r.budget_usd == 0.5  # the config's review cap
    coord.fail("a", r.token, "error_max_budget_usd", cost_usd=0.5, budget_exhausted=True)
    rec = coord.tasks["a"]
    assert rec.state is S.BLOCKED and "$0.5 budget cap" in rec.reason
    for bad in (0, -1, "abc", 1e30):  # 1e30 overflows the decimal context
        with pytest.raises(FleetError):
            coord.resume("a", review_budget_usd=bad)
    assert coord.resume("a", review_budget_usd=1.25, extra_budget_usd=2) is S.AWAITING_REVIEW
    assert coord.acquire(R1).budget_usd == 1.25 and rec.review_budget == usd_to_micros("1.25")
    resumed = [e for e in coord.events.events if e.data.get("reason") == "resumed by operator"]
    assert resumed[-1].data["review_budget_usd"] == 1.25
    b = coord.acquire(W2)
    coord.submit_work("b", b.token, work("b", files=("docs/x.md",)))
    assert coord.acquire(AgentInfo("r2", Role.REVIEWER)).budget_usd == 0.5  # other tasks keep the config cap


def test_budget_capped_worker_is_ordinary_rework():
    coord, _ = make(spec("a", max_attempts=3), max_review_budget_retries=0)
    a = coord.acquire(W1)
    coord.fail("a", a.token, "worker session failed: error_max_budget_usd", cost_usd=0.5, budget_exhausted=True)
    rec = coord.tasks["a"]
    assert rec.state is S.READY and rec.review_budget_hits == 0 and len(rec.feedback) == 1


def test_task_budget_exhaustion_blocks_and_resume_with_budget():
    coord, _ = make(spec("a", attempt_budget=usd_to_micros("2"), budget=usd_to_micros("2"), max_attempts=3))
    a = coord.acquire(W1)
    coord.fail("a", a.token, "flaky", cost_usd=1.5)
    assert coord.acquire(W1) is None
    rec = coord.tasks["a"]
    assert rec.state is S.BLOCKED and "task budget exhausted" in rec.reason
    assert coord.resume("a", "try again", extra_budget_usd=5) is S.READY
    assert coord.acquire(W1).attempt == 2


def test_run_budget_exhaustion_stalls_without_overspending():
    coord, _ = make(
        spec("a", scope=["a/"]),
        spec("b", scope=["b/"]),
        run_budget=usd_to_micros("3"),
        fleet_budget=usd_to_micros("3"),
    )
    a = coord.acquire(W1)
    assert coord.acquire(W2) is None  # $2 reserved, $1 left
    coord.fail("a", a.token, "x", cost_usd=2.0)
    assert coord.acquire(W1) is None
    reason = coord.stall_reason([W1, W2, R1, I1])
    assert "budget" in reason
    run = coord.ledger.account(coord.run_account)
    assert run.spent + run.reserved <= run.limit


def test_overrun_is_recorded_and_freezes_task():
    coord, _ = make(spec("a"))
    a = coord.acquire(W1)
    coord.fail("a", a.token, "x", cost_usd=2.5)
    acct = coord.ledger.account(task_account("a"))
    assert acct.overrun == usd_to_micros("0.5") and acct.frozen is False
    assert any(e.kind == "spend" and e.data["overrun_usd"] == 0.5 for e in coord.events.events)


def test_cancel_cascades_and_releases_everything():
    coord, _ = make(spec("a"), spec("b", deps=["a"]))
    a = coord.acquire(W1)
    assert coord.cancel("a")
    assert states(coord) == {"a": S.CANCELLED, "b": S.CANCELLED}
    assert not coord.scopes.holds("a") and not coord.leases.all()
    assert coord.ledger.open_reservations == 0
    assert not coord.cancel("a")
    with pytest.raises(StaleLease):
        coord.submit_work("a", a.token, work("a"))


def test_cancel_during_merge_is_deferred_until_outcome():
    for status, final in ((MergeStatus.MERGED, S.DONE), (MergeStatus.CONFLICT, S.CANCELLED)):
        coord, _ = make(spec("a"))
        a = coord.acquire(W1)
        coord.submit_work("a", a.token, work("a"))
        r = coord.acquire(R1)
        coord.submit_review("a", r.token, ReviewVerdict(True, "ok"))
        m = coord.acquire(I1)
        assert coord.cancel("a")
        assert coord.tasks["a"].state is S.MERGING
        coord.submit_merge("a", m.token, MergeOutcome(status, "m"))
        assert coord.tasks["a"].state is final
        if final is S.DONE:
            assert any(e.kind == "cancel_superseded" for e in coord.events.events)


def test_lost_integrator_lease_forces_reconciliation():
    coord, clock = make(spec("a"))
    a = coord.acquire(W1)
    coord.submit_work("a", a.token, work("a"))
    r = coord.acquire(R1)
    coord.submit_review("a", r.token, ReviewVerdict(True, "ok"))
    m1 = coord.acquire(I1)
    clock.advance(51)
    coord.tick()
    rec = coord.tasks["a"]
    assert rec.state is S.AWAITING_MERGE and rec.merge_unresolved
    with pytest.raises(FleetError, match="merge in flight"):
        coord.block("a")
    assert coord.cancel("a") and rec.state is S.AWAITING_MERGE and rec.cancel_requested
    m2 = coord.acquire(I2)
    assert m2.token > m1.token
    with pytest.raises(StaleLease):
        coord.submit_merge("a", m1.token, MergeOutcome(MergeStatus.MERGED, "late"))
    coord.submit_merge("a", m2.token, MergeOutcome(MergeStatus.MERGED, "m", "already on target (reconciled)"))
    assert rec.state is S.DONE and not rec.merge_unresolved


def test_merge_outcomes_route_correctly():
    routes = {
        MergeStatus.CONFLICT: S.READY,
        MergeStatus.VERIFY_FAILED: S.READY,
        MergeStatus.TARGET_MOVED: S.AWAITING_MERGE,
        MergeStatus.FENCED: S.AWAITING_MERGE,
        MergeStatus.SCOPE_VIOLATION: S.BLOCKED,
    }
    for status, expected in routes.items():
        coord, _ = make(spec("a"))
        a = coord.acquire(W1)
        coord.submit_work("a", a.token, work("a"))
        r = coord.acquire(R1)
        coord.submit_review("a", r.token, ReviewVerdict(True, "ok"))
        m = coord.acquire(I1)
        coord.submit_merge("a", m.token, MergeOutcome(status, detail="d"))
        assert coord.tasks["a"].state is expected, status


def test_block_and_resume_from_each_state():
    coord, _ = make(spec("a"), spec("b", deps=["a"]))
    assert coord.block("b") and coord.tasks["b"].resume_to is S.PENDING
    a = coord.acquire(W1)
    assert coord.block("a")
    assert coord.tasks["a"].state is S.BLOCKED and coord.ledger.open_reservations == 0
    with pytest.raises(StaleLease):
        coord.heartbeat("a", a.token)
    assert not coord.block("a")
    assert coord.resume("a", "go") is S.READY
    assert "operator: go" in coord.tasks["a"].feedback
    assert coord.resume("b") is S.PENDING
    with pytest.raises(FleetError, match="not blocked"):
        coord.resume("a")


def test_resume_requires_attempts():
    coord, _ = make(spec("a", max_attempts=1))
    a = coord.acquire(W1)
    coord.fail("a", a.token, "help", escalate=True)
    with pytest.raises(FleetError, match="extra_attempts"):
        coord.resume("a")
    assert coord.tasks["a"].state is S.BLOCKED
    assert coord.resume("a", extra_attempts=1) is S.READY


def test_stall_reason_none_while_work_possible():
    coord, _ = make(spec("a"))
    agents = [W1, R1, I1]
    assert coord.stall_reason(agents) is None
    coord.acquire(W1)
    assert coord.stall_reason(agents) is None
    coord2, _ = make(spec("a"))
    assert coord2.stall_reason([W1]) is None


def test_aging_lets_old_low_priority_work_overtake():
    from fleet_a import scheduler
    from fleet_a.coordinator import TaskRecord
    from fleet_a.dag import TaskGraph

    old = TaskRecord(spec("old", scope=["o/"], priority=0), 3, S.READY, state_since=0.0)
    new = TaskRecord(spec("new", scope=["n/"], priority=2), 3, S.READY, state_since=95.0)
    graph = TaskGraph([old.spec, new.spec])
    assert scheduler.age_bonus(29.9, 10.0) == 2 and scheduler.age_bonus(5.0, 0) == 0
    assert [r.spec.id for r in scheduler.rank(Role.WORKER, [old, new], 100.0, 0, graph)] == ["new", "old"]
    assert [r.spec.id for r in scheduler.rank(Role.WORKER, [old, new], 100.0, 30.0, graph)] == ["old", "new"]
    assert [r.spec.id for r in scheduler.rank(Role.INTEGRATOR, [new, old], 100.0, 30.0, graph)] == ["old", "new"]


def test_summary_and_log_projection():
    coord, _ = make(spec("a"), spec("b", scope=["b/"]))
    land(coord, "a")
    s = coord.summary()
    assert s["states"] == {"done": 1, "ready": 1} and s["tasks"]["a"]["merged_commit"] == "m-a"
    EventLog.verify(coord.events.events)
    assert project_states(coord.events.events) == {"a": "done", "b": "ready"}
