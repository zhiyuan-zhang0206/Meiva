# What a CLI's own sub-agents say and do

A harness CLI can start sub-agents of its own (Claude Code's Agent/Task tool, Codex's `spawnAgent`).
They are not cluster agents: no id, no lineage, no inbox (see the
[external harness decision](../../../../docs/decisions/agents/graph/2026-10-19-external-harness.md)).
On the live adapter stream, their text and tool events are kept distinct from the main agent's own
`AssistantText` so sub-agent output never impersonates the main agent's reply while still waking the
live record reader.

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
    so a child's text and calls reach the live event stream during the run: the lag between a row appearing on disk
    and its event was 0.1 to 0.5 s, one poll interval, not "at the end of the turn". The parent's turn
    does not end while a child is alive: a child that was told not to message back and to run 25 s still
    held the parent's `result` until it finished, so a child writing after its parent's turn closed was
    not seen with `invoke_subagent` (the end-of-turn read stays as the guarantee). Should a child write
    while no turn is open, it is shown in the next one. After a resume the children that already existed
    are marked shown up to what is on disk and only what they write next is reported (a resumed
    conversation with a finished child replayed none of its rows).
  - A descriptor or transcript that is not the recorded shape is skipped and logged once
    (`agy_subagent_descriptor_unreadable`): this is display only and never fails a turn.

## Timeline boundary

External harness timelines are read from the main CLI session record
([reading a record](transcript-projection.md)): a CLI sub-agent appears on the timeline as the
spawning tool call (`agent_code`) and its result (`code_output`) in the main record, without
separate sub-agent text rows or nested child tool rows. On the live stream, `SubagentText` and child
`ToolUse` / `ToolResult` events keep sub-agent output separate from `AssistantText` and mark
`ReadTimelineSink` dirty while the sub-agent works.

Parent: [harness](harness.md).
