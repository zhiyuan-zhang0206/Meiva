# Antigravity (agy) session bridge

`base/lm/harness/antigravity/` turns one Google Antigravity CLI (`agy`) process
into the runtime's turn engine. It reuses the shared contract modules
(`events`, `handoff`, `registry`, `process`) unchanged: what is special about
`agy` lives in this package.

## The transport is not ACP

`agy` 1.4.2 has no Agent Client Protocol server. The ACP surface is a separate
Google binary (`agy_acp_server.par`, release 1.3.0, ~926 MB unpacked) with its
own Google sign-in; it is not installed by `agy` and not used here. The CLI's own
programmatic surface is headless print mode:

```
agy --input-format stream-json --output-format stream-json --model <id> [--conversation <id>]
```

One process serves the whole session. stdin takes one JSON object per line,
`{"event":"user","message":{"content":"…"}}`; stdout emits `init`, `step_update`
and `result`. While a turn is running, the CLI's `printmode.runStreamInput` loop
blocks inside `pollResponse` until `IsCascadeRunning` becomes false, so stdin is
not read again until the turn completes. Mid-turn input
therefore talks directly to the child process's in-process Connect JSON-RPC
`LanguageServerService` on localhost: `AgyTransport` sets
`ANTIGRAVITY_SERVER_CSRF_TOKEN` in the child environment, discovers the child's
listening HTTP port via `psutil`, and invokes `SendUserCascadeMessage`
(`{"cascadeId": <conversation_id>, "items": [{"text": "…"}], "clientMessageId": <our key>,
"media": [{"mimeType": …, "inlineData": <base64>}]}`), which queues a
`CORTEX_STEP_TYPE_USER_INPUT` step into the running `AgentExecutor` (`messages.Hub`)
so `pollResponse` streams the steered continuation out on stdout within the same
turn.

## Delivery, acknowledgements and the phantom turn

Non-public interfaces, agy 1.5.5 (`rpc.AGY_VERSION_PREMISE`). `SendUserCascadeMessage`,
`GetCascadeTrajectory`, the `clientMessageId` / `media` fields and the message-queue files come from
the binary's proto descriptors and from experiments, not from a published API. The server answers 200
for an unknown or misspelt field (`images`, `items[].image` and snake_case names were accepted and
ignored) and for a message sent when no turn runs, so an HTTP 200 is only a weak receipt. A reply of
an unexpected shape or an unknown run status raises `AgyInterfaceError`; it is never read as "idle".

`deliver()` asks `GetCascadeTrajectory` (`status` RUNNING / IDLE, `numTotalSteps`, `trajectory.steps`)
before each input (`trajectory.route`): RUNNING is steered with our id; IDLE with no turn of ours
starts a turn on stdin; IDLE while our own turn's result is unread is refused (`InputRejected`) so the
input is handed over again. An input sent through RPC to an idle conversation would run a **phantom
turn**: agy runs it, stdout shows nothing (130 s observed), and at the next stdin input its steps are
printed again and its answer is folded into that turn's `result`.

Acknowledgements. ACK 1 is weak (stdin write, HTTP 200). ACK 2 is the trajectory step whose
`userInput.clientMessageId` is our key (polled every 0.5 s while an input is unsettled), or, for a turn
started on stdin, the first user-input step on stdout (no id is echoed there, so it is matched by
order; a CLI that prints none is acknowledged by the turn's first activity). A phantom turn that
still happens (the conversation was running when asked and idle when written to) is observed from the
trajectory: a running conversation with no turn of ours opens `TurnStarted(HARNESS,
QUEUED_INPUT_REVIVED)`, an idle one closes it with the answers read from the transcript, and the steps
already attributed are ignored when stdout replays them, so the next turn's text is its own (its
`result` still carries the combined usage). Frames of an earlier process generation are dropped.

Known limits. An image can only ride on a running turn (`media`): the stdin protocol has no image field. An
input with an image for an idle conversation therefore starts its turn with the text alone (which says the
image follows) and the image is steered into that turn as a second input, `<key>:images`, once the trajectory
reports the turn running (`AntigravityConfig.image_follow_seconds`, 30 s). Verified on a live agy: a turn
running `sleep 45` took a steered red image (answer "Red"), and an idle delivery of a blue image answered
"Blue" with the two acknowledgements `k1` then `k1:images`. The second key settles only itself; if the turn ends
or never runs in time, the image is not sent, `harness_image_not_delivered` is logged and the second key is
rejected, so the text of the message still stands and names the image. The trajectory reply of a text-only step was
not observed to carry the text, so the phantom turn's answer comes from the transcript file.
`deliveryStrategy=WHEN_IDLE` was tried once, never against an idle conversation, so nothing here relies on it.

Sub-agents. `invoke_subagent` creates a sibling conversation that the stream-json output never mentions;
`SubagentReader` reads the parent's `subagents/` descriptors and the child's transcript file and the
session reports them as sub-agent text and tool calls ([sub-agent text](../../docs/subagent-text.md)).

Reconciliation (`reconciliation/`): the durable queue is
`brain/<conversation>/.system_generated/messages/` (not the transcript). A message listed in
`read.json` is delivered (IN_CONTEXT); a stored one, listed in `undelivered/` or not, will be delivered
by the CLI before the next stdin input and must not be sent again (PERSISTED_UNDELIVERED); no file with
the id means NOT_FOUND. Only the two protobuf levels that hold the id are parsed.

There are no request ids on stdin/stdout, so **a cancel cannot be a
request**: the adapter sends `SIGINT`, and the CLI answers with a final `result`
(`status: ERROR`, `error: interrupted`) and exits 1. `interrupt()` therefore
retires the process; the next turn respawns it with `--conversation`, which keeps
the transcript while the process is replaced. Hosted foreground Cancel leaves
the CLI running and reports that the provider lacks a foreground-only control.

The CLI also offers an `agy acp` mode. This adapter uses headless stream JSON.

## Three harness differences, absorbed here

| Difference | Where it is absorbed |
|---|---|
| Headless cannot prompt: a tool that needs a permission the configured policy does not grant is **auto-denied**, yet the turn still reports `status: SUCCESS` and exits 0 | `mapping.ResultFields.is_error` is true when `denied_actions` is non-empty; `TurnCompleted.permission_denials` carries the entries. A caller that only reads `status` records a silent no-op as success |

The shape that makes this a silent failure, captured from `agy` 1.4.2 with a
`request-review` profile asked to run `echo hi > file`:

```json
{"event":"result","result":{"conversation_id":"…","status":"SUCCESS","response":"",
 "num_turns":1,"usage":{"input_tokens":11907,"output_tokens":71,"thinking_tokens":0,
 "cache_read_tokens":0,"total_tokens":11978},
 "denied_actions":[{"action":"command","display_name":"RunCommand"}]}}
```

Nothing ran, the exit code is 0, and `denied_actions` is the only structured
signal. The adapter turns that into `subtype="denied"`, `is_error=True`,
`permission_denials=("command:RunCommand",)`.
| Every `run_command` is a fresh shell: `cd` and `export` do not survive into the next call | `handoff_rules.SHELL_SEMANTICS`, appended to the handoff by `build_handoff()` |
| `run_command` runs non-interactively with stdin closed (`/dev/null`) under modern GNU bash | `wiring.session_environ` sets `SHELL` to the non-interactive bash wrapper script; commands reading stdin receive immediate EOF |
| A background process started with `&` is killed when the turn ends | same: the handoff tells the agent to use `setsid nohup …` or tmux |
| `run_command` caps `WaitMsBeforeAsync` at 10s, while cold `import meiva` attachment + plugin loading takes several seconds | same: the handoff tells the agent to pass `WaitMsBeforeAsync=10000` and batch synchronous `meiva.*` calls in one `"$MEIVA_PYTHON" -c` invocation |

The first is enforced in code and pinned by `test_denied.py`; the shell/runtime rules are
behaviour of the CLI that the adapter cannot change, so they are disclosed in the
text the agent reads, and `test_handoff.py` pins that the text is present (and
absent from a plain `Handoff`).

## Who a frame belongs to

`agy` names no turns: `init`, `step_update` and `result` carry no request id, and the
NDJSON stream is the only channel. A `result` is therefore ours only when this
session wrote a turn out (`_sent_turn`, set before the write) and has not seen it
end; anything else is the CLI's own work and becomes
`UnsolicitedOutput(source="result")`. Mid-turn the two are indistinguishable at the
protocol level, so the reply is built from the turn's own streamed text — the parts
already closed plus whatever the in-flight step streamed — and the frame's
`response` only fills a gap. That is what keeps a foreign `result` from replacing
the reply it interrupted.

A stream that ends names the turn it was serving when one was running
(`TurnFaulted`) and the session when none was (`SessionFailed`).

The rule and the three events are [[base/lm/harness/docs/attribution.md|Who
a provider message belongs to]]; `tests/test_unsolicited.py` pins this half —
including the residual risk that a `result` arriving mid-turn is acted on, because
nothing in the protocol can tell the two apart.

## Modules

| Module | Owns |
|---|---|
| `transport.py` | `AgyTransport`: spawn, NDJSON framing, SIGINT with process assertions, stderr tail |
| `session.py` | `AgySession`: ordered inbound queue, one event stream, totals, respawn after interrupt |
| `mapping.py` | pure `init` / `step_update` / `result` → event translation |
| `handoff_rules.py` | `build_handoff()` and the two shell-semantics rules |
| `config.py` | `AntigravityConfig` and the argv it builds |

## Config bounds

| Field | Default | Reason |
|---|---|---|
| `cancel_settle_seconds` | 5 | how long an interrupt waits for the CLI and its children before escalating to SIGTERM/SIGKILL and reporting residue |
| `max_buffered_events` | 256 | text deltas are droppable under lag; control events never are |
| `skip_permissions` | `False` | headless cannot prompt; a command outside the configured policy is denied, which the adapter reports as failure. Opting in trades that denial for auto-approval |
| `MAX_FRAME_BYTES` (`transport.py`) | `256 MiB` | one NDJSON `step_update` or `result` line can carry a multi-megabyte tool result or accumulated turn state; `AgyTransport` reads 64 KiB chunks into a `bytearray` without `StreamReader.readline()`'s 8 MiB separator limit while capping one frame at 256 MiB so an unterminated stream from a wedged child cannot grow memory without bound |

## Interrupt, measured on the real CLI

`SIGINT` delivery, tool-tree reaping on `interrupt()` and `close()`, and
`--conversation` respawn recovery are documented in
[Antigravity interrupt and process-tree reaping](interrupt.md).

## Usage and cost

`agy` reports `input_tokens`, `output_tokens`, `thinking_tokens` and
`cache_read_tokens`; thinking is billed as output, so it is folded into
`Usage.output_tokens` to keep the figure comparable with the other harnesses.
The CLI reports no price, so `cost_usd` stays `None` and is not accumulated —
an unknown cost is not the same as a zero one.

## Tests

`tests/fake_agy.py` is spawned exactly like the CLI and speaks the same framing,
including the SIGINT exit path and a real child process, so the cancel assertions
run against a real process tree. The suite pins the seven shared invariants plus
the three differences above.

## Scheduled work

`agy` has a `schedule` tool (cron jobs and timers). The step stream shows the call and its
success. A timer call (`DurationSeconds`, `Prompt`) blocks inside its turn and its result
arrives when it fires, so it adds nothing after the turn. A call without a duration
(cron-like) shows neither firing nor cancellation, so `schedules.py` holds it as pending for
one day. An interrupt kills `agy` and the schedules with it; the
respawn hands them to the next turn's note as lost. See
[background work](../../docs/background-work.md).
