import asyncio
import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import psycopg
import pytest
import redis as sync_redis
from fastapi import HTTPException, Request

from base.config import settings
from base.db import create_agent
from base.events.live.bus import EventBus, EventBusConfig
from base.events.live.projection import ChatDelta, LabelUpdated
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events

# meiva:note
# Pins the merged stream's contract: one frame per channel per flush with the
# channel named, per-source failures scoped to the channels they serve, and the
# bounded-queue drop that must reach both the frame and `sse_drop`.
# meiva:link gateway/events/docs/sse/sse.meiva.okf.md
from gateway.events.merged import (
    ALERTS_CHANNEL,
    CHANNELS,
    ChannelQueue,
    merged_event_stream,
    parse_agents,
    parse_channels,
    plan_sources,
    subscribe_errors,
)
from gateway.events.sse_frames import SseCadence
from gateway.tests.auth.gateway_config import gateway_test_client
from gateway.tests.events.sse_support import FakeRequest, refused_redis_url
from gateway.tests.events.test_sse import redis_client as redis_client
from ops.alerts import AlertRow

Channels = dict[str, list[dict[str, Any]]]


def _alert_payload(row_id: int = 1) -> str:
    now = datetime.now(UTC)
    return AlertRow(
        id=row_id,
        status="unresolved",
        severity="warning",
        alertname="DiskFillingUp",
        labels={},
        annotations={},
        starts_at=now,
        ends_at=None,
        fingerprint="fp-1",
        generator_url="http://grafana.local/alert",
        source="grafana",
        notified_at=None,
        created_at=now,
        updated_at=now,
    ).model_dump_json()


def _decode(frame: bytes) -> dict[str, Any]:
    text = frame.decode()
    assert text.startswith("data: "), f"not a data frame: {text!r}"
    payload = json.loads(text[len("data: ") :].strip())
    assert isinstance(payload, dict), f"merged frame is not a JSON object: {payload!r}"
    assert "ch" in payload, f"merged frame has no channel: {payload!r}"
    return cast(dict[str, Any], payload)


@dataclass
class Collected:
    by_channel: Channels
    errors: list[dict[str, Any]]
    dropped: list[tuple[str, int]]
    frames: int = 0

    def events(self, channel: str) -> list[dict[str, Any]]:
        return self.by_channel.get(channel, [])


async def _collect(
    publisher: Any,
    publishes: list[tuple[str | None, str]],
    stop: Callable[[Collected], bool],
    *,
    channels: tuple[str, ...] = CHANNELS,
    agent_filter: set[int] | None = None,
    throttle_rate: float = 50.0,
    pending_limit: int = 1000,
    frame_timeout: float = 5.0,
    overall_timeout: float = 60.0,
) -> Collected:
    req = FakeRequest()
    gen = merged_event_stream(
        EventBus.from_settings(),
        cast(Request, req),
        channels=parse_channels(",".join(channels)),
        agent_filter=agent_filter,
        throttle_rate=throttle_rate,
        pending_limit=pending_limit,
        cadence=SseCadence(),
    )
    seen = Collected(by_channel={}, errors=[], dropped=[])

    def absorb(frame: bytes) -> None:
        if not frame.startswith(b"data: "):
            return
        payload = _decode(frame)
        seen.frames += 1
        if "error" in payload:
            seen.errors.append(payload)
            return
        channel = str(payload["ch"])
        if payload.get("dropped"):
            seen.dropped.append((channel, int(payload["dropped"])))
        raw_events = payload["events"]
        assert isinstance(raw_events, list), f"events is not an array: {payload!r}"
        bucket = seen.by_channel.setdefault(channel, [])
        for event in cast(list[Any], raw_events):
            if not isinstance(event, dict):
                continue
            row = cast(dict[str, Any], event)
            if row.get("role") != "heartbeat":
                bucket.append(row)

    async def _publish() -> None:
        await asyncio.sleep(0.05)
        for source, payload in publishes:
            publisher.publish(source or settings.data_plane.events_channel, payload)

    absorb(await anext(gen))
    pub_task = asyncio.create_task(_publish())
    deadline = asyncio.get_running_loop().time() + overall_timeout
    try:
        while not stop(seen):
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(f"merged stream never satisfied stop; saw {seen}")
            absorb(await asyncio.wait_for(anext(gen), timeout=frame_timeout))
    finally:
        req.disconnected = True
        await pub_task
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(gen.aclose(), timeout=2.0)
    return seen


def _roles(events: list[dict[str, Any]]) -> list[str]:
    return [str(event["role"]) for event in events]


def _seen(channel: str, count: int) -> Callable[[Collected], bool]:
    return lambda seen: len(seen.events(channel)) >= count


def test_merged_stream_names_each_channel_and_keeps_their_filters(
    db_conn: psycopg.Connection,
    redis_client: sync_redis.Redis,
) -> None:
    tid = create_agent(db_conn)
    seen = asyncio.run(
        _collect(
            redis_client,
            [
                (None, LabelUpdated(agent_id=tid, label="busy").model_dump_json()),
                (None, ChatDelta(agent_id=tid, item_id="5.0", content="tok").model_dump_json()),
                (ALERTS_CHANNEL, _alert_payload()),
            ],
            stop=lambda seen: (
                len(seen.events("global")) >= 1
                and len(seen.events("alerts")) >= 1
                and len(seen.events("all")) >= 2
            ),
        )
    )
    assert seen.errors == []
    assert _roles(seen.events("global")) == ["label_updated"]
    assert sorted(_roles(seen.events("all"))) == ["chat_delta", "label_updated"]
    assert seen.events("alerts")[0]["alertname"] == "DiskFillingUp"


def test_merged_stream_batches_deltas_at_the_throttle_cadence(
    db_conn: psycopg.Connection,
    redis_client: sync_redis.Redis,
) -> None:
    tid = create_agent(db_conn)
    payloads = [
        ChatDelta(agent_id=tid, item_id="5.0", content=f"t{index}").model_dump_json()
        for index in range(12)
    ]

    async def _collect_until_twelve() -> Collected:
        return await _collect(
            redis_client,
            [(None, payload) for payload in payloads],
            stop=lambda seen: len(seen.events("all")) >= 12,
            channels=("all",),
            agent_filter={tid},
            throttle_rate=10.0,
            overall_timeout=30.0,
        )

    seen = asyncio.run(_collect_until_twelve())
    assert [event["content"] for event in seen.events("all")] == [
        f"t{index}" for index in range(12)
    ]
    assert seen.frames < len(payloads)


def test_merged_stream_agent_filter_keeps_system_level_events(
    db_conn: psycopg.Connection,
    redis_client: sync_redis.Redis,
) -> None:
    observed = create_agent(db_conn)
    other = create_agent(db_conn)
    seen = asyncio.run(
        _collect(
            redis_client,
            [
                (None, ChatDelta(agent_id=other, item_id="5.0", content="no").model_dump_json()),
                (
                    None,
                    ChatDelta(agent_id=observed, item_id="5.0", content="yes").model_dump_json(),
                ),
                (None, LabelUpdated(agent_id=0, label="cluster").model_dump_json()),
            ],
            stop=_seen("all", 2),
            agent_filter={observed},
            channels=("all",),
        )
    )
    assert sorted(_roles(seen.events("all"))) == ["chat_delta", "label_updated"]
    assert [event["content"] for event in seen.events("all") if event["role"] == "chat_delta"] == [
        "yes"
    ]


def test_merged_stream_honours_a_channel_subset(
    db_conn: psycopg.Connection,
    redis_client: sync_redis.Redis,
) -> None:
    tid = create_agent(db_conn)
    seen = asyncio.run(
        _collect(
            redis_client,
            [
                (None, ChatDelta(agent_id=tid, item_id="5.0", content="tok").model_dump_json()),
                (None, LabelUpdated(agent_id=tid, label="busy").model_dump_json()),
                (ALERTS_CHANNEL, _alert_payload()),
            ],
            stop=_seen("global", 1),
            channels=("global",),
        )
    )
    assert _roles(seen.events("global")) == ["label_updated"]
    assert set(seen.by_channel) == {"global"}


def test_each_redis_channel_is_subscribed_once_for_the_channels_it_serves() -> None:
    assert plan_sources("meiva:events", CHANNELS) == [
        ("meiva:events", ["global", "all"]),
        (ALERTS_CHANNEL, ["alerts"]),
    ]
    assert plan_sources("meiva:events", ("alerts",)) == [(ALERTS_CHANNEL, ["alerts"])]
    assert plan_sources("meiva:events", ("global",)) == [("meiva:events", ["global"])]


def test_a_failed_source_flags_only_the_channels_it_serves() -> None:
    error = sync_redis.ConnectionError("alerts unavailable")
    assert subscribe_errors(["alerts"], error) == {
        "alerts": "event stream interrupted: ConnectionError"
    }
    assert subscribe_errors(["global", "all"], error) == {
        "global": "event stream interrupted: ConnectionError",
        "all": "event stream interrupted: ConnectionError",
    }


def test_merged_stream_with_an_unreachable_redis_answers_every_channel_with_an_error() -> None:
    bus = EventBus(EventBusConfig(redis_url=refused_redis_url(), events_channel="meiva:events"))

    async def run() -> list[bytes]:
        gen = merged_event_stream(
            bus,
            cast(Request, FakeRequest()),
            channels=CHANNELS,
            agent_filter=None,
            throttle_rate=50.0,
            pending_limit=100,
            cadence=SseCadence(),
        )
        return [frame async for frame in gen]

    decoded = [_decode(frame) for frame in asyncio.run(run()) if frame.startswith(b"data: ")]
    assert {frame["ch"] for frame in decoded} == set(CHANNELS)
    assert {frame["error"] for frame in decoded} == {"event stream interrupted: ConnectionError"}


def test_merged_stream_counts_shed_payloads_in_the_frame_and_in_telemetry(
    db_conn: psycopg.Connection,
    redis_client: sync_redis.Redis,
    emitted_events: EmittedEvents,
) -> None:
    tid = create_agent(db_conn)
    payloads = [
        ChatDelta(agent_id=tid, item_id="5.0", content=f"tok{index}").model_dump_json()
        for index in range(4)
    ]
    seen = asyncio.run(
        _collect(
            redis_client,
            [(None, payload) for payload in payloads],
            stop=lambda seen: sum(len(events) for events in seen.by_channel.values()) >= 1,
            channels=("all",),
            throttle_rate=20.0,
            pending_limit=1,
        )
    )
    assert seen.events("all"), "the surviving payload must still reach the client"
    assert seen.dropped, "the frame must declare what it shed"
    reported = emitted_events.attributes("sse_drop")
    assert reported, "the drop must be reported, not only counted in the frame"
    assert {attributes["kind"] for attributes in reported} == {"queue_full"}
    assert sum(attributes["n"] for attributes in reported) >= seen.dropped[0][1]


def test_merged_frame_reports_drops_per_flush() -> None:
    queue = ChannelQueue("all", limit=2)
    for index in range(3):
        queue.add(f'{{"n":{index}}}')
    events, dropped = queue.take()
    assert dropped == 1
    assert events == ['{"n":1}', '{"n":2}']
    assert queue.take() == ([], 0)
    assert queue.dropped == 1


def test_channel_queue_unparseable_payloads_are_counted() -> None:
    queue = ChannelQueue("alerts", limit=10)
    queue.add_unparseable()
    assert queue.take() == ([], 1)
    assert queue.dropped == 1


def test_channel_queue_reports_the_first_drop_immediately_then_aggregates() -> None:
    queue = ChannelQueue("all", limit=1)
    queue.add("a")
    queue.add("b")
    assert queue.report_due(0.0) == 1
    queue.add("c")
    queue.add("d")
    assert queue.report_due(1.0) == 0
    assert queue.report_remaining(1.0) == 2


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, CHANNELS),
        ("global", ("global",)),
        ("alerts,global", ("alerts", "global")),
        (" global , all ", ("global", "all")),
        ("global,global", ("global",)),
    ],
)
def test_parse_channels_accepts_subsets(raw: str | None, expected: tuple[str, ...]) -> None:
    assert parse_channels(raw) == expected


@pytest.mark.parametrize("raw", ["bogus", "global,bogus", "", "  "])
def test_parse_channels_rejects_unknown_or_empty_selection(raw: str) -> None:
    with pytest.raises(HTTPException) as caught:
        parse_channels(raw)
    assert caught.value.status_code == 422


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), ("0", {0}), ("7", {7}), ("7,3", {7, 3})],
)
def test_parse_agents_matches_the_batch_endpoint(
    raw: str | None, expected: set[int] | None
) -> None:
    assert parse_agents(raw) == expected


def test_parse_agents_rejects_non_integers() -> None:
    with pytest.raises(HTTPException) as caught:
        parse_agents("a,b")
    assert caught.value.status_code == 422


@pytest.mark.parametrize(
    ("query", "detail"),
    [
        ("channels=bogus", "channels must be a comma-separated subset"),
        ("channels=", "channels must name at least one channel"),
        ("agents=x", "agents must be comma-separated integers"),
    ],
)
def test_stream_endpoint_refuses_bad_queries(query: str, detail: str) -> None:
    with gateway_test_client(raise_server_exceptions=False) as client:
        response = client.get(f"/api/stream?{query}")
    assert response.status_code == 422
    assert detail in response.text
