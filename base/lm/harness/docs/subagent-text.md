# What a CLI's own sub-agents say and do

A harness CLI can start sub-agents of its own (Claude Code's Agent/Task tool, Codex's `spawnAgent`).
They are not cluster agents: no id, no lineage, no inbox (see the
[external harness decision](../../../../docs/decisions/agents/graph/2026-10-19-external-harness.md)).
Their work used to be invisible to a person, who saw one tool call and its final result. The text they
write and the tool calls they make now reach the timeline as work detail, tied to the call that started them.

## The event

`SubagentText(turn_id, tool_use_id, agent, text)` (`base/lm/harness/events.py`). `tool_use_id` is the
call that started the sub-agent, `turn_id` the turn the text was observed in, `agent` the CLI's own
label when it gives one. It is never `AssistantText`: it is not the agent's reply, is not parsed for a
visibility prefix and is not part of `TurnCompleted.text`.

| CLI | Source | Label |
|---|---|---|
| Claude Code | `AssistantMessage` text blocks whose `parent_tool_use_id` is set | `subagent_type` of the call's task-started message |
| Codex | `item/completed` `agentMessage` of a thread named in a `spawnAgent` item's `receiverThreadIds` | none on the frames |
| Antigravity | the `PLANNER_RESPONSE` rows of the child conversation's transcript file | the descriptor's `role` |

A sub-agent's tool calls are the ordinary `ToolUse` / `ToolResult` events with `parent_tool_use_id` set to
the same call id (None for the main agent's own). Claude Code: the `parent_tool_use_id` of the
`tool_use` / `tool_result` frame. Codex: the child thread's tool items (`commandExecution`, `fileChange`,
`mcpToolCall`, `dynamicToolCall`, `webSearch`, a nested `collabAgentToolCall`) mapped by the same function as the
parent's and attributed through `SubagentThreads`. The recorded child thread sends a tool item as
`item/completed` alone, so a completed item the child never started is shown as a call followed by its
result.


What the CLIs send, recorded on Claude Code (claude-agent-sdk 0.2.168) and Codex 0.166:

- **Claude Code.** In this version the Agent tool starts an async sub-agent by default
  (`task_started` has `is_backgrounded`). Every message of a sub-agent carries the spawning call's id as
  `parent_tool_use_id`: its thinking, text, tool calls and tool results, plus the echo of its prompt as a
  user message. The sub-agent's text arrives with `forward_subagent_text` off for an async sub-agent
  and only with it on for a synchronous one, so the option is set. A background sub-agent keeps talking
  after the turn that launched it ended. Before this change that text was emitted as the main agent's
  own (it became the turn's reply text) and, arriving with no turn open, began a harness turn of cause
  UNKNOWN. Now thinking and the token stream of a sub-agent are dropped, its text blocks are
  `SubagentText`, and a sub-agent frame that arrives with no turn open begins a harness turn of cause
  `BACKGROUND_TASK`. Its tool calls and results are `ToolUse` / `ToolResult` carrying the call id in
  `parent_tool_use_id`.
- **Codex.** A sub-agent is a thread on the same app-server connection. Its notifications carry its own
  `threadId` and were dropped as another thread's. `SubagentThreads` learns the child id when the
  parent's `spawnAgent` item completes and shows the child's finished messages in the turn that is
  running. A child that speaks while no turn runs is not shown.
- **Antigravity.** `invoke_subagent` creates a sibling conversation (its own `brain/<id>/` directory) and
  its result returns to the parent as a system message. The stream-json frames carry nothing of the child
  and no usable step for the call (a `tool` step for `invoke_subagent` goes ACTIVE and never DONE, followed
  by `subagent` steps with no content; `map_step_update` drops it), so everything is read from files
  (recorded on agy 1.5.5, none of it a published interface):
  - The child's id is in the parent's `brain/<parent>/.system_generated/subagents/<child>.json`
    (`conversationId`, `subagentDescriptor` with `typeName` and `role`, `spawnStepIndex`) and in the
    output row of the call in the parent's transcript. `spawnStepIndex` is the output row of the last
    call of the planner row, so two calls started together carry the same value: the call is found by
    the child's id in the `GENERIC` output row, and its arguments are `tool_calls[k]` of the planner row
    before it.
  - The child's transcript, `brain/<child>/.system_generated/logs/transcript_full.jsonl`, has the parent's
    shape: a `PLANNER_RESPONSE` row's `content` is what it said and its `tool_calls` what it ran, the call
    `k` of the row at step `i` having its output in the `GENERIC` row at step `i + 1 + k`.
  - `AgySession` polls these files every 0.5 s while a turn of ours, or one it observes, is open, and once
    more as the turn ends (`SubagentReader`, `subagents.py`). The parent's own `invoke_subagent` call is
    written as a `ToolUse` / `ToolResult` with the child's conversation id as its call id (the stream
    never shows one that completes), the child's text is `SubagentText` labelled with the descriptor's
    `role`, and its tool calls carry that id as `parent_tool_use_id`.
  - Timing (live runs on agy 1.5.5, a child doing `sleep 5` steps with a sentence after each). agy
    appends every row to the child's transcript as the step finishes, while the parent turn is still open,
    so a child's text and calls reach the timeline during the run: the lag between a row appearing on disk
    and its event was 0.1 to 0.5 s, one poll interval, not "at the end of the turn". The parent's turn
    does not end while a child is alive: a child that was told not to message back and to run 25 s still
    held the parent's `result` until it finished, so a child writing after its parent's turn closed was
    not seen with `invoke_subagent` (the end-of-turn read stays as the guarantee). Should a child write
    while no turn is open, it is shown in the next one. After a resume the children that already existed
    are marked shown up to what is on disk and only what they write next is reported (a resumed
    conversation with a finished child replayed none of its rows).
  - A descriptor or transcript that is not the recorded shape is skipped and logged once
    (`agy_subagent_descriptor_unreadable`): this is display only and never fails a turn.

## On the timeline

The projector writes a `subagent_text` item per event: `payload` is the text, `source` the label,
`parent_item_id` the `agent_code` item of the spawning call, `user_facing=False`. It is secondary work: it folds into Details beside
the tool call, writes no `agent_publications` row, moves no unread cursor and sends no push.

A tool call a sub-agent made is the usual `agent_code` / `code_output` pair with the same
`parent_item_id`; the main agent's own pair has none. The Details view nests the sub-agent's rows under a left
rule and badges its tool headers `Sub-agent`.

Finding the spawning call: the projector knows the calls it wrote itself. A background sub-agent can speak or
act after the graph invocation that wrote its call ended, in a later invocation whose projector never saw it.
Every `agent_code` item of an external harness therefore stores the CLI's call id (`tool_use_id`), and the
projector looks the call up by that id once per call id and invocation
(`aget_harness_call_item_id`). A call found nowhere (the CLI's id was never written to this agent's timeline,
for example a forked history that predates the field) leaves `parent_item_id` empty and the item unmarked.

Volume: one message is cut at `SUBAGENT_ITEM_CHARS` (8,000) and one graph invocation stops after
`SUBAGENT_RUN_CHARS` (200,000) in total, after which sub-agent entries stop silently. A sub-agent's tool call input and
output count against the same total and are cut at the same size; after the limit a call writes nothing and an
output already running is replaced by `[output omitted: sub-agent limit reached]`. The main agent's own text and
tool output are not capped; these limits exist because nobody asked the agent to produce this text.

Parent: [harness](harness.md).
