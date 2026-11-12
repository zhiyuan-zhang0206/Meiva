# The CLI's own transcript is the source of an external agent's timeline

## Context

An external agent's timeline (`agent_harness_messages`) is built today from the adapter's event stream:
`mapping.py` / `inbound.py` turn SDK frames into `BridgeEvent`s, `HarnessTurnProjector` keeps a stateful
reconstruction of text, reasoning and tool items from those events, and the graph invocation is the only
writer, so what reaches the timeline depends on an invocation being subscribed at the moment the frame
arrives. The cost of that shape showed up as a family of defects, not a single bug: output of a turn the
CLI began itself, a sub-agent speaking after its parent's turn, an input absorbed at a tool boundary
and an inbound whose acknowledgement raced a turn end all needed their own special case; "was this
input delivered" has two rules (the live echo, and after a crash the CLI's own record,
[reconciliation](2026-11-11-harness-delivery-two-acks.md)); and three adapters each carry their own
event-to-item state machine.

Each CLI already writes a durable record of the conversation (Claude Code's transcript, Codex's thread
items and rollout, Antigravity's `transcript_full.jsonl`). A recorded study on 2026-11-12 (CLI 2.1.322,
Codex 0.166, agy 1.5.6) found that everything a timeline shows is in that record once it is complete, and
that what is not in it (a turn in flight, a cancel, an error that kills the process) is a state, not
content.

## Decision

The user ruled on 2026-11-12: the CLI's transcript is the one and only source of the timeline's content;
accept losing streaming first, in return for correctness and stability.

1. **Content comes only from the record.** An external agent's agent text, reasoning, tool calls and
   results, sub-agent activity, turns the CLI began itself, background-task notices, interrupts and
   compaction boundaries are projected from the CLI's record. Every timeline item carries a stable key
   from its record entry and is written idempotently by that key.
2. **One vocabulary, one projection.** Each adapter parses its raw record into a small shared entry
   vocabulary (human input, agent text, reasoning, tool call and result, sub-agent link, notice,
   interrupt, compaction, usage, turn end, failure) with an explicit order key and a stable key. One pure
   projection, shared by all three adapters, turns entries and the table of "what we sent" into timeline
   item drafts, delivery conclusions and usage. An adapter keeps only its parser and its reader.
3. **One delivery rule.** An input is delivered when its id (or, for a record that keeps none, its text in
   send order) appears in the record. The settlement of the inbound row and the timeline entries of the
   messages it carried are one transaction, written by the entry that proves it.
4. **One writer per session, one read path.** The writer follows the session, not a graph invocation, and reads
   each record **by stored position** and nothing else; a crash recovery is the same call as every other read.
   The timeline is appended in the order the writer reads, so a command that finishes late is appended when it
   finishes and a sub-agent's rows when they are read; entries carry only their order inside their own record.
5. **The event stream is a signal.** It wakes the writer (with the SDK's `session_store` callback and a low
   frequency poll) and says a turn started or ended, something failed, and what background work is alive. It
   supplies no content. The end of a turn does not depend on recognising a particular last row: the graph waits
   until the writer has seen the turn's closing entry or the record has been quiet briefly, and a row that comes
   later is written later, never lost. A missing closing entry is a registered warning, not a fault.
6. **Streaming is out for now.** An item appears when the CLI finishes it. A live view of the turn in
   flight, if wanted later, is a separate, non-durable layer and is never the source of a record.
7. **Cut over per CLI**, in the order Claude Code, Codex, Antigravity. A CLI keeps its event-stream
   projection until its own cutover and loses it in the same change as the cutover; there is no
   compatibility layer. A conversation that already has a timeline starts projecting from the end of the
   record at the cutover and never replays its history.

## Alternatives rejected

- **Keep rebuilding from the event stream and fix each defect.** It is what produced the family above; each
  fix added a special case to a state machine that three adapters implement differently, and the stream
  does not contain what a CLI does by itself.
- **Write from both and cross-check.** Two writers of one timeline need a rule for every disagreement
  (which wins, which is a duplicate); the study found disagreements by design (a half-streamed message the
  stream shows and the record never has, an echo two seconds ahead of the record). It doubles the code
  that has to be right and makes neither source authoritative.
- **Push content from the SDK's `session_store` callback and re-read the file only after a crash.** Two paths
  that overlap need de-duplication, and the recovery path would run only in an accident. One pull path
  with the callback as a wake-up is the same shape for all three CLIs.
- **Tail the files with no stream at all.** The stream is the prompt signal for a change, a turn boundary and a
  failure (Codex and Antigravity records have no turn-end for a cancel, or none at all), so it stays as a
  signal.
- **A global sort key with mid-timeline inserts.** It would place a late Codex command and sub-agent rows by
  logical time, at the price of re-snapshotting the live timeline and backfilling old rows. The timeline
  keeps today's meaning, append in arrival order.
- **Fail a turn whose last row is not recognised.** A CLI upgrade that renames a stop reason would fail every
  turn; the writer outlives the invocation, so a late row is only late.
- **One abstraction grown out of Claude's shape.** Claude's record is ordered by arrival and has no turn
  boundary; Codex's rows are in completion order (a late command result belongs to an earlier turn) and
  Antigravity's human messages carry no id. A vocabulary that assumes any of that breaks on the next CLI,
  so the vocabulary and the projection were checked against all three recordings before any CLI was cut
  over.

## Consequences

- No in-flight display: a long reply or command shows when it completes. Content is "as completed".
- A killed CLI process loses the message it was generating (the three CLIs write a message when it
  completes). The record is read to its end after a restart so nothing written is missed, and nothing is
  invented for what was not.
- The timeline depends on three unpublished formats. Mitigation: parsers are strict about the fields the
  projection needs and tolerant of unknown entry types (skipped, reported once per type); the CLI version
  is recorded in telemetry; golden tests run on sanitized real records labelled with the CLI version.
- The rule "the entry proves it" replaces the live echo as the delivery signal. The echo's earlier timing
  (Claude's echo comes after the request starts, the transcript row about two seconds before) is not lost:
  the transcript is earlier.
- The timeline store needs a stable-key column with a partial unique constraint and a table of read
  positions; nothing about ordering changes.
- The design of the cutover is in [the plan](../../../../future/agent/transcript-timeline-source.md); the
  shared layer is [the projection](../../../../base/lm/harness/docs/transcript-projection.md).

The event-to-item projection of [External harness adapters](2026-10-19-external-harness.md) and the
echo-as-acknowledgement of [two acknowledgements](2026-11-11-harness-delivery-two-acks.md) are superseded
by this record as each CLI is cut over.
