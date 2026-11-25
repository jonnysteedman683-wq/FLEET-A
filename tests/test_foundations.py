"""Scopes, DAG, plan/config parsing, money, event log, ledger, leases and the state machine."""

from __future__ import annotations

import json

import pytest

from conftest import plan_of, spec

from fleet_a.budget import Ledger
from fleet_a.config import FleetConfig
from fleet_a.dag import TaskGraph
from fleet_a.errors import (
    BudgetExceeded,
    ConfigError,
    FleetError,
    IllegalTransition,
    PlanError,
    StaleLease,
)
from fleet_a.events import EventLog, project_states
from fleet_a.fsm import TRANSITIONS, check_transition
from fleet_a.leases import LeaseTable
from fleet_a.model import TERMINAL_STATES, Plan, Role, TaskState, usd_to_micros
from fleet_a.scopes import (
    WHOLE_REPO,
    ScopeLocks,
    entries_overlap,
    normalize_entry,
    normalize_scope,
    scope_violations,
)

# ------------------------------------------------------------------------------------ scopes


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("src/api/", "src/api/"),
        ("./src//api/", "src/api/"),
        ("src/app.py", "src/app.py"),
        ("*", WHOLE_REPO),
        ("/", WHOLE_REPO),
        (".", WHOLE_REPO),
    ],
)
def test_normalize_entry(raw, expected):
    assert normalize_entry(raw) == expected


@pytest.mark.parametrize("raw", ["", "/etc/passwd", "src/../x", "src/*.py", ".git/config", "a\\b", 3])
def test_normalize_entry_rejects(raw):
    with pytest.raises(PlanError):
        normalize_entry(raw)


def test_normalize_scope_dedupes_and_collapses():
    assert normalize_scope(["src/a.py", "src/", "docs/x.md", "src/b/"]) == ("docs/x.md", "src/")
    assert normalize_scope(["src/", "*"]) == (WHOLE_REPO,)
    with pytest.raises(PlanError):
        normalize_scope([])
    with pytest.raises(PlanError):
        normalize_scope("src/")


@pytest.mark.parametrize(
    "a, b, overlap",
    [
        ("src/", "src/api/", True),
        ("src/api/x.py", "src/", True),
        ("src/a.py", "src/a.py", True),
        ("src/a", "src/a/", True),
        ("src/a.py", "src/b.py", False),
        ("src/api/", "src/apiv2/", False),
        ("docs/", "src/", False),
        (WHOLE_REPO, "anything/at/all.py", True),
    ],
)
def test_entries_overlap(a, b, overlap):
    assert entries_overlap(a, b) is overlap
    assert entries_overlap(b, a) is overlap


def test_scope_violations():
    assert scope_violations(("src/",), ["src/a.py", "README.md", "srcx/y.py"]) == ["README.md", "srcx/y.py"]
    assert scope_violations((WHOLE_REPO,), ["x", "y/z"]) == []


def test_scope_locks_all_or_nothing():
    locks = ScopeLocks()
    assert locks.try_acquire("A", ("src/api/", "docs/"))
    assert not locks.try_acquire("B", ("src/", "tests/"))
    assert not locks.holds("B")
    assert locks.blockers("B", ("docs/a.md",)) == ["A"]
    assert locks.try_acquire("C", ("tests/",))
    with pytest.raises(FleetError):
        locks.try_acquire("A", ("x/",))
    locks.release("A")
    assert locks.try_acquire("B", ("src/",))


# --------------------------------------------------------------------------------------- dag


def test_graph_order_depth_and_critical_path():
    g = TaskGraph(
        [
            spec("a"),
            spec("b", deps=["a"]),
            spec("c", deps=["a"]),
            spec("d", deps=["b", "c"]),
            spec("e"),
        ]
    )
    assert g.topo_order == ("a", "b", "c", "d", "e")
    assert g.depth("a") == 3 and g.depth("e") == 1 and g.depth("d") == 1
    assert g.critical_path() == ("a", "b", "d")
    assert g.descendants("a") == ("b", "c", "d")
    assert g.ancestors("d") == ("a", "b", "c")
    assert g.dependents("a") == ("b", "c")


def test_graph_rejects_cycles_with_path():
    with pytest.raises(PlanError, match="cycle: (a -> c -> b -> a|b -> a -> c -> b|c -> b -> a -> c)"):
        TaskGraph([spec("a", deps=["c"]), spec("b", deps=["a"]), spec("c", deps=["b"]), spec("d")])


@pytest.mark.parametrize(
    "specs, message",
    [
        ([spec("a", deps=["a"])], "itself"),
        ([spec("a", deps=["zz"])], "unknown dependency"),
        ([spec("a"), spec("a")], "duplicate"),
    ],
)
def test_graph_rejects_bad_edges(specs, message):
    with pytest.raises(PlanError, match=message):
        TaskGraph(specs)


# ------------------------------------------------------------------------------ plan/config


def test_plan_round_trip_and_strict_keys():
    raw = {
        "id": "demo",
        "goal": "g",
        "tasks": [
            {
                "id": "t1",
                "title": "T",
                "description": "D",
                "write_scope": ["src/"],
                "attempt_budget_usd": "1.25",
                "budget_usd": "10",
                "capabilities": ["python"],
            }
        ],
    }
    plan = Plan.from_dict(raw)
    assert plan.tasks[0].attempt_budget == 1_250_000
    assert Plan.from_dict(plan.to_dict()) == plan
    with pytest.raises(PlanError, match="unknown keys"):
        Plan.from_dict({**raw, "extra": 1})
    with pytest.raises(PlanError, match="unknown keys"):
        Plan.from_dict({**raw, "tasks": [{**raw["tasks"][0], "scope": []}]})
    with pytest.raises(PlanError, match="task id"):
        Plan.from_dict({**raw, "tasks": [{**raw["tasks"][0], "id": "bad id"}]})
    with pytest.raises(PlanError, match="budget_usd must be >="):
        Plan.from_dict({**raw, "tasks": [{**raw["tasks"][0], "budget_usd": "1"}]})
    for unsafe in ("a..b", "x.lock", "x."):
        with pytest.raises(PlanError, match="must match"):
            Plan.from_dict({**raw, "tasks": [{**raw["tasks"][0], "id": unsafe}]})


def test_money_is_exact():
    assert usd_to_micros("0.1") + usd_to_micros("0.2") == usd_to_micros("0.3")
    assert usd_to_micros(2) == 2_000_000
    for bad in ("-1", "nan", "inf", "abc", True):
        with pytest.raises(ValueError):
            usd_to_micros(bad)


def test_config_from_dict_merges_and_validates():
    cfg = FleetConfig.from_dict(
        {
            "run_budget_usd": "50",
            "lease_ttl_s": {"worker": 120},
            "agents": [{"role": "worker", "count": 3, "capabilities": ["python"]}, {"role": "reviewer"}, {"role": "integrator"}],
            "models": {"worker": {"effort": "high"}},
            "verify_command": ["make", "test"],
        }
    )
    assert cfg.run_budget == 50_000_000
    assert cfg.lease_ttl_s[Role.WORKER] == 120 and cfg.lease_ttl_s[Role.REVIEWER] == 600
    assert cfg.models["worker"].effort == "high" and cfg.models["worker"].model == "claude-sonnet-5-5"
    assert cfg.verify_command == ("make", "test")
    assert cfg.verify_before_review is True and cfg.max_review_budget_retries == 1
    # 3 attempts x ($2 + $1.50 review) + 1 review retry x $1.50
    assert FleetConfig().default_task_budget(2_000_000, 3) == 3 * 3_500_000 + 1_500_000
    tuned = FleetConfig.from_dict({"verify_before_review": False, "max_review_budget_retries": 0})
    assert tuned.verify_before_review is False and tuned.max_review_budget_retries == 0
    for bad in (
        {"verify_before_review": "false"},
        {"max_review_budget_retries": -1},
        {"max_review_budget_retries": True},
        {"verify_timeout_s": float("nan")},
        {"verify_timeout_s": float("inf")},
        {"run_budget_usd": "1e40"},
        {"typo_budget": 1},
        {"max_concurrent": {"integrator": 2}},
        {"models": {"worker": {"permission_mode": "bypassPermissions"}}},
        {"models": {"worker": {"effort": "extreme"}}},
        {"run_budget_usd": "600"},
        {"review_budget_usd": "0"},
        {"agents": [{"role": "worker"}, {"role": "worker"}]},
    ):
        with pytest.raises(ConfigError):
            FleetConfig.from_dict(bad)


# ------------------------------------------------------------------------------- event log


def test_event_log_chain_and_tamper_detection(tmp_path):
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    log.append("plan", None, {"tasks": ["a"]}, 0.0)
    log.append("transition", "a", {"from": "pending", "to": "ready"}, 1.0)
    log.append("transition", "a", {"from": "ready", "to": "running"}, 2.0)
    log.close()
    events = EventLog.load(path)
    EventLog.verify(events)
    assert project_states(events) == {"a": "running"}

    lines = path.read_text().splitlines()
    tampered = json.loads(lines[1])
    tampered["data"]["to"] = "done"
    lines[1] = json.dumps(tampered)
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(FleetError, match="event 1"):
        EventLog.verify(EventLog.load(path))
    with pytest.raises(FleetError, match="already exists"):
        EventLog(path)
    with pytest.raises(FleetError, match="sequence"):
        EventLog.verify(events[1:])


def test_projection_rejects_inconsistent_log():
    log = EventLog()
    log.append("plan", None, {"tasks": ["a"]}, 0.0)
    log.append("transition", "a", {"from": "ready", "to": "running"}, 1.0)
    with pytest.raises(FleetError, match="projected state"):
        project_states(log.events)


# ---------------------------------------------------------------------------------- ledger


def test_ledger_reserve_commit_hierarchy():
    ledger = Ledger()
    ledger.open("fleet", 100)
    ledger.open("run", 60, "fleet")
    ledger.open("t1", 50, "run")
    ledger.open("t2", 50, "run")
    r1 = ledger.reserve("t1", 40)
    assert ledger.available("t2") == 20
    with pytest.raises(BudgetExceeded) as exc:
        ledger.reserve("t2", 30)
    assert exc.value.account == "run"
    assert ledger.commit(r1, 10) == 0
    assert ledger.account("run").spent == 10 and ledger.account("run").reserved == 0
    with pytest.raises(FleetError, match="not open"):
        ledger.commit(r1, 1)


def test_ledger_overrun_freezes_until_limit_raised():
    ledger = Ledger()
    ledger.open("root", 100)
    ledger.open("t", 10, "root")
    res = ledger.reserve("t", 10)
    assert ledger.commit(res, 15) == 5
    t = ledger.account("t")
    assert t.frozen and t.overrun == 5 and t.spent == 15
    assert t.spent + t.reserved <= t.limit + t.overrun
    with pytest.raises(BudgetExceeded):
        ledger.reserve("t", 0)
    ledger.raise_limit("t", 10)
    assert not ledger.account("t").frozen
    ledger.reserve("t", 5)


# ---------------------------------------------------------------------------------- leases


def test_lease_tokens_fencing_and_expiry():
    table = LeaseTable()
    a = table.grant("t1", "w1", Role.WORKER, now=0.0, ttl=10.0)
    b = table.grant("t2", "w2", Role.WORKER, now=0.0, ttl=10.0)
    assert b.token > a.token
    with pytest.raises(FleetError):
        table.grant("t1", "w3", Role.WORKER, now=1.0, ttl=10.0)
    assert table.renew("t1", a.token, now=5.0, ttl=10.0).expires_at == 15.0
    with pytest.raises(StaleLease):
        table.check("t1", a.token + 100, now=6.0)
    assert [l.task_id for l in table.expired(now=12.0)] == ["t2"]
    with pytest.raises(StaleLease, match="expired"):
        table.check("t1", a.token, now=15.0)
    table.drop("t1")
    c = table.grant("t1", "w3", Role.WORKER, now=16.0, ttl=10.0)
    assert c.token > b.token
    with pytest.raises(StaleLease):
        table.check("t1", a.token, now=17.0)


# ---------------------------------------------------------------------------- state machine


def test_terminal_states_have_no_exits_and_every_state_is_reachable():
    for state in TERMINAL_STATES:
        assert not TRANSITIONS[state]
    reachable = {TaskState.PENDING}
    frontier = [TaskState.PENDING]
    while frontier:
        for nxt in TRANSITIONS[frontier.pop()]:
            if nxt not in reachable:
                reachable.add(nxt)
                frontier.append(nxt)
    assert reachable == set(TaskState)


def test_illegal_transitions_raise():
    check_transition(TaskState.READY, TaskState.RUNNING)
    for src, dst in [
        (TaskState.PENDING, TaskState.RUNNING),
        (TaskState.READY, TaskState.DONE),
        (TaskState.AWAITING_REVIEW, TaskState.AWAITING_MERGE),
        (TaskState.DONE, TaskState.READY),
        (TaskState.RUNNING, TaskState.DONE),
    ]:
        with pytest.raises(IllegalTransition):
            check_transition(src, dst)


def test_plan_requires_tasks():
    with pytest.raises(PlanError):
        Plan(id="p", goal="", tasks=())
    assert plan_of(spec("a")).task("a").id == "a"


# ------------------------------------------------------------------------ capability coverage


def test_capability_gaps_and_plannable_tags():
    from fleet_a.config import AgentPool
    from fleet_a.prompts import plan_schema

    cfg = FleetConfig(
        agents=(
            AgentPool(Role.WORKER, 2, frozenset({"python", "git"})),
            AgentPool(Role.WORKER, 1, frozenset({"rust"}), prefix="rusty"),
            AgentPool(Role.REVIEWER, 1, frozenset({"python", "git"})),
            AgentPool(Role.INTEGRATOR, 1),
        )
    )
    assert cfg.plannable_capabilities() == frozenset({"python", "git"})
    ok = spec("ok", capabilities=frozenset({"python"}))
    no_reviewer = spec("rs", capabilities=frozenset({"rust"}))
    nobody = spec("go", capabilities=frozenset({"go"}))
    gaps = cfg.capability_gaps([ok, no_reviewer, nobody])
    assert gaps == [
        "task rs: no reviewer pool has capabilities ['rust']",
        "task go: no worker pool has capabilities ['go']",
        "task go: no reviewer pool has capabilities ['go']",
    ]
    assert FleetConfig().plannable_capabilities() == frozenset()
    assert FleetConfig().capability_gaps([ok]) == [
        "task ok: no worker pool has capabilities ['python']",
        "task ok: no reviewer pool has capabilities ['python']",
    ]
    caps = plan_schema(["python", "git"])["properties"]["tasks"]["items"]["properties"]["capabilities"]
    assert caps == {"type": "array", "items": {"type": "string", "enum": ["git", "python"]}}
    assert plan_schema([])["properties"]["tasks"]["items"]["properties"]["capabilities"] == {"type": "array", "maxItems": 0}


def test_planner_learns_which_tags_combine():
    """Gitar on #4: tags from two different worker pools must not be offered as one combination."""
    from fleet_a.config import AgentPool
    from fleet_a.prompts import planner_prompt

    cfg = FleetConfig(
        agents=(
            AgentPool(Role.WORKER, 1, frozenset({"python"}), prefix="py"),
            AgentPool(Role.WORKER, 1, frozenset({"rust"}), prefix="rs"),
            AgentPool(Role.REVIEWER, 1, frozenset({"python", "rust"})),
            AgentPool(Role.INTEGRATOR, 1),
        )
    )
    assert cfg.plannable_capabilities() == frozenset({"python", "rust"})
    assert cfg.plannable_tag_sets() == [frozenset({"python"}), frozenset({"rust"})]
    assert cfg.capability_gaps([spec("both", capabilities=frozenset({"python", "rust"}))]) == [
        "task both: no worker pool has capabilities ['python', 'rust']"
    ]
    prompt = planner_prompt("g", cfg.plannable_tag_sets())
    assert "subset of ONE of these sets: {python}; {rust}" in prompt
    wide = FleetConfig(
        agents=(
            AgentPool(Role.WORKER, 1, frozenset({"git", "python"})),
            AgentPool(Role.REVIEWER, 1, frozenset({"git", "python", "sql"})),
            AgentPool(Role.INTEGRATOR, 1),
        )
    )
    assert wide.plannable_tag_sets() == [frozenset({"git", "python"})]
    assert set().union(*wide.plannable_tag_sets()) == wide.plannable_capabilities()
    assert FleetConfig().plannable_tag_sets() == []
    assert "defines no capability tags" in planner_prompt("g", [])
