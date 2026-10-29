---
type: doc
title: Silent Model Waits
description: Request-owned empty SSE keepalives and the transient model waiting card.
tags: []
---

# Silent model waits

Each active streaming request emits an empty `llm_keepalive` SSE event every
0.5 seconds, including before its first provider chunk. The timer belongs to
that request and stops on success, failure, stall or cancellation. It does not
reset provider deadlines, mark model progress, or enter messages, token counts
or reasoning timing. The UI uses `msg_idx` to show a transient waiting card
when no real block for that message exists, including for mid-request joins.
Real content replaces the card; completion, error, cancellation or a committed
snapshot removes it. Waiting does not imply a visible reasoning summary.
