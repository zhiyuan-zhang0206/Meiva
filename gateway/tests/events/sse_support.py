# meiva:note
# What a stream test needs besides Redis: a request that can disconnect, an address nothing
# listens on, and a decoder for the frames a stream produced.

from __future__ import annotations

import json
import socket
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from base.events.live.bus import EventBus


# meiva:note
# Minimal Request stub — `event_stream` only calls `await is_disconnected()`.
#
# `disconnected` flag is explicitly set True by tests to stop stream.
@dataclass
class FakeRequest:
    disconnected: bool = field(default=False)

    async def is_disconnected(self) -> bool:
        return self.disconnected


# meiva:note
# Every live event published on the bus's channel while the block runs.
#
# The list fills when the block ends: a real subscriber on the real Redis drains what arrived.
@contextmanager
def published_events(bus: EventBus) -> Generator[list[dict[str, Any]]]:
    client = bus.sync_redis(decode_responses=True)
    pubsub: Any = client.pubsub()  # pyright: ignore[reportUnknownMemberType]
    pubsub.subscribe(bus.channel)
    # meiva:note
    # the subscribe confirmation: from here nothing is missed
    pubsub.get_message(timeout=2.0)
    payloads: list[dict[str, Any]] = []
    try:
        yield payloads
    finally:
        while (
            message := pubsub.get_message(ignore_subscribe_messages=True, timeout=0.3)
        ) is not None:
            payloads.append(json.loads(message["data"]))
        pubsub.close()
        client.close()


# meiva:note
# A Redis address nothing listens on: bind a port, release it, keep the number.
def refused_redis_url() -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"redis://127.0.0.1:{port}/0"


# meiva:note
# Test helper: slice an SSE byte stream into frames + parse the JSON
#     on data lines, the way the browser does: split lines on "
# " only
#     (str.splitlines() would also break on U+0085 / U+2028 / U+2029 and
#     silently re-merge a payload the writer split there) and rejoin data
#     lines with "
# ", so writer corruption fails here as a parse error.
#
#     Ignores `:` comment frames (heartbeat / stream open / dropped).
#
def decode_frames_for_test(chunks: list[bytes]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    text = b"".join(chunks).decode()
    for frame in text.split("\n\n"):
        data_lines = [
            line[len("data: ") :] for line in frame.split("\n") if line.startswith("data: ")
        ]
        if not data_lines:
            continue
        out.append(json.loads("\n".join(data_lines)))  # pyright: ignore[reportUnknownArgumentType]
    return out
