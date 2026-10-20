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
from base.events.live.bus import EventBus
from base.events.live.projection import ChatDelta, LabelUpdated

# meiva:note
# Pins the merged stream's contract: one frame per channel per flush with the
# channel named, per-source failures scoped to the channels they serve, and the
# bounded-queue drop that must reach both the frame and `sse_drop`.
# meiva:link gateway/events/docs/sse.meiva.okf.md
from gateway.events.merged import (
    ALERTS_CHANNEL,
    CHANNELS,
    ChannelQueue,
    merged_event_stream,
    parse_agents,
    parse_channels,
)
from gateway.tests.events.test_sse import FakeRequest
from gateway.tests.events.test_sse import redis_client as redis_client
from ops.alerts import AlertRow
from tests.fixtures.gateway_config import gateway_test_client

Channels = dict[str, list[dict[str, Any]]]


def _no_report(queue: ChannelQueue, n: int) -> None:
    return None


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
    reporter: Callable[[ChannelQueue, int], None] = _no_report,
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
        reporter=reporter,
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


class _FakePubsub:
    def __init__(self, messages: list[tuple[str, str]], refused: set[str]) -> None:
        self.messages = messages
        self.refused = refused
        self.subscribed: list[str] = []
        self.closed = False

    async def subscribe(self, *sources: str) -> None:
        for source in sources:
            if source in self.refused:
                raise sync_redis.ConnectionError(f"{source} unavailable")
        self.subscribed.extend(sources)

    async def get_message(self, *, ignore_subscribe_messages: bool, timeout: float) -> Any:
        await asyncio.sleep(0)
        if not self.messages:
            return None
        source, payload = self.messages.pop(0)
        return {"channel": source, "data": payload}

    async def unsubscribe(self, *sources: str) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True


class _FakeClient:
    async def aclose(self) -> None:
        return None


def test_merged_stream_reports_one_channel_failure_and_keeps_streaming(
    db_conn: psycopg.Connection,
) -> None:
    tid = create_agent(db_conn)
    pubsub = _FakePubsub(
        messages=[
            (
                settings.data_plane.events_channel,
                LabelUpdated(agent_id=tid, label="busy").model_dump_json(),
            )
        ],
        refused={ALERTS_CHANNEL},
    )

    async def run() -> list[bytes]:
        req = FakeRequest()
        gen = merged_event_stream(
            EventBus.from_settings(),
            cast(Request, req),
            channels=CHANNELS,
            agent_filter=None,
            throttle_rate=50.0,
            pending_limit=100,
            open_connection=lambda _bus: (pubsub, _FakeClient()),
        )
        frames: list[bytes] = []
        while True:
            frame = await asyncio.wait_for(anext(gen), timeout=5.0)
            frames.append(frame)
            if b'"global"' in frame:
                break
        req.disconnected = True
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(gen.aclose(), timeout=2.0)
        return frames

    frames = asyncio.run(run())
    decoded = [_decode(frame) for frame in frames if frame.startswith(b"data: ")]
    assert [frame.get("ch") for frame in decoded if "error" in frame] == ["alerts"]
    assert decoded[-1]["ch"] == "global"
    assert [event["role"] for event in decoded[-1]["events"]] == ["label_updated"]
    assert pubsub.subscribed == [settings.data_plane.events_channel]
    assert pubsub.closed


def test_merged_stream_counts_shed_payloads_in_the_frame_and_in_telemetry(
    db_conn: psycopg.Connection,
    redis_client: sync_redis.Redis,
) -> None:
    reported: list[tuple[str, int, int]] = []

    def record(queue: ChannelQueue, n: int) -> None:
        if n:
            reported.append((queue.channel, n, queue.dropped))

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
            reporter=record,
        )
    )
    assert seen.events("all"), "the surviving payload must still reach the client"
    assert seen.dropped, "the frame must declare what it shed"
    assert reported, "the drop must be reported, not only counted in the frame"
    assert {channel for channel, _n, _total in reported} == {"all"}
    assert sum(n for _channel, n, _total in reported) >= seen.dropped[0][1]


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
