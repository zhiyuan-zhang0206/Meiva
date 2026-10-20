---
type: doc
title: SSE (Server-Sent Events)
description: Redis pub/sub → browser EventSource real-time event bridge. Two async generators (per-event event_stream + batch-throttled throttled_event_stream) plus the merged multi-channel stream drive 5 SSE endpoints.
tags: []
---

# SSE (Server-Sent Events)

Redis pub/sub → browser EventSource real-time event bridge. The core real-time communication mechanism of the Gateway—every agent output (code_delta, tool result, idle/terminate state change) is broadcast via Redis pub/sub to the `meiva:events` channel, `gateway/events/sse.py` subscribes and forwards to the browser.

## Architecture

```
Agent process ──▶ Redis pub/sub (meiva:events)
                        │
          ┌─────────────┴──────────────┐
          ▼                            ▼
  event_stream (per-event)   throttled_event_stream (batch)
          │                            │
          ▼                            ▼
   Browser EventSource          Browser EventSource
```

## Two Generators

### event_stream (per-event forwarding)
- Each Redis message is immediately forwarded as a frame `data: {event_json}\n\n`, no buffering/batching—the browser receives the same streaming granularity as the terminal UI.
- Parameters: `agent_id` (filter by this when not broadcast), `channel`, `role_filter` (`frozenset[str]`, only forward matching roles), `broadcast` (if True, no agent_id filtering).

### throttled_event_stream (optionally filtered batch throttled)
- With `agent_filter=None`, pushes all events from all agents. With a set, pushes only those agents plus system-level `agent_id=0` events. There is no role filtering; the validated event model supplies `agent_id` in the same drain pass.
- Drain Redis messages into a batch within a flush window, flush at a fixed rate: default `settings.gateway.sse_throttle_rate = 10/sec`.
- Each frame is a JSON array: `data: [{event}, {event}, ...]\n\n`.

## Endpoints (across two routers)

| Endpoint | Generator | Filtering | Defined in |
|---|---|---|---|
| `/api/agents/{id}/events/stream` | event_stream | agent_id | agent_events.py |
| `/api/agents/{id}/system` | event_stream | agent_id + SYSTEM_ROLES | system.py |
| `/api/system` | event_stream(broadcast) | GLOBAL_ROLES | system.py |
| `/api/system/all` | throttled_event_stream | optional comma-separated `?agents=`; `agent_id=0` always passes | system.py |
| `/api/stream` | merged_event_stream | `?channels=` subset + optional `?agents=` | merged.py |

## Merged multi-channel stream (`/api/stream`)

`gateway/events/merged.py` carries several projections over ONE connection, each
frame naming its channel: `data: {"ch": "all", "events": [...], "dropped": 3}`.
It exists because a browser allows six connections per origin and every
single-channel endpoint holds one forever, so a few open tabs starve the
document's own asset requests. `global` and `all` project the events channel,
`alerts` projects `meiva:alerts`; one pubsub connection is subscribed to both.

The five single-channel endpoints above are unchanged and remain the contract for
the desktop app, the SDK and every other subscriber. The Web UI reaches the
merged stream only through its own transport switch (`web.sseSingleStream`,
default off).

Merged mode keeps the batch cadence (`settings.gateway.sse_throttle_rate`, one
flush per window), so streaming deltas arrive exactly as fast as on
`/api/system/all`; `global` and `alerts` gain at most one flush interval.

Isolation, because the connection is now shared by every projection:

- **Per-channel error frames.** A source that refuses its subscription reports
  `{"ch":..,"error":..}` for the channels it serves and the stream keeps
  running for the rest. A read failure on the shared pubsub connection is not a
  per-channel condition: it reports one error frame per channel and ends the
  stream, which is what the single-channel endpoints always did (the browser
  reconnects).
- **Per-channel bounded queues.** Each channel owns its own pending list
  (`settings.gateway.sse_channel_pending_limit`), so an alert burst cannot
  starve `all`. A full queue sheds its OLDEST payload — the same policy as the
  agent-side `AgentEventPublisher` — keeping the newest state; the turn-end
  snapshot repairs the gap.
- **Drops are counted, never silent.** The flush after a drop carries
  `"dropped": <n>` for that channel, and the count is emitted as the existing
  `sse_drop` telemetry event (`kind=queue_full`, `n`) that the ops panel and the
  `meiva-ops-sse-drop-backlog` alert already read. Reports are aggregated per
  channel (first drop immediately, then at most once per 10 s) so a sustained
  overflow cannot become a write storm.

## Key Features

- **Two wire formats**: `event_stream` each frame is a `events.Event.model_dump_json()`; `throttled_event_stream` each frame is a JSON array of raw payloads.
- **Heartbeat**: every 15 seconds without events, sends `data: {"role":"heartbeat"}` (drives client watchdog); otherwise sends `: hb` comment (keeps TCP/proxy alive but invisible to browser `onmessage`).
- **Subscription failure**: `StreamingResponse` sends HTTP 200 before it iterates either generator. If `pubsub.subscribe()` then fails with a Redis IO or ACL error, the stream sends one `error` data frame and closes; the per-event endpoint sends an object, while the throttled endpoint sends a one-element array.
- **Disconnect detection**: polls `request.is_disconnected()` (`MEIVA_SSE_DISCONNECT_POLL_SECONDS`, default 1s).
- **Gateway shutdown**: both generators also poll `gateway.http.middleware.stopping.is_stopping()` each cycle and return cleanly once `gateway.cluster.server.GatewayServer` has marked its shutdown, so an open stream never holds a stop for uvicorn's drain budget; the browser's `EventSource` reconnects to the next gateway.
- **Lifecycle metrics**: successful subscriptions increment the per-mode `meiva_sse_active_connections` gauge and `meiva_sse_opened_total`; generator teardown decrements active depth and increments `meiva_sse_closed_total`. Modes are `filtered`, `throttled` and `merged`; gateway startup publishes zero depth for all three.

## Fault Tolerance

- Redis IO or ACL exceptions during subscription or reading → push an `error` event frame then exit the generator; the frontend can show the failure instead of silently receiving an empty stream.
- In finally, each cleanup step (unsubscribe / pubsub.aclose / client.aclose) is independently suppressed and logged; one failure does not swallow others.

## Entry Points

- `gateway/events/sse.py:event_stream()` — per-event generator
- `gateway/events/sse.py:throttled_event_stream()` — batch throttled generator
- `gateway/events/sse.py:_drain_redis_messages()` — drain batch messages within one flush window
- `gateway/events/merged.py:merged_event_stream()` — merged multi-channel generator
- `gateway/events/merged.py:ChannelQueue` — one channel's bounded pending queue and drop accounting

## Notes

SSE is unidirectional push (server→client). Frontend sends messages to agent via HTTP POST `/api/agents/{id}/messages`, not through SSE.
