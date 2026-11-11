# meiva:note
# `meiva agents get` / `meiva agents inbox` against a gateway on a loopback port: the command dials
# it through its real transport and headers, and each case asserts the request the gateway saw
# (only the filters named) and what the operator was shown.

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import JsonValue

from cli.commands.agents.observe import InboxFilter, cmd_agents_get, cmd_agents_inbox
from cli.commands.cluster.tests.gateway_server import Reply, access_to, serving

INBOUND = ("GET", "/api/agents/7/inbound")


def _query(target: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(target).query)


def _agent() -> dict[str, JsonValue]:
    return {
        "agent_id": 7,
        "label": "ops",
        "status": "idling",
        "harness": "meiva",
        "machine": "macmini",
        "liveness_state": "online",
        "spawner": "user",
        "spawned_at": "2026-11-01T01:00:00+00:00",
        "last_active_at": "2026-11-12T01:00:00+00:00",
        "last_inbound_at": "2026-11-12T00:50:00+00:00",
        "heartbeat_paused_until": None,
        "harness_exit_code": None,
        "questions_awaiting_response": [{"id": 1}],
        "observation": None,
    }


def test_get_prints_the_agent(capsys: pytest.CaptureFixture[str]) -> None:
    with serving({("GET", "/api/agents/7"): Reply(body=_agent())}) as gateway:
        assert cmd_agents_get(access_to(gateway.url), 7, as_json=False) == 0
    assert gateway.requests[0].headers["authorization"] == "Bearer test-cluster-secret"
    assert capsys.readouterr().out.splitlines() == [
        "id:                7",
        "label:             ops",
        "status:            idling",
        "machine:           macmini (online)",
        "harness:           meiva",
        "spawned by:        user",
        "spawned:           2026-11-01T01:00:00+00:00",
        "last active:       2026-11-12T01:00:00+00:00",
        "last inbound:      2026-11-12T00:50:00+00:00",
        "heartbeat paused:  ",
        "harness exit code: ",
        "open questions:    1",
    ]


def test_get_json_is_the_response_unchanged(capsys: pytest.CaptureFixture[str]) -> None:
    with serving({("GET", "/api/agents/7"): Reply(body=_agent())}) as gateway:
        assert cmd_agents_get(access_to(gateway.url), 7, as_json=True) == 0
    assert json.loads(capsys.readouterr().out) == _agent()


def test_get_of_a_missing_agent_prints_the_gateways_reason_and_exits_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = Reply(status=404, body={"detail": "agent 9 does not exist"})
    with serving({("GET", "/api/agents/9"): missing}) as gateway:
        assert cmd_agents_get(access_to(gateway.url), 9, as_json=False) == 1
    assert capsys.readouterr().err == "gateway 404: agent 9 does not exist\n"


def _message(
    message_id: int, content: str, *, status: str = "claimed", cut: bool = False
) -> dict[str, JsonValue]:
    return {
        "id": message_id,
        "kind": "chat",
        "source": "agent:12",
        "status": status,
        "created_at": "2026-11-12T01:00:00+00:00",
        "claimed_at": None if status == "pending" else "2026-11-12T01:00:05+00:00",
        "content": content,
        "content_truncated": cut,
    }


def _inbox(items: list[dict[str, JsonValue]], *, has_more: bool = False) -> dict[str, object]:
    return {
        "agent_id": 7,
        "meta": {"window_from": "2026-11-11T01:00:00+00:00", "limit": 100, "has_more": has_more},
        "items": items,
    }


def test_inbox_without_filters_sends_none_and_prints_the_table(
    capsys: pytest.CaptureFixture[str],
) -> None:
    body = _inbox(
        [_message(9, "line one\nline two", cut=True), _message(8, "ok", status="pending")]
    )
    with serving({INBOUND: Reply(body=body)}) as gateway:
        assert cmd_agents_inbox(access_to(gateway.url), 7, InboxFilter(), as_json=False) == 0
    assert [r.target for r in gateway.requests] == ["/api/agents/7/inbound"]
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["id", "created", "status", "kind", "source", "content"]
    assert out[1].split()[:5] == ["9", "2026-11-12T01:00:00+00:00", "claimed", "chat", "agent:12"]
    assert out[1].endswith("line one\\nline two...")
    assert out[2].endswith("ok")
    assert out[3] == "(2 messages since 2026-11-11T01:00:00+00:00)"


def test_inbox_forwards_every_filter_under_the_gateways_names() -> None:
    flt = InboxFilter(status="pending", kind="chat", source="user", hours=2.5, limit=5, full=True)
    with serving({INBOUND: Reply(body=_inbox([]))}) as gateway:
        assert cmd_agents_inbox(access_to(gateway.url), 7, flt, as_json=False) == 0
    assert _query(gateway.requests[0].target) == {
        "status": ["pending"],
        "kind": ["chat"],
        "source": ["user"],
        "hours": ["2.5"],
        "limit": ["5"],
        "full": ["true"],
    }


def test_inbox_full_prints_each_message_whole(capsys: pytest.CaptureFixture[str]) -> None:
    body = _inbox(
        [_message(9, "line one\nline two"), _message(8, "next", status="pending")], has_more=True
    )
    with serving({INBOUND: Reply(body=body)}) as gateway:
        assert (
            cmd_agents_inbox(access_to(gateway.url), 7, InboxFilter(full=True), as_json=False) == 0
        )
    assert capsys.readouterr().out.splitlines() == [
        "--- #9 2026-11-12T01:00:00+00:00 claimed chat agent:12 claimed 2026-11-12T01:00:05+00:00",
        "line one",
        "line two",
        "--- #8 2026-11-12T01:00:00+00:00 pending chat agent:12",
        "next",
        "(2 messages since 2026-11-11T01:00:00+00:00)",
        "(older messages exist: raise --limit or narrow --hours)",
    ]


def test_inbox_says_when_the_window_is_empty(capsys: pytest.CaptureFixture[str]) -> None:
    with serving({INBOUND: Reply(body=_inbox([]))}) as gateway:
        assert cmd_agents_inbox(access_to(gateway.url), 7, InboxFilter(), as_json=False) == 0
    assert capsys.readouterr().out == "(no inbound messages since 2026-11-11T01:00:00+00:00)\n"


def test_inbox_json_is_the_response_unchanged(capsys: pytest.CaptureFixture[str]) -> None:
    body = _inbox([_message(9, "hi")])
    with serving({INBOUND: Reply(body=body)}) as gateway:
        assert cmd_agents_inbox(access_to(gateway.url), 7, InboxFilter(), as_json=True) == 0
    assert json.loads(capsys.readouterr().out) == body


def test_inbox_refusal_prints_the_gateways_reason_and_exits_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    refusal = Reply(status=404, body={"detail": "agent 7 not found"})
    with serving({INBOUND: refusal}) as gateway:
        assert cmd_agents_inbox(access_to(gateway.url), 7, InboxFilter(), as_json=False) == 1
    assert capsys.readouterr().err == "gateway 404: agent 7 not found\n"
