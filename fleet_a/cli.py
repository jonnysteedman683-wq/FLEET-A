"""Command line: validate, plan, run, simulate, verify-log.

Exit codes: 0 success; 1 invalid input or failed check; 2 the run ended with failed or
cancelled tasks; 3 the run stalled or timed out.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Sequence

from .config import FleetConfig
from .coordinator import Coordinator, SystemClock
from .dag import TaskGraph
from .errors import FleetError
from .events import EventLog, project_states
from .executors import ClaudeCodeExecutor, Executor
from .git import Git
from .guard import RepoGuard
from .mergequeue import MergeQueue, landed_tasks
from .model import AgentInfo, Plan, Role, TaskState, format_usd, micros_to_usd
from .planner import plan_goal
from .runner import AgentRunner, Fleet
from .sim import SimConfig, simulate
from .workspaces import Workspaces, create_worker_clone


def _load_json(path: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FleetError(f"cannot read {path}: {exc}") from exc


def _load_config(path: str | None) -> FleetConfig:
    return FleetConfig.from_dict(_load_json(path)) if path else FleetConfig()


def _emit(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def cmd_validate(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    plan = Plan.from_dict(_load_json(args.plan))
    graph = TaskGraph(plan.tasks)
    budgets = {
        t.id: t.budget if t.budget is not None else config.default_task_budget(t.attempt_budget, t.max_attempts)
        for t in plan.tasks
    }
    total = sum(budgets.values())
    gaps = config.capability_gaps(plan.tasks)
    _emit(
        {
            "plan": plan.id,
            "tasks": len(plan.tasks),
            "topological_order": list(graph.topo_order),
            "critical_path": list(graph.critical_path()),
            "worst_case_spend_usd": micros_to_usd(total),
            "run_budget_usd": micros_to_usd(config.run_budget),
            "fits_run_budget": total <= config.run_budget,
            "unservable": gaps,
        }
    )
    if total > config.run_budget:
        print(
            f"note: worst-case task budgets {format_usd(total)} exceed the run budget "
            f"{format_usd(config.run_budget)}; the run stalls if it is spent",
            file=sys.stderr,
        )
    if gaps:
        print("error: " + "; ".join(gaps), file=sys.stderr)
        return 1
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    executor = ClaudeCodeExecutor(args.claude)
    plan, cost = plan_goal(args.goal, Path(args.repo), executor, config, args.id)
    text = json.dumps(plan.to_dict(), indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    print(f"planned {len(plan.tasks)} tasks for {format_usd(round(cost * 1_000_000))}", file=sys.stderr)
    return 0


def build_fleet(
    repo: Path,
    plan: Plan,
    config: FleetConfig,
    *,
    events_path: Path | None,
    worktrees: Path,
    claude_binary: str,
    executor: Executor | None = None,
) -> tuple[Coordinator, Fleet]:
    git = Git(repo, name=config.git_name, email=config.git_email)
    git.rev_parse(f"refs/heads/{config.target_branch}")
    guard = RepoGuard(git, config.target_branch)
    # Workers and reviewers get worktrees of a separate clone; only the integrator touches ``repo``.
    worker_git = create_worker_clone(git, worktrees / "worker-clone.git")
    workspaces = Workspaces(worker_git, worktrees / "work", upstream=git)
    merge_queue = MergeQueue(
        git,
        Workspaces(git, worktrees / "integrator"),
        config.target_branch,
        config.verify_command,
        config.verify_timeout_s,
        guard=guard,
        source=worker_git,
    )
    executor = executor or ClaudeCodeExecutor(claude_binary)
    coordinator = Coordinator(plan, config, SystemClock(), EventLog(events_path, fsync=True))
    runners = []
    for pool in config.agents:
        for agent_id in pool.agent_ids():
            agent = AgentInfo(agent_id, pool.role, pool.capabilities)
            runners.append(
                AgentRunner(
                    coordinator,
                    agent,
                    config,
                    executor=executor if pool.role is not Role.INTEGRATOR else None,
                    triage_executor=executor if pool.role is Role.WORKER else None,
                    workspaces=workspaces,
                    merge_queue=merge_queue if pool.role is Role.INTEGRATOR else None,
                    guard=guard,
                )
            )
    return coordinator, Fleet(coordinator, runners, config.poll_interval_s)


def cmd_run(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    plan = Plan.from_dict(_load_json(args.plan))
    gaps = config.capability_gaps(plan.tasks)
    if gaps:
        raise FleetError(
            "refusing to start: " + "; ".join(gaps) + ". Fix the plan's capabilities or the config's agents"
        )
    repo = Path(args.repo).resolve()
    worktrees = Path(args.worktrees).resolve() if args.worktrees else repo.parent / f".fleet-a-{plan.id}"
    events_path = Path(args.events) if args.events else worktrees / "events.jsonl"
    coordinator, fleet = build_fleet(
        repo, plan, config, events_path=events_path, worktrees=worktrees, claude_binary=args.claude
    )
    report = fleet.run(timeout_s=args.timeout)
    coordinator.events.close()
    _emit(
        {
            "finished": report.finished,
            "stall_reason": report.stall_reason,
            "timed_out": report.timed_out,
            "errors": report.errors,
            "events": str(events_path),
            **report.summary,
        }
    )
    if not report.finished:
        return 3
    states = {r.state for r in coordinator.tasks.values()}
    return 0 if states == {TaskState.DONE} else 2


def cmd_simulate(args: argparse.Namespace) -> int:
    failures = 0
    rows = []
    for seed in range(args.seed, args.seed + args.runs):
        report = simulate(
            SimConfig(
                seed=seed,
                tasks=args.tasks,
                workers=args.workers,
                reviewers=args.reviewers,
                integrators=args.integrators,
                run_budget_usd=args.run_budget,
            )
        )
        failures += not report.ok
        rows.append(report.to_dict() if args.verbose else {
            "seed": seed,
            "ok": report.ok,
            "finished": report.finished,
            "steps": report.steps,
            "states": report.states,
            "stale_rejected": report.stale_rejected,
            "merges": report.merges,
            "violations": report.violations[:5],
            "stall_reason": report.stall_reason,
        })
    _emit({"runs": args.runs, "failed": failures, "results": rows})
    return 1 if failures else 0


def cmd_verify_log(args: argparse.Namespace) -> int:
    events = EventLog.load(Path(args.log))
    EventLog.verify(events)
    states = project_states(events)
    counts: dict[str, int] = {}
    for state in states.values():
        counts[state] = counts.get(state, 0) + 1
    _emit({"events": len(events), "chain": "ok", "tasks": counts})
    return 0


def cmd_landed(args: argparse.Namespace) -> int:
    git = Git(Path(args.repo))
    git.rev_parse(f"refs/heads/{args.target}")
    landed = landed_tasks(git, f"refs/heads/{args.target}", args.run)
    _emit({"run": args.run, "target_branch": args.target, "landed": landed, "count": len(landed)})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fleet_a", description="FLEET-A: a fleet of autonomous coding agents")
    parser.add_argument("-v", "--log-level", default="WARNING", help="logging level (default WARNING)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="validate a plan and report order, critical path and worst-case spend")
    p.add_argument("plan")
    p.add_argument("--config")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("plan", help="ask the planner model to turn a goal into a plan")
    p.add_argument("--repo", required=True)
    p.add_argument("--goal", required=True)
    p.add_argument("--id")
    p.add_argument("--config")
    p.add_argument("--out")
    p.add_argument("--claude", default="claude", help="Claude Code binary")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("run", help="execute a plan against a repository")
    p.add_argument("--repo", required=True)
    p.add_argument("--plan", required=True)
    p.add_argument("--config")
    p.add_argument("--events", help="event log path (default <worktrees>/events.jsonl)")
    p.add_argument("--worktrees", help="worktree directory (default ../.fleet-a-<plan id>)")
    p.add_argument("--timeout", type=float, help="stop dispatching after this many seconds")
    p.add_argument("--claude", default="claude", help="Claude Code binary")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("simulate", help="run the fault-injecting simulator and check invariants")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--runs", type=int, default=20)
    p.add_argument("--tasks", type=int, default=25)
    p.add_argument("--workers", type=int, default=5)
    p.add_argument("--reviewers", type=int, default=2)
    p.add_argument("--integrators", type=int, default=2)
    p.add_argument("--run-budget", default="10000")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_simulate)

    p = sub.add_parser("verify-log", help="verify an event log's hash chain and replay task states")
    p.add_argument("log")
    p.set_defaults(func=cmd_verify_log)

    p = sub.add_parser("landed", help="list a run's tasks whose changes are on the target branch (read from git)")
    p.add_argument("--repo", required=True)
    p.add_argument("--run", required=True, help="run id, from the run summary or the log's plan event")
    p.add_argument("--target", default="main", help="target branch (default main)")
    p.set_defaults(func=cmd_landed)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s")
    try:
        return int(args.func(args))
    except FleetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
