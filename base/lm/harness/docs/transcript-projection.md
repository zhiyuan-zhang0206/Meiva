# Reading a harness CLI's record into timeline drafts

The shared layer in `base/lm/harness/session/transcript/` and the three parsers
(`claude/transcript/`, `codex/transcript/`, `antigravity/transcript/`) read a CLI's own conversation record
into one vocabulary and project it into timeline item drafts. An external agent's timeline is read from its
record when it is asked for and stored nowhere ([the decision](../../../../docs/decisions/agents/graph/2026-11-13-timeline-read-from-the-record.md),
which keeps the vocabulary and the one projection of
[the transcript decision](../../../../docs/decisions/agents/graph/2026-11-12-transcript-is-the-timeline-source.md) and replaces its stored
copy). The stored transcript pipeline has been removed. Antigravity now uses the same record-read path; the older adapter-event projector and display table remain until its authorized stable rollout, in
[the plan](../../../../future/agent/transcript-timeline-source.md).

## The read-time path

`session/transcript/record_file.py` is the half every CLI shares: `RecordFile` reads blocks of the main record backwards
from a line boundary (and counts the bytes it reads); `ZstdRecordFile` does the same for a compressed Codex rollout from a
bounded tail of one decompression pass. Each CLI has a `window.py` for what is its own. `claude/transcript/window.py`:
`EntryParser` runs `parse_row` over rows in file order with the row's byte offset as the entry's `seq`, and
`session_file` finds the file the way the session does (the session's own `HOME`, the project directory named by the
working directory, a search of every project directory for the session id as the fallback). `codex/transcript/window.py`:
`EntryParser` runs `parse_rollout_line` the same way (the offset replaces the rollout's own `ordinal`), and `session_file`
finds `<codex home>/sessions/YYYY/MM/DD/rollout-*<thread id>.jsonl` (the home the session's environment names: `CODEX_HOME`,
else `$HOME/.codex`). Both name which rows are a person's input and the id our input was sent with: Claude's row `uuid`
(derived from the input key), Codex's `client_id` on the `UserMessage` item, which is the input key itself.
`base/agents/history/transcript_timeline/record/harness.py` is where the harness is chosen, by enum, for these functions and for the reply's publication
key (`harness:claude:<row uuid>`, `harness:codex:<message id>`). Sub-agent files are not read: a CLI sub-agent is one tool
call and its result in the main record. [What the Codex rollout holds](../codex/docs/rollout.md) is stated beside the adapter.

`base/agents/history/transcript_timeline/` is the shared half: `RecordProjector` runs the one `Projector` over those
entries and turns each draft into an item (`items.py`, which also holds reply visibility and the tool-call
rendering), names it `h<offset>.<n>`, marks a call that has no result and is still open as running, and does not show
a person's input that no inbox row accounts for. A row of a kind a CLI's reader does not know is skipped and
reported once for each kind (`harness_record_row_unread`, by `UnreadRows` of `record_file.py`), for all three CLIs. A marker that is not a tagged note reaches the page as a `status:` or
an `error:` line, the only two a page shows: the interrupt, compaction and failure markers are written that way, and a
notice the CLI filed (a finished background task, a timer, a sub-agent's start or end) is made a status line in
`items.py`, with its cause kept as the item's source. `sent.py` reads back which inbox rows were handed to the CLI as which
input (their key gives the row's `uuid`) and what each of them shows; `read.py` reads a window of `limit` items from
the end of the file or before an item, with a bounded look-back for the call of a result and for the start of a turn.
The same `RecordProjector` over only the bytes appended since the last read is the live view
([harness turn](../../../../agent/graph/docs/harness-turn.md#record-read-timeline-external-harnesses)); the two give the same items.
Antigravity reads the main full transcript in file order, with the same bounded reader. Its input attribution uses the user-selected content-and-order rule rather than adding an identifier to the model-visible prompt. Repeated step indices never establish identity. Background handoffs and missing results leave closed call displays.

## The two pieces

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

Reply visibility (`resolve_agent_reply`) and the rendering of a tool call's payload are not here: they live
above `base/lm` (`base/agents`, `agent/graph/claim`), so whoever turns a draft into an item applies them.

## Parsers

A parser is a pure function from one raw row to entries, strict about the fields the projection needs. An
unknown row type is an `Anomaly(UNKNOWN_TYPE)` (reported by the projection once for each distinct kind and detail, here the type, among the latest `REPORTED_LIMIT` it remembers, not once for each row), a known type that
lacks a needed field an `Anomaly(MISSING_FIELD)`; neither puts anything on a timeline. Each parser's header lists
the row types it does not read yet. Golden tests run on sanitized recordings (`tests/fixtures/<scenario>/`,
each with a `source.json` naming the CLI version, the recording and the sanitization rules).

## Readers

`session/transcript/record_file.py` reads the session's local JSONL, and `position.py` (`rows_after`) splits the bytes
of a block into rows: only complete lines (ending in a newline) are used, so a row the CLI is still writing is read
on a later pass. A complete line that cannot be used (not JSON, not an object, invalid UTF-8, nested too deep) is
not a fault: it is skipped and the read goes on. Every row is scrubbed of what `jsonb` cannot hold (NUL, lone
surrogates) before it is parsed. Nothing is stored: the position of a read lives in the reader's memory only.

## Native records and the vocabulary

| Entry | Claude Code transcript | Codex history items (rollout for the rest) | Antigravity `transcript_full.jsonl` |
|---|---|---|---|
| `HumanInput` | `user` row whose `uuid` is ours; `attachment/queued_command.source_uuid` for an absorbed one | `userMessage.clientId`; a second user input of the same turn is a steer | `USER_INPUT` (text inside `<USER_REQUEST>`); no id unless the trajectory's `clientMessageId` is supplied |
| `AgentText` | `assistant` text block (one block per row, blocks share `message.id`); `isAbortedMidStream` is incomplete | `agentMessage` (whole, at completion) | `PLANNER_RESPONSE.content` |
| `Reasoning` | `thinking` block, `thinkingDurationMs` | `reasoning` item, text empty (encrypted) | `PLANNER_RESPONSE.thinking`, high-thinking tier only |
| `ToolCall` | `tool_use` block | `commandExecution`, `spawnAgent` and other items, completed state only | `PLANNER_RESPONSE.tool_calls[k]` (no id; identity is the row offset and call position) |
| `ToolResult` | `tool_result` block of a `user` row (`toolDenialKind`, `toolUseResult.backgroundTaskId`) | the same item as its call | `GENERIC` rows following the call in file order; `RUNNING` means a completed background handoff |
| `SubagentLink` | `toolUseResult.agentId` on the call's result; child rows `isSidechain` + `agentId` | `spawnAgent` receiver thread ids; child thread items | child id inside the spawn row; `subagents/<child>.json` |
| `Notice` | `user` row with `origin.kind` (`<task-notification>`) | `<subagent_notification>` (pseudo turn id) | `SYSTEM_MESSAGE` (task notification, child message, timer) |
| `Interrupt` | `user` row `[Request interrupted by user...]` | `turn_aborted` / turn status `interrupted` | none (a signal) |
| `Compaction` | `system/compact_boundary` | `contextCompaction` / `compacted` | `CHECKPOINT` |
| `TokenUsage` | `message.usage` on every row of a message (deduplicated by `message.id`) | `token_usage_record` per sample (rollout only) | token fields of `PLANNER_RESPONSE` and `CHECKPOINT` rows |
| `TurnEnd` | last assistant row of a turn (`stop_reason` end_turn, max_tokens...) | `task_complete` | none (a signal) |
| `Failure` | none observed | `turn.error` / status `failed` (schema only) | `ERROR_MESSAGE.error`; `GENERIC/ERROR` and `GENERIC/INVALID` are failed tool results |

## What does not fit, and who owns it

- **Turn in flight, and "is it running".** No record has it. Owned by the event stream and the process.
- **Cancel.** Antigravity leaves nothing (the cancelled step is a hole); Codex loses the half-streamed text and
  a steer accepted just before the interrupt. Owned by the stream's terminal signal; a reader may turn it into
  an `Interrupt` entry carrying an order at the last position it read.
- **Turn end for Antigravity**, and the exact end of a Claude turn (no end row; the result frame can arrive
  before the last row). Owned by the stream plus the record's closing entry, when the CLI writes one.
- **Which inbound message an Antigravity ordinary turn answers.** The record has text only; the projection's
  text fallback covers the common case and reports an input it cannot place as unmatched.
- **Errors that kill the process.** No record shows them.
- **Image bytes**, which the record keeps inline (Claude, Codex) or as a path (Antigravity); the projection
  takes images from the table of what we sent.

## Bounds and what is held

The projector's memory has a bound for every structure that could grow with a conversation (see the note above
`SEEN_LIMIT` in `projection.py`). Nothing is held for ever: a call still unanswered when its turn closes, or when the next
turn on its record begins, is abandoned (it no longer keeps the record from a quiet point; its late result is still
paired); a result whose call has not been read, and the output of a sub-agent whose link has not been read, are held for a
bounded number of entries (`HOLD_ENTRIES`) and are then released as top-level
items with an `ORPHAN_RELEASED` or `UNLINKED_RELEASED` anomaly each.

## Origins

A row or a queued command may carry `origin.kind`. The parser keeps what the row says (`HumanInput.origin`: the kind, the
cause the harness vocabulary has for it and the notice the row would be) and decides nothing; the projection decides, in
this order, for a user row and a queued command alike: (1) the row's wire id is one of the inputs we sent (`SentInput`):
it is ours and it settles, whatever origin the row names (the records we have of the SDK path show no origin on our own
inputs, so what a CLI tags them with is unknown, and an input that is not settled is sent again, which the CLI would receive
twice); (2) the origin is one `harness_cause` knows (`task-notification`, a timer): a notice; (3) no origin, or `human`
(what a newer CLI puts on what a person typed): an input we did not send, shown as unmatched; (4) any other origin: an
anomaly (`origin/<kind>`) that is reported and puts nothing on the timeline, because nothing can say what it is. An
anomaly is reported once per (type, detail) within the latest `REPORTED_LIMIT` ones the projector remembers, so a restart
reports it again. The sample of a long real session (`claude/transcript/tests/fixtures/long_session`, sanitized to structure) pins
this, and that the bookkeeping rows of such a session are ignored and not reported.
