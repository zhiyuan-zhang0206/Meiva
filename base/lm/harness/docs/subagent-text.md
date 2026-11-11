# What a CLI's own sub-agents say

A harness CLI can start sub-agents of its own (Claude Code's Agent/Task tool, Codex's `spawnAgent`).
They are not cluster agents: no id, no lineage, no inbox (see the
[external harness decision](../../../../docs/decisions/agents/graph/2026-10-19-external-harness.md)).
Their work used to be invisible to a person, who saw one tool call and its final result. The text they
write now reaches the timeline as work detail.

## The event

`SubagentText(turn_id, tool_use_id, agent, text)` (`base/lm/harness/events.py`). `tool_use_id` is the
call that started the sub-agent, `turn_id` the turn the text was observed in, `agent` the CLI's own
label when it gives one. It is never `AssistantText`: it is not the agent's reply, is not parsed for a
visibility prefix and is not part of `TurnCompleted.text`.

| CLI | Source | Label |
|---|---|---|
| Claude Code | `AssistantMessage` text blocks whose `parent_tool_use_id` is set | `subagent_type` of the call's task-started message |
| Codex | `item/completed` `agentMessage` of a thread named in a `spawnAgent` item's `receiverThreadIds` | none on the frames |
| Antigravity | not read (below) | |

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
  `BACKGROUND_TASK`. Its tool calls and results still flow as the main agent's `ToolUse` / `ToolResult`
  (not yet labelled as the sub-agent's).
- **Codex.** A sub-agent is a thread on the same app-server connection. Its notifications carry its own
  `threadId` and were dropped as another thread's. `SubagentThreads` learns the child id when the
  parent's `spawnAgent` item completes and shows the child's finished messages in the turn that is
  running. A child that speaks while no turn runs is not shown.
- **Antigravity.** `invoke_subagent` creates a sibling conversation with its own transcript, and its
  result returns to the parent as a system message. The stream-json adapter observes no step for the call
  and nothing of the child, so nothing is emitted. Reading the child's transcript is the remaining work.

## On the timeline

The projector writes a `subagent_text` item per event: `payload` is the text, `source` the label,
`parent_item_id` the `agent_code` item of the spawning call when this projector saw the call (None for
text that outlives its run), `user_facing=False`. It is secondary work: it folds into Details beside
the tool call, writes no `agent_user_messages` row, moves no unread cursor and sends no push.

Volume: one message is cut at `SUBAGENT_ITEM_CHARS` (8,000) and one graph invocation stops after
`SUBAGENT_RUN_CHARS` (200,000) in total, leaving one `status:` marker. The main agent's own text and tool
output are not capped; these limits exist because nobody asked the agent to produce this text.

Parent: [harness](harness.md).
