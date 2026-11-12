# Cutting an external agent's timeline over to the CLI's own transcript

Status: plan, revised 2026-11-12 after review. PR A (storage, read-in) is merged; PR B (the writer for Claude, wired
behind the off-by-default agent setting `harness_transcript_timeline`) is open and not yet verified against a real CLI
end to end (section 9.4). The decision is
[the transcript is the timeline source](../../docs/decisions/agents/graph/2026-11-12-transcript-is-the-timeline-source.md);
the vocabulary, the projection, the three parsers and their golden tests (the shared pure core this plan builds
on) are described in [reading a record into timeline drafts](../../base/lm/harness/docs/transcript-projection.md)
and landed without changing behaviour. This document is the plan for Claude Code, the first CLI to cut over;
Codex and Antigravity follow the same shape (section 12). The user's priorities are correctness,
compatibility and stability, then completeness: every choice below takes the simpler form.

## 1. What changes, in one paragraph

Today the graph invocation subscribes to adapter events, a stateful `HarnessTurnProjector` rebuilds items from
them, and the inbound row is settled by the live echo. After the cutover a **session-scoped writer** reads the
CLI's transcript **by position**, keeps the rows the projection needs in the database (a whitelist of filtered
raw rows, section 4), runs them through the shared `Projector`, and in one transaction per read stores those
rows, appends keyed timeline items, settles the inbound rows the entries prove, and stores the new position.
The timeline is a derived, re-projectable result of the stored rows. The
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
byte offset, checks that the bytes ending there are still the last row read (the offset is for speed, the hash
for verification), and returns the complete lines after it, at most 4 MiB per call (`MAX_BATCH_BYTES`; the writer
calls again until nothing is left, so catching up on a large file is a series of bounded transactions); a trailing
line without a newline is not consumed. The position is `{start, offset, lines, sha256}` per record: the byte span
of the last row read, the SHA-256 of its bytes and the row count. The check hashes the span and parses nothing, so
it works for a last row with no `uuid` and costs no JSON decode however large the row is. A position whose bytes
are not at the offset (the file was replaced, truncated or rewritten) is a fault with the evidence in its
message; it never restarts from the beginning. A complete line that cannot be used (not JSON, not an object,
invalid UTF-8, nested too deep to decode, longer than `MAX_ROW_BYTES`, 64 MiB) is not a fault: it is skipped, the
position moves past it and an anomaly (`bad_row`, with the reason) is reported, so one bad row cannot keep every
later row from being read. NUL and lone surrogates, which `jsonb` refuses, are removed from every row before it is
parsed or stored (a rolled-back write would otherwise leave the writer on that row for good). `records()` lists the main record and the sub-agent files. Rows the CLI never wrote (the message
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
closed with the runtime's harness bridge (`services/agent_runner/agent_host/transcript/timeline.py`, built by
`build_runtime` when the setting is on; closed through `HarnessSessions._close` and the host's shutdown, each with a
last read before the CLI closes and the writer stopped after). The session id is asked for at every read (a fresh
session learns it when the CLI starts); the runtime incarnation is whatever the host last bound (`bind_turn`, at every
hosted turn): the row's generation survives from turn to turn while the same host owns it, so the idle writer keeps
committing, and a different owner's admission fences it until the new one is bound (Q2, answered in section 13). A
new writer holds its reads until the first invocation has registered the rows an earlier process left claimed
(`start_reading`), and repeats a read while it brings something, so catching up is a series of bounded transactions. It is not owned by a graph
invocation: output after a parent turn, a turn the CLI starts by itself and a sub-agent's late rows are written
with no invocation present. It is one asyncio task: wait for a wake-up, read every record after its position,
feed the projector, write, publish. `plan.py` (strict, pure) turns a `Projection` plus the writer's state into a
`WritePlan`; `writer.py` (shell) owns the loop and the transaction; the SQL is in
`agent/harness_timeline/store.py` (and the call lookup in `base/agents/history/harness_messages.py`).

**One transaction per read**, on the ops pool:
1. `lock_inbound_owner(conn, agent_id, incarnation=...)` (`agent/ownership/inbound.py`): the fence
   `commit_handed` and `aupsert_harness_timeline_item` use. A stale writer writes nothing and stops itself.
2. Append the whitelisted rows of this read to the record table (section 4), once each. Insert each draft
   `ON CONFLICT (agent_id, source_key) DO NOTHING` at the next `seq` (the `h<seq>.0` item id stays flat and
   append-only).
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

**Restart.** The projector's state (open calls, sent inputs not yet proven, sub-agent links) is never stored as a
blob. Each record's position row also holds a **rebuild ordinal**: the ordinal of the last stored row at the most
recent *quiet point* (a commit after which no call is open, no result waits for its call and no sub-agent record
is unlinked). A new writer reads back, from the database, the stored rows after that ordinal in bounded
batches, parses them with the adapter's own parser (`RecordReader.parse_stored`) and feeds them silently into a
fresh projector. The cost is the distance from the last quiet point, one turn or the span of the background work
that kept a call open, never the session; and the CLI's local file is not read for it at all, only to continue
reading. Keeping the rebuild ordinal is one extra column on a row the writer updates anyway.

## 4. Storage: stored records, derived timeline, strictly O(n)

The user's rulings (2026-11-12): the transcript's entries go into our database, only what we need, and the design
exploits that the prefix of a conversation almost never changes, so nothing is rewritten or re-read in
proportion to the conversation. The native checkpoint stores whole message state, inline images included, and
rewrites all of it on every save; reads and writes grew with the square of the conversation and once stalled
writes. This design must not have that shape anywhere.

### 4.1 Tables (one expand-only migration; `db/docs/migrations.md`, `schema.sql` synchronized)

| Change | Meaning |
|---|---|
| `agent_harness_messages.source_key TEXT`, partial unique `(agent_id, source_key) WHERE source_key IS NOT NULL` | the stable key of the entry a timeline row was projected from; the idempotency constraint; null for rows the graph writes itself |
| `agent_harness_messages.projection_version INT` | the version of the projection rules that produced the row (null on rows written before) |
| `agent_harness_transcript_rows(agent_id, session_id, record, ordinal, row JSONB, written_at)`, primary key `(agent_id, session_id, record, ordinal)` | the stored records: one row per whitelisted CLI row, appended once, never updated; `ordinal` is the row's own position in its record |
| `agent_harness_transcript_positions(agent_id, session_id, record, position, rebuild_ordinal, cli_version, updated_at)`, primary key `(agent_id, session_id, record)` | where each record has been read to, and the quiet point a restart rebuilds from: one small row per record, O(1) |

The timeline stays **append-only in `seq`**, one writer making it so (the meaning of today's event-stream arrival
order): a command that completes late is appended when it completes, a sub-agent's row when it is read, a
phantom or CLI-started turn when it is read. No sort key, no mid-timeline insert, no backfill. An agent that
already has a timeline at the cutover has its position set to the end of its current record by the first
writer start (a read that stores and projects nothing).

### 4.2 What is stored: filtered raw rows, by whitelist

**Whitelist, not blacklist.** Only the row types and fields the projection consumes are stored. For Claude Code:
`user`, `assistant`, `system/compact_boundary` and `attachment/queued_command`, with the fields `uuid`,
`timestamp`, `type`, `isSidechain`, `agentId`, `isMeta`, `isCompactSummary`, `isApiErrorMessage`,
`isAbortedMidStream`, `promptId`, `origin`, `toolUseResult.agentId`/`isAsync`/`backgroundTaskId`,
`toolDenialKind`, `thinkingDurationMs`, `error`, `apiErrorStatus`, `subtype`, `attachment` (queued command), and
`message` (`id`, `model`, `stop_reason`, `usage`, `content`). Never stored: the bookkeeping rows
(`api-request`, `api-request-shape`, `api-request-blob`, `queue-operation`, `file-history-snapshot`,
`session-owner`, `cost-state`, `last-prompt`, `mode`, `tools-used`, titles), the context attachments
(`prompt_snapshot`, `skill_listing`, `environment`...), and any row carrying an account identifier or a full
request body. A row of a type the parser does not know is not stored; it is counted and reported once per type
as a registered anomaly. The fallback is the CLI's own local file (kept 30 days by default): if a new type
turns out to be needed, the whitelist changes and the record is re-read from the file.

**Raw rows, not normalized entries.** What is stored is the filtered row as the CLI wrote it, not the parser's
entries, because the test is whether a corrected parser can reproduce a correct timeline from the database
alone: entries bake in the parser's mistakes (a misread field is gone), a filtered raw row keeps every field the
projection might use as it was. The price is that the parser runs on read-back (re-projection and restart),
which is cheap.

**Large content is not duplicated.** Inline image bytes are not stored (the images we sent are already in the
inbound rows); a stored image block keeps `{sha256, bytes, media_type}` only. Every text field (a text or
thinking block, a tool result, a queued prompt, a string value of a tool input) is cut and marked with its
original length (`truncated_from_bytes`) above a cap that depends on the record: **256 KiB for the main record**
(`MAIN_TEXT_CAP_BYTES`, so the main agent's own output is not truncated, as on today's timeline) and **16 KiB for a
sub-agent record** (`SUBAGENT_TEXT_CAP_BYTES`, about twice the 8,000 characters the timeline shows of a sub-agent's
item, `SUBAGENT_ITEM_CHARS`). A tool input is bounded as a whole: over the cap it is replaced by a shallow summary
(scalars and short values kept, long strings cut, nested structures that are not short replaced by their type and
size, the original size under `meiva_truncated_from_bytes`). `error` is kept only as the string the parser reads
and `origin` only as its `kind`.

**Measured on the recordings** (CLI 2.1.322, 18 transcript files): the whitelisted rows are 2-8% of the raw file
bytes (193 KB of 5.17 MB; about 6-34 KB against 260-425 KB per recorded session, the rest being per-session
constants such as `prompt_snapshot`, `skill_listing` and `api-request-blob`), before the `usage` objects, which
add about 0.4 KB per assistant row.

**Measured on a real heavy session** (the coordinator's own, 2026-11-12, filter as above; byte and row counts
only, run offline, nothing stored; the file was still growing during the runs):

| | files | raw rows | raw bytes | stored rows | stored bytes | stored / raw |
|---|---|---|---|---|---|---|
| main record | 1 | 16,962 | 35.7 MB | 4,085 | 7.2 MB | 20% |
| sub-agent records | 124 | 72,309 | 269.9 MB | 39,786 | 66.2 MB | 25% |

Sub-agent stored bytes by row type: user rows (tool results) 41.4 MB of 55.4 MB raw, assistant rows (text, thinking,
tool inputs) 24.0 MB of 85.7 MB, queued prompts 0.8 MB; everything else (`api-request*`, `prompt_snapshot`,
`skill_listing`, `instructions`, `deferred_tools_*`...) is dropped (about 125 MB). The 16 KiB cap truncated 251
sub-agent rows and saved 2.0 MB (3% of what the uncapped whitelist would store); the 256 KiB main cap truncated
none. So for a long session with many sub-agents the stored size is about a quarter of the raw size: the bytes
are in the many ordinary-sized rows (tool results and assistant output under the cap are stored whole), not in
a few huge ones. A cap of 8,000 instead of 16 KiB would truncate 1,280 rows and bring the sub-agent figure from
66.2 MB to 60.7 MB, so the cap is not the lever; storing less of a sub-agent record would need a different rule
(for example keeping only what the timeline shows of it), which re-projection would then also lose. 30-day
retention bounds the total; the coordinator decides whether it is acceptable.

### 4.3 The O(n) checklist (every item holds, and a test enforces the shape)

- **Storage:** each row is written once, appended; no column or blob is rewritten per update; the positions
  table is one small row per record. The projector's state is never stored whole (section 3, restart).
- **Reading in:** always incremental by position. Claude seeks to the stored byte offset and never scans from
  the start; Codex uses only the history cursor (`thread/items/list`) and never a call that returns the whole
  history (`thread/read`, `thread/resume` with turns; resume uses `excludeTurns`); Antigravity continues by
  `step_index`, and its cumulative trajectory is fetched only when an inbound id is needed and is never stored.
- **Quadratic sources in the CLI's files are kept out:** the per-request `api-request-blob` (the whole request
  every call), cumulative file-history snapshots, repeated summaries after compaction, repeated context
  attachments: none is on the whitelist.
- **Writing the timeline:** append only, no reorder, no backfill.
- **Reading the timeline:** by window, as today; storing records does not touch that query.
- **Re-projection is the one O(n) operation** and an explicit operator action, never on a regular path.
- **A CI protection:** a synthetic long session (thousands of entries) is fed in batches; the test asserts that
  total rows and bytes written grow linearly with the number of entries and that the rows and bytes read and
  written per batch do not grow with the length already stored.

### 4.4 Re-projection

The timeline rows are a derived result. Re-projecting an agent (or one session) is an explicit action: take a
new `projection_version`, replay the stored rows of that session in order through the current parser and
projector into the same `source_key` space (`ON CONFLICT DO NOTHING` keeps what exists; rows whose projection
changed are written by a purpose-built rebuild that replaces the agent's derived rows in one operation, and is
refused while the agent's writer is running). It reads the stored rows once, in order, in bounded batches.
Whether a parser fix needs it is judged by the version on the rows.

### 4.5 Retention

Stored rows are kept 30 days by default (configurable) and the timeline permanently. Expiry runs in the
existing upkeep service (`services/upkeep/events_maintenance`, the hourly pass in `daemon.py`; `ttl_reaper` reaps
shell sessions only) as a time-batched delete on `written_at` (an index on it, at most 20 batches of 5000 rows a
pass), never a table scan, the retention a constant for now (`TRANSCRIPT_ROW_RETENTION_DAYS = 30`). A row is deleted
only when it is older than the retention **and** lies at or before its record's rebuild point; the rows after the
rebuild point are never deleted however old, so a live session that was idle for longer than the retention still
rebuilds on restart. (Old rows that are protected are skipped by the index walk: they are the tail of an idle
record, one turn's worth.) The pass is isolated from the other steps of the hourly pass: a failure is logged and
retried the next hour. The delete runs as the gateway login, which the migration grants DELETE on the table. The coordinator decides if the measured sizes make retention unnecessary.

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
| `base/lm/harness/claude/` | `session.py` delivery, interrupt, background work, signals | the echo handling of `inbound.py` (~150), `reconciliation/` (~70), the content-to-item consumers of `mapping.py`'s output; **`mapping.py`'s mapping of stream deltas and tool frames to events (`_stream`, `_assistant`, `_user`, ~120) is kept for the live overlay (section 11), no longer consumed by the write path** | `inbound.py` keeps turn start/end signals; `options.py` adds the store (wake-up only) |
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
- **Usage (ruled 2026-11-12: sub-agent tokens count).** The projection reports each model message's spend once,
  when it first reads it (`Projection.usage`, `MessageUsage`: deduplicated by `message.id`, with `record`, the
  `parent_call_id` of the sub-agent record, the turn of that call, the message's own model and `reliable`).
  The writer logs one `llm_usage` event per reliable message, priced with `_resolve_pricing_model` on that
  message's model (a sub-agent may run another model than the main agent), as the same agent. A sub-agent's
  message is a separate event, never merged into the main figure: it carries a sub-agent marker (the `llm_usage`
  event is a log event with `usage_kind`, `source` and `for_agent_id`, not a table, so there is no migration;
  PR B adds the sub-agent marker as a new `usage_kind` value plus the `parent_call_id` attribute on
  `UsageFields` and checks the consumers of `usage_kind` first). Attribution rule: an entry is reported when it
  is read; a background sub-agent's message read after its parent turn was settled is reported then, labelled
  with that turn, and never back-filled into a settled turn's figure. A sub-agent message read before the link to
  its call waits for the link. Interrupted (unreliable) messages are not logged. For success turns the main
  record's tally equals the recorded `ResultMessage.usage`, which excludes sub-agent tokens (turn 1 of the
  recording: main 858, sub-agents 252 and 217; tested), so logged usage rises by exactly the sub-agent
  figure. `persist_last_active` and `LLMDone` follow the writer's turn-closed signal.
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
   reads" and the preview rule). Steps, in the checkout under test, with its own `.venv` and nothing of production:
   1. `export MEIVA_HOME="$(mktemp -d)" MEIVA_CONFIG_FETCH=skip`; `.venv/bin/meiva init`, then `.venv/bin/meiva start`
      (the home's own Postgres, Redis and gateway; never `~/.meiva`, a production database or gateway).
   2. Create one external Claude agent on this machine's logged-in `claude` (no credential file is read or copied;
      the CLI uses its own login) and turn the setting on while it is idle:
      `meiva agents restart <id> --config '{"harness_transcript_timeline": true}' -m 'timeline from transcript'`
      (a `config_overlay` pin; the restart rebuilds its runtime); check `meiva agents timeline <id>` is unchanged.
   3. Scenarios, each checked against the assertions below: (a) a plain turn with a Bash tool call; (b) a steer during
      a long command; (c) a stop; (d) a sub-agent plus a background command whose notification starts a CLI turn;
      (e) `/compact`; (f) `kill -9` of the CLI between two messages, then a message (resume); (g) a fork of the agent;
      (h) a message sent while the agent is mid-turn and an `own_turn` wake.
   4. Assertions: the timeline items all carry `source_key` and appear once and in order (`agent_harness_messages`);
      every sent input's row is `done` exactly once and no input reaches the CLI twice (the transcript holds one row per
      wire id); `agent_harness_transcript_rows` holds only whitelisted rows and their count grows with the record, not
      with the batch (the per-batch statement counts in the writer's linear test are the model); `llm_usage` has one
      event per model message, sub-agent messages as `usage_kind="agent_subagent"` with `parent_call_id`, and the main
      figure equals the result frame's usage; no `harness_timeline_*` anomaly beyond `turn_end_unseen` for (c);
      the stop shows only the record's own interrupt marker; (f) shows no duplicate item and the claimed row of an
      input the CLI never wrote is delivered again; (g) shows none of the parent's history re-projected.
   5. Roll back by setting it false and restarting the runtime; the old path resumes from the end of the timeline.

   **Run so far (2026-11-12, this machine's logged-in `claude` 2.1.322, model `haiku`, throwaway Postgres of the test
   harness, a scratch test, not a cluster).** The real bridge and `ClaudeSession`, the real `harness_turn` node, the
   writer, the effects and the database; no gateway, dispatcher or host. Results: (a) a turn with a Bash call:
   input `done`, items `inbound_chat, agent_reasoning, agent_code, code_output, agent_chat`, two `llm_usage` events,
   5 stored rows (5 KB); (b) a steer during `sleep 12`: both inputs `done`, the absorbed steer between the output and the
   final reply, no anomaly; (c) a stop: the record's `status:Turn interrupted` is the only interrupt marker, no
   `turn_end_unseen`; (f) `kill -9` of the CLI between two messages, then a message: both inputs `done` once, no
   duplicate item, the resumed session answered from the earlier turn; (d) a sub-agent and a background command: the
   sub-agent's items nest under its call, `llm_usage` gives `agent` and `agent_subagent` events, both background
   notifications are `Background task ... completed` markers, 22 stored rows, no anomaly. The first runs found two
   parser gaps, fixed here: row types the CLI writes in a long session were reported as anomalies (`ai-title`, titles,
   links, `informational`, `stop_hook_summary`, many context attachments), and a `queued_command` attachment whose own
   `origin` is `task-notification` was read as an input nobody sent. A pitfall for whoever repeats it: the CLI finds
   its login through `USER` and `LOGNAME`; a child environment without them answers "Not logged in".
   **Not run**: `/compact`, a fork of the agent, a CLI-started turn while the agent is idle (`own_turn` wake), the full
   cluster (`meiva init`/`start`) and the turn-on-while-idle rollout step; they remain for the coordinator.

## 10. Pull requests, rollback, deployment

| PR | Content | Rollback |
|---|---|---|
| 1 (#8922) | shared vocabulary, projection, read port; three parsers; golden tests; decision and plan. No behaviour change | revert |
| A | expand migration (`source_key`, partial unique, `projection_version`, record table, positions table, `schema.sql`), keyed append, whitelist filter and idempotent record-row append, the Claude position reader, the linear-growth test, retention in the upkeep sweeper, tests. No behaviour change | the columns are nullable and unused; revert the code |
| B | the writer (plan + shell) for Claude, projecting from the stored rows and rebuilding its state from the last rebuild point on restart, behind an off-by-default agent setting; wake-ups from the stream and `session_store` (`options.py`); settlement by the writer; the turn-end rule of section 6; the graph changes of section 5 effective when the setting is on; DB tests | setting off |
| later | Claude formal cutover (delete the old path and the setting), then the live overlay for Claude (section 11, its own PR), then Codex (history cursor), then Antigravity (with ACP) | per CLI |
| last | cleanup: delete `aupsert_harness_timeline_item`, `activity_*` fields, `delivery/reconcile.py` | revert |

Deployment: PR A needs the migration (`meiva start` applies it). PR B changes nothing while the setting is off.
Turning it on for an agent is a restart of that agent's runtime; each agent's writer starts at the end of its
current record, so the rows of a turn in flight at that moment are written by the old path only up to its last
event: turn it on while the agent is idle. No full-cluster stop is required.

## 11. Live overlay (optional, after cutover)

Streaming was given up for correctness (decision point 6); the user wants it back as a nice to have, on top of
this, and this section only leaves room for it so the cutover does not delete what the overlay needs. It is a
separate PR after the Claude cutover is verified. It is **not durable and not a source**: it never writes
`agent_harness_messages`, takes no part in settlement or in ending a turn, and losing or garbling it changes no
record.

**Source.** The adapter event stream's increments: text and reasoning deltas (`TextDelta`, `ReasoningDelta`),
a tool call's start and its running output. The adapters already map these (Claude: `mapping.py` `_stream`,
`_assistant`, `_user`; Codex: the delta and output-delta notifications; Antigravity: ACP message chunks after the
ACP migration), so the mapping stays in the adapters and the write path stops consuming it.

**What native does today, and the gap (checked on this tree).** Native streams from the LLM node and nothing of
it is stored: the live events (`base/events/live/projection.py`: `chat_start` / `chat_delta`, `reasoning_start` /
`reasoning_delta`, `code_start` / `code_delta`, `exec_start` / `exec_output_chunk` / `exec_output`; deltas
coalesced by `base/events/live/coalescer.py`) reach the frontend, whose fold (`ui/web/src/lib/fold/timeline.ts`)
keeps the in-flight items by id in `streamingIds`, merges them with the next `timeline_snapshot`
(`mergeSnapshotWithStreaming`, which drops a streaming id once a snapshot carries it) and removes an abandoned
generation by id on `cancelled`. Native pairs the streamed item with the committed one **by item id**, because
the item id is the position (`msg_idx.block_idx`) known before the commit. External is the opposite today: the
projector writes revisable rows (`activity_pending`, `activity_revision`) and the position `h<seq>.0` exists only
after the write. So the gap is the pairing key, not the mechanism.

**Plan: external uses native's path.** The overlay emits the same live events (`chat_start/_delta`,
`reasoning_*`, `code_*`, `exec_*`) with an overlay id, to the live channel only. The frontend keeps them in
`streamingIds` like native's. When the durable item arrives in a snapshot, the overlay item is replaced by
pairing on provider ids, not positions: the durable `TimelineItem` already has `source_message_id` and
`source_block_idx` (durable source coordinates, today unused for external items) and `tool_use_id`; the writer
fills `source_message_id` with the provider's message id (Claude: the stream's `message.id` equals the
transcript's `message.id`; the tool's `tool_use_id` is the same on both sides; Codex: the item id; Antigravity:
the ACP chunks carry no message id, so an overlay item can be paired only by "the next durable item of that kind
in this turn", which is not reliable; the overlay for Antigravity shows text and drops it when the turn's
durable items arrive). A pairing that fails is not repaired: when a turn closes or the writer has caught up
(section 6), **every remaining overlay item of that turn is cleared**; a flash is better than a ghost. A
`cancelled` or restart clears them as native does. With this, `activity_pending` / `activity_revision` have no
user and the fields can be deleted when all three CLIs have cut over.

**What each CLI can give.** Claude: character deltas (`include_partial_messages`), tool starts, no running tool
output. Codex: text deltas and running command output (`outputDelta`), exactly what its record lacks while a
command runs. Antigravity (ACP): message and thought chunks and tool call updates, no ids.

**Failure semantics.** An error in the overlay is logged and that item's overlay display is stopped; it never
reaches the writer, the settlement or the turn-end wait. Nothing the overlay holds is needed to restart.

## 12. Codex and Antigravity, in short

Same port and shape. **Codex**: reader on `thread/items/list` with the history cursor as the position; rollout
only for usage, the synthetic abort and the sub-agent notification; a late command is appended when it arrives,
carrying its original turn; a steer is a user item of the same turn; double writes share one key; child threads
are records linked by the spawn call. **Antigravity**: reader tails `transcript_full.jsonl` by `step_index`
(holes allowed), with `clientMessageId` and `isInterruptingStep` from the trajectory as an optional side input;
a background command stays `RUNNING` (`BACKGROUND`) and its end is a notice; a cancel is a signal (an
`Interrupt` entry the reader builds, and `ABORTED` results for calls no row answered); no turn-end row, so the
rule of section 6 ends a turn on the quiet period.

## 13. Open questions

Resolved by review (kept for the record): start from the end of the record at cutover; one "interrupted" marker
(the record's), #8911's early write removed; reply visibility applied by the writer; the temporary setting.

1. **Sub-agent usage**: ruled, counted (section 8). PR B states the change of the usage figure in its description.
2. **Fencing for a writer with no invocation**: answered. The incarnation of the latest admission, bound at every
   hosted turn. `admit_hosted_runtime` keeps the generation while the same host boot owns the row and a settled turn
   leaves the row `idling` with it (a lifecycle restart or terminate clears it; a different owner mints a new one), so
   the idle writer commits with the incarnation it holds and a takeover fences it. Evidence and tests:
   `agent/harness_timeline/tests/test_writer_lifecycle.py` (admission by the same owner returns the same
   incarnation; a different owner's admission refuses the old one until the new one is bound).
3. **Registered warning events**: registered in PR B (`harness_timeline_anomaly`, `_turn_end_unseen`, `_unmatched_input`,
   `_usage_mismatch`, `_input_unproven`, `_rebuild_bounded`, `_writer_retry`, `_writer_faulted`, `_fenced`,
   `_unsupported_harness`).
4. **Fork, rebuild bound, project path** (PR B): a session that starts with history is read from its first sent
   input (sub-agent files already there from their ends); the rebuild reads at most the latest 20,000 rows and says
   so; the same session id under several project directories resolves to the agent's own workspace directory, else
   the newest file, pinned for the session.
5. **Not yet recorded**: an API error that arrives mid-turn after tool use, a retried 429/529, and `Failure` on
   Codex and Antigravity (schema only).
6. **The text fallback for Antigravity delivery** and its turn boundaries remain the weakest part of the shared
   abstraction (no turn-end row, no id on ordinary turns); revisit when its raw record is re-recorded.
