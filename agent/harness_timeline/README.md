# Harness timeline writer

The session-scoped writer of an external Claude Code agent's timeline, from
[the plan](../../future/agent/transcript-timeline-source.md) and
[the decision](../../docs/decisions/agents/graph/2026-11-12-transcript-is-the-timeline-source.md). It reads the
CLI's own transcript by stored position (a `RecordReader`, see
[reading a record into timeline drafts](../../base/lm/harness/docs/transcript-projection.md)), projects what it
read, and commits keyed timeline items, the settlement of the inbound rows the entries prove delivered and the
new positions in one transaction under the runtime's incarnation fence. It is wired behind the agent setting
`harness_transcript_timeline` (default **off**; a temporary rollout switch deleted when Claude, Codex and
Antigravity have all cut over).

- `plan.py` (pure): a projection becomes a `WritePlan` (items in read order, settlements, usage, closed turns,
  reports). Reply visibility and the rendering of a tool call are applied here, once.
- `writer.py` (shell): the loop (woken by a stream frame, a `session_store` callback or a poll; it repeats while a
  read brings something), the transaction, the retry of an uncommitted plan, the rebuild after a restart (bounded),
  the fence, the history skip of a session that starts with history, markers and the turn-end wait.

Composition. The writer is built and closed with the runtime by the agent host
(`services/agent_runner/agent_host/transcript/timeline.py`): the host binds each hosted turn's context and admitted
incarnation, the SDK's `session_store` (`base/lm/harness/claude/transcript/wake.py`) wakes it, the graph invocation
holds it as `ctx.harness_timeline` (`base/agents/context/harness_timeline.py` is the contract), and what follows a
commit (publish, capture for delivery, usage, reports) is `services/agent_runner/agent_host/transcript/effects.py`, which the
host passes as `after_commit` because it needs the graph's delivery and pricing.

**The incarnation without an invocation.** The fence is the incarnation the host admitted for the latest hosted turn,
bound to the writer at every turn. `admit_hosted_runtime` keeps the row's generation while the same host owns it and
the settled row stays `idling` with it, so the writer keeps committing between turns; a different owner's admission
mints a new generation, which fences the old one: the writer commits nothing and keeps its plan until the new
incarnation is bound. Tests: `agent/harness_timeline/tests/test_writer_lifecycle.py`.

**Starting on a session with history.** A fork's record starts with the parent's history, and an agent that was on the
event path already has its timeline: with no stored position and a non-empty timeline, the main record is read from the
row of the first input this process sent (found by its wire id) and sub-agent records already on disk from their ends,
so the history is not projected again. `copy_harness_timeline` copies the keys and nothing else: no rows, no positions.

**Restart bound.** A restart rebuilds the projector from the stored rows after the record's rebuild point, at most
`REBUILD_MAX_ROWS` (20,000) of the latest rows; beyond that it says so (`harness_timeline_rebuild_bounded`) and a call
that began earlier is not known, so its later output is held for a bounded time and then shown top-level (`orphan_released`).

**One read at a time.** `read_now` is serialized by a lock: the loop, the invocation's waits, the final read and the
delivery side's checks (an input the stream calls refused, the inputs an earlier process left claimed) all read through
it, so a plan is committed once and accounted for once. The effects that follow a commit
(`services/agent_runner/agent_host/transcript/effects.py`) are independent of each other and of the commit: publishing
goes through a publisher of the runtime (it outlives the invocation), the replies meant for the person and the spend are
kept as owed until they have gone through, both idempotent, and `recover` offers the latest stored replies to the capture
again when the writer starts, which covers a process that died between a commit and its effects. A process that dies there
loses the spend of that one read (it is a log line, not a table).

**Nothing is held for ever.** A result whose call has not been read and the output of a sub-agent whose link has not been
read are held by the projector for a bounded number of entries and by the writer for `HOLD_SECONDS`; then they are shown
top-level with an anomaly (`orphan_released`, `unlinked_released`). A call whose turn closed, or whose turn was followed by
the next one, is abandoned and no longer keeps the record from a quiet point. A sub-agent record that appears after the
writer started is read from its first row; only records on disk at the start are history to skip.

**Which session.** The writer reads the live session's id once the CLI has reported it and, before that, the id the session
registry holds for the agent (`session_with_resume`), so the inputs an earlier process left claimed are decided against the
record of the session the CLI is resuming, not against nothing.
