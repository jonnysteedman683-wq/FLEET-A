# FLEET-A

A fleet of autonomous coding agents that work on one repository in parallel and land their changes safely.

A planner turns a goal into a dependency graph of tasks. Workers implement tasks in isolated git worktrees, reviewers on a stronger model approve or reject each attempt, and a deterministic integrator rebases, verifies and lands approved work on the target branch one change at a time. A single coordinator owns all task state and enforces leases with fencing tokens, non-overlapping write scopes, separation of duties, and a reserve-before-spend budget.

The design is specified in **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**. Its safety invariants are checked by a fault-injecting simulator, and mutation tests confirm that each safety mechanism is necessary.

![How one task moves through FLEET-A: lease and token, one squashed commit, independent review, then a fenced atomic landing on main. Late reports with expired tokens are rejected.](docs/fleet-a-task-flow.svg)

## Requirements

- Python 3.11+ (standard library only)
- git 2.27+
- [Claude Code](https://code.claude.com) (`claude`) authenticated on the machine, for `plan` and `run`

## Use

```bash
# 1. Plan: one read-only Opus 5.5 session reads the repo and writes a task graph
python3 -m fleet_a plan --repo ../myrepo --goal "Add per-client rate limiting to the API" --out plan.json

# 2. Check the plan: order, critical path, worst-case spend against the budget
python3 -m fleet_a validate plan.json --config examples/fleet.json

# 3. Run the fleet (in an isolated container: see docs/ARCHITECTURE.md section 12)
python3 -m fleet_a run --repo ../myrepo --plan plan.json --config examples/fleet.json

# 4. Audit: verify the event log's hash chain and replay final task states
python3 -m fleet_a verify-log ../.fleet-a-<plan-id>/events.jsonl
```

`run` exits 0 when every task landed, 2 if some failed or were cancelled, and 3 if the run stalled (for example, out of budget or waiting for an operator) or timed out.

Use a dedicated clone of the repository with the target branch not checked out. Push the target branch yourself after reviewing the run summary.

## Verify the design

```bash
python3 -m pytest -q                          # 150 tests, about 15 s
python3 -m fleet_a simulate --runs 200        # randomized fault-injection runs; exits 1 on any violation
```

## Layout

| Path | Contents |
|---|---|
| `fleet_a/coordinator.py` | state machine driver, leases, scope locks, budget admission, cancel/block/resume |
| `fleet_a/fsm.py` | the complete transition table |
| `fleet_a/scheduler.py` | dispatch order: priority with aging, critical-path depth, FIFO merges |
| `fleet_a/leases.py`, `scopes.py`, `budget.py`, `dag.py`, `events.py` | leases and fencing tokens, write scopes, the ledger, the task graph, the hash-chained log |
| `fleet_a/runner.py` | the per-agent loop with heartbeats, and the multi-threaded `Fleet` |
| `fleet_a/executors.py`, `prompts.py` | headless Claude Code sessions per role, prompts and output schemas |
| `fleet_a/workspaces.py`, `mergequeue.py`, `git.py` | per-lease worktrees and the fenced, idempotent integrator |
| `fleet_a/planner.py` | goal to validated plan |
| `fleet_a/sim.py` | fault-injecting simulator and invariant checks |
| `examples/` | a five-task plan and a full configuration |
| `tests/` | unit, real-git integration, end-to-end, simulator and mutation tests |
