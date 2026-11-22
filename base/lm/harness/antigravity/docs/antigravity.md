# Antigravity (agy) session bridge

The bridge drives one Google Antigravity CLI conversation through
[Agent Client Protocol v1](https://agentclientprotocol.com/protocol/v1/overview).
It requires `agy` 1.5.14 or later. The minimum version is the first verified
here to keep a running tool and both inputs through a mid-turn prompt.

## Conversation and delivery

`agy acp` exchanges newline-delimited JSON-RPC over stdio. The adapter sends
`initialize` and either `session/new` or `session/load`. During load it discards
history playback. It always reapplies the configured model and reasoning effort
with the combined model ID in `session/set_model`.

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
both records has an unknown outcome.

## Interrupt and permission handling

`session/cancel` is a notification. The matching `cancelled` result confirms the
interrupt, and the process remains available for the next prompt. The adapter
reports whether observed foreground tool processes settle. Closing a session
remains a process shutdown and uses the shared process-tree cleanup. Losing the
process requires a new ACP handshake and `session/load` before another prompt.

The client rejects `session/request_permission` with the offered rejection
option, or cancels the permission request when no such option is available.
The tool's `failed` status and the permission label reach the turn report.
The explicit `skip_permissions` setting asks the CLI to auto-approve.

## Background updates and sidecar reads

The stdio reader runs for the whole session. In-flight tool results keep the
ownership of the original call even when they arrive after the prompt ends.
The idle answer chunks are joined at the following `usage_update` and sent
through `UnsolicitedOutput` to the bridge's wake sink. ACP v1 update frames
carry a session ID. They lack a prompt ID, so overlapping text from the
provider's own work and an active prompt shares the prompt's stream.

Unresolved timed `schedule` calls that survive their prompt are represented as
background work until completion or the declared deadline. Cron-like calls
are kept for the existing one-day bound. Timers also supply the foreground
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
cover several planner steps. The adapter compares reply totals with the
disjoint planner rows appended during the turn. When counters overlap or are
missing, the transcript supplies the shared counters, and matching ACP spans
supply any additional thought and cache-write counts. Those two counts are
unknown for spans without a matching report. The CLI supplies no price.

## Bounds

| Setting | Default | Purpose |
|---|---|---|
| `startup_request_seconds` | 30 s | Bounds each initialization, session setup and model selection request, which otherwise could leave the host waiting on a dead stdio peer. |
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
