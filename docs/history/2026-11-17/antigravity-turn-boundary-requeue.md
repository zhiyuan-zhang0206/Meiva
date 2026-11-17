# Antigravity turn-boundary requeue ownership

An Antigravity input rejected at the instant an old turn finishes now crosses two explicit ownership
boundaries. The foreground graph returns the input to the inbox and ends that delivery run after the old
turn becomes quiet; a later run owns the retry. The Antigravity session treats an IDLE trajectory for an
ACK-2-confirmed active ticket as the old provider turn's terminal fact, completes it from streamed output,
and retires that process before the retry can begin.

## Why

The earlier graph returned a refused row to `pending`, slept for two seconds, and reserved the same row
again inside the same `ForegroundRun`. This was safe only if the session's old active ticket cleared in
the meantime. Antigravity cleared that ticket exclusively when stdout produced `result`, while its
trajectory poll stopped after the input reached the context. If the trajectory had already become IDLE
and the result frame never arrived, every attempt saw the same finished ticket, rejected the same input,
and scheduled the same in-run retry forever.

The inbox is the durable retry owner. Making requeue end the delivery run preserves row order, keys and
exactly-once timeline projection without a second retry mechanism inside the run. Keeping synthetic wake
inputs in the graph checkpoint gives rowless recovery notes the same handoff semantics.

The trajectory is Antigravity's live authority for whether provider work is running. Once the ticket has
reached ACK 2, IDLE means the provider turn ended even if stdout's summary frame is absent. The streamed
reply remains usable; terminal result metadata and exact usage remain unknown. Retiring the old transport
prevents a late uncorrelated frame from being attributed to the next ticket.

## Alternatives declined

- Continuing to retry within one `ForegroundRun` keeps two owners for the same row and can recreate the
  loop whenever a session adapter retains stale foreground state.
- Adding another unbounded trajectory poll waits for the same missing stdout result and does not establish
  a terminal owner.
- Treating every IDLE+active observation as terminal can cut off a stdin turn before it starts. Rollover is
  therefore limited to tickets that already reached ACK 2.

The resulting rule is one delivery attempt per run after a refusal, and one active process generation per
Antigravity ticket.
