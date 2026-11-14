# External agents' timelines read from the CLI's record

Status: complete in code across all three external harnesses (`claude_code`, `codex`, `antigravity`). The decision is
[the timeline is read from the record](../../docs/decisions/agents/graph/2026-11-13-timeline-read-from-the-record.md);
it replaces both the stored transcript copy that
[the transcript decision](../../docs/decisions/agents/graph/2026-11-12-transcript-is-the-timeline-source.md)
planned and the earlier adapter-event database timeline path. The vocabulary, projection and readers are in
[reading a record](../../base/lm/harness/docs/transcript-projection.md).

## Done

An external agent's timeline is read from its CLI record file when asked for: the runner's `harness_timeline_read`
operation, the gateway's window read, the live read by `ReadTimelineSink`, reply delivery under the record's stable
identity, and inbox rows set `done` with no database timeline entry (`window.py` per CLI and
`base/agents/history/transcript_timeline/record/harness.py` choose the file and row reading).

Both legacy persistence paths have been removed:
- the stored-record writer, its host wiring and sink, the two stored-record tables, retention job and grants;
- the legacy adapter-event projector, sink and database display store, along with the insights and understanding
  fallbacks, the gateway's table branch, sub-agent nesting and replay staging in `session/transcript/`, and the
  frontend's legacy sub-agent row nesting and external streaming revision branches.

## Kept open

- **History when the machine is offline, and history past the CLI's own cleanup period.** Both are accepted costs of the
  decision. If they are wanted, the fix is a layer under the read (a stored copy of the rows, with a policy), not a return
  to a stored timeline.
- **Streaming.** The live view shows what the CLI has finished. A non-durable overlay of the turn in flight can be added
  without changing the source.
- **Sub-agent activity.** A CLI sub-agent is one tool call and its result. Showing its steps is a separate decision.
- **Insights.** The run timeline does not support external harness agents (it shows the page's unsupported state); it can be
  built on the same projection when asked for.
