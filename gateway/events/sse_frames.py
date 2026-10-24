# meiva:note
# SSE wire frames and the cadence that decides when a quiet stream speaks.
#
# Pure over values: a stream's shell (`gateway.events.sse`, `gateway.events.merged`) reads the
# clock and the transport, and asks here which frame, if any, goes out and how bytes are framed.

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "HEARTBEAT_FRAME",
    "SseCadence",
    "parse_agent_ids",
    "sse_batch_frame",
    "sse_frame",
    "warm_frame",
]

# meiva:note
# The visible heartbeat: a real `data:` event the browser EventSource `onmessage` can see, so a
# client-side watchdog can detect a half-dead connection (server graceful restart / a proxy hop
# that stays OPEN but stops delivering data). The `: hb` comment keeps the TCP / proxy hop warm
# but is invisible to `onmessage`, so it cannot drive the watchdog on its own.
HEARTBEAT_FRAME = b'data: {"role":"heartbeat"}\n\n'


# meiva:note
# When a stream that has nothing to say still speaks, in seconds.
#
# `heartbeat_s`: after this much silence, emit the visible heartbeat frame.
# `keepalive_s`: at most one `: hb` comment per interval on paths that yield nothing else. A
# busy events channel must not turn the wire silent (read-timeout clients, httpx/curl, would
# disconnect), and a quiet one must not get a comment per poll tick.
@dataclass(frozen=True)
class SseCadence:
    heartbeat_s: float = 15.0
    keepalive_s: float = 1.0


# meiva:note
# A frame that keeps the wire warm, or None: heartbeat when the last data frame is older than
# `cadence.heartbeat_s` (the visible event drives client watchdogs), otherwise a `: hb` comment
# at most once per `cadence.keepalive_s`. Returns `(frame, last_comment, last_data_frame)` — the
# caller stamps both clocks, so any yielded frame counts as traffic; when nothing is due, neither
# clock moves (a poll shorter than `keepalive_s` must not keep postponing the comment).
# Shared with the merged stream.
# meiva:link gateway/events/docs/sse/sse.meiva.okf.md
def warm_frame(
    now: float, last_comment: float, last_data_frame: float, cadence: SseCadence
) -> tuple[bytes | None, float, float]:
    if now - last_data_frame >= cadence.heartbeat_s:
        return HEARTBEAT_FRAME, now, now
    if now - last_comment >= cadence.keepalive_s:
        return b": hb\n\n", now, last_data_frame
    return None, last_comment, last_data_frame


# meiva:note
# Wrap a data frame per the SSE protocol.
#
# Multi-line payloads must prefix each line with `data:` to be compliant. Our event JSON is
# single-line, but we split defensively and only on "\n" - never str.splitlines(): that also
# breaks on U+0085 / U+2028 / U+2029, which are legal unescaped inside JSON strings, and the
# client rejoins data lines with "\n", so a split there plants a raw newline inside the JSON
# literal and JSON.parse fails ("Bad control character in string literal"). The trailing
# empty line (`\n\n`) is the SSE frame separator.
def sse_frame(payload: str) -> bytes:
    lines = payload.split("\n") or [""]
    return ("".join(f"data: {line}\n" for line in lines) + "\n").encode()


# meiva:note
# Wrap a batch of already-serialized event JSON strings into ONE SSE
# frame carrying a JSON array of objects: `data: [{...},{...}]`.
#
# Each element of `events` is ALREADY valid JSON (a `model_dump_json()`
# payload straight off Redis), so the array is built by joining the raw
# strings — NOT `json.dumps(events)`, which would re-encode each element
# as a JSON string and emit `data: ["{...}","{...}"]` (an array of
# strings). The browser does a single `JSON.parse`, so a string element
# has no `.role` / `.agent_id`, fails the active-thread guard, and is
# silently dropped — the live timeline / token / pending streams go dark
# while only the separate `/api/system` object-stream (last-active) keeps
# working. Join raw so the client folds real objects.
def sse_batch_frame(events: list[str]) -> bytes:
    return sse_frame("[" + ",".join(events) + "]")


# meiva:note
# The agent ids of a comma-separated `agents` query, None when the query is absent.
#
# Raises ValueError for a token that is not an integer.
def parse_agent_ids(raw: str | None) -> set[int] | None:
    if raw is None:
        return None
    return {int(token) for token in raw.split(",")}
