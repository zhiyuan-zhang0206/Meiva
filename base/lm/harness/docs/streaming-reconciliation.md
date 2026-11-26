# Reconciling a harness CLI's stream with its record

The external-harness streaming overlay shows `TextDelta` / `ReasoningDelta` text as temporary `live.*` items and retires them when the CLI's own record
holds the same block. This note records what real turns showed about the keys that tie the two together and about
how far the record lags the stream. The recordings are in each adapter's `tests/fixtures/streaming/<scenario>/`:
`stream.jsonl` (the adapter's input, in arrival order), `record.jsonl` (the CLI's own rows, stamped with the moment the
file grew to hold them, 5 ms poll) and `source.json` (versions, scenario, sanitization). `t` is seconds since the prompt
was sent, on one clock for both files.

Recorded on 2026-11-25 with Claude Code 2.1.322 (SDK 0.2.168, `include_partial_messages`, adaptive summarized thinking, as in
`claude/options.py`), codex-cli 0.166.0 app-server and agy 1.5.16 acp. One recording per scenario: the millisecond figures
are one measurement each, so read them as orders of magnitude, not constants.

## Keys

| | Claude Code | Codex | Antigravity |
|---|---|---|---|
| Exact key | `message_start.message.id` equals the row's `message.id`; `content_block_start.index` equals the row's `apiBlockIndex` | delta `itemId` equals `item.id` of `item/started` / `item/completed` and the rollout `response_item` id (agent message and reasoning alike) | none: chunks carry no id or index |
| Use | block `"<message.id>:<index>"` | block `itemId` | text-prefix fallback, retire on turn end |

- Claude: a message with N blocks is N rows sharing `message.id`, in block order (thinking 0, text 1, tool_use 2 observed). A committed row of block N
  therefore also retires the still-live earlier blocks of its message: a thinking block whose row renders nothing has no twin of its own. The loop
  `index` in `claude/transcript/parse.py` counts blocks inside one row (always 0 for a one-block row); the API block
  index is `raw["apiBlockIndex"]`.
- Codex reasoning: `item/reasoning/summaryPartAdded` and `summaryTextDelta` carry the reasoning item's `itemId` and a `summaryIndex`
  (index into `item.summary`). When the model produced a summary, the rollout `response_item/reasoning` holds it as
  `summary_text`; only `encrypted_content` is unreadable. `summary` is `[]` when the model gave none.
- agy: the concatenated `agent_message_chunk` texts of a reply equal the record row's `content` exactly, so a prefix match is reliable.
  Chunks are coarse (a sentence or about eight numbers per chunk, 0.15 to 0.2 s apart), and no `agent_thought_chunk` appeared in any turn
  although `thoughtTokens` was non-zero.

## Timing: when the record catches up

- Claude writes the rows of one message together, after `message_stop`: +5 ms for a text-only message, about +100 ms for a message
  ending in `tool_use`, and the tool_use row does not wait for the tool (a 4 s Bash call still saw it at +100 ms). No row exists between a
  `content_block_stop` and `message_stop`. The SDK `AssistantMessage` arrives about 1 ms before `content_block_stop`, so it is not evidence
  of a committed row.
- Codex: the rollout line appears with `item/completed` (0 to 3 ms).
- agy: the transcript row appears with the `session/prompt` result (1 to 3 ms).
- Reasoning deltas are not paced: Claude's summarized thinking arrives as a burst at the end of the block; Codex sends the whole summary as
  one delta about 10 ms before `item/completed` (and none at all when the model gave no summary).

## Interruption

- Claude, interrupted while a text block streams: the stream closes with `content_block_stop`, `message_delta` (`stop_reason` null) and
  `message_stop`, and within 3 ms the record gets the partial row (`isAbortedMidStream: true`, `stop_reason` null, text equal to what streamed,
  trailing space included) followed by a user row `[Request interrupted by user]`. Interrupted during a thinking block: nothing is
  recorded except the user row.
- Codex: no `item/completed`; `turn/completed` has `status: "interrupted"` and `items: []`; the rollout has only `turn_aborted`. The partial
  text never reaches the record.
- agy: `session/cancel` ends the prompt with `stopReason: "cancelled"`; the transcript holds only the `USER_INPUT` row.
- A turn-end retirement must read the record once before it retires: Claude's partial row lands within milliseconds of the stream
  end, so retiring first would blank the text and then bring it back.

## Not covered

Several `tool_use` blocks in one message, subagent streams, Codex steer during a delta stream, models other than haiku-5-5 for Claude, and an
agy turn that emits `agent_thought_chunk`.
