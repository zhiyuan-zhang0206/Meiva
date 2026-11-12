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
goes through a publisher of the runtime (it outlives the invocation), the spend is kept as owed until it is logged (once per
message), and the replies are not kept in memory at all: the writer's first commit makes the agent's reply progress
(`agent_harness_timeline_progress.replies_seq`) in its own transaction, at the end of the timeline as it was before that
commit, and the effects raise it only after the replies up to it have been through `capture_items`. The replies owed are
the keyed `agent_chat` rows after it, read as a range, so a pass reads the part not yet handed over and not the history;
`recover`, run when the writer starts, is the same pass, which covers a process that died between a commit and its
effects: a reply is tried at least once after its commit, and the publication key (`harness:<item id>`) keeps the record to
one (an extra attempt that finds the record there publishes nothing again, but may send one more `agent_updated`). A fork, whose copy has no progress row until its
own first commit, an agent that was on the event path, and replies written while delivery was `sdk` are therefore never
offered again, and a reply that conflicts with a delivery already recorded is dropped with
`harness_timeline_reply_dropped` and holds none of the others. Passes run one at a time under a lock, and a failure that
repeats is reported once a minute. A reply that fails is tried again after an interval of the injected clock (1 second, doubled up
to 60, with nothing read while nothing is owed), without waiting for a commit; one that conflicts, whose stored row does not
validate or that has kept failing for ten minutes (over at least five attempts, from its first failure, by the injected clock; a success or a restart starts the count again) is dropped with the event, which says why, for how long and after how many attempts, and never carries the text, and the
ones after it go on. The mode a reply is captured under is the one of the context bound at the latest hosted turn, as the
writer's reply policy is the runtime's: a reply the CLI writes while the agent is idle, after the mode was changed and before
the next turn, is delivered under the earlier mode. Everything an agent's writer or its publisher can raise stays in that agent's own task
group (`TranscriptTimeline.run`), so it cannot cancel the other agents' writers in the host's group. A process that dies
between a commit and its effects loses the spend of that one read (it is a log line, not a table).

**A turn is paired with its closing by identity.** The invocation waits for the record turns its own inputs were in, not for
"the next closing": the writer keeps, bounded, the record turn each input it settled was delivered in (`DELIVERED_KEPT`) and
the spend of each closed turn (`CLOSED_KEPT`), `wait_for_turn_end(keys)` is over when the turns of all `keys` have closed, and
`closed_usage(keys)` is the spend of those turns. A late closing, a lost result frame and any number of turns the CLI ran on
its own cannot shift a later wait or compare another turn's spend (`tests/test_writer_turns.py`).

**Origins.** Whose input a row is is decided by the projection, by the wire id first and the origin second
([origins](../../base/lm/harness/docs/transcript-projection.md#origins)).

**Nothing is held for ever.** A result whose call has not been read and the output of a sub-agent whose link has not been
read are held by the projector for a bounded number of entries and by the writer for `HOLD_SECONDS`; then they are shown
top-level with an anomaly (`orphan_released`, `unlinked_released`). A call whose turn closed, or whose turn was followed by
the next one, is abandoned and no longer keeps the record from a quiet point. A sub-agent record that appears after the
writer started is read from its first row; only records on disk at the start are history to skip.

**The login of the CLI.** The CLI finds its login with `HOME`, `USER` and `LOGNAME`; the child environment of a session
carries all three (`HOST_PASSTHROUGH_KEYS`, part of `session_child_env_keys`), and the writer reads the transcript under the
same `HOME`.

**Which session.** The writer reads the live session's id once the CLI has reported it and, before that, the id the session
registry holds for the agent (`session_with_resume`), so the inputs an earlier process left claimed are decided against the
record of the session the CLI is resuming, not against nothing.

## Linear, bounded, orderly

The cost of a round does not depend on how much is already stored, and the test that says so
(`tests/test_writer_linear.py`) measures it where a quadratic term hides: it takes, for every statement the writer
sends in a transaction, the rows read by sequential scans and fetched through indexes from the database's own counters
(`pg_stat_xact_user_tables`), for a session with hundreds of turns, forty background sub-agents that keep producing, calls
that are never answered and a restart; it also bounds the bytes read from the CLI's files, the statements and the memory.

- **Calls are found by index.** A sub-agent's item nests under its call; the call's item is found by the CLI's id through
  the partial index `idx_agent_harness_messages_call_id` on `(agent_id, call_id)`, one statement for all the calls a batch needs
  and none for a call already known: the writer keeps the latest `CALLS_KEPT` (4,096) `tool_use_id -> item id` it has
  written or looked up, absent ones included, and a call that fell out of it is one lookup through the index.
  `call_id` is a column of its own (the call id of an `agent_code` item, NULL otherwise, written by every insert of an item,
  old path and new) and not an index over an expression of `item`: an index that references `item` would stop every
  in-place revision of a live activity (`UPDATE ... SET item`, the old path that runs with the setting off) from being a
  heap-only update. `tests/test_call_id_index.py` shows both: the revisions stay heap-only with this index and would not
  with an expression index, and the lookup is planned on this index.
- **One transaction is bounded.** Records are read in turn, each at most one reader batch (4 MiB), until a transaction has
  `MAX_TRANSACTION_BYTES` (8 MiB) or `MAX_TRANSACTION_ENTRIES` (5,000) entries; the other records are read by the next
  transaction, which starts with them. The items of a transaction are one insert (pipelined) under one lock of the agent's
  row, the settlements one `UPDATE`, the positions one pipelined upsert, whatever their number.
- **An idle poll costs one `stat`.** The reader remembers, per record, the size and modification time at which a read from
  the stored position found nothing new, and answers without opening the file while they are unchanged; the sub-agent
  listing is made again only when the folder's modification time moves (and at every poll for the 2 seconds after it moved, because a second file of the same clock tick leaves it where it was), and the session's own file is looked up by its
  path (the all-projects search is made at the first miss and every 20th after it). There is still one thread hop per
  record per poll; the cost of it is a `stat`.
- **Memory is bounded by the conversation's constants, plus one small entry per record.** Writer: `memory_sizes()`
  (registered inputs only until their settlement, `NON_HUMAN_LIMIT`, `TURN_USAGE_LIMIT`, `CLOSED_KEPT`, `DELIVERED_KEPT`, `CALLS_KEPT`).
  Projector: the limits in `projection.py` (`SEEN_LIMIT`, `CALLS_LIMIT`, `TURNS_LIMIT`, `LINKS_LIMIT`, `USAGE_LIMIT`,
  `REPORTED_LIMIT`, `HOLD_ENTRIES`). Reader: the parse state of the latest two positions of each of the latest 256 records
  that were read (a record that falls out loses only the duration of a call that was open in it), and one stat stamp per
  record (cleared at 4,096). What is **not** bounded by a constant is what a record is: the writer's `_positions` and
  `_last_ordinal`, the projector's links (one per sub-agent) and the reader's listing grow by one small entry (tens of bytes)
  for each sub-agent record of the session, and a poll costs one `stat` per record. They do not grow with the length of
  any record or with the number of turns; a session that has run a hundred thousand sub-agents would hold a hundred
  thousand entries. A finished sub-agent's entry is not evicted: its position is how the reader knows it has nothing more to read.
- **A lost position is not a fault.** A position the file no longer holds (replaced, truncated, rewritten) puts the record
  at its current end, in the same way as a switch to a session with history, and raises
  `harness_timeline_position_lost` with the agent, session, record and the position that was lost. The new position counts
  its rows from above the highest ordinal stored for the record, so the rows of the rewritten file never take the
  ordinals of stored ones (a stored ordinal is not written again). What the CLI wrote in between is not on the
  timeline; it is still in the CLI's local file and can be projected afterwards:
  set the record's stored position to the start and restart the agent's runtime.

  ```sql
  UPDATE agent_harness_transcript_positions
     SET position = '{"start":0,"offset":0,"lines":0,"sha256":""}', rebuild_ordinal = -1
   WHERE agent_id = :agent AND session_id = :session AND record = :record;
  ```

  (A session with no stored position at all is not a candidate: it is read as a session with history and skipped, so
  insert the row with that position instead of updating it.) The record is then read from its first row and every row
  already projected is skipped by its key, so only what is missing is written. What is written now is **appended to the
  end of the timeline**, after what was shown meanwhile, not placed where it happened; the timeline stays append-only, and
  that is a boundary of the procedure. Read order inside the recovered rows is the CLI's own.
- **Retries back off, and a stop wakes the wait.** A failed read or commit is tried again after 2, 4, 8 ... 30 seconds; the
  first failure and every tenth are logged with their stack, the others in one line; 60 seconds of failures without a success
  stop the loop with a fault. A `stop` ends the wait at once and a `stop` asked for before the loop ran is kept
  (`clear_fault` clears the fault only). A runtime that closes waits 10 seconds for its writer and then retires it: it starts
  no read and commits no plan from then on, and waits at most `RETIRE_WAIT_SECONDS` (10) for the read in flight, which, if it
  is stuck past that, commits nothing when it returns, so a writer of the same agent in a new runtime is never one of two
  and a close never hangs on a read.
- **Order of the records.** The main record is read first in every transaction and never waits; only the sub-agent records
  rotate (every transaction reads at least one of them), so a sub-agent's output is not read ahead of the link in the
  main record while the writer is catching up. The sub-agent folder's listing is not trusted when it was made in the same
  clock tick as the folder's last change (the modification time cannot tell a second file of that tick), and the
  search for the session's file in other project directories is counted per session and made at once by the last read
  before a close.
