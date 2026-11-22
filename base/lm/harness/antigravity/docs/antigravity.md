# Antigravity (agy) session bridge

The bridge drives one Google Antigravity CLI conversation through
[Agent Client Protocol v1](https://agentclientprotocol.com/protocol/v1/overview).
It requires `agy` 1.5.14 or later. The minimum version is the first verified
here to keep a running tool and both inputs through a mid-turn prompt.

## Conversation and delivery

`agy acp` exchanges newline-delimited JSON-RPC over stdio. The adapter sends
`initialize` and either `session/new` or `session/load`. The protocol ends history playback at the load response. The adapter queues
later frames, including while it reapplies the configured model and reasoning effort
with the combined model ID in `session/set_model`. The first prompt after a
load verifies output against the newly written input marker and planner rows.
This keeps late historical chunks outside the new prompt and allows the verified
planner counters to supply usage when its ACP result omits usage.

Each input is one `session/prompt` with ordered text and base64 image blocks.
A steer sends another prompt within the foreground turn. On 1.5.14 the previous
request can return `end_turn` immediately while its command keeps running. The
latest request governs the foreground turn boundary. Earlier replies contribute
delivery and usage evidence. A reply from another process generation or a
request outside the current turn cannot end the current turn.

A write emits a weak receipt. ACP has no durable client input ID, so the
adapter includes a leading HTML comment containing a digest of the input key
in the text. A matching user chunk or `USER_INPUT` transcript row confirms
delivery. The latest non-cancelled prompt result also confirms that prompt.
The timeline removes the comment from the displayed input. Recovery reads the
transcript with the shared JSONL reader and can still consult the private
message queue for inputs written before this migration. An input absent from
both records has an unknown outcome if a write may have started. A failed
dispatch that never reached the write hands its specific input keys back to the
inbox for requeueing. An earlier, potentially written prompt in the same batch
still goes through reconciliation.

## Interrupt and permission handling

`session/cancel` is a notification. The matching `cancelled` result confirms the
interrupt, closes outstanding foreground tool entries, and keeps the process
available for the next prompt. The adapter reports whether observed tool
processes settle. Prompt writes have a deadline, and cancel can proceed while
stdin is backpressured. A write failure or unconfirmed cancel retires the
process generation. Closing the transport aborts stdin, closes stdout and
stderr, and joins the reader tasks. The process-tree cleanup checks process
identity and covers observed descendants and members of the CLI's private
POSIX session. Broken-generation cleanup also checks same-user processes that
retain the generation's unique inherited environment marker. This identifies a child that creates its own session and
outlives the CLI. On Linux and macOS the marker scan uses psutil's process
environment reader. If a process clears its environment or the platform
denies access, process-tree and POSIX session observations remain available. A residue fence
prevents reopening while an observed process still holds the generation.
Losing the process requires a new ACP handshake and `session/load` before
another prompt.

The client rejects `session/request_permission` with the offered rejection
option, or cancels the permission request when no such option is available.
The tool's `failed` status and the permission label reach the turn report.
The explicit `skip_permissions` setting asks the CLI to auto-approve.

## Background updates and sidecar reads

The stdio reader runs for the whole session. In-flight tool results keep the
ownership of the original call even when they arrive after the prompt ends.
ACP v1 update frames carry a session ID. While a detached timer can produce
output, the adapter buffers unkeyed assistant and new tool frames until the
corresponding completed planner rows identify the owner. A Hub notice identifies
a detached task. During overlap, planner rows after the notice stay provisional
until a unique ordered foreground span matches the prompt's ACP usage receipts.
Assigning a provisional row normally requires a separate terminal planner row
for the detached reply. The session reader searches at most ten provisional rows
for an ordered subset. The completion of a shell command launched within
the same marked input before its terminal planner row is a tool result in that
input. The command's provider-generated launch row carries the task ID and step
index, and the Hub completion envelope carries the same task ID. A completion
after the input's terminal planner row or closure boundary retains its native
notice. An earlier text block cannot close a turn
while its planner span contradicts complete ACP usage receipts. Closing a
verified foreground records the transcript boundary and the accounted span.
A late timer reply requires a separate task notice after that boundary or
planner evidence excluded from the verified foreground span. A confirmed
cancellation also allows an already registered timer to deliver a separate
terminal planner row added after the cancellation boundary. A planner row
already present at cancellation cannot acquire timer ownership by reaching
DONE afterward.

A completed planner reply whose native ownership is proven can be delivered
before its ACP text arrives. Its notice row identifies the reply for delivery
within the generation. An unresolved native notice stays in the projection
across foreground settlement. When a prompt omits ACP usage, a terminal
planner row belonging to its input can still close the prompt before a later
native notice. Available planner usage stays on its own turn, and incomplete
counts are marked uncertain. A current textless terminal row can close a
prompt despite unmatched replay. Cancellation settles proved foreground
evidence immediately, preserving recorded text and usage. At the attribution
deadline, a successful ACP response with its current input marker in the
transcript can settle with only attributed foreground output. The adapter logs
`agy_acp_attribution_degraded`, flags usage as uncertain, and leaves unresolved
planner rows quarantined for native attribution. A pending notice from a
degraded input can receive the detached planner steps appended after closure
and before the next input marker. Native usage is uncertain when unresolved
earlier steps could belong to that reply. Otherwise the notice stays
provisional across markers until a verified detached terminal precedes the
marker. A new input with one terminal can confirm the unique ordered subset
that matches its ACP receipt when every provisional row comes from a carried
notice. Above the subset limit, only the whole span is tested. The notice
remains pending for a separate native reply. Missing input evidence retains
the attribution failure.

The matcher first consumes contiguous text and then joins compatible fragments
across remaining interleaved frames. Identical text for a single owner consumes
that owner's earliest unused row. After terminal ACP and a verified planner
span, the adapter can recover missing foreground text from that span. Foreground
completion is independent of unmatched timer chunks. Outside a prompt,
unkeyed text only signals that the adapter should inspect the transcript.
A new native task notice or a callback from a child named by the parent’s
subagent descriptors identifies a provider-started turn once its planner reply is complete. A transcript boundary captured before loading keeps
historical task replies outside this scan, even if old text frames arrive
after the load response. The adapter emits a harness-origin turn, with the
native notice and reply offsets and planner usage when available. Both buffered
and live starts point the graph to the exact reply row, even when the timeline
reader has already passed it. The graph presents the transcript and applies the
normal reply visibility policy.
A future timer still allows foreground input.
A due or triggered timer holds new dispatch and keeps the process alive, and
an unverified reply at its deadline retires the generation. Cron-like
calls retain the existing one-day bound. Timers also supply the foreground
watchdog's silence floor while a prompt is active. See
[background work](../../docs/background-work.md).

The local, read-only `GetCascadeTrajectory` RPC remains the source of live
background-wait state. Top-level IDLE with live background work and without a
live planner step is reactive waiting. An idle trajectory can also corroborate
a tracked schedule. The same RPC supplies native timing. Sub-agent output
continues to come from descriptors and child transcript files because ACP
1.5.14 does not stream it. See [sub-agent text](../../docs/subagent-text.md).

## Usage

The ACP result reports uncached input, cache read and write, visible output,
and thought tokens. Meiva combines the input counters and bills thought as
output. A result can omit usage on cancel or steer, while a later result may
cover several planner steps. A single prompt's usage is authoritative. For a
steered turn, the adapter checks ordered receipt spans against planner rows
bounded by its input markers and final streamed answer. A disjoint set of
receipts must cover the owned rows in sequence. A cumulative final receipt
must cover the span, and each available earlier receipt must match a proper
prefix. If a superseded request omitted usage, the terminal cumulative receipt
can still verify a unique span with planner evidence for each foreground
input. Disjoint billing requires every request receipt.
Planner rows attributed to a detached task are excluded. With incomplete
evidence, the completion carries available counters and a usage uncertainty
flag. The graph logs `harness_usage_uncertain` and skips the exact cost row.
The CLI supplies no price.

## Bounds

| Setting | Default | Purpose |
|---|---|---|
| `startup_request_seconds` | 30 s | Bounds each initialization, session setup and model selection request, which otherwise could leave the host waiting on a dead stdio peer. |
| `prompt_write_seconds` | 5 s | Bounds stdout-independent stdin backpressure for prompt delivery. |
| `background_reply_seconds` | 30 s | Covers provider generation after a detached timer fires. In the measured trace the delay was 5.65 s. |
| `cancel_settle_seconds` | 5 s | Bounds the wait for a cancel confirmation and the following wait for tracked tools, and bounds each close escalation interval. |
| `max_buffered_events` | 256 | Allows dropping deltas when readers lag while retaining control events. |
| `max_schedule_timer_seconds` | 86400 s | Caps the wait declared by any one schedule so malformed durations cannot disable stall detection indefinitely. |
| `MAX_FRAME_BYTES` | 256 MiB | Allows multi-megabyte tool results and caps memory for an unterminated frame. |

## Native reasoning measurements

The transcript omits `plannerResponse.thinkingDuration` even when the native trajectory supplies it.
`AgyTransport.trajectory()` collects only completed planner measurements into a separate atomic
`logs/meiva-thinking-timing.json` with verified offsets, identity digests and native milliseconds
without rewriting the vendor transcript or storing thought text. `AgySession` also schedules a
single-flight, throttled background trajectory refresh on ACP usage updates, turn completion,
and when `ReadTimelineSink` reads newly persisted unmeasured
`PLANNER_RESPONSE` rows during reactive waiting. Matching requires a unique index,
creation second and thought digest. Duplicate identities remain unknown with a diagnostic. Tool
status spans are not substituted for native reasoning duration: real observations show that
`thinkingDuration` differs from GENERATING-to-DONE time.

Timing is provisional until a snapshot observes the completed step. A missing, corrupt or replaced
sidecar leaves the original conversation readable and its timing unknown. When the transcript grows
by appending new rows (`stat.st_size > data.size` on the same `(device, inode)`), readers retain
previously verified measurements for offsets `< data.size` and invalidate only entries whose
identity fingerprint collides with a row in the appended tail; same-size rewrites, truncations, and
inode replacements still invalidate the sidecar. A source revision refresh publishes only reasoning
items at their original IDs; it never replays replies, inputs, tool execution or delivery. The UI
shows plain Worked while any work measurement is absent. Invalid present terminal durations and
corrupt sidecars use existing cluster warning events. Invalid duration diagnostics follow the
collector's existing native-facts and record watermark change gate; historical sidecar parse
diagnostics retain their reader revision gate. Missing or null durations remain silent.
