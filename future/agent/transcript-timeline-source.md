# Cutting an external agent's timeline over to the CLI's own transcript

Status: design for review, written 2026-11-12. Nothing described here runs yet. The decision is
[the transcript is the timeline source](../../docs/decisions/agents/graph/2026-11-12-transcript-is-the-timeline-source.md);
the vocabulary, the projection, the three parsers and their golden tests (the shared pure core this plan
builds on) are in
[reading a record into timeline drafts](../../base/lm/harness/docs/transcript-projection.md) and landed
without changing behaviour. This document is the plan for Claude Code, the first CLI to cut over; Codex and
Antigravity follow the same shape and are summarized at the end.

## 1. What changes, in one paragraph

Today the graph invocation subscribes to adapter events, a stateful `HarnessTurnProjector` rebuilds items
from them, and the inbound row is settled by the live echo. After the cutover, a **session-scoped writer**
reads the CLI's transcript entries (pushed by the SDK's `session_store` callback, read from the local file
after a crash), runs them through the shared `Projector`, and in one transaction per batch writes keyed
timeline items, settles the inbound rows whose input the batch proves, and stores the read position. The
event stream stays, as a signal: a change happened, a turn started or ended, something failed, background
work is alive. The graph invocation still delivers the inbox and ends a turn, but it no longer produces or
settles anything from content.

## 2. Reading (Claude Code)

### 2.1 `session_store` as the entry path

`ClaudeAgentOptions.session_store` with `session_store_flush="eager"` makes the CLI push every transcript row
it writes, in write order, to `SessionStore.append(key, entries)` as a `transcript_mirror` frame; the main
transcript has no `subpath`, a sub-agent's is `subagents/agent-<id>`. The SDK batcher coalesces frames per file
in enqueue order and serializes appends, so one append callback sees one record's rows in order. The recorded
latency equals the file's (the callback and the file observation were within about 10 ms of each other). The SDK flushes the
batcher before it yields a `ResultMessage` ("consumers observing the result can rely on the store being up to
date for this turn"; `claude_agent_sdk/_internal/query.py`), but the CLI can emit the result before it writes
the turn's last row (recorded: the last assistant row lands from 0 to 11 ms after the result), so the flush
guarantee alone is not a fence (section 6).

The adapter's `session_store` implementation is a thin shell in `base/lm/harness/claude/` (new
`transcript_store.py`, an effect shell): `append` parses each row with `parse_row` (carrying the per-record
`ParseState` it owns), builds an `EntryBatch(record, entries, position)` and hands it to the writer's
`EntrySink.accept`, which returns when the batch is queued. `append` must not raise for a row the parser
rejects (anomalies are entries) and must raise, so the SDK retries, if the writer cannot take the batch.
`record` is `main` or the agent id read from the key's `subpath` (the rows carry `agentId` as well; the parser
uses the row).

### 2.2 `load` and resume

`load` is required by the protocol and the SDK calls it on resume: when it returns entries the SDK
materializes them into a **temporary `CLAUDE_CONFIG_DIR`**, copies the account's auth files there and resumes
the CLI from that copy (`_internal/session_resume.py`); when it returns `None` the SDK falls through to the
normal resume from the CLI's own `~/.claude` file. The plan implements `load` to **always return `None`**:
the local transcript stays the resume source exactly as today, nothing is copied, no credential file is
duplicated into a temp directory, the CLI keeps writing to the file the recovery path reads, and fork
(`fork_session` plus a caller-chosen `session_id`, [fork](../../base/lm/harness/docs/fork.md)) keeps working
unchanged. `list_sessions` / `list_subkeys` / `delete` stay unimplemented (optional in the protocol). The
store is write-only for us. The SDK refuses `session_store` together with `enable_file_checkpointing`; Meiva
does not enable file checkpointing.

### 2.3 Sub-agents

A sub-agent's rows arrive under its own `subpath` with `isSidechain` and `agentId`. The link to the call that
started it is on the **parent's** `tool_result` row (`toolUseResult.agentId`), which for a synchronous
sub-agent is written after all of the child's rows (recorded: child rows from 57076 to 60372, the parent's
result at 60419, in ms of the session). The projection holds child drafts until the link arrives
and releases them nested under the call, in whichever order the two records arrive (tested in all three
orders). A background sub-agent's rows continue after the parent's turn ended and after the CLI started
other turns; they are only more entries. `agent_metadata` entries (the SDK's mirror of the sidecar
`meta.json`, with `toolUseId`) were not part of any recording because none of the recorded mirror sessions
had a sub-agent; the plan does not depend on them (open question 7).

### 2.4 Recovery from the local file

The writer stores, per `(agent, session, record)`, the **last projected row's uuid** as the position.
`CatchUp.read_after(record, position)` for Claude scans the record's local JSONL (path as
`reconciliation/transcript.py` finds it today, `projects/*/<session>.jsonl`, sub-agents under
`<session>/subagents/`), locates the row with that uuid and returns the rows after it; a trailing line
without a newline is not consumed. A position that is not in the file (the file was replaced) is a fault, not a
restart from zero. Catch-up runs when a session starts or resumes (before the first push is accepted), after a
`mirror_error` system message (the SDK drops a batch after three failed appends and says so), and once more
when a turn's end cannot be proven (section 6). Both paths feed the same projector, so an overlap is
harmless: a key already projected gives nothing. Rows the CLI never wrote (the message being generated when
the process was killed) cannot be recovered; that is accepted.

## 3. The writer

### 3.1 Where it lives and how long

New package `agent/harness_timeline/` (effect shell plus pure planning; `agent` is the lowest layer that
holds the database and the incarnation types). One writer per `AgentRuntime`, created and closed with the
runtime's harness bridge (`services/agent_runner/agent_host/runtime.py`, `harness_bridge_for`; the bridge is
already closed on eviction, `drop_agent` and host shutdown, and a faulted session retires itself). It is not
owned by a graph invocation: it runs while the session exists, so output after a parent turn, a turn the CLI
starts by itself and a sub-agent's late output are written with no invocation present. It is one asyncio
task consuming an ordered queue of `EntryBatch`es, with one projector for the session.

Split by the repository's placement rules: `plan.py` (strict, pure) turns a `Projection` plus the writer's
state into a `WritePlan` (rows to insert with their keys and sort keys, inbound rows to settle, the cursor to
store, usage to log, notifications to publish); `writer.py` (shell) owns the queue, the projector, the
transaction and the publish step; the SQL stays in `base/agents/history/harness_messages.py`.

### 3.2 The transaction and its protection

Per batch, one `async_write_transaction` on the ops pool:

1. `lock_inbound_owner(conn, agent_id, incarnation=ctx.original_incarnation)` (`agent/ownership/inbound.py`):
   locks `agents_meta` and refuses a writer whose runtime generation and owner are no longer the row's, the
   same fence `commit_handed` and `aupsert_harness_timeline_item` use (`StaleIncarnationError`,
   `RuntimeOwnershipLostError`). A stale writer writes nothing and stops itself; the host re-creates it for the
   new incarnation.
2. Insert each draft with `ON CONFLICT (agent_id, source_key) DO NOTHING`, assigning `seq` after the current
   maximum (the `h<seq>.0` item id is the frontend's position key and stays flat).
3. For each `Delivery`, `UPDATE inbound_messages SET status='done' ... WHERE status='claimed'`, the
   statement `commit_handed` uses; the row count is checked against the rows the sent-input table says the
   input carried. A delivery whose rows are already `done` (re-delivered after a retry) is a no-op; a delivery
   whose rows are `pending` (the row was handed back to the queue before the record proved it) is the case
   "requeued, then proven delivered": the writer settles it as done and the requeued copy is withdrawn in the
   same statement, so it is not delivered twice.
4. Upsert the read position (section 4.2).
5. After commit: publish the live timeline events (`publish_inbound` / `TimelineSnapshot`), capture
   user-facing items for delivery to the person (`capture_items(..., source_key="harness")`), log usage
   (section 8) and wake the graph's waiters (a turn closed, an input settled).

### 3.3 Failure

A database error does not drop the batch and does not crash the session: the writer keeps the batch at the
head of its queue (memory is bounded by the SDK's own 500-entry / 1 MiB batch limits times a small depth) and
retries with backoff; later batches wait behind it, so order is preserved. A persistent failure past a bound
(order of a minute) faults the session with a named error (`SessionFailed`) so the graph reports it and the
next start resumes from the stored position: nothing is lost, because the local file is the durable copy and
catch-up re-reads from the last committed position. A process crash loses only the in-memory queue; catch-up
refills it. `StaleIncarnationError` ends the writer without retry.

## 4. Storage

### 4.1 `agent_harness_messages` (expand migration)

Add, nullable, in one expand-only migration (`db/docs/migrations.md`, immutable once merged; the baseline
`db/schema.sql` is synchronized):

| Column | Meaning |
|---|---|
| `source_key TEXT` | the stable key of the entry (draft key); null for rows the graph writes itself (restart notes, the failure marker) |
| `sort_ms BIGINT`, `sort_seq INT`, `sort_sub INT` | the entry's `Order`; for an unkeyed row `sort_ms` is its `created_at` in ms and the other two are 0 |
| `UNIQUE (agent_id, source_key)` | the idempotency constraint (partial: `WHERE source_key IS NOT NULL`) |

`seq` remains the contiguous per-agent sequence and the item id; **display order** is `(sort_ms, sort_seq,
sort_sub, seq)`, and every reader that today orders by `seq` (`load_harness_timeline_items`, the tail window,
`copy_harness_timeline` for a fork) orders by it, with legacy rows given `sort_ms` = their `created_at` by the
migration's backfill (a bounded `UPDATE` in the same expand step, or computed in the reader: open
question 3). The runner role needs `INSERT` on the new columns (the existing grant covers the table) and
`UPDATE` stays on `item` only. The `UPDATE (item)` revision path (`aupsert_harness_timeline_item`) is not used
by Claude's writer; it is deleted when the last CLI is cut over (contract step, a later migration only if a
column is dropped).

### 4.2 Positions

New table `agent_harness_transcript_positions(agent_id, session_id, record, position TEXT, cli_version TEXT,
updated_at, PRIMARY KEY (agent_id, session_id, record))`, written in the same transaction as the items. The
`cli_version` (read from the row's `version`, `row_version`) is what telemetry reports per session. A fork's
clone and a resumed session under the same session id keep their positions; a new session id starts a new
position and, per decision point 7, **an agent that already has a timeline at cutover gets its position set to
the end of its current record** by the first writer start (a one-time catch-up that projects nothing).

### 4.3 Ordering

A Claude record is ordered by arrival, except where a sub-agent's rows interleave with the parent's by time;
the writer therefore always writes with the entry's `Order`. For Codex a late command result sorts back by
`startedAtMs`, so the column design has to be right once, in this PR, rather than retrofitted.

## 5. What the graph keeps, deletes and changes

Line counts are orders of magnitude from the files at the time of writing.

| File (lines) | Keeps | Deletes | Changes |
|---|---|---|---|
| `agent/graph/claim/harness_turn.py` (797) | node, `_deliver_until_quiet`, `_deliver_ready`, `_reserve`/`_give_back`/`_take_back`, `_render`, backoff, correction input, wake (~450) | the content branches of `_on_event` and `_streamed` bookkeeping, the `Commit` branch of `_settle`, `_adopt_claimed`, `retire_unconfirmed`, `_reconcile_after_failure`, `_apply`, `_recheck_kept`, `_queue_resume_note` (~250) | `_nothing_left` (waits on the writer's turn state and inputs settled, section 6), `_on_turn_completed` (signal only, ~100) |
| `agent/graph/claim/harness_foreground.py` (315) | `Handed`, `merge_handed`, `plan_wake`, ordering helpers (~170) | `Reconciled`, `plan_reconciliation` (~60) | `Foreground.on_event` settles on the writer's `Delivery`, keeps `Requeue` for a refusal or loss signal (~70) |
| `agent/graph/claim/harness_inbox.py` (501) | `reserve_head_batch`, `peek_head_batch`, `claimed_deliverable`, `requeue_handed`, `mark_input_group`, `recovery_notes_first`, `is_passive` (~250) | `commit_reconciled`, `reconcile_handed`, `mark_unconfirmed`, `apply_reconciled`, `defer_reconciliation`, `unconfirmed_input_items`, `write_interrupt_notes` and `TIMELINE_WRITTEN_KEY` (~200) | `commit_handed` moves into the writer's transaction (~30) |
| `agent/graph/claim/harness_activity.py` (477) | `log_turn_usage`, `_resolve_pricing_model` (~60) | `HarnessTurnProjector` and its constants (~370) | none |
| `harness_interrupt.py` (171), `harness_render.py` (177), `harness_tool_format.py` (75) | all | none | `announce_interrupt` stops writing the note (~-20) |
| `base/agents/history/harness_messages.py` (474) | loaders, copy, `aget_harness_call_item_id` (nesting lookup by `tool_use_id`) | `aupsert_harness_timeline_item` revision path (~60, after all CLIs) | add keyed insert (~80), ordering by sort key |
| `base/lm/harness/claude/` | `session.py` delivery, interrupt, background work, signals | `mapping.py` content mapping (`_assistant`, `_user`, `_stream`, ~120), the echo handling of `inbound.py` (`_handle_echo`, `_absorb`, `_open_input_turn`, ~150), `reconciliation/` (~70) | `inbound.py` keeps turn start/end signals; `options.py` adds the store (not in this PR) |
| `base/lm/harness/delivery/reconcile.py` | read by the other two CLIs | deleted with the last CLI | none |

`HarnessBridge.deliver()` and the input key / uuid scheme (`key_uuid`) are unchanged; the writer's sent-input
table is filled from `Handed` (key, `key_uuid(key)`, rendered text, and the inbound rows each input carried)
at `deliver()` time.

## 6. When a graph invocation may end

An invocation ends only when no input is handed over and unproven, no turn is running, and **the writer has
caught up with every closed turn**. Per Claude turn:

- *Start*: the stream's turn start (a human echo or a CLI-started turn) opens it; the writer's
  `TurnObserved` confirms from the record.
- *End signal*: the `ResultMessage` (`TurnCompleted`).
- *Proof of the end*: the writer has projected a **closing entry** for the turn: `TurnEnd` (the last
  assistant row, `stop_reason` end_turn / max_tokens / refusal) or `Interrupt` (`[Request interrupted by user`).
  There is no turn-end row in the transcript and the result frame can precede the last row by up to ~10 ms, so
  the result alone never ends the turn.
- *Fail-closed*: if the proof is not there within `TRANSCRIPT_CATCH_UP_SECONDS` of the result (a constant, 5 s
  to start), the writer runs one catch-up read of the local file; if there is still no closing entry the turn
  is reported as a fault (`TurnFaulted("the transcript did not reach the end of the turn")`) and the session is
  retired like any session fault. It never ends quietly.
- *Pairing a result with a turn*: results and closing entries are matched in order per session (the n-th
  non-sidechain result with the n-th closed turn); a result with `num_turns` 0 (a `/compact` turn) is closed by
  its `Compaction` entry (open question 1).
- *Integrity cross-check (telemetry, not a gate)*: the tally of the turn's message usage equals the result's
  usage for success turns (verified on the recordings, section 11); a mismatch logs
  `harness_transcript_usage_mismatch` and is not a fault (interrupted messages carry a placeholder count).

`last-prompt` (a bookkeeping row with `leafUuid`) was examined as an exact fence and rejected: its leaf is
the last assistant row only on some turns (multi-block input turn: never; interrupted turn: the interrupt
row), so it cannot be a proof.

## 7. Inputs: rejected, lost, crashed

One rule: an input is delivered when the record shows it; the entry that shows it settles it.

| Case | Path after the cutover |
|---|---|
| Normal / absorbed mid-turn | the human row with our uuid, or the `queued_command` attachment with `source_uuid` = our uuid: `Delivery` (absorbed = `joined_running_turn`), rows `done` and one inbound item per original row, same transaction |
| Refused (`InputRejected`) or the session could not take it | unchanged: requeue (`requeue_handed`) with backoff; never settled by the record |
| Handed over, process killed before the CLI wrote it | not in the record: the row stays `claimed`; at the next session start the writer's catch-up runs first, then every still-`claimed` row is judged by the projector's `undelivered()` (absent from the record = not delivered) and returned to `pending` for re-delivery, in order. This replaces `plan_reconciliation` and `claude/reconciliation/judge.py` (the judge's two row shapes are exactly the two `HumanInput` forms the parser reads) |
| Written by the CLI, process killed before the writer saw it | catch-up reads it, `Delivery` settles it, and the recovery input ("the turn was interrupted, continue") is queued as today (`reconciled-resume-<id>`) because the turn it was in died |
| A steer lost to an interrupt | never in the record: stays `claimed`, then requeued; it is delivered again as the next input |
| A steer that was not absorbed | the CLI runs it as its own turn about a second later: a human row with our uuid, not joined; same `Delivery`; there is no recording of this form with the new CLI (open question 6) |
| An input the record cannot place | `unmatched_inputs`: shown as an inbound item with source `unmatched`, an event is logged; it never blocks |

## 8. Where the rest goes

- **Reply visibility and delivery to the person.** The projection emits `agent_chat` drafts with the raw
  text and the turn's origin (`TurnObserved.origin`). The writer applies `resolve_agent_reply(text,
  non_human_wake=<origin is not input>, explicit_visibility, delivery_mode)` to produce `payload` and
  `user_facing`, and calls the existing `capture_items` after commit. `resolve_agent_reply` lives in
  `base/agents` and `base/lm` may not import it (agents already reach lm; a lm to agents edge closes a cycle),
  so this is a deviation from "projection includes visibility", kept to a single call site. The correction
  input for an invalid visibility prefix (`harness_reply_correction`, ~40 lines) stays in the graph, fed by the
  writer's per-turn flag instead of the projector's.
- **Usage.** Logged by the writer when a turn closes, one `llm_usage` row per distinct `message.id`
  (`UsageTally`, the `steps` shape `log_turn_usage` already prices), excluding unreliable (interrupted) messages.
  The recorded `ResultMessage.usage` equals the main record's tally for every success turn
  (`test_usage_sums_once_per_message_and_equals_the_result_message`) and **excludes sub-agent usage**
  (turn 1 of the sub-agent recording: 858 output tokens = main only, the two sub-agents add 252 and 217): the
  record gives the sub-agent numbers separately (`usage_by_record`), which the stream never did (open
  question 9). `persist_last_active` and `LLMDone` follow the writer's turn-closed signal.
- **Background work keep-alive and reaping.** Signal-side, untouched: `background_work`, task started/updated,
  and the "task notification answered by a CLI turn, else delivered" path stay in `session.py`. The
  notification's *display* is the record's `Notice` row.
- **Interrupts.** The CLI's `[Request interrupted by user...]` row projects to a `system_marker`
  ("status:Turn interrupted") and closes the turn; it is written when the CLI writes it. #8911 writes the same
  fact earlier (the stop's passive note as an external timeline item at confirmation) and delivers the note
  with the next input. To avoid a double marker the plan removes the early timeline write
  (`write_interrupt_notes`, `TIMELINE_WRITTEN_KEY`, `announce_interrupt`'s write): the note stays an inbound
  for the model, its timeline entry is written when it is delivered (`timeline_items_of` as before), and the
  record's marker is the only "interrupted" fact on the timeline (open question 5).
- **Compaction.** The record's `compact_boundary` is the timeline marker ("status:Context compacted"). The
  `meiva_instructions.md` re-read reminder stays signal-owned: `ContextCompacted` from the stream makes the
  bridge prepend the reminder to the next input, as today.
- **Images.** The table of what we sent owns the image references (the inbound rows' images); the record's
  inline base64 is read only for matching and never stored.

## 9. Frontend

Items appear when the CLI completes them: there are no revisions and no pending output, so for external
agents `activity_revision` / `activity_pending` stay unset (and become dead fields once all three CLIs are
cut over; removing them from `TimelineItem` and the frontend is a final cleanup PR). A tool call is visible
from the moment the assistant message completes (the transcript writes the `tool_use` row before the tool
runs), its output when the result row is written; between them the call has no output item, so the
"Tool running" look must come from "an `agent_code` item with no output yet". Codex commands are only
visible once complete, so the view must handle that anyway. The ordering change (section 4.3) means a row
can be inserted before rows already shown (a Codex late command, a Claude sub-agent row interleaved with the
parent's); the live timeline event for such an insert carries a position and the frontend re-snapshots
instead of appending (open question 3). `subagent_text`, `agent_code` and `code_output` nesting
(`parent_item_id`, `tool_use_id`) is unchanged: the writer resolves `parent_item_id` from the draft's
`parent_tool_use_id` through `aget_harness_call_item_id`, as the projector's `find_call` does today.

## 10. Verification

1. **Golden tests (landed with the shared layer).** Sanitized recordings of CLI 2.1.322 for basic, multiblock,
   steer, interrupt, kill, resume, subagents (sync and background), task notifications and CLI-started turns,
   compact, denied and fork, asserting draft kinds, order, ownership, keys, delivery conclusions, nesting in all
   arrival orders, and usage equal to the recorded `ResultMessage`.
2. **Writer tests (PR 2)** against a throwaway Postgres (`db_conn`): idempotent re-delivery of a batch, the
   unique key, order of rows inserted out of arrival order, the stale-incarnation refusal, crash between
   commit and publish (publish is repeatable from the stored position), retry after a database failure
   without reordering, settle-and-withdraw of a requeued row, catch-up equals push.
3. **Real CLI end to end** on a throwaway cluster, per `docs/conventions/dev-setup.md` ("Which home a worktree
   reads" and the preview rule): a fresh `MEIVA_HOME` from `mktemp -d` with `MEIVA_CONFIG_FETCH=skip`, the
   checkout's own `.venv`, its own Postgres/Redis data plane, gateway and (if the UI is checked) frontend;
   never a production home, database or gateway. It needs the machine's logged-in `claude` CLI (the cluster's
   Claude auth is the user's own, so the run is attended), one external Claude agent, and the scenarios: plain
   turn with a tool, a steer during a long command, a stop, a sub-agent plus a background command whose
   notification starts a CLI turn, `/compact`, and a `kill -9` of the CLI between two messages followed by a
   resume. Checks: the timeline equals the golden projection of that session's transcript, every sent input is
   settled exactly once, nothing is duplicated after the restart, the unknown-type event count is zero.
4. **Gaps to record before PR 2** (not recorded in the study): a steer that is not absorbed and starts its own
   turn; a live `session_store` mirror of a session with a sub-agent (the `agent_metadata` entry); an API error
   turn; a model/CLI upgrade (a second CLI version).

## 11. Pull requests, rollback, deployment

| PR | Content | Rollback |
|---|---|---|
| 1 (landed with this plan) | shared vocabulary, projection, ports; Claude, Codex and Antigravity parsers; golden tests; decision and plan. No behaviour change | revert |
| 2 | expand migration (columns, positions table, grants, schema.sql) and the keyed insert / ordering readers; no writer yet | the columns are nullable and unused; revert the code, leave the columns |
| 3 | the writer (effect shell + plan), the Claude `session_store`, the `append`/`load`/catch-up shell and `options.py` wiring behind an agent-level setting off by default; golden + DB tests | setting off |
| 4 | Claude cutover: the setting on for Claude; delete `HarnessTurnProjector`, the Claude event-to-content mapping, the echo settlement and `claude/reconciliation`; graph changes of section 5; graph tests rewritten | revert the PR (the old path is in git; the table keeps the new columns); data written by the new writer is valid for the old code because `item` and `seq` are unchanged |
| 5 | Codex cutover (cursor reader on `thread/items/list`), then Antigravity (with its ACP migration) | per CLI |
| 6 | cleanup: delete `aupsert_harness_timeline_item`, `activity_*` fields, `delivery/reconcile.py` | revert |

Deployment: PR 2 needs the migration applied by `meiva start` (a gateway start applies pending migrations; a
runner refuses a database behind its code), nothing else. PR 4 changes what an external Claude agent's runtime
does, so it deploys like any runtime change: a rolling restart of the agent runtime with the setting on; each
agent starts its writer at the end of its current record, so an agent mid-turn at the switch has the rows of
that turn written by the old path up to its last event and nothing for the rest (open question 9); the
simplest safe order is to let external Claude agents be idle (or restart them with the interrupt-and-resume
path, which already stops the turn) at the moment the setting turns on. No full-cluster stop is required.

## 12. Codex and Antigravity, in short

The vocabulary and projection were exercised on both before this PR (their parsers and golden tests are in
it). **Codex**: primary reader `thread/items/list` with the history cursor as the position, rollout only for
usage, the synthetic abort and the sub-agent notification; a late command is an entry whose order is its start
time and whose turn is its original turn; a steer is a user item of the same turn; double writes
(`event_msg` / `response_item`) share one key; child threads are records linked by the spawn call. **Antigravity**:
primary reader the `transcript_full.jsonl` tail by `step_index` (holes allowed), `clientMessageId` and
`isInterruptingStep` from the trajectory RPC as an optional side input; background commands stay `RUNNING`
(`BACKGROUND`) and their end is a notice; a cancel is a signal (an `Interrupt` entry built by the reader, and
`ABORTED` results for calls no row answered); no turn-end row, so every turn end is a signal and the fence of
section 6 is the stream's `stopReason` plus a catch-up read.

## 13. Open questions

Each states what the plan assumes and the evidence it rests on or lacks.

1. **The turn-end fence.** The plan requires a closing entry. Evidence: no turn-end row exists; `last-prompt`
   is not a reliable fence (multi-block turn has none whose leaf is the end; `leafUuid` of the interrupted turn is
   the interrupt row); the result frame can precede the last row by up to 11 ms (study, 11 turns). Not covered:
   a `/compact` turn (`num_turns` 0, no assistant row; the plan closes it by the compaction entry), a turn that
   ends with an API error (no recording), and a turn where the CLI dies (no row at all: the fail-closed timeout
   faults it, which may be too strict for an `error_during_execution` result that wrote nothing). Needs a ruling:
   is a fault right for "result says error and the record has no closing row", and what timeout.
2. **Mirror as the content path, or as a signal only.** The plan reads content from the mirror and the file only
   for recovery. The alternative is one path: the mirror (or the stream) only says "read the file again", and the
   writer always tails the file by position. One path is simpler to reason about and has no overlap case; it
   needs the local file to be on the runner's host (true) and handles sub-agents by listing the directory. The
   mirror's advantage (ordered, includes sub-agent subpaths, no polling) is real but not recorded under load.
   Recommendation: the mirror, unless the user prefers a single reader.
3. **Ordering by an explicit key versus insertion order.** The vocabulary needs `Order` (a late Codex result
   belongs before rows written earlier). That means a timeline row can appear in the middle, so the store, the
   live events and the frontend must not assume append-only positions for external agents; legacy rows need a
   sort key (backfill from `created_at`). For Claude alone insertion order would almost do; the cost is paid once
   in PR 2 for Codex's sake. Needs confirmation that a mid-timeline insert with a re-snapshot is acceptable.
4. **Existing timelines at cutover** start from the record's end (decision 7): rows the CLI wrote after the old
   path's last event and before the first writer start are never shown. The gap is empty for an idle agent and
   at most the unfinished turn otherwise; the plan accepts it (open question 9 on deployment).
5. **The interrupt marker.** The record's marker replaces #8911's early timeline entry; the user-visible effect
   is that the "interrupted" marker appears when the CLI writes its interrupt row (observed up to 2 s after the
   stop when a message was streaming) instead of at confirmation. If the earlier marker matters more, the
   alternative is to keep #8911's write and make the record's `Interrupt` entry not draw (the projection would
   skip the marker); needs a ruling.
6. **A steer that is not absorbed** is documented (two-acks decision) as a turn of its own, but no recording of
   the transcript form exists; the parser handles a human row not joined to a running turn, and the projection
   test is built from hand-made entries, not a recording.
7. **Sub-agent mirror metadata.** No recording of a live `session_store` mirror with a sub-agent exists. The plan
   uses the rows' own `agentId` and the parent's `toolUseResult.agentId`; if the mirror also sends
   `agent_metadata`, it is redundant. Record it before PR 3.
8. **Fencing for a writer with no invocation.** The writer uses `ctx.original_incarnation` as
   `commit_handed` does; the runtime must hand the writer its own incarnation token at creation (the graph
   context is per invocation). Needs a check that the runtime holds it for a CLI-started turn (evidence to
   gather in PR 3: the incarnation type is `RuntimeIncarnation`, taken from `agents_meta`).
9. **Usage and sub-agents.** `ResultMessage.usage` (what `llm_usage` logs today) excludes sub-agent tokens;
   the record has them. Logging them is a behaviour change in accounting; the plan logs the main record's
   tally only in PR 4 and leaves sub-agent usage unlogged until ruled.
10. **Parser tolerance and the "registered warning event".** Unknown types are reported once per type through
    the projection; the event kind (`harness_transcript_unknown_type`) and its registration site are to be
    added in PR 3; the registry location was not checked.

## 14. What I trust least in the shared abstraction

- **One total `Order` per entry is an assumption.** It holds for Claude (file order), Codex (start time plus
  ordinal) and Antigravity (step index), but it makes the store and frontend handle mid-timeline inserts, and a
  parser's choice of clock (Claude's clamped timestamp, Antigravity's one-second `created_at`) decides how
  records interleave: sub-agent rows from a child record are placed by wall clock against the parent's, and
  Antigravity's seconds-precision stamps cannot do that well.
- **"The open turn of this record" for entries with no turn** is a heuristic: it is right for Claude (rows
  carry the prompt id only on inputs) and wrong wherever a record's late rows belong to an earlier turn without
  saying so (Antigravity phantom turns).
- **The text fallback for delivery** (Antigravity ordinary turns) can confuse two identical messages and
  cannot see a message that was merged with others; the projection reports what it cannot place, it does not
  guess further.
- **Turn closing is adapter-specific evidence** (Claude derives it, Codex has it, Antigravity has none), so
  the writer's wait rule is the one place the three adapters' differences cannot be hidden by the vocabulary.
