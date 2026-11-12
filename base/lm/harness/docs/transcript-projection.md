# Reading a harness CLI's record into timeline drafts

The shared layer in `base/lm/harness/session/transcript/` and the three parsers
(`claude/transcript/`, `codex/transcript/`, `antigravity/transcript/`) read a CLI's own conversation record
into one vocabulary and project it into timeline item drafts. Nothing in the running system calls them yet:
they are the pure core of the plan in [the transcript timeline plan](../../../../future/agent/transcript-timeline-source.md),
chosen by [the decision](../../../../docs/decisions/agents/graph/2026-11-12-transcript-is-the-timeline-source.md).

## The three pieces

- **Entries** (`entries.py`): `HumanInput`, `AgentText`, `Reasoning`, `ToolCall`, `ToolResult`,
  `SubagentLink`, `Notice`, `Interrupt`, `Compaction`, `TokenUsage`, `TurnEnd`, `Failure`, `Anomaly`. Each has
  a stable `key` (idempotency), an `Order(seq, sub)` that gives the entry's place inside its own record (the timeline itself is
  appended in arrival order, so no clock and no cross-record order), a `record` (`main` or a sub-agent's record id) and an optional `turn`. A kind is in the
  vocabulary only if two of the three CLIs have a counterpart.
- **The projection** (`projection.py`, `Projector`): the only place that pairs a call with its output
  (whichever arrives first), nests a sub-agent record under the call that started it (whichever record
  arrives first), recognises an input we sent (by the id the CLI kept, else by text in send order) and expands
  it into the original inbound messages it carried, opens and closes turns, and reports each model message's usage once (`Projection.usage`: main or sub-agent record, the call that
  started the sub-agent, the message's model) and tallies it per turn and per record. It does no I/O and reads no clock; an entry whose key was projected gives nothing a second time, and
  `known_keys` lets a forked conversation skip history it already has. Its output is `TimelineDraft`s
  (`drafts.py`) in the existing `TimelineItem` kind vocabulary, `Delivery` conclusions, turn observations and
  closings, usage tallies and `Anomaly` reports.
- **Port** (`ports.py`): `RecordReader`, the one way a record reaches the writer: `read_after(record, position)`
  returns the complete entries past the position and the new one, `records()` lists the records (a sub-agent's
  is its own). `position` is the adapter's own opaque mark; the writer stores it and never reads it. Stream
  frames and `session_store` callbacks only wake the writer.

Reply visibility (`resolve_agent_reply`) and the rendering of a tool call's payload are not here: they live
above `base/lm` (`base/agents`, `agent/graph/claim`), so the writer applies them to a draft.

## Parsers

A parser is a pure function from one raw row to entries, strict about the fields the projection needs. An
unknown row type is an `Anomaly(UNKNOWN_TYPE)` (reported once per type by the projection), a known type that
lacks a needed field an `Anomaly(MISSING_FIELD)`; neither puts anything on a timeline. Each parser's header lists
the row types it does not read yet. Golden tests run on sanitized recordings (`tests/fixtures/<scenario>/`,
each with a `source.json` naming the CLI version, the recording and the sanitization rules).

## Readers and storage

`claude/transcript/reader.py` (`ClaudeRecordReader`) is the Claude `RecordReader`: it reads the session's local
JSONL after a stored position (`position.py`: the byte span of the last row read, the SHA-256 of that row and the
row count, which is the next row's `Order.seq`), consumes only complete lines, at most 4 MiB of them per call (the
writer calls again until the answer is nothing), and checks by hash, without parsing, that the last row read is
still at its place; a position the file no longer holds is a fault. A complete line that cannot be used (not JSON,
not an object, invalid UTF-8, nested too deep, over 64 MiB) is not a fault: it is skipped with a `BAD_ROW` anomaly
and the position moves past it. Every row is scrubbed of what `jsonb` cannot hold (NUL, lone surrogates) before it
is parsed or stored. The reader's parse state is keyed by the position it belongs to, so reading again from a
position gives the same entries. A sub-agent's file is its own record. The store side is
`aappend_keyed_timeline_items` (append-only in `seq`, idempotent by `agent_harness_messages.source_key`) and
`agent_harness_transcript_positions` (the position and the rebuild ordinal), the stored whitelisted rows
(`agent_harness_transcript_rows`: `claude/transcript/store_filter.py` decides what is kept and its size caps (256 KiB
per text field for the main record, 16 KiB for a sub-agent's), `aappend_transcript_rows` appends them once,
`aload_transcript_rows` reads them back by ordinal; `services/upkeep/events_maintenance/transcript_retention.py`
expires the rows older than 30 days that lie before their record's rebuild point, in bounded batches), all in
`base/agents/history/harness_messages.py`; nothing calls them yet. A reader returns the rows to store with each
batch and parses stored rows back with `parse_stored`.

## Native records and the vocabulary

| Entry | Claude Code transcript | Codex history items (rollout for the rest) | Antigravity `transcript_full.jsonl` |
|---|---|---|---|
| `HumanInput` | `user` row whose `uuid` is ours; `attachment/queued_command.source_uuid` for an absorbed one | `userMessage.clientId`; a second user input of the same turn is a steer | `USER_INPUT` (text inside `<USER_REQUEST>`); no id unless the trajectory's `clientMessageId` is supplied |
| `AgentText` | `assistant` text block (one block per row, blocks share `message.id`); `isAbortedMidStream` is incomplete | `agentMessage` (whole, at completion) | `PLANNER_RESPONSE.content` |
| `Reasoning` | `thinking` block, `thinkingDurationMs` | `reasoning` item, text empty (encrypted) | `PLANNER_RESPONSE.thinking`, high-thinking tier only |
| `ToolCall` | `tool_use` block | `commandExecution`, `spawnAgent` and other items, completed state only | `PLANNER_RESPONSE.tool_calls[k]` (no id; id is `step:k`) |
| `ToolResult` | `tool_result` block of a `user` row (`toolDenialKind`, `toolUseResult.backgroundTaskId`) | the same item as its call | `GENERIC` row at `step + 1 + k`; `RUNNING` stays `RUNNING` = background |
| `SubagentLink` | `toolUseResult.agentId` on the call's result; child rows `isSidechain` + `agentId` | `spawnAgent` receiver thread ids; child thread items | child id inside the spawn row; `subagents/<child>.json` |
| `Notice` | `user` row with `origin.kind` (`<task-notification>`) | `<subagent_notification>` (pseudo turn id) | `SYSTEM_MESSAGE` (task notification, child message, timer) |
| `Interrupt` | `user` row `[Request interrupted by user...]` | `turn_aborted` / turn status `interrupted` | none (a signal) |
| `Compaction` | `system/compact_boundary` | `contextCompaction` / `compacted` | `CHECKPOINT` |
| `TokenUsage` | `message.usage` on every row of a message (deduplicated by `message.id`) | `token_usage_record` per sample (rollout only) | token fields of `PLANNER_RESPONSE` and `CHECKPOINT` rows |
| `TurnEnd` | last assistant row of a turn (`stop_reason` end_turn, max_tokens...) | `task_complete` | none (a signal) |
| `Failure` | none observed | `turn.error` / status `failed` (schema only) | `ERROR` status (documented, unobserved) |

## What does not fit, and who owns it

- **Turn in flight, and "is it running".** No record has it. Owned by the event stream and the process.
- **Cancel.** Antigravity leaves nothing (the cancelled step is a hole); Codex loses the half-streamed text and
  a steer accepted just before the interrupt. Owned by the stream's terminal signal; a reader may turn it into
  an `Interrupt` entry carrying an order at the last position it read.
- **Turn end for Antigravity**, and the exact end of a Claude turn (no end row; the result frame can arrive
  before the last row). Owned by the stream plus the writer's rule: the closing entry or a short quiet period.
- **Which inbound message an Antigravity ordinary turn answers.** The record has text only; the projection's
  text fallback covers the common case and reports an input it cannot place as unmatched.
- **Errors that kill the process.** No record shows them.
- **Image bytes**, which the record keeps inline (Claude, Codex) or as a path (Antigravity); the projection
  takes images from the table of what we sent.
