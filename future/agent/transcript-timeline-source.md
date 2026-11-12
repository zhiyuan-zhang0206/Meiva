# Finishing the move of external agents' timelines to the CLI's record

Status: Claude Code is done; Codex and Antigravity follow. The decision is
[the timeline is read from the record](../../docs/decisions/agents/graph/2026-11-13-timeline-read-from-the-record.md);
it replaces the stored copy that [the transcript decision](../../docs/decisions/agents/graph/2026-11-12-transcript-is-the-timeline-source.md)
planned and an earlier version of this document described (a resident writer, stored rows, read positions). The vocabulary,
the one projection and the parsers are in [reading a record](../../base/lm/harness/docs/transcript-projection.md).

## Done

A Claude Code agent's timeline is read from its record file when asked for: the runner's `harness_timeline_read` operation,
the gateway's window read, the live read by `ReadTimelineSink`, reply delivery under the row's `uuid`, inbox rows set `done`
with no timeline entry. Nothing about such an agent is written to `agent_harness_messages`.

## Remaining, in order

1. **Remove what Claude no longer reaches.** The writer (`agent/harness_timeline/`), its host wiring
   (`services/agent_runner/agent_host/transcript/`, `TranscriptSink`), the setting `harness_transcript_timeline` and its
   config entries, the tables `agent_harness_transcript_rows` and `agent_harness_transcript_positions` with their
   retention job and grants, `claude/transcript/reader.py`, `position.py`, `store_filter.py` and `wake.py`, and the parts
   of the shared projector that exist only for sub-agent nesting, held output and replay after a restart
   (`SubagentLink`, `known_keys`, `parse_stored`, the hold bounds). A migration drops the two tables.
2. **Codex.** Its own file-to-entries reader (the history items from the app-server, the rollout for the rest, a
   window read with offsets or its own stable position), `reads_timeline_from_record` extended, the same three changes
   (the gateway's read, the live read, delivery) and the same removal of its event-path writes. Open: a Codex record has
   no turn end for a cancel, and its rows are in completion order, so the offset is not the order of the conversation; the
   id scheme needs the same care as here.
3. **Antigravity.** The same, from `transcript_full.jsonl`; its parser's samples are constructed and the raw
   recording must be redone before it is switched.
4. **Remove the old projector and the table.** `HarnessTurnProjector`, `TurnSink` and the legacy checkpoints in
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
- **Insights.** The run timeline does not support Claude Code agents (it shows the page's unsupported state); it can be
  built on the same projection when asked for.
