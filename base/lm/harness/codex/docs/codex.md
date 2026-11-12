# Codex app-server session bridge

`base/lm/harness/codex/` turns one `codex app-server` process into the
runtime's turn engine, alongside the Claude Agent SDK bridge. It is not an MCP client
and not a provider plugin: the app server runs the agent loop locally, so a turn
is a real agentic loop with tool calls, a resumable thread and a
subscription-backed quota.

**Codex has no ACP server.** No `acp` subcommand, `--acp` rejected, no protocol
symbols in the binary. The ACP packages in the ecosystem are third-party
adapters that lag the CLI, so the bridge speaks the app-server protocol
directly: the uniform surface is this adapter, not a shared wire protocol.

## Three responsibilities

1. **Open the thread.** `CodexSession.start()` spawns `codex app-server --listen
   stdio://`, performs `initialize` + `initialized`, then `thread/start` — or
   `thread/resume` with the recorded thread id. A rejected `thread/resume` fails
   fast instead of silently starting a fresh thread.
2. **The handoff rides the first input.** The bridge puts the rendered
   sectioned document in front of the first input of a fresh thread; a resumed
   thread gets none. The handoff directs macOS Codex Computer Use to
   paste non-ASCII text and verify the field value. Native `typeText` dropped
   the Chinese characters in a mixed-script Chrome input while plain-text
   paste preserved the full input.
3. **Map notifications back and persist the identity pair.** `events()` is the
   single outbound stream, and the registry writes `agent_id ↔ thread_id`. When
   `turn/start` returns the authoritative Codex `turn.id`, `CodexSession` binds
   the running turn to that id and discards any stale notification state from an
   earlier turn replayed during `thread/resume`.

## Modules

| Module | Owns |
|---|---|
| `session.py` | `CodexSession`, the shell: start / resume / close, ordered inbound queue, one event stream, approvals, interrupt, totals |
| `transport.py` | `StdioAppServer`, the shell of the child's pipes: spawn, write a line, read newline-delimited frames in 64 KiB chunks, correlate a reply |
| `frames.py` | the JSON-RPC wire: frames, the compact one-line encoding, what an inbound line is (`classify`), the reply-to-result rule, the two errors |
| `requests.py` | what the session sends (`initialize`, `thread/*`, `turn/start`, `turn/steer`, `turn/interrupt`) and the frame it answers a server request with |
| `turns.py` | what a message means and what a settled turn is: `inbound`, `item`, `binding`, `error_report`, `unclaimed_text`, `TurnRecord`, `settle` |
| `mapping.py` | pure `ThreadItem` → event translation and usage extraction |
| `config.py` | `CodexConfig` and the command line |
| `auth.py` | the credential environment the child receives: which variables are removed, and the explicit opt-in that keeps them |
| `transcript/` | reading the thread's own record into timeline entries, for the writer behind `harness_transcript_timeline` (default off): the reader, its link to the session, the history cursor position, the rollout tail and the stored-row whitelist; see [the timeline from the thread's own record](#the-timeline-from-the-threads-own-record) |

`events.py`, `handoff.py`, `process.py` and `registry.py` are the shared bridge
modules; this package does not restate them.

## Notification mapping

[[base/lm/harness/codex/docs/notification-mapping.md|The notification
mapping]] lists every app-server method the bridge reads and what each one
becomes; `turns.inbound` decides which of them is which, and a method the bridge
does not model maps to nothing because the runtime does not model it.

## Who a notification belongs to

[[base/lm/harness/codex/docs/attribution.md|Who an app-server
notification belongs to]]: the turn id the adapter binds, and why a name it never
bound has no owner.

## Execution policy

The sandbox the child runs in, and the approvals the bridge answers when a caller
turns them on, are [[base/lm/harness/codex/docs/execution-policy.md|Execution
policy]]: `danger-full-access` by default, with the approval path dormant unless a
caller opts into it.

## Configuration

| Field | Default | Reason |
|---|---|---|
| `model` | `None` | inherit the machine's `codex` config; pinning an id the account does not serve fails every turn |
| `sandbox` | `danger-full-access` | the only sandbox that runs on Ubuntu 24.04 — see the [execution policy](execution-policy.md) node |
| `approval_policy` | `never` | headless: no prompt to park on |
| `approval_decision` | `accept` | only consulted when the policy produces requests |
| `binary` / `extra_args` | `codex` / `()` | the CLI on `PATH`; tests substitute a peer |
| `api_key_mode` | `False` | off means the child gets no credential variables; on is the explicit choice of per-token billing |
| `startup_timeout_seconds` | 120 | `initialize`/`thread/*` are local round trips; a wedged server is a fault |
| `cancel_settle_seconds` | 5 | reaping is milliseconds; 5s tolerates load without hiding a leak |
| `max_buffered_events` | 256 | text deltas are droppable past this; control events are bounded by tool-call count |
| `MAX_FRAME_BYTES` (`transport.py`) | `256 MiB` | `thread/resume` returns a long-lived agent's entire accumulated thread JSON on a single stdout line (39.4 MiB observed on agent #6111); `StdioAppServer` reads 64 KiB chunks into a `bytearray` without `StreamReader.readline()`'s separator limit while capping one frame at 256 MiB so an unterminated stream from a wedged child cannot grow memory without bound |

## Foreground input

`deliver()` first waits for a turn of ours that is still starting to become interruptible, then sends the
stable `turn/steer` RPC to the running turn (a turn the server began on its own counts), with
`expectedTurnId` set to that turn. When no turn runs and the session is idle it sends `turn/start`
instead; when a turn it cannot steer (being interrupted, or its id not yet known) is in the way, the
input is refused as `InputRejected`, never started beside it. Both requests carry the Meiva input key as
`clientUserMessageId`, and Codex rejects a steer whose turn is no longer active. Images go as
`image` data URLs (recorded against app-server 0.166.0 between text items, with the `userMessage` echo carrying
our `clientId`). The two
acknowledgements are separate events: the RPC response naming the expected turn is ACK 1
(`InputReceived`, strong), and `item/completed` of a `userMessage` whose `clientId` is the key is ACK 2
(`InputInContext`, attributed to the turn named by the notification). A turn that completes while a
steered input has no ACK 2 declares it `InputLost` before `TurnCompleted`. `-32600 no active turn to
steer`, a missing method and invalid params are `InputRejected` (the rest of the batch is refused with
it); any other RPC error raises. `clientUserMessageId` carries no replay guarantee, so a lost input may
be handed over again, and only after a rollout reconciliation says it is not there
(`reconciliation/`, matching `item_completed.item.client_id`; the rollout format was observed on
app-server 0.166 and is not a published interface).

A `turn/started` for a turn this session did not start, with `status: inProgress`, while none of ours runs,
is observed as a harness turn (`TurnStarted(origin=HARNESS, cause=UNKNOWN)`): its text, tools and usage are
attributed to it, it completes once, and an input delivered meanwhile is steered into it. Frames are attributed by
provider turn id, so a late command completion of an interrupted turn never lands on the next turn.
Details: [delivery](../../docs/delivery.md).

## Invariants

Adapter invariants are pinned by tests in `tests/`:

| # | Invariant | Test |
|---|---|---|
| 4 | interrupt ends the turn's foreground commands and leaves background commands and helpers alone | `test_cancel.py::test_interrupt_ends_the_command_the_turn_started`, `test_a_background_command_of_an_earlier_turn_is_left_running`, `test_the_app_servers_own_helpers_are_neither_waited_for_nor_ended` |
| 5 | a turn can finish after two quiet days | `test_cancel.py::test_default_turn_can_finish_after_two_quiet_days` |
| 6 | usage accumulates per turn | `test_session.py::test_usage_accumulates_across_turns` |
| 7 | a resume names the recorded thread and a rejected resume fails fast | `test_session.py::test_resume_uses_the_registry` |
| 8 | steering waits for a correlated execution receipt | `test_delivery.py` |

## The timeline from the thread's own record

Behind the agent setting `harness_transcript_timeline` (**default off**; a temporary rollout switch shared with Claude Code,
deleted when all three CLIs have cut over) a Codex agent's timeline is written by the session-scoped writer
(`agent/harness_timeline/`) from the thread's own record and not from this adapter's event stream. With the setting off none
of this runs: `CodexConfig.transcript` is unset, the session holds no link and every request is byte-identical to what it was.

- **The one reading path** is the protocol's stable `thread/items/list`, over the session's own app-server connection, by an
  opaque cursor the server gives (`transcript/reader.py`, `history.py`). A read is the newest eight items when the stored
  position is up to date, or the next page of at most 32 items after the stored cursor when it is behind; a position is the
  cursor, whether it is at the last item read, that item's id, the next row ordinal and the rollout's byte span
  (`position.py`). **No call returns a whole history**: `thread/read` and `thread/resume` with turns are never sent on this
  path (a test scans the package for them), `thread/resume` is sent with `excludeTurns` (the reply is the thread's metadata,
  about 2.5 KB for a thread whose full reply is 4.8 KB at two turns and 39 MiB observed at scale), and `thread/fork` already was.
  Nothing in the adapter read the history out of a resume reply (`OpenedThread` takes the id, model, cwd, rollout path and CLI
  version); against the real 0.166.0 app-server the notifications after a resume are identical with and without the history bar
  one `deprecationNotice`, and the stale `thread/tokenUsage/updated` it sends for the previous turn names a turn no ticket of ours
  holds, so attribution ignores it as before.
- **The rollout** (`~/.codex/sessions/.../rollout-*.jsonl`, found from the path the server reports, else by thread id) is read
  from a stored byte offset for the four things the history lacks: a turn's end or abort (`task_complete`, `turn_aborted`; a failed
  turn writes `task_complete` with an `error` object), a sample's token usage (`token_usage_record`), a finished child's report
  (`<subagent_notification>`), and nothing else: every other line is passed over by a byte test without being decoded. It is read
  before the items are asked for and used only when the items reached the end of the history, so a turn's end is never read before its
  last item. Its growth is also the cheap "anything new?" check: while the rollout has not grown, no notification names the thread
  and the last request is under 30 s old, a read costs a `stat`.
- **The session and the reader share one link** (`transcript/link.py`, the `TranscriptTap` protocol in `frames.py`): the session
  attaches its connection when the thread is open and detaches it on close or when the server is gone, and passes every
  notification on. A reader's request is one more call on the transport, which correlates replies by id under one write lock, so it
  never interferes with a running turn's own requests; it has its own 30 s timeout and a page is bounded. A notification only
  says that a thread had activity: `item/completed`, `turn/completed`, `turn/started` and `thread/tokenUsage/updated` wake the
  writer, deltas and statuses do not, and none carries content.
- **Children.** A `spawnAgent` call links its receiver thread, which is a record of its own read by the same means (its token usage
  from its own rollout; its own turn ends are not its parent's). A child is read from the moment a notification names it or a spawn
  call links it until a read brings nothing, so a conversation with many finished children costs a request for the main record and
  one for each child still working; the child's items nest under the call that spawned them, also after the parent's turn ended.
- **What an input proves.** The `clientUserMessageId` we send comes back as `userMessage.clientId`; that record is the delivery
  (once). A steer the interrupt swallowed is in no record, so it is never proven and goes back to the queue.
- **Positions that do not hold** (the history no longer starts with the last item read, the server refuses the stored cursor) are
  `PositionLostError`; the writer puts the record at its current end and registers `harness_timeline_position_lost`. A server that
  answers `thread/items/list` with "not supported yet" is reported once as an anomaly (the first run found that a thread no turn
  has run in answers so; the reader does not ask while the rollout does not exist).
- **What is stored** is a whitelist (`transcript/store_filter.py`): a history row by item type, a rollout line by kind, text cut at
  256 KiB for the main record and 16 KiB for a child with its original length, inline images as their hash and size.

## Version pinning

The CLI versions the protocol. `codex app-server generate-json-schema --out DIR`
writes the installed binary's exact schema — generate it at build time and diff
it. 0.163.0 removed the legacy `newConversation` / `sendUserTurn` RPCs, so a
client written against older docs fails outright.

The adapter uses the **stable surface only** (105 client methods on 0.163.0); the
experimental surface is not adopted.

See [Session environment](environment.md) for authentication filtering,
explicit environment inputs and construction/reconnect timing.

## Background commands

`unified_exec` commands outlive their turn while the app-server runs and die with it.
`commands.py` follows `commandExecution` items (`agent` and `unifiedExecStartup` sources)
from `item/started` to `item/completed`; open ones count as pending background work only
while the process tree shows a descendant the app-server did not start with (the app-server does send
`item/completed` when a background command ends after its turn; the check covers an item
whose completion never comes). A turn that ends
interrupted or failed drops its open items (its interrupt ended them,
[what an interrupt stops](../../docs/harness.md#what-an-interrupt-stops)). See [background work](../../docs/background-work.md).

## Sub-agent threads

A `spawnAgent` child is a thread of the same app-server and an independent line of work: it outlives the
parent's turn, and the turn is not ours to end for it. A working child therefore holds the session.
`subagents.py` follows each child from the spawn call (unless `agentsStates` already reports it finished, or
its own end frame came before the call completed, which the session remembers for the last 64 such threads)
until its `turn/completed`, a `thread/status/changed` that is not `active`, an `error` that will not be
retried, or a `closeAgent` call of the parent; its `turn/started` or an `active` status reopens it. A working
child is `BackgroundKind.AGENT` work, so the idle TTL does not close the app-server it lives in and the
session record names it should the process go anyway.

The hold is bounded: every frame of a known child, shown or not (an `item/completed` included), restarts the
clock, and a child that sent no frame for `CHILD_SILENCE_SECONDS` (7,200 s) is taken as finished. The constant
errs long because releasing a working child kills it, while holding a dead one costs one idle process, and a
quiet child is only one inside a single model or tool call. Once the app-server is gone
(`_on_transport_closed`) the session reports no background work at all, so a dead process cannot keep a
runtime from being evicted; the record keeps what was pending for the lost-work note. See
[background work](../../docs/background-work.md).
