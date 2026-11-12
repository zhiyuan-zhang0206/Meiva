# Harness timeline writer

The session-scoped writer of an external agent's timeline, from
[the plan](../../future/agent/transcript-timeline-source.md) and
[the decision](../../docs/decisions/agents/graph/2026-11-12-transcript-is-the-timeline-source.md). It reads the
CLI's own transcript by stored position (a `RecordReader`, see
[reading a record into timeline drafts](../../base/lm/harness/docs/transcript-projection.md)), projects what it
read, and commits keyed timeline items, the settlement of the inbound rows the entries prove delivered and the
new positions in one transaction under the runtime's incarnation fence.

- `plan.py` (pure): a projection becomes a `WritePlan` (items in read order, settlements, usage, closed turns,
  reports). Reply visibility and the rendering of a tool call are applied here, once.
- `writer.py` (shell): the loop (woken by a stream frame, a `session_store` callback or a poll), the
  transaction, the retry of an uncommitted plan, the silent replay after a restart, and the turn-end wait
  (the turn's closing entry, a short quiet period, or a ceiling).

Not wired into the runtime yet: nothing constructs a writer, and no graph code consults it.
