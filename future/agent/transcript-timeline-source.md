# Cutting an external agent's timeline over to the CLI's own transcript

Status: plan, revised 2026-11-12 after review. The decision is
[the transcript is the timeline source](../../docs/decisions/agents/graph/2026-11-12-transcript-is-the-timeline-source.md);
the vocabulary, the projection, the three parsers and their golden tests (the shared pure core this plan builds
on) are described in [reading a record into timeline drafts](../../base/lm/harness/docs/transcript-projection.md)
and landed without changing behaviour. This document is the plan for Claude Code, the first CLI to cut over;
Codex and Antigravity follow the same shape (section 11). The user's priorities are correctness,
compatibility and stability, then completeness: every choice below takes the simpler form.

## 1. What changes, in one paragraph

Today the graph invocation subscribes to adapter events, a stateful `HarnessTurnProjector` rebuilds items from
them, and the inbound row is settled by the live echo. After the cutover a **session-scoped writer** reads the
CLI's transcript **by position**, runs the entries through the shared `Projector`, and in one transaction per
read appends keyed timeline items, settles the inbound rows the entries prove, and stores the new position. The
event stream stays, as a signal that wakes the writer and says a turn started or ended or something failed. The
graph invocation still delivers the inbox and ends a turn, but it no longer produces or settles anything from
content.

## 2. Reading: one path, by position

Every record has one reader, and it **always** reads after the stored position; a crash recovery is the same
call as every other read, so it runs all day and not only after an accident. The port is
`RecordReader.read_after(record, position)` (`base/lm/harness/session/transcript/ports.py`): it returns the
complete entries past the position and the new position, or nothing. Nothing is pushed and nothing overlaps, so
there is no de-duplication between two paths. (The projector still ignores a key it has projected, which makes a
re-read after a failed commit harmless.)

**Wake-ups carry no content.** The writer reads when woken by any of: a stream frame or a turn-end signal; the
SDK's `session_store` callback (`append` is called after the CLI's local write, in write order; its content is
**not parsed or used**, only the fact that the record grew and its `subpath`, which names a sub-agent record
to read); a low-frequency fallback poll while the session is alive (every few seconds it asks whether the
record has complete lines past the position), which also covers a mirror batch the SDK dropped (it reports
`mirror_error` after three failed appends); and a last read before the session is closed or reclaimed.

**Claude.** The reader opens the record's local JSONL (`projects/*/<session>.jsonl`, sub-agents under
`<session>/subagents/agent-<id>.jsonl`, as `reconciliation/transcript.py` finds it today), starts at the stored
byte offset, checks that the row ending there is the stored key (the offset is for speed, the key for
verification), and returns the complete lines after it; a trailing line without a newline is not consumed. The
position is `{key: <last projected row's uuid>, offset: <bytes>}` per record. A position whose key is not at the
offset (the file was replaced or truncated) is a fault with the evidence in its message; it never restarts from
the beginning. `records()` lists the main record and the sub-agent files. Rows the CLI never wrote (the message
being generated when the process was killed) cannot be read; that is accepted.

**`load` and resume.** `session_store.load` always returns `None`. When it returns entries the SDK materializes
a **temporary `CLAUDE_CONFIG_DIR`**, copies the account's auth files there and resumes from that copy
(`claude_agent_sdk/_internal/session_resume.py`); returning `None` keeps resume on the CLI's own file exactly as
today, nothing is duplicated, and fork (`fork_session` plus a caller-chosen `session_id`,
[fork](../../base/lm/harness/docs/fork.md)) is unchanged. `list_sessions`, `list_subkeys` and `delete` stay
unimplemented. The SDK refuses a `session_store` together with `enable_file_checkpointing`; Meiva does not enable
it.

**Sub-agents** are separate records. The link to the call that started one is on the parent's `tool_result` row
(`toolUseResult.agentId`), which for a synchronous sub-agent is written after all of the child's rows; the
projection holds child drafts until the link arrives (tested for every arrival order). Their drafts are appended
as they are read and attributed by `parent_tool_use_id` / `tool_use_id`, which is how the frontend already
indents them.

## 3. The writer

**Where and how long.** New package `agent/harness_timeline/` (effect shell plus pure planning; `agent` is the
lowest layer that holds the database and the incarnation types). One writer per `AgentRuntime`, created and
closed with the runtime's harness bridge (`services/agent_runner/agent_host/runtime.py`, `harness_bridge_for`;
the bridge is already closed on eviction, `drop_agent` and host shutdown). It is not owned by a graph
invocation: output after a parent turn, a turn the CLI starts by itself and a sub-agent's late rows are written
with no invocation present. It is one asyncio task: wait for a wake-up, read every record after its position,
feed the projector, write, publish. `plan.py` (strict, pure) turns a `Projection` plus the writer's state into a
`WritePlan`; `writer.py` (shell) owns the loop and the transaction; the SQL stays in
`base/agents/history/harness_messages.py`.

**One transaction per read**, on the ops pool:
1. `lock_inbound_owner(conn, agent_id, incarnation=...)` (`agent/ownership/inbound.py`): the fence
   `commit_handed` and `aupsert_harness_timeline_item` use. A stale writer writes nothing and stops itself.
2. Insert each draft `ON CONFLICT (agent_id, source_key) DO NOTHING` at the next `seq` (the `h<seq>.0` item id
   stays flat and append-only).
3. For each `Delivery`, `UPDATE inbound_messages SET status='done' ... WHERE status='claimed'` as
   `commit_handed` does, checked against the rows the sent-input table says the input carried; a row already
   `done` is a no-op; a row that was returned to `pending` before the record proved it is settled as done and
   withdrawn in the same statement, so it is not delivered twice.
4. Upsert the position.
5. After commit: publish the live events, capture user-facing items for delivery
   (`capture_items(..., source_key="harness")`), log usage, and wake the graph's waiters.

**Failure.** A database error does not drop anything: the position did not advance, so the next read returns the
same entries; the writer retries with backoff. A failure past a bound faults the session with a named error so
the graph reports it. `StaleIncarnationError` ends the writer without retry. A process crash loses nothing the
CLI wrote, because the next read starts at the stored position.

## 4. Storage

One expand-only migration (`db/docs/migrations.md`; the baseline `db/schema.sql` is synchronized):

| Change | Meaning |
|---|---|
| `agent_harness_messages.source_key TEXT` | the stable key of the entry's draft; null for rows the graph writes itself |
| partial unique `(agent_id, source_key) WHERE source_key IS NOT NULL` | the idempotency constraint |
| table `agent_harness_transcript_positions(agent_id, session_id, record, position TEXT, cli_version TEXT, updated_at, PRIMARY KEY (agent_id, session_id, record))` | where each record has been read to; written in the transaction that writes the rows; `cli_version` is the CLI version telemetry reports |

The timeline stays **append-only in `seq`**, one writer making it so: the same meaning as today's event-stream
arrival order. A command that completes late is appended when it completes (its call and its result together,
with its original turn on the draft), a sub-agent's row when it is read, a phantom or CLI-started turn when it
is read. There is no sort key, no mid-timeline insert, no backfill. An agent that already has a timeline at the
cutover has its position set to the end of its current record by the first writer start (a catch-up that
projects nothing).

## 5. What the graph keeps, deletes and changes

Line counts are orders of magnitude from the files at the time of writing.

| File (lines) | Keeps | Deletes | Changes |
|---|---|---|---|
| `agent/graph/claim/harness_turn.py` (797) | node, `_deliver_until_quiet`, `_deliver_ready`, `_reserve`/`_give_back`/`_take_back`, `_render`, backoff, correction input, wake (~450) | the content branches of `_on_event` and `_streamed` bookkeeping, the `Commit` branch of `_settle`, `_adopt_claimed`, `retire_unconfirmed`, `_reconcile_after_failure`, `_apply`, `_recheck_kept`, `_queue_resume_note` (~250) | `_nothing_left` (section 6), `_on_turn_completed` (signal only, ~100) |
| `harness_foreground.py` (315) | `Handed`, `merge_handed`, `plan_wake`, ordering helpers (~170) | `Reconciled`, `plan_reconciliation` (~60) | `Foreground.on_event` settles on the writer's `Delivery`; keeps `Requeue` for a refusal or loss signal (~70) |
| `harness_inbox.py` (501) | `reserve_head_batch`, `peek_head_batch`, `claimed_deliverable`, `requeue_handed`, `mark_input_group`, `recovery_notes_first`, `is_passive` (~250) | `commit_reconciled`, `reconcile_handed`, `mark_unconfirmed`, `apply_reconciled`, `defer_reconciliation`, `unconfirmed_input_items`, `write_interrupt_notes`, `TIMELINE_WRITTEN_KEY` (~200) | `commit_handed` moves into the writer's transaction (~30) |
| `harness_activity.py` (477) | `log_turn_usage`, `_resolve_pricing_model` (~60) | `HarnessTurnProjector` and its constants (~370) | none |
| `harness_interrupt.py` (171), `harness_render.py` (177), `harness_tool_format.py` (75) | all | none | `announce_interrupt` stops writing the note on an external harness (~-20; the native path is untouched) |
| `base/agents/history/harness_messages.py` (474) | loaders, copy, `aget_harness_call_item_id` | `aupsert_harness_timeline_item` revision path (~60, after all CLIs) | add keyed append (~80) |
| `base/lm/harness/claude/` | `session.py` delivery, interrupt, background work, signals | `mapping.py` content mapping (~120), the echo handling of `inbound.py` (~150), `reconciliation/` (~70) | `inbound.py` keeps turn start/end signals; `options.py` adds the store (wake-up only) |
| `base/lm/harness/delivery/reconcile.py` | read by the other two CLIs | deleted with the last CLI | none |

`HarnessBridge.deliver()` and the `key_uuid` scheme are unchanged; the sent-input table is filled from `Handed`
(key, `key_uuid(key)`, rendered text, the inbound rows each input carried) at `deliver()` time. During the
rollout the writer ships **behind an agent-level setting, off by default**, so it can be verified on single
agents; the setting and the old path are deleted when all three CLIs have cut over (PR 6).

## 6. When a graph invocation may end

The writer outlives every invocation, so a row that arrives late is written late, never lost; the end of an
invocation therefore does not need to recognise a particular last row, and a CLI upgrade that changes a
`stop_reason` cannot fail every turn.

- On the turn-end signal (the `ResultMessage`), the invocation asks the writer to read now and waits until the
  writer has **seen the turn's closing entry** (`TurnClosed` from `TurnEnd` or `Interrupt`), **or** until a short
  quiet period with no new row has passed (`TRANSCRIPT_QUIET_SECONDS`, 1 s), **or** a ceiling
  (`TRANSCRIPT_END_CEILING_SECONDS`, 5 s). Rows that arrive afterwards are written as usual.
- A result that reports an error itself (`error_during_execution`, an API error, an aborted turn) ends the turn
  with the error it reported; the record is not required to hold a closing row.
- Waiting out the ceiling without a closing entry records one registered warning event (the way to notice a
  format drift); it is not a fault and does not retire the session.
- Before a session is closed or reclaimed the writer reads once more.
- A CLI that dies and never writes the rest is the process-fault path, unchanged.

`last-prompt` was examined as an exact fence and rejected: its leaf is the last assistant row on some turns only
(a multi-block input turn has none; an interrupted turn's leaf is the interrupt row).

## 7. Inputs: rejected, lost, crashed

One rule: an input is delivered when the record shows it; the entry that shows it settles it.

| Case | Path |
|---|---|
| Normal, or absorbed mid-turn | the human row with our uuid, or the `queued_command` attachment with `source_uuid` = our uuid: `Delivery` (`joined_running_turn` for the absorbed one); rows `done` and one inbound item per original row, same transaction |
| Refused or not taken by the session | unchanged: requeue with backoff; never settled by the record |
| Handed over, process killed before the CLI wrote it | not in the record: the row stays `claimed`; at the next session start the writer's first read runs, then every still-`claimed` row the projector's `undelivered()` lists is returned to `pending` for re-delivery in order. This replaces `plan_reconciliation` and `claude/reconciliation/judge.py` |
| Written by the CLI, process killed before the writer read it | the next read finds it and settles it; the recovery input ("the turn was interrupted, continue") is queued as today (`reconciled-resume-<id>`) |
| A steer lost to an interrupt | never in the record: stays `claimed`, then requeued, delivered as the next input |
| A steer not absorbed | the CLI runs it as a turn of its own: a human row with our uuid, written after the first turn's last assistant row, with a new `promptId`; not joined; same `Delivery` (recorded 2026-11-12, fixture `own_turn`) |
| An input the record cannot place | `unmatched_inputs`: shown as an inbound item with source `unmatched` and logged; never blocks |

## 8. Where the rest goes

- **Reply visibility and delivery to the person.** The projection emits `agent_chat` drafts with raw text and the
  turn's origin. The writer applies `resolve_agent_reply(text, non_human_wake=<origin is not input>,
  explicit_visibility, delivery_mode)` in one place and calls `capture_items` after commit.
  `resolve_agent_reply` is in `base/agents`; `base/lm` cannot import it without a cycle. The correction input for
  an invalid prefix (`harness_reply_correction`, ~40 lines) stays in the graph, fed by the writer's per-turn flag.
- **Usage.** Logged by the writer when a turn closes, one row per distinct `message.id`, excluding unreliable
  (interrupted) messages; for success turns the main record's tally equals the recorded `ResultMessage.usage`
  (tested). `ResultMessage.usage` excludes sub-agent tokens; the record has them (`usage_by_record`). Until the
  user rules, only the main record's tally is logged (today's behaviour).
- **Background work, keep-alive and reaping.** Signal-side and untouched. The notification's display is the
  record's `Notice` row.
- **Interrupts.** The CLI's `[Request interrupted by user...]` row projects to a marker and closes the turn: the
  only "interrupted" marker on an external timeline. The early timeline write #8911 added for an external harness
  (`write_interrupt_notes`, `TIMELINE_WRITTEN_KEY`, the write in `announce_interrupt`) is removed. The passive
  interrupt note still rides into the CLI with the next input (it says who interrupted) but no longer produces a
  timeline entry of its own on an external harness; the native path is untouched.
- **Compaction.** The record's boundary is the marker; the `meiva_instructions.md` re-read reminder stays
  signal-owned (`ContextCompacted` makes the bridge prepend it to the next input).
- **Images.** The sent-input table owns the image references; the record's inline base64 is read only to match.

## 9. Verification and recordings

1. **Golden tests (landed).** Sanitized recordings of CLI 2.1.322: basic, multiblock, steer, interrupt, kill,
   resume, subagents, task notifications and CLI-started turns, compact, denied, fork; draft kinds, ownership,
   keys, deliveries, nesting in every arrival order, usage equal to the recorded `ResultMessage`.
2. **Recordings added 2026-11-12** (CLI 2.1.322): a second input that missed the running turn and started its
   own (`own_turn`), and a turn that ends in an API error (`api_error`, a nonexistent model): the CLI writes a
   synthetic assistant row (`model` `<synthetic>`, `isApiErrorMessage`, `error`, `apiErrorStatus`, stop_sequence),
   projected as a `Failure` that fails the turn; the `ResultMessage` reports `subtype` success with `is_error`
   true and zero usage, which is why the result's own error ends the turn (section 6). (The Antigravity fixtures are CONSTRUCTED from trajectory captures, the child
   record partly synthesized; **raw `transcript_full.jsonl` must be re-recorded before the Antigravity cutover**,
   which this plan does not do now.)
3. **Writer tests** against a throwaway Postgres: idempotent re-read, the unique key, stale-incarnation
   refusal, a crash between commit and publish, retry after a database failure without reordering, a requeued
   row later proven delivered, first read after a restart.
4. **Real CLI end to end** on a throwaway cluster (`docs/conventions/dev-setup.md`, "Which home a worktree
   reads" and the preview rule): fresh `MEIVA_HOME` from `mktemp -d` with `MEIVA_CONFIG_FETCH=skip`, the
   checkout's own `.venv`, its own data plane and gateway; never a production home. It needs the machine's
   logged-in `claude`, one external Claude agent, and: a plain turn with a tool, a steer during a long command, a
   stop, a sub-agent plus a background command whose notification starts a CLI turn, `/compact`, and a `kill -9`
   between two messages followed by a resume. Done by the coordinator after PR B.

## 10. Pull requests, rollback, deployment

| PR | Content | Rollback |
|---|---|---|
| 1 (#8922) | shared vocabulary, projection, read port; three parsers; golden tests; decision and plan. No behaviour change | revert |
| A | expand migration (`source_key`, partial unique, positions table, `schema.sql`), keyed append, the Claude position reader (pure position/line handling plus a thin file shell), tests. No behaviour change | the columns are nullable and unused; revert the code |
| B | the writer (plan + shell) for Claude, behind an off-by-default agent setting; wake-ups from the stream and `session_store` (`options.py`); settlement by the writer; the turn-end rule of section 6; the graph changes of section 5 effective when the setting is on; DB tests | setting off |
| later | Claude formal cutover (delete the old path and the setting), then Codex (history cursor), then Antigravity (with ACP) | per CLI |
| last | cleanup: delete `aupsert_harness_timeline_item`, `activity_*` fields, `delivery/reconcile.py` | revert |

Deployment: PR A needs the migration (`meiva start` applies it). PR B changes nothing while the setting is off.
Turning it on for an agent is a restart of that agent's runtime; each agent's writer starts at the end of its
current record, so the rows of a turn in flight at that moment are written by the old path only up to its last
event: turn it on while the agent is idle. No full-cluster stop is required.

## 11. Codex and Antigravity, in short

Same port and shape. **Codex**: reader on `thread/items/list` with the history cursor as the position; rollout
only for usage, the synthetic abort and the sub-agent notification; a late command is appended when it arrives,
carrying its original turn; a steer is a user item of the same turn; double writes share one key; child threads
are records linked by the spawn call. **Antigravity**: reader tails `transcript_full.jsonl` by `step_index`
(holes allowed), with `clientMessageId` and `isInterruptingStep` from the trajectory as an optional side input;
a background command stays `RUNNING` (`BACKGROUND`) and its end is a notice; a cancel is a signal (an
`Interrupt` entry the reader builds, and `ABORTED` results for calls no row answered); no turn-end row, so the
rule of section 6 ends a turn on the quiet period.

## 12. Open questions

Resolved by review (kept for the record): start from the end of the record at cutover; one "interrupted" marker
(the record's), #8911's early write removed; reply visibility applied by the writer; the temporary setting.

1. **Subagent usage** (user): whether sub-agent tokens are logged. Until ruled, main record only.
2. **Fencing for a writer with no invocation**: the writer needs the runtime's incarnation token, not the graph
   context's. To be verified with evidence in PR B.
3. **Registered warning events** (unknown entry type, end ceiling reached, unmatched input): registered in PR B.
4. **Not yet recorded**: an API error that arrives mid-turn after tool use, a retried 429/529, and `Failure` on
   Codex and Antigravity (schema only).
5. **The text fallback for Antigravity delivery** and its turn boundaries remain the weakest part of the shared
   abstraction (no turn-end row, no id on ordinary turns); revisit when its raw record is re-recorded.
