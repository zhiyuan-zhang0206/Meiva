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

## Update: IDLE is necessary but not sufficient

Review found that the ACK-2 rule above did not establish the sampling order: an IDLE reply could be
sampled before ACK 2, block in transit, and return after stdout had already acknowledged and started the
turn. ACK evidence is now captured before the trajectory request and tied to the same ticket, minimum
observed step count and transport.

A live agy 1.5.9 probe then disproved the stronger premise that ACK 2 followed by top-level IDLE always
means the turn ended. During a background `sleep 8`, the trajectory was IDLE from 8,901 ms while step 2
remained RUNNING; at 14,210 ms that same turn resumed RUNNING, and its result arrived at 16,003 ms. The
sanitized provider snapshots are preserved in
`base/lm/harness/antigravity/tests/fixtures/background_idle_trajectory.json`.

The recovery rule was narrowed again: all trajectory steps must be terminal, the snapshot must cover the
pre-request ACK count, and an unchanged second poll after 0.5 seconds must still belong to the same ticket,
ACK and transport. The earlier statements that ACK 2 plus one IDLE observation is terminal are superseded
by this measured rule; the durable inbox remains the sole owner of the rejected input's later retry.

## Update: reactive-IDLE is steerable; silent terminal inference removed

The original incident trace established that the RUNNING child was the fact that mattered: the outer
turn was waiting for either that background task or a new reactive wake. Live agy 1.5.9 probes then tested
both input paths in that exact state. A stdin write at 9,132 ms was not read until the background command
finished and the old result arrived at 28,614 ms. By contrast, `SendUserCascadeMessage` at 12,183 ms added
a DONE user-input step carrying its `clientMessageId` at 12,297 ms, while the old background step remained
RUNNING; its planner response completed at 14,621 ms and the one outer result later contained both replies.

The adapter now steers IDLE only when its active ticket still has a `RUNNING` or `GENERATING` trajectory step. The
trajectory client id supplies ACK 2 and ties that input to the same ticket; no second submission is made.
IDLE without a live step continues to reject the new input until stdout supplies the outer result.

The 0.5-second all-terminal rule above is also superseded. It was a silence heuristic rather than a
provider terminal signal and could hold between planner steps. The adapter no longer synthesizes
`TurnCompleted`, closes a process, or claims result metadata from trajectory silence. Only stdout
`result` completes the outer ticket. The foreground graph's one-attempt-per-run boundary remains: a true
rejection returns durable ownership to the inbox and ends that delivery run without the old three-second
in-run loop.
