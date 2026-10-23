---
type: doc
title: Merged SSE stream
description: One connection carrying several event projections, each frame naming its channel, with per-channel errors, bounded queues and counted drops.
tags:
- gateway
- events
---

# Merged multi-channel stream (`/api/stream`)

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

Merged mode keeps the batch cadence (`gateway.sse_throttle_rate`, one
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
  (`gateway.sse_channel_pending_limit`), so an alert burst cannot
  starve `all`. A full queue sheds its OLDEST payload — the same policy as the
  agent-side `AgentEventPublisher` — keeping the newest state; the turn-end
  snapshot repairs the gap.
- **Drops are counted, never silent.** The flush after a drop carries
  `"dropped": <n>` for that channel, and the count is emitted as the existing
  `sse_drop` telemetry event (`kind=queue_full`, `n`) that the ops panel and the
  `meiva-ops-sse-drop-backlog` alert already read. Reports are aggregated per
  channel (first drop immediately, then at most once per 10 s) so a sustained
  overflow cannot become a write storm.

Parent: [[gateway/events/docs/sse/sse.meiva.okf.md]].
