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
python3 -m pytest -q                          # 223 tests, about 35 s
python3 -m fleet_a simulate --runs 200        # randomized fault-injection runs; exits 1 on any violation
```

## Cost

Every attempt costs one worker session and, if it passes the tests, one review session; the planner runs once per run and the integrator spends nothing. Each phase reserves its budget before it starts, and no phase starts once `run_budget_usd` cannot cover it. These settings decide how much each phase costs. The figures were measured in this project's own runs.

| Setting | What it does | Measured |
|---|---|---|
| Session isolation (always on) | Sessions start with `--setting-sources project --strict-mcp-config --exclude-dynamic-system-prompt-sections`, so the host's MCP servers, skills and user settings stay out of the prompt | a minimal session costs about $0.004, against about $0.039 with `--strict-mcp-config` alone |
| `models.reviewer` | The reviewer is the most frequent paid session after the worker | Opus 5.5 reviews cost $0.13 to $0.33 each, Sonnet 5.5 reviews $0.05 to $0.17 (on different tasks, so a rough guide) |
| `verify_before_review` (default `true`) | Runs `verify_command` in the worker's worktree before review. An attempt that fails goes back to rework with the test output, and no review is paid for it. If the target branch moved since the attempt began, a failure is reviewed anyway and left to the merge queue's verify on the rebased tree, so a stale base never costs an attempt | saves one review per broken attempt; costs test time, since passing attempts are verified again on the rebased tree at merge |
| `max_review_budget_retries` (default `1`) | A review stopped at `review_budget_usd` is retried this many times, then the task is blocked until it is resumed with a higher per-task cap (`Coordinator.resume(task, review_budget_usd=...)`). A CLI `run` exits on the stall (code 3): raise `review_budget_usd` in the config for the next run | each retry at the same cap can spend the whole cap again; one capped review here cost $0.33 against a $0.30 cap |
| `attempt_budget_usd`, `max_attempts` (per task, in the plan) | The worker's cap per attempt and the number of attempts | two small work orders built and reviewed with Sonnet 5.5 cost $0.44 in total at $0.30 per attempt |

[`examples/fleet-economy.json`](examples/fleet-economy.json) combines the cheaper settings: Sonnet 5.5 reviewers at medium effort with a $0.50 review cap, two workers, a $10 run budget, and Opus 5.5 kept for the single planning session. Give the plan's tasks a smaller `attempt_budget_usd` (for example `"0.50"`) and `max_attempts` of 2, or `validate` will warn that the worst case exceeds the run budget. Sonnet reviews are a weaker check than Opus reviews; keep `examples/fleet.json` for security-sensitive or wide changes.

## Layout

| Path | Contents |
|---|---|
| `fleet_a/coordinator.py` | state machine driver, leases, scope locks, budget admission, cancel/block/resume |
| `fleet_a/fsm.py` | the complete transition table |
| `fleet_a/scheduler.py` | dispatch order: priority with aging, critical-path depth, FIFO merges |
| `fleet_a/leases.py`, `scopes.py`, `budget.py`, `dag.py`, `events.py` | leases and fencing tokens, write scopes, the ledger, the task graph, the hash-chained log |
| `fleet_a/runner.py` | the per-agent loop with heartbeats, and the multi-threaded `Fleet` |
| `fleet_a/executors.py`, `prompts.py` | headless Claude Code sessions per role, prompts and output schemas |
| `fleet_a/workspaces.py`, `mergequeue.py`, `git.py`, `guard.py` | per-lease worktrees in a worker clone separate from the landing repository, the fenced, idempotent integrator, and the check that blocks a session which moved the target or changed git config or hooks |
| `fleet_a/planner.py` | goal to validated plan |
| `fleet_a/sim.py` | fault-injecting simulator and invariant checks |
| `examples/` | a five-task plan and a full configuration |
| `tests/` | unit, real-git integration, end-to-end, simulator and mutation tests |
