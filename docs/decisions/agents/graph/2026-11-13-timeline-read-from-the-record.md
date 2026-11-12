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
   this model by `reads_timeline_from_record` (`base/api_contracts/agent_contract.py`); Codex and Antigravity
   follow, each with its own file-to-entries reader.
2. **An item is named by the byte offset of its row: `h<offset>.<n>`.** The file only grows, so the name is stable,
   ordered and the same for a cold read and a live one, and finding a row needs no parse of what is before it.
3. **A read costs the window.** The record is read backwards from a line boundary in blocks until `limit` items
   are in hand; `has_more` is whether the window starts after the file's start. What a request reads follows
   the window's size, never the file's. A result is read together with its call and a reply with the start of its
   turn, each by at most a bounded look-back, past which a result stands alone.
4. **The record is read where it lives.** The gateway sends one `harness_timeline_read` operation to the agent's
   machine (short timeout, no retry; unreachable is a 503), where the runner takes the session id and working
   directory from its own session record and projects the window. A session with no record, or no file yet, is an
   empty timeline. The native conversation archive of an agent that was once built-in merges by `created_at` as
   before.
5. **Who said what comes from the inbox.** A person's message in the record is the text we composed; its row's
   `uuid` is derived from our input key, and the inbox rows claimed around the rows' time carry that key
   (`payload.harness_input_key`, or `inbound-<id>` for a row sent alone). The window is joined to them, so an
   item carries `inbound_id`, source, images and files, one item per message of a merged input; that is what
   clears the page's pending strip. A person's input no row accounts for (a context reset, a recovery note) is
   the host's own and is not shown. A message shows what the person typed: a `/command` is not expanded and no
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
   before this process began (the crash case) is offered again and skipped when the same text was published among
   the last 20 publications, which is how a reply the earlier scheme delivered is not delivered twice. Usage is
   logged from the turn result, as before. An input is settled by the CLI's acknowledgement (or a reading of the
   record after a failure), by setting its row `done`; no timeline entry is written for it.
8. **What Meiva wrote into the timeline and the record does not contain stops being shown**: restarts, lost
   turns, unconfirmed inputs, harness switches, fork markers, the handoff and system-prompt seed. Interrupts and
   compaction are in the record and show as before. A CLI sub-agent is one tool call and its result; its
   own record is not read. A fork shows nothing before its first start. The run timeline of the insights does
   not support these agents and never reads the rows an earlier scheme left; those rows are not migrated.

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

## Consequences

- **A machine that is offline shows no history for its Claude agents** (503) until it is back; the native
  checkpoint is in the database and does not have this limit.
- **History lasts as long as the CLI keeps its record.** The CLI's own cleanup period decides how long a
  session file is kept; this decision changes no setting on any machine. Raising it is an operator step.
- A running tool call shows as running until its result or a later turn appears; the CLI writes a synthetic
  result for a call abandoned by a killed process when it resumes, so the state ends with the next turn.
- A crash can lose the delivery (not the display) of the last reply when the record is not found again; the
  record's last turn is read again at the next start, which delivers it by key.
- A window that begins in the middle of a turn shows its replies as a person's (the turn's opening row is looked
  for within a bounded distance); an opening further back than that is the one imprecision.
- The page's `msg_count` for these agents is the file's size in bytes. The page uses it only to drop running
  items a snapshot does not mention; every snapshot carries the item published before it and every call still
  running, so a live snapshot is never read as a gap in the page's view.
- The writer, its `harness_transcript_timeline` setting, the stored-rows tables and the legacy projector are
  left in place for the other two CLIs and removed by [the plan](../../../../future/agent/transcript-timeline-source.md)
  as each follows.
