# An external agent's timeline is read from the CLI's record when it is asked for, and stored nowhere

Supersedes decisions 4, 7 and the "store nothing" rejection of
[the CLI's own transcript is the source](2026-11-12-transcript-is-the-timeline-source.md); its
decision 1 (the record is the one source of content), 2 (one vocabulary, one projection) and 6 (no
streaming) stand.

## Context

The day after that decision, the part that kept a copy in our database proved to be the problem. A resident
writer followed the record, stored its rows, materialised timeline items into `agent_harness_messages`, kept
sub-agent output apart until its parent call was known, and rebuilt its state from stored rows after a restart.
Every review of it found the same class of defect: the stored copy disagreed with the record, or with itself
after a restart, a crash or a failed commit. Each fix added state (positions, rebuild ordinals, a hold timer, a
fence) to keep a derived copy honest.

The user ruled on 2026-11-13: store no derived state; what can be computed when asked for is not written when it
happens; start from the simplest design that is certain to work and add streaming and retention later; a CLI's
sub-agent is one long tool call; no two paths in the code, clean up completely. The built-in harness already
works this way: the gateway reads the conversation archive and projects it with a pure function.

## Decision

1. **The timeline of a Claude Code agent is a function of its record file and of the inbox.** Nothing is
   written for it: not the rows of the record, not the items, not a read position. The projection is the shared
   one (`Projector` over the parser's entries); a read runs it over a window of the main record. A harness enters
   this model by `reads_timeline_from_record` (`base/api_contracts/agent_contract.py`); Claude Code and Codex are in it, Antigravity
   follows, each CLI with its own file-to-entries reader.
2. **An item is named by the byte offset of its row: `h<offset>.<n>`.** The file only grows, so the name is stable,
   ordered and the same for a cold read and a live one, and finding a row needs no parse of what is before it.
3. **A read costs the window.** The record is read backwards from a line boundary, the look-back doubling at each
   step (so the rows parsed in all are at most twice the range covered) and capped at 16 MiB a request, until `limit` items
   are in hand (at the cap the window is what was found, `has_more` set); `has_more` is whether the window starts after the file's start. What a request reads follows
   the window's size, never the file's. A result is read together with its call and a reply with the start of its
   turn, each by at most a bounded look-back, past which a result stands alone.
4. **The record is read where it lives.** The gateway sends one `harness_timeline_read` operation to the agent's
   machine (short timeout, no retry; unreachable is a 503), where the runner takes the session id and working
   directory from its own session record and projects the window. A session with no record, or no file yet, is an
   empty timeline. The native conversation archive of an agent that was once built-in merges by `created_at` as
   before.
5. **Who said what comes from the inbox.** A person's message in the record is the text we composed; its row's
   `uuid` is derived from our input key, and the agent's inbox rows carry that key
   (`payload.harness_input_key`, or `inbound-<id>` for a row sent alone). The window is joined to them, so an
   item carries `inbound_id`, source, images and files, one item per message of a merged input; that is what
   clears the page's pending strip. A person's input no row accounts for (a context reset, a recovery note) is
   the host's own and is not shown. The match is by id and by no window of time (a message may have waited days to
   be claimed): the agent's deliverable rows are read newest first, 200 at a time, until every id of the window is matched,
   given up on (the rows read were claimed before the input's row was written, and rows are claimed in creation order, so
   it is the host's own: one batch at most) or 1000 rows are read, when the count unmatched is the event
   `harness_timeline_inputs_unmatched`. No index on `claimed_at` is needed: the scan is the `(agent_id, created_at)` index
   with a limit. A message shows what the person typed: a `/command` is not expanded and no
   envelope is added.
6. **The live view is the same projection, read incrementally.** The CLI's events say only that the record grew;
   after a short debounce the agent process reads the bytes appended since its last read (its offset, the open turn
   and the calls awaiting a result live in its memory only) and publishes a `TimelineSnapshot` whose ids equal
   what a cold read gives. A call without a result shows as running until its result is in the record or a later turn
   has begun; the same item is published again without the flag. A process that starts over begins at the start of
   the record's last turn and lets the page merge by id.
7. **Three duties leave the projector and stay as they were.** The reply to the user is delivered from the
   projected items, once each, under the key `harness:claude:<row uuid>`: the CLI's own id, so it is the same
   however often the row is read, and cannot meet a key of the earlier scheme (`harness:h<seq>.0`). A reply read from
   before this process began (the crash case) is offered again. At the switch from the old keys an agent has no
   publication under `harness:claude:` yet, and the first sink then delivers only what the record gains from its start;
   once the agent has one, everything the sink reads is offered and the key makes a delivered reply a no-op. There is no
   comparison of text, so two replies that say the same thing both arrive. Usage is
   logged from the turn result, as before. An input is settled by the CLI's acknowledgement (or a reading of the
   record after a failure), by setting its row `done`; no timeline entry is written for it.
8. **What Meiva wrote into the timeline and the record does not contain stops being shown**: restarts, lost
   turns, unconfirmed inputs, harness switches, fork markers, the handoff and system-prompt seed. Interrupts and
   compaction are in the record and show as before. A CLI sub-agent is one tool call and its result; its
   own record is not read. A fork shows nothing before its first start. The run timeline of the insights does
   not support these agents and never reads the rows an earlier scheme left; those rows are not migrated.

## Codex

Codex follows the same model; only where its record is and how its rows are read differ
(`base/lm/harness/codex/transcript/window.py`, chosen by `base/lm/harness/records.py`; everything after the entries is the
code Claude uses). What the rollout holds is in [what a Codex rollout holds](../../../../base/lm/harness/codex/docs/rollout.md).

1. **The rollout is read from disk, not through the app-server.** The cold read runs in the runner's ops process, which is not the
   agent host and holds no app-server connection; and an earlier attempt that read the history through the connection took a
   missing connection for an empty history and had an input that was already delivered sent again. A file has no lifecycle
   to mistake. The file is found by the thread id under `CODEX_HOME` (else `$HOME/.codex`) of the session's own environment.
2. **The file only grows, so the offset names an item** (observed: a recording compared row by row with the final file had no
   difference; a resume appends to the same file, a compaction appends and keeps every earlier row, a fork is a new file holding
   only its own rows). An item is `h<offset>.<n>` as for Claude.
3. **One path through the rows**: the `item_completed` rows carry the content (the same message is also written as a
   `response_item`, and is not read, so that a window's first row cannot name an item differently than a read from further back),
   and the file order is shown (rows are written in the order items complete; a message added to a running turn is
   written before the command that was still running; no case needing another order was met).
4. **The input is matched by the `clientUserMessageId`** we sent, which is the input key, so `wire_id` is the key itself and the
   inbox query is the same one, by id, with the same bounds.
5. **The reply is delivered under `harness:codex:<AgentMessage id>`**, the CLI's own id of the message (the same however often
   it is read, and not a key of the earlier scheme `harness:h<seq>.0`); the first sink after the switch does not resend what
   the record already holds, as for Claude. Usage, settlement of an input and the correction input are as before.
6. **A compressed rollout is read, any other form is reported.** The CLI compresses a rollout idle for seven days to
   `<name>.jsonl.zst` (one zstd stream of the plain bytes) and restores the plain file under its name when it resumes the thread,
   before appending (source and measurement in the rollout document), so a live reader never sees a compressed file. The cold
   read reads it when the plain file is absent: one streaming decompression per request, ids from the decompressed offsets
   (the same as the plain file's), and only the last 16 MiB before the window's end held in memory. A stream that cannot be
   decoded or a file of another form is not an empty history: the cold read returns empty and logs `harness_record_unreadable`
   with the thread and the file name, and a turn that ends with no readable file fails as one with no file does.
7. **No picture is copied into an item.** An input of ours shows the images of its inbox row (an upload address); the image
   view of a tool and any other row show no bytes, only the file's name. The inline data URLs in a row are read into
   memory (a request is bounded by the block limits) and dropped.

What is not shown for Codex, beyond what Claude loses: a command that is still running (a command is written when it finishes,
so there is no running item); a reply that was streaming when the turn was interrupted (never written); the history before a
fork (the clone's file starts at the clone); the tool kinds with no recorded sample (MCP calls, plans: skipped and reported once
by kind). The rollout is not a published interface, so a row or item kind of a newer CLI is skipped and reported once, not an
error.

## Alternatives rejected

- **Keep the writer, store the rows, project into the table** (the previous decision). Rejected above: the copy is
  the defect.
- **Store the CLI's rows and project at read time.** It keeps history when the machine is offline, at the price of
  a storage policy, a retention job and a parser fix that cannot reach what was stored wrong. Added later if wanted, as
  a layer under this one.
- **Project once and cache by file size and modification time.** A cache is derived state with an invalidation rule;
  a window is cheap enough without one.
- **Ids from a row's `uuid`.** They need a lookup from the id to a place in the file, which is state; an offset is
  the place.
- **Read the whole file in the gateway.** The record is on the agent's machine; shipping a file per page view is
  the cost the operation avoids.
- **Ask Codex for its own paged history (`thread/items/list` of the app-server) instead of reading the rollout.** It is
  the stable interface and Codex maintains its format, and what it returns was measured to be item for item what the
  rollout holds (58,728 items of 71 threads). It was not taken because a page load would then depend on a Codex process
  on the agent's machine that has to be kept running (each start opens outbound connections and writes the home's state
  files), and on Codex's own store: a thread the store does not hold reads as empty without an error. The store stays
  the reference a replay of the reader is compared against, and the path to take if a CLI stops writing the rollout;
  the handlers of the items are the same either way.
- **The Agent Client Protocol as the source.** Only Antigravity speaks it natively; for Claude Code and Codex it is an
  adapter over the same records. The protocol has no place for three things the timeline needs: a message added to a
  running turn, an id of ours on an input, and a turn the agent starts by itself.

## Consequences

- **A machine that is offline shows no history for its Claude agents** (503) until it is back; the native
  checkpoint is in the database and does not have this limit.
- **History lasts as long as the CLI keeps its record.** The CLI's own cleanup period decides how long a
  session file is kept; this decision changes no setting on any machine. Raising it is an operator step.
- A running tool call shows as running until its result or a later turn appears; the CLI writes a synthetic
  result for a call abandoned by a killed process when it resumes, so the state ends with the next turn.
- **A failed turn is shown only on the page of the moment.** The decision keeps nothing for it: the stored scheme
  wrote a failure marker into the table that stayed visible after a refresh, and that is gone. A Claude turn that fails
  (the CLI dies, an API error, a login that has lapsed, or a turn that did something with no record file to show for it,
  which names the session and where it was looked for) is raised as a fatal turn and the runloop sends the live `Error` event,
  exactly as for a built-in agent, whose failures are not stored either.
- **After an interrupt a new sink may re-publish a few earlier turns.** Seen on a preview: the first snapshot of a sink
  started after an interrupt began about four turns before the last turn's opening. The content equals a cold read, the
  page merges by id and replies are absorbed by their keys, so it is not a correctness fault; the cause is not established
  (an unattributed tail entry was a guess that the sample record does not support: its tail has turn names), and the
  sink's start (`_turn_start`) is left as it is until a real post-interrupt record shows it.
- A crash can lose the delivery (not the display) of the last reply; so can a database error while delivering, because the
  sink does not retry (its read position has moved on). The record's last turn is read again at the next invocation,
  which delivers it by key.
- A window that begins in the middle of a turn shows its replies as a person's (the turn's opening row is looked
  for within a bounded distance); an opening further back than that is the one imprecision.
- The page's `msg_count` for these agents is the file's size in bytes. The page uses it only to drop running
  items a snapshot does not mention; every snapshot carries the item published before it and every call still
  running, so a live snapshot is never read as a gap in the page's view.
- The legacy projector is left in place for Antigravity and removed by
  [the plan](../../../../future/agent/transcript-timeline-source.md) as each follows. The writer, its
  rollout setting and the stored-rows tables were removed in a following change, since nothing
  reached them any more.
