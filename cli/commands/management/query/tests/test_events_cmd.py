# meiva:note
# `meiva events ls` / `meiva events count` against a gateway on a loopback port: the command dials it
# through its real transport and headers, and each case asserts the request the gateway saw (which
# filters were forwarded, and that an omitted one was not invented) and what the operator was shown.

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import JsonValue

from cli.commands.cluster.tests.gateway_server import Reply, access_to, serving
from cli.commands.management.query.events import EventsFilter, cmd_events_count, cmd_events_ls

EVENTS = ("GET", "/api/events")
COUNTS = ("GET", "/api/events/counts")


def _item(
    name: str, *, agent: int | None = 7, attributes: dict[str, JsonValue] | None = None
) -> dict[str, JsonValue]:
    return {
        "id": 1,
        "ts": "2026-11-12T01:02:03+00:00",
        "agent_id": agent,
        "machine": "m1",
        "category": "audit",
        "event_name": name,
        "level": "info",
        "source": "test",
        "attributes": attributes or {},
    }


def _page(
    items: list[dict[str, JsonValue]], *, has_more: bool = False, total: int | None = None
) -> dict[str, object]:
    return {
        "meta": {
            "total": total,
            "window_from": "2026-11-11T01:00:00+00:00",
            "offset": 0,
            "has_more": has_more,
        },
        "items": items,
    }


def _query(target: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(target).query)


def test_ls_without_filters_sends_no_window_or_page(capsys: pytest.CaptureFixture[str]) -> None:
    with serving({EVENTS: Reply(body=_page([_item("spawn")]))}) as gateway:
        assert cmd_events_ls(access_to(gateway.url), EventsFilter(), as_json=False) == 0
    assert [r.target for r in gateway.requests] == ["/api/events"]
    assert gateway.requests[0].headers["authorization"] == "Bearer test-cluster-secret"
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == [
        "ts",
        "level",
        "agent",
        "machine",
        "category",
        "event",
        "source",
        "attributes",
    ]
    assert out[1].split() == [
        "2026-11-12T01:02:03+00:00",
        "info",
        "7",
        "m1",
        "audit",
        "spawn",
        "test",
    ]
    assert out[2] == "(1 events since 2026-11-11T01:00:00+00:00)"


def test_ls_forwards_every_filter_under_the_gateways_names() -> None:
    flt = EventsFilter(
        category="audit",
        name="spawn",
        agent_id=7,
        trace_id="abc",
        machine="m1",
        level="warning",
        tier="business,anomaly",
        since="2026-11-12T00:00:00Z",  # time-bomb-ok: explicit request range under test
        until="2026-11-12T02:00:00Z",  # time-bomb-ok: explicit request range under test
        limit=5,
        offset=10,
        with_total=True,
    )
    with serving({EVENTS: Reply(body=_page([]))}) as gateway:
        assert cmd_events_ls(access_to(gateway.url), flt, as_json=False) == 0
    assert _query(gateway.requests[0].target) == {
        "category": ["audit"],
        "event_name": ["spawn"],
        "agent_id": ["7"],
        "trace_id": ["abc"],
        "machine": ["m1"],
        "level": ["warning"],
        "tier": ["business,anomaly"],
        "from": ["2026-11-12T00:00:00Z"],
        "to": ["2026-11-12T02:00:00Z"],
        "limit": ["5"],
        "offset": ["10"],
        "with_total": ["true"],
    }


def test_ls_sends_hours_as_the_window() -> None:
    with serving({EVENTS: Reply(body=_page([]))}) as gateway:
        cmd_events_ls(access_to(gateway.url), EventsFilter(hours=2.5), as_json=False)
    assert _query(gateway.requests[0].target) == {"hours": ["2.5"]}


def test_ls_says_when_the_window_is_empty(capsys: pytest.CaptureFixture[str]) -> None:
    with serving({EVENTS: Reply(body=_page([]))}) as gateway:
        assert cmd_events_ls(access_to(gateway.url), EventsFilter(), as_json=False) == 0
    assert capsys.readouterr().out == "(no events since 2026-11-11T01:00:00+00:00)\n"


def test_ls_shows_a_service_event_without_an_agent_and_cuts_long_attributes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    items = [_item("host_turn_crashed", agent=None, attributes={"detail": "x" * 300})]
    with serving({EVENTS: Reply(body=_page(items, has_more=True, total=40))}) as gateway:
        assert (
            cmd_events_ls(access_to(gateway.url), EventsFilter(with_total=True), as_json=False) == 0
        )
    out = capsys.readouterr().out.splitlines()
    row = out[1]
    assert row.split()[2] == "m1"
    assert row.endswith("...")
    assert len(row.split()[-1]) == 100
    assert out[-2] == "(1 of 40 events since 2026-11-11T01:00:00+00:00)"
    assert out[-1] == "(more events: repeat with --offset 1)"


def test_ls_json_prints_the_response_unchanged(capsys: pytest.CaptureFixture[str]) -> None:
    body = _page([_item("spawn")])
    with serving({EVENTS: Reply(body=body)}) as gateway:
        assert cmd_events_ls(access_to(gateway.url), EventsFilter(), as_json=True) == 0
    assert json.loads(capsys.readouterr().out) == body


def test_a_refusal_prints_the_gateways_reason_on_stderr_and_exits_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    refusal = Reply(status=422, body={"detail": "level must be one of ['critical'], got 'loud'"})
    with serving({EVENTS: refusal}) as gateway:
        assert cmd_events_ls(access_to(gateway.url), EventsFilter(level="loud"), as_json=False) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "gateway 422: level must be one of ['critical'], got 'loud'\n"


def _counts(groups: list[tuple[str | None, int]], *, truncated: bool = False) -> dict[str, object]:
    return {
        "meta": {
            "by": "event_name",
            "window_from": "2026-11-11T01:00:00+00:00",
            "window_to": None,
            "limit": 100,
            "total": sum(count for _, count in groups),
            "truncated": truncated,
            "generated_at": "2026-11-12T01:00:00+00:00",
        },
        "groups": [{"key": key, "count": count} for key, count in groups],
    }


def test_count_forwards_the_dimension_and_filters_and_prints_the_groups(
    capsys: pytest.CaptureFixture[str],
) -> None:
    body = _counts([("send_message", 12), ("spawn", 3)])
    with serving({COUNTS: Reply(body=body)}) as gateway:
        code = cmd_events_count(
            access_to(gateway.url),
            EventsFilter(machine="m1", hours=2),
            by="event_name",
            as_json=False,
        )
    assert code == 0
    assert _query(gateway.requests[0].target) == {
        "by": ["event_name"],
        "machine": ["m1"],
        "hours": ["2"],
    }
    assert capsys.readouterr().out.splitlines() == [
        "count  event_name",
        "12     send_message",
        "3      spawn",
        "(15 events since 2026-11-11T01:00:00+00:00)",
    ]


def test_count_names_a_null_key_and_a_cut_group_list(capsys: pytest.CaptureFixture[str]) -> None:
    with serving({COUNTS: Reply(body=_counts([("7", 2), (None, 1)], truncated=True))}) as gateway:
        assert (
            cmd_events_count(access_to(gateway.url), EventsFilter(), by="agent_id", as_json=False)
            == 0
        )
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "count  agent_id"
    assert out[2] == "1      (none)"
    assert out[-1] == "(more groups: raise --limit)"


def test_count_refusal_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    refusal = Reply(
        status=422, body={"detail": "more than 1000 distinct agent_id values in the window"}
    )
    with serving({COUNTS: refusal}) as gateway:
        assert (
            cmd_events_count(access_to(gateway.url), EventsFilter(), by="agent_id", as_json=False)
            == 1
        )
    assert capsys.readouterr().err.startswith("gateway 422: more than 1000")
