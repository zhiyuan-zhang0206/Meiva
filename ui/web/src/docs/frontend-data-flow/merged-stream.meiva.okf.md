---
type: doc
title: Merged realtime stream (`web.sseSingleStream`)
description: One /api/stream connection carries the global, active-agent and alerts projections behind a default-off switch, with solo mode for plain HTTP, per-channel bounded queues and counted drops.
tags: [frontend, sse]
---

# Merged realtime stream (`web.sseSingleStream`)

One `/api/stream` connection can carry every projection the UI needs, each frame
naming its channel (`data: {"ch":"all","events":[...],"dropped":0}`). The switch
is the user setting `web.sseSingleStream` (Display → Realtime connection,
default **off**): off means every provider keeps its own endpoint exactly as
before, on means the browser holds one socket per tab instead of three.

- `lib/state/sse-merge.ts` owns `MERGED_SSE_CHANNELS` (the rollback lever:
  empty or a single channel returns the browser to the per-channel endpoints)
  and reads the setting. Switch on with an empty set is a half state; it is
  warned about once and treated as "do not merge".
- `lib/transport/sse-share.ts` opens that one source wherever the lock holder
  would have opened three, maps each wire channel to the logical channel its
  subscribers already use, and keeps the payload shape they parse (an object for
  `system`/`alerts`, the batch array for `systemAll`). With no
  `navigator.locks` — plain-HTTP LAN, the setup that exhausts a browser's six
  connections — it runs in **solo mode**: one page-local source, no lock and no
  relay, because the per-provider fallback is exactly where the sockets
  multiplied.
- A channel-scoped `{"ch":..,"error":..}` frame marks only that channel
  `reconnecting`; the socket stays open. A `dropped` count means the gateway shed
  payloads for that channel: providers see it as a gap and reconcile (the fold on
  open, alerts by invalidating `["alerts"]`), so a shed payload never renders as
  silence.
- The selected-agent union still travels in the URL (`agents=`, `0` when no page
  observes an agent), so changing the selection reopens the one socket; global
  and alert data repair themselves through the open reconcile, as they did when
  they had their own sockets.

Gateway owner: [[gateway/events/docs/sse.meiva.okf.md]].

Parent: [[ui/web/src/docs/frontend-data-flow/frontend-data-flow.meiva.okf.md|Frontend Data Flow]].
