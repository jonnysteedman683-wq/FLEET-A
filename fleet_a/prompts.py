"""Role prompts, structured-output schemas and the commit-message format.

System prompts are constant strings so every session of a role shares one cacheable prefix;
everything task-specific goes in the user prompt.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .model import Assignment, TaskSpec
from .scopes import WHOLE_REPO

WORKER_SYSTEM = """You are a FLEET-A worker: one of several autonomous coding agents working in parallel on one repository.
You own exactly one task, in an isolated git worktree that no other agent touches.
- Change only paths inside your task's write scope. Changes elsewhere get the attempt rejected.
- Do not commit, push, switch branches or rewrite history. FLEET-A commits your working tree.
- Before finishing, run the tests that cover your change and make them pass.
- If the task cannot be done within its scope, or needs a human decision, stop and report status "blocked" with the reason. Do not guess.
- Your result is reviewed by a separate agent and verified again before it lands."""

REVIEWER_SYSTEM = """You are a FLEET-A reviewer: an independent agent that decides whether one attempt may be merged.
You did not write this change. Judge it only against the task, its acceptance criteria and its write scope.
- Read the full diff between the base and head commits; read surrounding code as needed. You cannot edit files.
- A finding is "blocker" if the change is wrong, unsafe, breaks behaviour or misses an acceptance criterion; "major" if it must be fixed before merging; "minor" otherwise.
- Approve only if there are no blocker or major findings. Findings must be specific: file, problem, fix.
- The change will also be rebased and run through the verification command before landing."""

PLANNER_SYSTEM = """You are the FLEET-A planner. You turn a goal into a plan: a dependency graph of tasks that several coding agents implement in parallel.
- Read the repository first: layout, build and test commands, conventions.
- Each task is one reviewable change an agent can finish in one session, with concrete, testable acceptance criteria.
- write_scope lists the only paths the task may change: directory prefixes ending in "/" or exact file paths. No globs. Tasks whose scopes overlap run one after another, so keep scopes tight and disjoint where the work allows.
- deps lists the task ids that must land first. No cycles. Prefer wide graphs over long chains.
- Task ids match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ and are unique.
- Do not change any files."""

TRIAGE_SYSTEM = """You condense feedback for a coding agent's next attempt.
Keep every distinct actionable problem: failing test names, error messages, file paths, reviewer findings.
Drop repetition, stack frames that add nothing, and passing output. Output at most 15 short points."""

WORK_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "summary", "blocker"],
    "properties": {
        "status": {"type": "string", "enum": ["done", "blocked"]},
        "summary": {"type": "string", "description": "What changed and how it was verified."},
        "blocker": {"type": "string", "description": "Why the task cannot be completed; empty when done."},
    },
}

REVIEW_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["approve", "summary", "findings"],
    "properties": {
        "approve": {"type": "boolean"},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "file", "detail"],
                "properties": {
                    "severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
                    "file": {"type": "string"},
                    "detail": {"type": "string"},
                },
            },
        },
    },
}

TRIAGE_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["points"],
    "properties": {"points": {"type": "array", "items": {"type": "string"}, "maxItems": 15}},
}

_ID = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"}
_STRINGS = {"type": "array", "items": {"type": "string"}}

PLAN_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "goal", "tasks"],
    "properties": {
        "id": _ID,
        "goal": {"type": "string"},
        "tasks": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "title", "description", "acceptance", "deps", "write_scope"],
                "properties": {
                    "id": _ID,
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "acceptance": _STRINGS,
                    "deps": {"type": "array", "items": _ID},
                    "write_scope": {**_STRINGS, "minItems": 1},
                    "capabilities": _STRINGS,
                    "priority": {"type": "integer"},
                    "max_attempts": {"type": "integer", "minimum": 1, "maximum": 20},
                },
            },
        },
    },
}


def _bullets(items: Sequence[str], empty: str = "(none)") -> str:
    return "\n".join(f"- {item}" for item in items) if items else empty


def _scope_lines(scope: Sequence[str]) -> str:
    return _bullets(["(entire repository)" if e == WHOLE_REPO else e for e in scope])


def worker_prompt(task: TaskSpec, base_commit: str, feedback: Sequence[str]) -> str:
    parts = [
        f"TASK {task.id}: {task.title}",
        "",
        task.description.strip(),
        "",
        "ACCEPTANCE CRITERIA:",
        _bullets(task.acceptance),
        "",
        "WRITE SCOPE (the only paths you may create, modify or delete):",
        _scope_lines(task.write_scope),
        "",
        f"BASE COMMIT: {base_commit}",
    ]
    if feedback:
        parts += ["", "FEEDBACK FROM PREVIOUS ATTEMPTS (address every point):", _bullets(feedback)]
    parts += ["", 'Implement the task, verify it, then report status "done" with a summary, or "blocked" with the reason.']
    return "\n".join(parts)


def reviewer_prompt(assignment: Assignment) -> str:
    task = assignment.task
    return "\n".join(
        [
            f"REVIEW TASK {task.id} (attempt {assignment.attempt}): {task.title}",
            "",
            task.description.strip(),
            "",
            "ACCEPTANCE CRITERIA:",
            _bullets(task.acceptance),
            "",
            "WRITE SCOPE:",
            _scope_lines(task.write_scope),
            "",
            f"BASE COMMIT: {assignment.base_commit}",
            f"HEAD COMMIT: {assignment.commit} (checked out in your working directory)",
            f"Inspect the change with: git diff {assignment.base_commit} {assignment.commit}",
            "",
            "AUTHOR'S SUMMARY:",
            assignment.summary.strip() or "(none)",
        ]
    )


def planner_prompt(goal: str) -> str:
    return f"GOAL:\n{goal.strip()}\n\nRead the repository, then return the plan."


def triage_prompt(task: TaskSpec, feedback: str) -> str:
    return f"TASK {task.id}: {task.title}\n\nFEEDBACK TO CONDENSE:\n{feedback}"


def commit_message(assignment: Assignment, summary: str) -> str:
    task = assignment.task
    body = summary.strip()
    if len(body) > 4000:
        body = body[:4000] + "..."
    lines = [f"fleet({task.id}): {task.title}", ""]
    if body:
        lines += [body, ""]
    lines += [
        f"Fleet-Task: {task.id}",
        f"Fleet-Attempt: {assignment.attempt}",
        f"Fleet-Agent: {assignment.agent_id}",
        f"Fleet-Token: {assignment.token}",
    ]
    return "\n".join(lines) + "\n"
