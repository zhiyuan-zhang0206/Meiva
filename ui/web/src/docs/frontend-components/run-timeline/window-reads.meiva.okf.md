---
type: doc
title: Agent view window reads
description: Bounded viewport requests, snapshot ownership and preparation recovery.
tags: [frontend]
---

# Agent view window reads

**Window reads.** The initial `recent_hours=24&max_units=5000` and subsequent range reads share a 35-second HTTP budget for headers and body. A retryable index-preparation response is retried at two-second intervals for at most 30 retries; permanent failures become explicit errors. Prepared histories use indexed database time ranges and only the current live segment is reconstructed. The service returns a `snapshot` identity that every subsequent window, raw-message and context read carries. A 409 refreshes the initial owner and retries against its new snapshot. Selections belong to their original snapshot and are cleared on replacement so an old index never selects a different message. Each query passes its cancellation signal: removing an agent
or leaving the page cancels its request. Timeouts use the per-agent failure and
Retry flow; selection cancellation is not a business failure. This client budget
does not impose a backend history-reconstruction deadline.

**Pan continuity.** Viewport changes are sampled every 80 ms during a gesture,
without waiting for it to end. Approaching the edge of loaded coverage (within a
quarter viewport) requests missing ranges with one viewport of padding on each
side, clamped to the agent lifetime. Pending ranges prevent duplicate reads;
panning does not cancel a read that can fill the cache.

Every visited range stays observed in TanStack Query until its agent leaves or
its snapshot changes. Local state holds query identities, never copies of server
data. The display merges all loaded nodes and units by their stable identities,
so revisiting any loaded subrange paints immediately and selected blocks remain
available outside the viewport. Complete coverage is an interval union: the
merged display's bounding window must not be mistaken for coverage of gaps.
Dense responses are not complete message coverage; a narrower visible-range read
can recover detail, and a still-dense range offers zoom without a retry loop.
Already loaded bars remain painted alongside that hint. Failed reads keep all
loaded data and can be retried explicitly. Each agent's range status has a
reserved single line beside the window summary, so loading, success and retry do
not move the chart vertically. Snapshot replacement drops all old ranges.

Parent: [[ui/web/src/docs/frontend-components/run-timeline/run-timeline.meiva.okf.md]].
