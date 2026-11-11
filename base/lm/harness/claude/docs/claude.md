# Claude harness adapter

`base/lm/harness/claude/` turns one Claude Agent SDK session into a turn engine.
It is not a LangChain chat model and not a provider plugin: the SDK drives a
bundled Claude Code CLI, so a turn is an agentic loop with real tool calls, a
resumable transcript and a subscription-backed quota.

The shared half — events, handoff, registry, process probe — lives in
[Provider harness](../../docs/harness.md). This adapter owns only what the Claude SDK
decides differently from another provider.

## Modules

| Module | Owns |
|---|---|
| `session.py` | the effect shell of `ClaudeSession`: start / resume / close, `deliver()`, the observed-turn model, one event stream, interrupt, totals — it awaits the SDK and holds the state, and every decision it makes comes from the modules below |
| `inbound.py` | what one inbound message is (dispatch by class name), what an input is sent as and which echo opens a turn (`delivery.py`), what a step and a turn's text are, the background-task set, and whether a task report goes to the sink or the stream |
| `options.py` | the keyword values handed to the SDK's option class, including the two that ride only when resolved or supported |
| `settle.py` | how one result settles a turn: the stream event, the new session id and the running totals |
| `mapping.py` | pure SDK-message → `BridgeEvent` translation, dispatched by class name |
| `auth.py` | the credential boundary: OAuth by default, API key only on request, secrets never returned |
| `config.py` | `BridgeConfig` |

The package door (`__init__.py`) re-exports the pure half; `ClaudeSession` and the errors it
exports come from `base.lm.harness.claude.session`, so the door itself stays `strict`.

## Three responsibilities

1. **Start / resume the session.** `ClaudeSession.start()` opens the session and
   writes the CLI's own working surface: no system-prompt override, no approval
   callback, no MCP servers of ours. `resume` comes from the caller or from the
   registry.
2. **The handoff rides the first input.** The bridge puts the rendered handoff in
   front of the first input of a fresh session; a resumed session gets none, its
   transcript already carries it.
3. **Map events back and persist the identity pair.** `events()` is the single
   outbound stream, and the registry writes `agent_id ↔ session_id` as soon as
   the SDK reports its init.

## Configuration

| Field | Default | Reason |
|---|---|---|
| `model` | `claude-sonnet-5-5` | full id from the bundled CLI's model table; an alias would let an upgrade change the model |
| `tools` | `None` | retain the complete native CLI tool set; an explicit tuple restricts it |
| `permission_mode` | `bypassPermissions` | headless session with no approval layer: the CLI must not park on a prompt nobody can answer. The workspace is the boundary. |
| `cancel_settle_seconds` | 5 | observed reaping is ~40ms; 5s tolerates load without hiding a leak |
| `max_buffered_events` | 256 | text deltas are droppable past this; control events are bounded by tool-call count |

Native user, project and local settings remain enabled, including CLAUDE.md,
skills, hooks, plugins and configured MCP servers. The adapter does not set
`setting_sources=[]` or strict MCP isolation. It adds no Meiva MCP configuration
and never overrides the provider system prompt. Model/effort selections override
only those execution choices; authentication still follows the credential boundary.

## Background tasks

A `Bash` command the model starts with `run_in_background=true` outlives its turn,
and the CLI reports its end on its own:
[background tasks in a Claude session](background-tasks.md).

## Claude-specific invariants

- **Inputs carry a uuid; the CLI's echo is the acknowledgement.** Every input is sent as a user
  message with a uuid derived from its key (`delivery.key_uuid`), and the session starts the CLI with
  `--replay-user-messages` (an SDK that cannot pass it is refused; a CLI that does not echo fails the
  session at the first result instead of guessing). The write is a weak receipt (`InputReceived`,
  WEAK); the echo of our uuid is `InputInContext`. Where the model gets the input depends on the CLI:
  at the next tool boundary (the echo arrives mid-turn and joins that turn) or, when the turn has no
  tool left to cross, about a second after that turn's result, as a turn of its own (the echo arrives
  with no turn open and starts one: `TurnStarted(HARNESS, QUEUED_INPUT_REVIVED, keys)`). The same
  happens to an input queued when `interrupt()` ran. Observed on CLI 2.1.301 (the SDK's pinned binary;
  the machine's `claude --version` can be older).
- **Turns are observed, not submitted.** A turn opens at the first echo of our uuid, at an echo that
  names an origin (`task-notification`, a timer word), or at the first content with no turn open; a
  `ResultMessage` ends it. Echoes that are not ours (`[Request interrupted by user]`) are ignored.
  Each turn is reported once under its own id; there is no alias completion.
  Causes of a CLI-started turn: `BACKGROUND_TASK` (a task notification preceded it or its origin is
  `task-notification`), `SCHEDULED_WAKE` (an origin naming cron, schedule, timer, wake, loop or
  monitor; no real sample exists yet, the shape is a guess), otherwise `UNKNOWN`.
- **Cancellation is asserted.** `interrupt()` reports whether the turn's foreground tool
  processes are gone ([what an interrupt stops](../../docs/harness.md#what-an-interrupt-stops): the MCP
  servers the CLI starts, even mid-turn, and background commands are not counted), and `settled=None` when the CLI pid is unobservable. A
  result attributed to the foreground confirms cancellation when its
  `terminal_reason` is `aborted_streaming` or `aborted_tools`. Older CLIs can
  omit this field, in which case the bridge uses the ordinary result verdict.
- **The child environment is an allowlist.** `auth.build_env` names every key the
  session may carry (`registry.session_child_env_keys`) and blanks every other key
  the host process holds, because the SDK spawns the CLI with
  `{**os.environ, **options.env}` — a key left out of the mapping is filled back in
  from the parent, so omission is not removal. The inherited model credentials are
  therefore empty in the session: before this rule a claude session's Bash tool
  could read the host's `OPENAI_API_KEY` (#5512). The two Claude credential
  variables stay blanked rather than dropped, and API-key mode restores exactly the
  one the caller names; the adapter never reads, stores or forwards a secret. The
  registration handle and the runner's home ride because they are on the allowlist —
  a tool the CLI spawns needs the handle to attach as the agent it belongs to.
- **The CLI's own system prompt, chosen explicitly.** `option_values` passes
  `system_prompt={"type": "preset", "preset": "claude_code"}` with no `append`. The
  SDK treats an unset `system_prompt` as `--system-prompt ""`, so omitting the option
  does not leave "the CLI's preset" in place: the model would get a ~485-character
  stub ("You are a Claude agent, built on Anthropic's Claude Agent SDK.") instead of
  Claude Code's default prompt (~29k characters). The bridge does not override the
  harness's system prompt; selecting the preset explicitly is how it keeps that
  promise. A preset without `append` adds no CLI flag, and Meiva's own instructions
  still travel as the handoff message, never as a prompt edit. An SDK whose options
  lack `system_prompt` fails the session with a `ValueError` rather than falling back
  to the stub. Every session now carries the default prompt as a prefix. After
  deployment, new sessions use the default prompt; whether a resumed session switches
  depends on the CLI's `--system-prompt-snapshot` (default `on`: the prompt "is
  rendered on the conversation's first request ... and recorded; every later request
  and resume sends the record as-is"), which has not been measured here.
- **Subagent text is not forwarded.** `forward_subagent_text` stays off. Turned on,
  subagent text blocks arrive as `AssistantMessage`s with `parent_tool_use_id`, which
  `mapping.py` would emit as `AssistantText` and the projector would present as the
  main agent's reply (visibility parsing, reply fallback). A correct presentation
  needs a sub-activity event and timeline item kind that do not exist yet.
- **A killed host leaves the CLI behind.** If the host is SIGKILLed, the bundled
  CLI reparents to init and keeps the session; a supervisor must sweep it by the
  session id in the registry.

## Testing

`base/lm/harness/claude/tests/` runs against `fake_sdk.py`, a scripted stand-in
whose classes carry the SDK's names and fields: turns, tool calls, interrupts and
rate-limit events, with no CLI and no quota.
