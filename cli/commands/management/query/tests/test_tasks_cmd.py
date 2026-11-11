# meiva:note
# `meiva tasks ls` / `meiva tasks get` against a gateway on a loopback port: the command dials it
# through its real transport and headers, and each case asserts the request the gateway saw (the
# summary projection, only the filters named) and what the operator was shown.

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import JsonValue

from cli.commands.cluster.tests.gateway_server import Reply, access_to, serving
from cli.commands.management.query.tasks import TasksFilter, cmd_tasks_get, cmd_tasks_ls

LIST = ("GET", "/api/tasks")


def _summary(task_id: int, *, title: str = "t", ghost: bool = False) -> dict[str, JsonValue]:
    return {
        "id": task_id,
        "parent_id": None,
        "title": title,
        "status": "in_progress",
        "priority": "P1",
        "owner": 7,
        "owner_label": "ops",
        "created_by": "user",
        "created_at": "2026-11-10T01:00:00+00:00",
        "updated_at": "2026-11-12T01:00:00+00:00",
        "remind_interval_seconds": 3600,
        "last_reminded_at": None,
        "reminder_count": 2,
        "reminders_paused_until": None,
        "ghost": ghost,
    }


def _query(target: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(target).query)


def test_ls_without_a_filter_refuses_and_asks_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    with serving({}) as gateway:
        assert cmd_tasks_ls(access_to(gateway.url), TasksFilter(), as_json=False) == 2
    assert gateway.requests == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("tasks ls needs a filter:")


def test_ls_forwards_the_filters_with_the_summary_projection_and_prints_the_table(
    capsys: pytest.CaptureFixture[str],
) -> None:
    body = {"tasks": [_summary(5, title="fix the thing"), _summary(4, ghost=True)]}
    with serving({LIST: Reply(body=body)}) as gateway:
        flt = TasksFilter(owner=7, status="in_progress", window="7d")
        assert cmd_tasks_ls(access_to(gateway.url), flt, as_json=False) == 0
    assert gateway.requests[0].headers["authorization"] == "Bearer test-cluster-secret"
    assert _query(gateway.requests[0].target) == {
        "fields": ["summary"],
        "owner": ["7"],
        "status": ["in_progress"],
        "window": ["7d"],
    }
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == [
        "id",
        "status",
        "pri",
        "owner",
        "parent",
        "updated",
        "reminders",
        "title",
    ]
    assert out[1].split()[:4] == ["5", "in_progress", "P1", "7"]
    assert out[1].endswith("fix the thing")
    assert out[2].split()[0] == "4*"
    assert out[3] == "(2 tasks)"


def test_ls_sends_only_the_filters_that_were_named() -> None:
    with serving({LIST: Reply(body={"tasks": []})}) as gateway:
        cmd_tasks_ls(access_to(gateway.url), TasksFilter(status="done"), as_json=False)
    assert _query(gateway.requests[0].target) == {"fields": ["summary"], "status": ["done"]}


def test_ls_says_when_nothing_matches(capsys: pytest.CaptureFixture[str]) -> None:
    with serving({LIST: Reply(body={"tasks": []})}) as gateway:
        assert cmd_tasks_ls(access_to(gateway.url), TasksFilter(owner=9), as_json=False) == 0
    assert capsys.readouterr().out == "(no tasks)\n"


def test_ls_json_is_the_response_unchanged(capsys: pytest.CaptureFixture[str]) -> None:
    body = {"tasks": [_summary(5)]}
    with serving({LIST: Reply(body=body)}) as gateway:
        cmd_tasks_ls(access_to(gateway.url), TasksFilter(owner=7), as_json=True)
    assert json.loads(capsys.readouterr().out) == body


def test_get_prints_the_task_in_full(capsys: pytest.CaptureFixture[str]) -> None:
    task = {
        **_summary(5, title="fix the thing"),
        "description": "why it matters",
        "results": "done so far: A",
        "last_reminded_at": "2026-11-12T00:30:00+00:00",
        "reminders_paused_until": "2026-11-13T00:00:00+00:00",
    }
    with serving({("GET", "/api/tasks/5"): Reply(body=task)}) as gateway:
        assert cmd_tasks_get(access_to(gateway.url), 5, as_json=False) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[:3] == [
        "id:           5",
        "title:        fix the thing",
        "status:       in_progress",
    ]
    assert "owner:        7 ops" in out
    assert (
        "reminders:    every 3600s, 2 sent, last 2026-11-12T00:30:00+00:00, "
        "paused until 2026-11-13T00:00:00+00:00"
    ) in out
    assert out[-5:] == ["description:", "why it matters", "", "results:", "done so far: A"]


def test_get_of_a_missing_task_prints_the_gateways_reason_and_exits_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = Reply(status=404, body={"detail": "Task 9 not found."})
    with serving({("GET", "/api/tasks/9"): missing}) as gateway:
        assert cmd_tasks_get(access_to(gateway.url), 9, as_json=False) == 1
    assert capsys.readouterr().err == "gateway 404: Task 9 not found.\n"
