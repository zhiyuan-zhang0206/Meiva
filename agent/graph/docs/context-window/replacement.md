# Compaction replacement

## Summary request prefix

Preserving the request prefix preserves its cache opportunity. Forced summarization cannot execute ordinary tools, so it receives the same summary contract even when the standing SDK reference omits `self`. The existing system prompt is reused for this request; `init_context` establishes the next standing head after replacement.

## Clean replacement

Every applied compact clears the entire messages channel and parks its summary behind a newly established standing head through INIT_CONTEXT. Raw conversation is never retained as a tail; claim defers co-batched chats to pending and finalizes previously claimed history before replacement.

Framework lifecycle markers may follow the summary. RemoveMessage operations are channel instructions, not summary content. History dumps are taken before the wipe and their notes go only into the fresh tail: inserting a note inside an AI/tool pair would break provider message ordering. Both paths stamp the summary message type and, when present, the live compact run id so timeline readers can join it to the running operation.

## Compact failure boundary

The summary call streams (`agent/graph/llm_stream/stream.py:stream_message`) under the stall rules of a turn's llm node: the agent's or model's TTFT and inter-chunk bounds and the stream total ceiling. A model that keeps emitting is never cut by a wall-clock total of the call, and a stall fails at once without a repeat, since the same request would stall again. Both paths retry only what warrants another identical request (`compaction_retryable`: an empty summary, a transient provider error) within the attempt bound.

Automatic compaction raises CompactionFailedError on exhaustion, without replacing history, and the aborted turn leaves a `compact_failed` system note (with the reason) in the history; programming failures escape immediately. A compact request (`meiva agents compact`, `/compact`) does not raise: the claim records the failure as a `compact_failed` system note (the reason, with the failure's type) and closes the live run as a failure, and the chats claimed in the same batch are delivered as usual. A batch whose only messages are that note idles an agent that was idle, with its history intact; an agent in the middle of a turn goes on to its next model call and reads the note. A request that fails supersedes nothing: an earlier summary of the same batch is still applied and its run still ends as a success. Only model-call failures (`compaction_failure`) become a note; any other exception is a programming error and propagates after the live run is closed as a failure. Cancellation wins over a pending compact in claim and closes its live run as replaced.

`CompactionFailedError` is a turn-abort signal, not a process-death instruction: the hosted turn settles to idling with no corpse marker (`PendingTurnFailure.marks_crash`), so the reaper never terminates the agent, and the turn's Error is published once the row is settled. An interrupted automatic request leaves the pending command for claim without a version bump or replacement. Audit and history-dump failures are reported without aborting an otherwise successful compaction; failure must not strand a live run without its terminal signal.

## Retained boundary checkpoints

The boundary retains the closing request metadata for reads and audits. The full pre-compaction snapshot remains recoverable; the old comments describing checkpoint trimming no longer describe this path.
