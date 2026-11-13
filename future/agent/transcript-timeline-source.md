# Finishing the move of external agents' timelines to the CLI's record

Status: Claude Code and Codex are done; Antigravity follows. The decision is
[the timeline is read from the record](../../docs/decisions/agents/graph/2026-11-13-timeline-read-from-the-record.md);
it replaces the stored copy that [the transcript decision](../../docs/decisions/agents/graph/2026-11-12-transcript-is-the-timeline-source.md)
planned and an earlier version of this document described (a resident writer, stored rows, read positions). The vocabulary,
the one projection and the parsers are in [reading a record](../../base/lm/harness/docs/transcript-projection.md).

## Done

A Claude Code or Codex agent's timeline is read from its record file when asked for: the runner's `harness_timeline_read` operation,
the gateway's window read, the live read by `ReadTimelineSink`, reply delivery under the row's `uuid`, inbox rows set `done`
with no timeline entry. Codex reads its rollout from disk (`harness_timeline_read` takes either harness; a `window.py` per CLI and
`base/agents/history/transcript_timeline/record/harness.py` choose the file and the row reading). Nothing about such an agent is written to `agent_harness_messages`.

What Claude no longer reached was removed once the read-time path landed: the writer, its host wiring and sink, the
agent-level rollout setting, the two stored-record tables with their retention job and grants (a migration drops them),
the Claude reader, store filter and wake store, and the port they implemented. Not removed, because Antigravity's parser and tests still use them: the parts of the shared projector that exist only for sub-agent nesting,
held output and replay after a restart (`SubagentLink`, `known_keys`, the hold bounds); they go with step 3.

## Remaining, in order

1. **Antigravity.** The same, from `transcript_full.jsonl`; its parser's samples are constructed and the raw
   recording must be redone before it is switched.
2. **Remove the old projector and the table.** `HarnessTurnProjector`, `TurnSink` and the legacy checkpoints in
   `agent/graph/claim`, `agent_harness_messages`, `base/agents/history/harness_messages.py`, the insights' and
   understanding's reads of it, the gateway's table branch, the frontend's `subagent_text` nesting and streaming-only
   branches. Then there is one path.

## Kept open

- **History when the machine is offline, and history past the CLI's own cleanup period.** Both are accepted costs of the
  decision. If they are wanted, the fix is a layer under the read (a stored copy of the rows, with a policy), not a return
  to a stored timeline.
- **Streaming.** The live view shows what the CLI has finished. A non-durable overlay of the turn in flight can be added
  without changing the source.
- **Sub-agent activity.** A CLI sub-agent is one tool call and its result. Showing its steps is a separate decision.
- **Insights.** The run timeline does not support Claude Code or Codex agents (it shows the page's unsupported state); it can be
  built on the same projection when asked for.
