---
type: doc
title: Compaction replacement
description: Summary request preservation, replacement and failure boundaries.
tags: [agent, compaction, contracts]
---

# Compaction replacement

## Summary request prefix

Preserving the request prefix preserves its cache opportunity. Forced summarization cannot execute ordinary tools, so it receives the same summary contract even when the standing SDK reference omits `self`. The existing system prompt is reused for this request; `init_context` establishes the next standing head after replacement.

## Clean replacement

Every applied compact clears the entire messages channel and parks its summary behind a newly established standing head through INIT_CONTEXT. Raw conversation survives only as the retained tail; the setting keeps none by default. Claim defers co-batched chats to pending and finalizes previously claimed history before replacement.

Framework lifecycle markers may follow the summary. A security note attached to a deferred chat goes with that chat; the fresh delivery scans it again. RemoveMessage operations are channel instructions, not summary content. History dumps are taken before the wipe and their notes go only into the fresh tail: inserting a note inside an AI/tool pair would break provider message ordering. Both paths stamp the summary message type and, when present, the live compact run id so timeline readers can join it to the running operation.

## Retained tail

A replacement may park a configured number of trailing messages verbatim — the `keep_tail_messages` setting, zero by default. They follow the summary and every note parked with it, so the last message the agent reads is the last thing that happened and no note describing the replacement sits behind it.

The window sits last because parked messages are notes only: the claim path defers a chat that rode in the same batch to a pending inbound instead of parking it, so nothing that arrived after the compaction can end up ahead of the replaced history. If that ever stops holding, the window must move ahead of the parked chat instead.

The window is made self-contained before it is parked. It never copies the standing system prompt, never starts with a tool result, keeps a tool call only with every result of it, and drops the `meiva.self.compact` call that triggered the replacement together with its `[system halt]` result: keeping the request would show the agent a compaction call inside a context that was already replaced, which reads as if the replacement had not happened. A retained AI message also loses its usage metadata — the token count it recorded measured the context that was just replaced, and keeping it would make the fresh context look full on the next occupancy check.

## Compact failure boundary

Automatic compaction retries explicitly recoverable empty/short summaries and retryable provider failures within its attempt bound. Exhaustion raises CompactionFailedError without replacing history; programming failures escape immediately. Cancellation wins over a pending compact in claim and closes its live run as replaced.

`CompactionFailedError` is a turn-abort signal, not a process-death instruction. An interrupted automatic request leaves the pending command for claim without a version bump or replacement. Audit and history-dump failures are reported without aborting an otherwise successful compaction; failure must not strand a live run without its terminal signal.

## Retained boundary checkpoints

The boundary retains the closing request metadata for reads and audits. The full pre-compaction snapshot remains recoverable; the old comments describing checkpoint trimming no longer describe this path.
