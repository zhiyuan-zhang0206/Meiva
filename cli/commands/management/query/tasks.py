# meiva:note
# `meiva tasks ls` and `meiva tasks get` — the task registry read through `GET /api/tasks` and
# `GET /api/tasks/{id}`.
#
# Thin clients: `ls` forwards the owner, status and window filters and prints the metadata
# projection (no description or results); `get` prints one task in full. The registry holds every
# task ever created, so `ls` refuses to run with no filter at all: name an owner, a status or a
# window (`--window all` is the explicit way to ask for everything).

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel

from base.cluster.machine import GatewayAccess
from cli.boundary.gateway_read import fetch, print_json, render_table

_USAGE = "tasks ls needs a filter: --owner ID, --status STATUS or --window 24h|7d|30d|all"


# meiva:note
# The filters `meiva tasks ls` forwards; `None` means the operator did not set it.
@dataclass(frozen=True)
class TasksFilter:
    owner: int | None = None
    status: str | None = None
    window: str | None = None


class _TaskSummary(BaseModel):
    id: int
    parent_id: int | None
    title: str
    status: str
    priority: str
    owner: int | None
    updated_at: datetime
    reminder_count: int
    ghost: bool = False


class _TaskList(BaseModel):
    tasks: list[_TaskSummary]


# meiva:note
# Print the matching tasks, newest created first (the gateway's order). A `ghost` row is an
# out-of-window ancestor the gateway delivers for tree shape; it is marked with a trailing `*`.
def cmd_tasks_ls(access: GatewayAccess, flt: TasksFilter, *, as_json: bool) -> int:
    if flt.owner is None and flt.status is None and flt.window is None:
        print(_USAGE, file=sys.stderr)
        return 2
    resp = fetch(
        access,
        "/api/tasks",
        {"fields": "summary", "owner": flt.owner, "status": flt.status, "window": flt.window},
    )
    if resp is None:
        return 1
    if as_json:
        print_json(resp)
        return 0
    tasks = _TaskList.model_validate_json(resp.content).tasks
    if not tasks:
        print("(no tasks)")
        return 0
    print(
        render_table(
            ("id", "status", "pri", "owner", "parent", "updated", "reminders", "title"),
            [
                (
                    f"{task.id}{'*' if task.ghost else ''}",
                    task.status,
                    task.priority,
                    "" if task.owner is None else str(task.owner),
                    "" if task.parent_id is None else str(task.parent_id),
                    task.updated_at.isoformat(timespec="seconds"),
                    str(task.reminder_count),
                    task.title,
                )
                for task in tasks
            ],
        )
    )
    print(f"({len(tasks)} tasks)")
    return 0


class _Task(BaseModel):
    id: int
    parent_id: int | None
    title: str
    description: str
    results: str | None
    status: str
    priority: str
    owner: int | None
    owner_label: str | None
    created_by: str
    created_at: datetime
    updated_at: datetime
    remind_interval_seconds: int | None
    last_reminded_at: datetime | None
    reminder_count: int
    reminders_paused_until: datetime | None


# meiva:note
# Print one task: its fields, the reminder state, then the description and the results in full.
def cmd_tasks_get(access: GatewayAccess, task_id: int, *, as_json: bool) -> int:
    resp = fetch(access, f"/api/tasks/{task_id}", {})
    if resp is None:
        return 1
    if as_json:
        print_json(resp)
        return 0
    task = _Task.model_validate_json(resp.content)
    owner = "" if task.owner is None else f"{task.owner} {task.owner_label or ''}".rstrip()
    print(f"id:           {task.id}")
    print(f"title:        {task.title}")
    print(f"status:       {task.status}")
    print(f"priority:     {task.priority}")
    print(f"owner:        {owner}")
    print(f"parent:       {'' if task.parent_id is None else task.parent_id}")
    print(f"created by:   {task.created_by}")
    print(f"created:      {task.created_at.isoformat(timespec='seconds')}")
    print(f"updated:      {task.updated_at.isoformat(timespec='seconds')}")
    interval = "" if task.remind_interval_seconds is None else f"{task.remind_interval_seconds}s"
    last = (
        "never"
        if task.last_reminded_at is None
        else task.last_reminded_at.isoformat(timespec="seconds")
    )
    paused = (
        ""
        if task.reminders_paused_until is None
        else f", paused until {task.reminders_paused_until.isoformat(timespec='seconds')}"
    )
    print(f"reminders:    every {interval}, {task.reminder_count} sent, last {last}{paused}")
    print()
    print("description:")
    print(task.description)
    print()
    print("results:")
    print(task.results or "(none)")
    return 0
