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

Ordinary submissions wait for the previous `turn/completed` before sending
`turn/start`. Foreground input uses the stable `turn/steer` RPC with
`expectedTurnId` set to the bound Codex turn and `clientUserMessageId` set to
the Meiva inbound key. Codex rejects the request if that turn is no longer active.

`deliver()` sends `turn/steer` to the running turn (a turn the server began on its own counts) or,
when none runs, `turn/start`; both carry the input key as `clientUserMessageId`, and images go as
`image` data URLs (schema-supported, not yet observed against a real server). The two
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
