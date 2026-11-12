# What a Codex rollout holds

The rollout is the file `codex app-server` writes for a thread:
`<codex home>/sessions/YYYY/MM/DD/rollout-<local time>-<thread id>.jsonl`, the home being `CODEX_HOME` or `$HOME/.codex`
of the environment the session runs in. It is not a published interface; everything here was observed on codex-cli
0.166.0 with the app-server protocol, on real threads (`gpt-5.6-luna`, low reasoning), and is what the read-time timeline
([the decision](../../../../../docs/decisions/agents/graph/2026-11-13-timeline-read-from-the-record.md),
[reading a record](../../docs/transcript-projection.md)) stands on. The parser is `transcript/rollout.py`, the file reader
`transcript/window.py`; the sanitized recordings the tests read are `transcript/tests/fixtures/rollout/`.

## The file only grows

- Every row is one JSON line, written whole; rows are never rewritten. A recording of a 112-row thread compared with the
  final file row by row had no difference and no half line was seen. A reader still uses only complete lines.
- A resume appends to the same file (two `thread_settings_applied` rows, then the new turn). A compaction appends
  (`task_started`, a `compacted` row holding the replaced history, about 100 KB, then `ContextCompaction`,
  `task_complete`); every earlier row stays. A fork is a new file holding only the clone's own rows, no copy of the
  parent's history. A sub-agent thread has its own file. A thread that has run no turn has no file: it is created when the
  first turn starts.
- So a row's byte offset is a stable, ordered name for it. A compressed rollout (the app-server documentation mentions
  the form; none was met) has another extension and is not read: `unreadable_file` names it, and the read reports an
  event instead of an empty history that looks like nothing happened.

## Rows

Each row is `{timestamp, ordinal, type, payload}`. The ones the timeline reads:

| Row | Becomes |
|---|---|
| `event_msg/item_completed` `UserMessage` | the person's input; `item.client_id` is the `clientUserMessageId` we sent (the input key), on the first input of a turn and on one added to a running turn alike; images are inline data URLs and are never copied into an item |
| `event_msg/item_completed` `AgentMessage` | a reply (phase `commentary` or `final_answer`), written whole when it is finished; its `id` (`msg_…`) names the reply |
| `event_msg/item_completed` `Reasoning` | nothing: the text is empty and the real one is encrypted |
| `event_msg/item_completed` `CommandExecution` | a call and its result, written only when the command has finished |
| `event_msg/item_completed` `FileChange`, `ImageView`, `Extension` (`kind: web.search`) | a call and its result (`patch`, `view_image`, `web_search`); the image view names the file only |
| `event_msg/item_completed` `CollabAgentToolCall` | a call (`spawn_agent`) and its result; the child thread's own file is not read |
| `event_msg/item_completed` `ContextCompaction` | the compaction marker |
| `event_msg/task_complete`, `turn_aborted` | the end of a turn; the interrupt marker |
| `response_item/message` role user holding `<subagent_notification>` | the sub-agent's report, shown as a marker that opens no turn (it is filed under a pseudo turn id, `auto-compact-N`) |
| `token_usage_record` | usage, read by the parser (the usage logged for a turn still comes from the turn result) |

Not read, and not reported: `session_meta`, `turn_context`, `world_state`, `compacted`, `additional_tools`,
`event_msg/task_started`, `token_count`, `thread_settings_applied`, and the `response_item` rows (`message`,
`reasoning`, `custom_tool_call`, `custom_tool_call_output`, `function_call`, `function_call_output`).
Any other row type or item type is skipped and reported once, by kind, for the reader that met it
(`harness_record_row_unread`): `McpToolCall`, `plan`, `WebSearch` items and similar were not recorded and have no mapping.

## The same content is in the file twice

An assistant message and a reasoning item are written as an `item_completed` row and again as a `response_item` row with
the same id (the model's view of it). A command is written structured only as `item_completed` (the `response_item` is the
JavaScript the orchestrator ran). The reader takes the `item_completed` row only: if both were read, a window that begins
between the two would name the item by another row's offset than a read that began earlier. A user message also appears as
a `response_item` without the `client_id`; it is not read.

## Order and time of writing

A row is written when its item is complete, with no row when an item starts and none for a delta: a command that runs
for twelve seconds leaves nothing in the file until it ends (measured from first sight of the row: 15 s after the input
for a `sleep 12`), and an answer appears whole. So the timeline has no "running" call for Codex, and a reply that was being
streamed when the turn was interrupted is never in the file.

The order of rows is the order of completion. A message added to a running turn is written after the output of the
sampling step it interrupted, so it comes before the command that was still running; a command that finishes after its
turn has ended is appended at the end of the file with the turn id it started in. The timeline appends in the order the
rows arrive, so the file order is what it shows; no case that needs another order to be correct was met.

An interrupt writes `custom_tool_call_output` ("aborted by user"), a synthetic `<turn_aborted>` user message and
`event_msg/turn_aborted` and no `task_complete`. A command that was running was not written afterwards in the one
recording made for this (research on 0.166 saw it appended later).

## Size

Rows can be large: the first row holds the instructions (about 19 KB), `compacted` about 100 KB, a command's output up to
tens of KB, and an image is an inline data URL in both its `item_completed` and `response_item` rows. The reader's blocks
are 256 KiB, widened while one row is longer than the block, up to 16 MiB for a request; a row longer than that is not read
and the window ends before it.
