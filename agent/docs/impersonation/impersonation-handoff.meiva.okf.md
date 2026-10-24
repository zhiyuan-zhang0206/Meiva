---
type: doc
title: Impersonation checkpoint handoff
description: Durable return notes, pending-input receipts, and event coverage after a takeover.
tags: [agent-lifecycle]
---

# Impersonation checkpoint handoff

`agent/impersonation_handoff.py` saves one JSON file under the agent workspace
(`TakeoverHost.workspace(agent_id)`), then appends the impersonator summary and path
as the first new system note.
The introduction, takeover, and closing notes include their creation time in
the agent-visible text when message timestamps are enabled, using the same
instant as their timeline metadata. Existing receipts retain their original
notes and timestamps across retries. The process owner supplies a `TimestampSource`
from `base.agents.messages.timestamp_policy`, the clock and the timestamp switch, and mints a
`TimestampPolicy` (the switch, the timezone, the weekday flag and one instant) for each
operation. The shell turns it once per note-writing operation into a `NoteStamp` (the
creation instant and the display prefix) and `agent/takeover/notes.py` builds the
note drafts as values, which the shell renders into messages. Constructing the inputs
reads neither input; a write that finds its receipt already in the checkpoint reads
neither. Native graph notes read the clock and flag from their explicit agent context;
invocation settlement carries the same narrow inputs from the host owner.
The stable note id and `impersonation_handoff_id` channel survive retries.
Checkpoint flush is unconditional before setting `handoff_applied_at`; only
that receipt consumes captured pending input and opens the normal claim gate.
The file retains incoming ACK state and every recorded body. Input arriving
after release remains queued behind the note. The note is the resumed input:
while it is the newest message, claim runs its first turn even with an
otherwise empty queue and no conversation yet, and delivery ends by publishing
a wake so a turn ending first still resumes the agent. File or checkpoint
errors retain the native gate. The host preserves this fresh input when its
first invocation sees the matching trailing note and `halted=False` in the
committed checkpoint. A completed reply, an explicit halt, or an open circuit
still prevents automatic continuation on later wakes.
An unavailable event backend leaves accounting explicitly
pending without blocking the control handoff; the registered agent-host event
reconciler supplements the same file after late events become readable. The
resume note identifies ordinary assistant text in the current conversation as
the reply channel, then asks the native agent to review incoming requests and continue work
whose completion is not established, regardless of message receipt status. It
warns that missing activity entries do not prove an action never happened. In the
record, `event_delivery` describes coverage: pending coverage is unknown, so zero
consumed events is not a zero-call claim. The upstream
manifest's `complete_emitted_events` coverage is only a census of emitted
events; SDK sampling policy remains unknown, so even a completed zero SDK count
is not a zero-call claim.
