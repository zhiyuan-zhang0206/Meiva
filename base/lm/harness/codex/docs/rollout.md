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
- So a row's byte offset is a stable, ordered name for it.

## Compression

From the source at `rust-v0.166.0` (`codex-rs/rollout/src/compression.rs`) and an experiment on a thread of ours:

- **When.** A background worker (`spawn_rollout_compression_worker`) compresses a rollout whose modification time is at least
  seven days old (`MIN_ROLLOUT_AGE`). It is started at app-server startup when compression is enabled in the thread store
  (`codex-rs/core/src/thread_store_builder.rs`) and by the `rollout/compress` request; the run is throttled by a marker under
  the Codex home. Production counts agree: files under eight days are plain, files over thirteen days are compressed, and no
  thread has both forms.
- **Form.** `<plain name>.zst` next to where the plain file was (`compressed_rollout_path`): one zstd stream, level 3, content
  size pledged, of exactly the bytes of the plain file; the plain file is removed once the stream is verified.
- **Reading.** `open_rollout_line_reader` reads either form; a plain file that disappears during the change is looked for again.
- **Resuming.** Opening a thread's rollout for append (`materialize_rollout_for_append`, called from `recorder.rs`
  `open_rollout_for_append` and `open_log_file`) decodes the `.zst` into a temporary file, copies mode and times, publishes it
  under the plain name without overwriting and removes the `.zst`. Measured: a rollout of ours (346,272 bytes) was compressed
  by the rule above and its plain file removed; `thread/resume` alone (before any turn) already restored `<name>.jsonl` and
  removed the `.zst`; after one more turn the file was 356,832 bytes, its first 346,272 bytes identical to the original, and its
  modification time had moved. So a plain file is back before anything is appended, with the same offsets, and a live reader
  never meets a compressed file of a thread that runs.
- **What the timeline does.** The cold read reads the compressed form when the plain file is not there: one streaming
  decompression per request (the `zstandard` package, no external command), counting the decompressed bytes and keeping only
  the last 16 MiB before the window's end, so offsets and ids are those of the plain file and a window reaches back at most
  as far as that tail (older rows are `has_more`). A stream that cannot be decoded, and any other form of file
  (`harness_record_unreadable`), is reported, not shown as an empty history.

## Rows

Each row is `{timestamp, ordinal, type, payload}`. The ones the timeline reads:

| Row | Becomes |
|---|---|
| `event_msg/item_completed` `UserMessage` | the person's input; `item.client_id` is the `clientUserMessageId` we sent (the input key), on the first input of a turn and on one added to a running turn alike; images are inline data URLs and are never copied into an item |
| `event_msg/item_completed` `AgentMessage` | a reply (phase `commentary` or `final_answer`), written whole when it is finished; its `id` (`msg_…`) names the reply |
| `event_msg/item_completed` `Reasoning` | nothing: the text is empty and the real one is encrypted |
| `event_msg/item_completed` `CommandExecution` | a call and its result, written only when the command has finished |
| `event_msg/item_completed` `FileChange`, `ImageView`, `Extension` (`kind: web.search`) | a call and its result (`patch`, `view_image`, `web_search`); the image view names the file only |
| `event_msg/item_completed` `McpToolCall` | a call `mcp:<server>/<tool>` with its `arguments`, and a result of the text parts of `result.content` joined (other parts by type name only, cut at 2000 characters); an error when `status` is `failed` or `result.isError` |
| `event_msg/item_completed` `Extension` (`kind: clock.sleep`) | a call `sleep` with `durationMs` and an empty result |
| `event_msg/item_completed` `SubAgentActivity` | `started` and `completed` each a status marker naming `agent_path`; `interacted` nothing; no call is paired across turns |
| `event_msg/item_completed` `CollabAgentToolCall` | a call (`spawn_agent`) and its result; the child thread's own file is not read |
| `event_msg/item_completed` `ContextCompaction` | the compaction marker |
| `event_msg/task_complete`, `turn_aborted` | the end of a turn; the interrupt marker |
| `response_item/message` role user holding `<subagent_notification>` | the sub-agent's report, shown as a marker that opens no turn (it is filed under a pseudo turn id, `auto-compact-N`) |
| `token_usage_record` | usage, read by the parser (the usage logged for a turn still comes from the turn result) |

Not read, and not reported: `session_meta`, `turn_context`, `world_state`, `compacted`, `additional_tools`,
`inter_agent_communication_metadata`, `response_item/agent_message` (the traffic between agents' threads),
`response_item/configuration_update`,
`event_msg/task_started`, `token_count`, `thread_settings_applied`, and the `response_item` rows (`message`,
`reasoning`, `custom_tool_call`, `custom_tool_call_output`, `function_call`, `function_call_output`).
Any other row type or item type is skipped and reported once, by kind, for the reader that met it
(`harness_record_row_unread`): `plan` and other kinds with no recorded shape.

The shapes of `McpToolCall`, `SubAgentActivity`, `clock.sleep`, `agent_message`, `configuration_update` and
`inter_agent_communication_metadata` come from a count over the rollouts of Meiva agents on five machines (174 files, CLI 0.163
to 0.166; only row kinds, key names and value types, no content) and not from a recording of ours: the model of the one run made
for this had no sleep tool and no MCP server or second agent was configured. The tests build those rows from the observed
shapes (`transcript/tests/test_observed_shapes.py`). A thread may record a sub-agent as `SubAgentActivity` rows only (CLI 0.166 on
one machine) or as `CollabAgentToolCall` (others); both are shown.

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
and the window ends before it. (Widening did not work before the compressed form was added: a block holding no row start gave an
empty window and ended the paging at the first row larger than a block, which an inline picture easily is.)
