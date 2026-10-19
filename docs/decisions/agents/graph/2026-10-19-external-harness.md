# External harness adapters

Status: `base/lm/harness/` and its Claude adapter landed on main with #6358
(2026-10-19) — the four shared modules, the versioned handoff, the session
registry and the `harness-claude` extra. Nothing is wired into the agent loop yet;
§13 is that design. The Codex adapter (#5405) and the Antigravity adapter (#5406)
are being built against this contract, and each row below moves from plan to
landed as its adapter merges.

This record replaces
[wiring the Claude Agent SDK session into the turn model](2026-10-19-claude-sdk-session-bridge.md),
which is kept as the predecessor of the Claude-specific half.

## 0. One page

An external agent is not "a model we hand a toolset to". It is a harness —
Claude Code, Codex, Antigravity — that already has tools, a turn loop and a
transcript. We hand it a **handoff document**, and it works with its own
capabilities plus our Python SDK.

So: no permission layer, no MCP server, no system-prompt rewrite, and no second
copy of a tool it already has. The only structure we take into the cluster is
**fork**.

## 1. What we do not build

| Not built | Why |
|---|---|
| Permission system / capability gate | User ruling, 2026-10-19: too complex to get right for a fleet; skipped for now. |
| MCP server | Code mode calls `meiva.*` directly, the same way impersonation does. |
| A rewritten system prompt | The harness keeps its own; we only decide what the handoff tells it. |
| A copy of our tool surface | Its bash / edit / monitor / subagent are not duplicated. |

## 2. One logic, two entry points

| | Spawn an external agent | Impersonation |
|---|---|---|
| Who opens the session | We do | A human is already working in it |
| Can a human watch | No tmux panel | Yes, their own surface |
| Identity | Bound to a new agent id | Bound to the agent id it already serves |
| Entry | A new session | **Continuing an existing session** (session id) |
| Everything else | The same: handoff, event mapping, identity mapping, accounting | The same |

## 3. The handoff

Sent as the first message of a fresh session; **not** re-sent on `resume`.

Six sections, in this order, because the order is the priority when an agent
skims:

1. **Identity** — agent id, label, machine, workspace, cwd.
2. **Cluster facts** — machine names and roles (gateway / agent-runner), only the
   ones this task needs.
3. **Entry points** — a short index of `import meiva` entry points, one purpose
   per entry. Not the docstrings, not an API manual.
4. **Do not use** — the parts that overlap with the harness's own tools (bash,
   edit, monitor, subagent), so two ways of doing the same thing never coexist.
5. **Reporting** — how a turn ends, when to reply to the human, when to call
   `meiva.ui.notify`.
6. **Task** — what this wake is for, plus the context it needs.

Extraction rule: take only three blocks from our own system prompt — identity and
role boundaries, task and reporting discipline, cluster facts. The tool-usage
details, formatting preferences and injection-scan rules stay ours; they are
runtime behaviour, not something the external agent needs to know.

Budget: the handoff is a fixed cost per wake. The target is ≤ 2,000 characters;
when it overflows, section 3 is the first thing to cut.

## 4. Overlap subtraction

| Capability | Harness has it | We provide it | Shape |
|---|---|---|---|
| shell / run a command | yes | no | not written |
| read / edit / write files | yes | no | not written |
| background monitoring | yes (monitor) | no | watcher docs not written |
| sub-agents | yes | no | not a cluster agent |
| plans / todos | yes | no | not written |
| **heartbeat** | no | **yes** | named in the handoff |
| **task registry** | no | **yes** | named in the handoff |
| **agent messaging / spawn / terminate** | no | **yes** | named in the handoff |
| **UI / page** | no | **yes** | named in the handoff |
| **fork** | yes (inside the harness) | **yes, as a cluster agent** | see §6 |
| memory | partly (CLAUDE.md, project files) | pool on demand | named only when the task needs it |

The principle is **subtraction by documentation, not by isolation**: the SDK stays
callable; we simply do not advertise it.

## 5. Identity and sessions

- The **agent id is stable** and is the audit owner: everything the external agent
  does is recorded under it.
- Registry record: `agent_id | harness | external_session_id | model | cwd |
  created_at | updated_at | resumes`.
- One agent may own several sessions (each resurrect or fork creates one).
  `resurrect` takes the most recently updated record whose harness is available.
- If the session is gone (deleted or expired on the harness side), `resurrect`
  degrades to a fresh session with a fresh handoff.

## 6. Fork

- A fork **creates a new cluster agent**: new id, its own workspace, lineage
  pointing at the parent.
- A harness-internal sub-agent is **not** a cluster agent: no id, no lineage.
- User ruling, 2026-10-19 16:42: a fork copies the parent's message history. The
  cost is storage and two histories that diverge from then on; the benefit is a
  complete history at resurrect.

## 7. Failure and boundaries

- The harness process dies or the session drops: the agent is marked
  `disconnected`, and `resurrect` continues per §5.
- The machine is unreachable: the session cannot resume; either wait for the
  machine or rebuild on a new one, which loses context and has to be said in the
  handoff.
- Our inbound versus the harness's turn boundary: a message waits for the turn to
  complete; it is never inserted mid-turn.

## 8. Open questions

1. ~~Can Antigravity execute arbitrary Python?~~ Resolved 16:42: treat it as
   having bash; code mode is common to all three. The probe only confirms; if a
   provider turns out to have no shell, it falls back to its own file tools and
   section 3 of the handoff is rewritten.
2. Which entry points section 3 lists — cut by "what will this agent actually do",
   not by enumeration.
3. Fork copy timing and granularity: the whole session at once, or incrementally
   per turn (it changes the recovery window).

## 9. Measured: Antigravity's code-execution surface (agy 1.4.2)

- **Code mode works**: `run_command` runs in the host shell and inherits the
  launcher's environment (PATH, venv, `MEIVA_*`). Spawning it from a process with
  our venv makes `python -c "import meiva"` work; at worst the venv path is
  hard-coded.
- **Every `run_command` is a new shell**: `cd` and `export` do not carry over. The
  handoff must say so — each command carries its own cwd and env, or uses
  absolute paths.
- **Background processes are killed at the end of a turn**: a plain `&` does not
  survive; anything resident needs `setsid nohup` or tmux. A resident bootstrap
  (a heartbeat, say) has to be designed around this.
- **A denied approval is silent**: in headless mode, a call that needs the
  `command` permission and does not get it is refused while the result still
  reports SUCCESS; only `denied_actions` shows it. The adapter must report a
  non-empty `denied_actions` as a failure, or it becomes another silent-failure
  class.
- No sandbox by default (`/tmp` and `$HOME` writable, network reachable);
  `--sandbox` is opt-in.

## 10. Measured: the Codex integration surface (0.163.0)

- **The only path is `codex app-server`** (JSON-RPC over stdio or a unix socket).
  Codex does not speak ACP; the codex-acp projects are third-party adapters that
  lag. So "all three speak ACP" is false — the three transports differ: Claude =
  Agent SDK, Codex = app-server, Antigravity = its headless CLI (§12). **The
  unification happens at the adapter contract, not at the protocol layer.**
- Six capabilities verified: streaming deltas; tool-call events (19 `ThreadItem`
  kinds, commands carrying exitCode, output and parsed argv); approvals
  (server→client requests answered accept / decline / …, with a decline reported
  honestly by the model); per-turn token usage; `turn/interrupt`; `thread/resume`
  and `thread/fork` (with `forkedFromId`).
- Auth: the subscription is enough — after deleting `OPENAI_API_KEY` it still
  reports chatgpt/pro. When both exist, the stored chatgpt login wins.
- Code mode: the tool is `unified_exec` → `/bin/bash -lc`, cwd is the thread's
  cwd, and the environment passes through from the app-server process. Spawning it
  from a Meiva venv is enough for `import meiva`.
- **Hard constraint on ubuntu**: 24.04's AppArmor forbids unprivileged user
  namespaces, so bwrap cannot start and every command fails under `read-only` or
  `workspace-write`; only `sandbox="danger-full-access"` actually runs. A Codex
  agent on ubuntu is therefore **effectively unsandboxed**; changing that means
  kernel parameters, which are not ours to change. macmini untested.
- Versions: ubuntu 0.163.0, macmini 0.162.0, and 0.163 removed the old v1 session
  RPCs (following older docs fails). Unify both machines on 0.163.0 and check
  `generate-json-schema` into the tree as a diff guard.

## 11. Adapter contract v1 (frozen 2026-10-19 17:20)

Package layout, `base/lm/harness/`:

| Module | Owner | Holds |
|---|---|---|
| `events.py` | shared | the `BridgeEvent` union, independent of any harness |
| `handoff.py` | shared | the six sections, `HANDOFF_VERSION = 1` |
| `registry.py` | shared | `agent_id ↔ session`, atomic write |
| `process.py` | shared | child-process observation (residue after an interrupt) |
| `claude/` | adapter | claude-agent-sdk: auth / config / mapping / session |
| `codex/` | adapter | `codex app-server` JSON-RPC |
| `antigravity/` | adapter | agy headless CLI (NDJSON over stdio, see §12) |

Landed with #6358: the four shared modules and `claude/`. Still a plan: `codex/`
and `antigravity/` — their transports are already measured (§9, §10, §12), so an
adapter is the only thing missing.

The inbound queue, turn ticket, timeout and accounting state machine in
`session.py` are **not** extracted into a base class yet: wait for the second
adapter and extract only what is genuinely shared (rule of two). Extracting now
is guessing.

Every adapter does five things — `connect(resume)`, `send(text)`, `interrupt()`,
`stream()`, `close()` — and owns its own message-model → `BridgeEvent` mapping.

Seven invariants, common to all three, already pinned by tests in the Claude
adapter:

1. inbound is queued only at a turn boundary, never mid-turn;
2. a repeated submission key returns the same ticket, and a completed key replays
   its recorded outcome;
3. a full queue refuses; it does not buffer;
4. `interrupt()` asserts the tool's child processes were reaped; an unobservable
   process tree is not a pass;
5. a turn timeout interrupts rather than silently dropping the turn;
6. usage and cost accumulate per turn;
7. the handoff is injected once, at session creation, never on resume.

Differences are absorbed in the adapter and never leak upward:

| | Claude | Codex | Antigravity |
|---|---|---|---|
| Approval | SDK callback | server→client request (accept / decline) | only visible afterwards as `denied_actions` → non-empty means failure |
| Background processes | we spawn them, they live as long as the turn | same | killed at the end of the turn → must `setsid` |
| Sandbox | available | ubuntu has only `danger-full-access` | none by default |
| Session id | `session_id` | `thread_id` | `--conversation <id>` |

Two settled points:

- `claude-agent-sdk` is the optional extra `meiva[harness-claude]`, imported
  lazily; a probe reports "not installed". Reason: the #6358 measurement showed
  +240MB per venv (the bundled CLI), which should not be charged to every machine.
- The handoff carries `HANDOFF_VERSION = 1` and the field set is pinned by a test;
  changing a field means bumping the version.

## 12. Correction (2026-10-19 17:20): Antigravity has no ACP

An earlier reading of "Antigravity = ACP" was wrong; corrected against #5400:

- `agy` 1.4.2 has no ACP: `strings ~/.local/bin/agy | grep -i 'acp|agent-client-protocol'`
  returns nothing, `agy --help` has no acp subcommand, and upstream issue #31 is
  the community *asking* for ACP.
- The real ACP surface is a different Google binary,
  `agy_acp_server.par` (1.3.0, 926MB unpacked on linux-x64, needs a separate
  Google login, not installed here).
- All three behaviours we measured (`denied_actions`, a new shell per
  `run_command`, background processes killed at the end of a turn) are properties
  of the headless CLI; the real ACP surface has no `denied_actions` — it is
  `session/request_permission`.

Decision: transport = `agy --input-format stream-json --output-format stream-json`
(NDJSON over stdio, one resident process). It has no id/method, only
`{"event":"user",…}` in and `init | step_update | result` out; cancel is SIGINT;
continuation is `--conversation <id>`.

The contract is unaffected: still connect / send / interrupt / stream / close and
the seven invariants, with every difference confined to one transport file. The
reasoning is that the CLI is the path that is installed and logged in today,
while the ACP server needs another 333MB download and a separate login and is
older (1.3.0 < 1.4.2). If the ACP server becomes the default surface, only the
transport changes.

## 13. Wiring the first adapter into the turn model

### Context

`base/lm/harness/claude` drives one Claude Agent SDK session: start / resume /
close, one inbound message per turn, the handoff injected as the first message,
one event stream, an asserted interrupt, and `agent_id ↔ session_id`
persistence. It is deliberately not connected to the agent loop yet.

The user's 2026-10-19 16:40 ruling fixed the scope: external agents run **Python
code mode** (no MCP server), the bridge **does not touch the harness's system
prompt**, there is **no bridge-level permission layer**, and the agent id must be
stable and bound to the external session history because `resurrect` needs it.

The spike (`~/.meiva/workspaces/6982/spike/REPORT.md`) established the facts this
design rests on: the SDK runs on a subscription login without an API key;
`session_id` resumes across processes; `interrupt()` returns in ~5ms and the CLI
reaps the tool's child processes itself (~40ms observed); and a `query()` issued
mid-turn is folded into the pending tool result, where the model treats it as
injected content and refuses it.

### The decision

**2026-10-19 19:59 update (ruled):** the wiring is a graph node, not a
replacement for the llm node's body. Claim keeps its durable queue and lifecycle
handling and routes a "has work" verdict on a non-native harness to
`harness_turn` (`agent/graph/claim/harness_turn.py`, `base/lm/harness/bridge.py`, implementation task #5413). The ruled
alternative — swapping the body of one LLM round — cannot work: `llm` is one
model call whose contract is "return tool calls for `exec`", while a harness turn
performs any number of tool calls internally and returns one text; and routing
from claim keeps the single implementation of terminate / cancel / restart /
resurrect / fork instead of a second copy that would drift. The session cache
lives with `AgentRuntime`, so the existing idle TTL/LRU is what closes it.
`before_llm`, `llm`, `before_exec`, `exec` and `after_exec` never run on such an
agent, and the graph test asserts that over a real compiled graph.

**2026-10-19 21:25 update (ruled):** every registration row carries `expires_at`,
and the bridge rewrites it at the start of each turn. `descends_from(controller_pid)`
is only as good as the kernel's pid allocation, so a row without an expiry plus a
reused pid is a bearer capability that never dies; the TTL
(`MEIVA_HARNESS_REGISTRATION_TTL_SECONDS`, one hour, above the adapter's own 900 s
turn timeout) is what makes a crashed host's claim lapse on its own. A reader may
still accept a row without one; the writer may not produce it.

**2026-10-19 21:18 update (ruled):** two semantics belong to the wiring slice
rather than a later one. A `SessionFaulted` (or a session closing) under a turn
settles as a fatal stream abort — the agent does not stop half-way — and the
session is retired so the next inbound opens a fresh one from the registry. And
the cold-start handoff turn is committed as its own message, because injecting
the handoff is itself a turn: folding it into the first chat reply makes "the
first message" mean two different things. Streaming deltas, tool-call timeline
items and explicit `/compact` stay visibility work for later slices.

The ruled remedy for a fault supersedes the "retry path" cell in the event table
below: there is no in-turn retry against a session that died, and the abort is
reported the way an exhausted stream abort already is.

What follows is the section as written before that ruling; the event and
persistence contracts in it are what the node implements.

The bridge replaces the *model call*, not the graph — in the first draft that
meant `agent/graph/llm` keeping the turn's shape and only the body of one LLM
round changing.

**How a message gets in.** The claim node already owns the durable inbound queue
and keeps that role. It gains one step: instead of handing user text to a chat
model, it calls `ClaudeSession.submit(text, key=inbound_row_id)`.

- One claim batch = one bridge turn; the `ResultMessage` is the turn boundary.
- The idempotency key is the inbound row id, so a gateway redelivery inside
  `dedupe_window` replays the recorded outcome.
- Backpressure stays where the durable copy is: `InboundQueueFullError` means the
  claim node leaves the row unclaimed and retries.
- The claim node does not submit while a bridge turn runs; it waits for the
  boundary, which is the condition it already uses today.

**How the session is woken.** On a fresh start the caller passes a `Handoff`; the
session submits the rendered document as its first turn. On a resume the handoff
is not re-sent. Which cluster facts and which excerpts from our system prompt go
into sections 2 and 3 is decided by the waking path, not by the bridge: the
bridge takes text, it does not read `agent/graph/prompt`. That keeps the selection
reviewable in one place and the bridge free of the prompt layer.

**How the identity pair is persisted.** `FileSessionRegistry` writes
`agent_id ↔ session_id` as soon as the adapter reports its init, and refreshes
`updated_at` on every turn. Resurrect reads that record:

```python
record = registry.load(agent_id)      # None → the agent has no session yet
await session.start(handoff=handoff)  # resumes record.session_id when present
```

The agent id is the durable identity; the session id is the transcript. Neither
is derivable from the other, which is why the pair is stored rather than
recomputed.

**How output leaves.** One consumer drains `session.events()` for the life of the
session and maps each event onto what the graph already produces:

| Bridge event | Destination |
|---|---|
| `TextDelta` | the existing token stream (SSE) |
| `AssistantText` | the assistant message body for the round |
| `ToolUse` / `ToolResult` | the tool message + timeline event `execute_code` produces today |
| `TurnCompleted` | the round's final `AIMessage` (`text`), plus usage/cost |
| `RateLimitObserved` | the usage signal behind the quota warning |
| `SessionStarted` / `SessionBound` | binds the incarnation to the session id and the agent id |
| `HandoffInjected` | the per-wake cost record (size in bytes) |
| `SessionFaulted` | a fatal stream abort: the turn settles, the session is retired |

The harness's tool surface is a real tool call rather than a Python namespace, so
one `ToolUse`/`ToolResult` pair maps onto the existing exec-node message pair, and
`BridgeConfig.tools` must name the same capability set the exec node offers —
otherwise the brain and the hands disagree.

**How cancel is sent.** Today's native cancel path (`agent/ownership/native_cancel.py`)
owns the lifecycle decision; the bridge gets one call from it:

```python
report = await session.interrupt()
if report.settled is not True:
    ...   # residue, or an unobservable CLI — surface it, never assume success
```

`settled is None` (no observable CLI pid) is not a pass; the wiring records it as
unknown. This is the point impersonation used to get wrong, so the assertion lives
in the bridge rather than in a caller's memory.

**How cost is recorded.** `TurnCompleted` carries the per-turn `usage` and
`total_cost_usd`; `SessionTotals` carries the running sum. The wiring writes the
same `llm_usage` row the LangChain path writes (`base/lm/usage.py`), with
`usage_kind="agent"` and `source="claude_sdk"` so the two runtimes stay
comparable. Under OAuth, `total_cost_usd` is the **list price**, not money paid:
the real budget signal is `RateLimitObserved` (`utilization`, `resets_at`), which
the wiring feeds to the quota monitor instead of adding dollars.

**Where it runs.** Whichever machine holds the Claude subscription login — today
macmini. That is a **credential constraint, not a capacity constraint**: the
executing machine is chosen by where `auth status` reports `authMethod:
claude.ai`. An agent on another machine reaches this runtime by having the turn
executed on the login machine.

## 14. Consequences and open questions

- **Cold start**: a fresh CLI process costs ~2–4s before the first token. The
  session is long-lived per agent incarnation, with `resume` as the restart story
  — not one process per turn.
- **Context ownership**: the harness owns the transcript of a resumed session; the
  graph owns `state.messages`. The first wiring change must pick one owner
  (recommended: the harness transcript, with the graph's window built from the
  bridge's events) or the two histories drift.
- **Tool surface**: `BridgeConfig.tools` is a hard allowlist wider than
  `execute_code`'s single tool; the system prompt and the allowlist change
  together.
- **Permission mode** is the harness's own (`bypassPermissions` by default)
  because there is no bridge-level approval layer. A deployment that wants a
  narrower mode sets `permission_mode` and accepts that headless prompts are
  denied rather than answered.
- **Orphaned CLI on a hard kill**: `close()` reaps the CLI, but a supervisor killed
  with SIGKILL leaves the CLI reparented to init and still holding the session.
  The supervisor must sweep leftover `claude` children on agent restart, keyed by
  the session id in the registry, before starting a new session.

## 15. Measured: Antigravity's cancel semantics (2026-10-19, agy 1.4.2)

**SIGINT stops the CLI, not its tools.** Measured: after an interrupt, `agy` exits
while the `bash -c 'sleep 90'` and `sleep 90` it spawned are still alive eight
seconds later. This is a different path from the normal turn end, where the CLI
does clean up after itself; only cancellation leaves the tree behind.

**The adapter reaps, not the caller.** Three reasons, and they hold for every
harness: only the adapter knows the descendant tree at the moment of
cancellation; the turn is already over, so the model cannot track those processes
any more; and the four adapters must be interchangeable, so one that reaps and
one that does not would make the same contract mean two different things. The
sweep is unconditional rather than switchable — a switch is exactly where this
bug would live again.

**Report honestly.** `settled=True` only when nothing survived. Survivors set
`settled=False` with `remaining` naming them; a clean cancellation is never
claimed on a partial sweep.

**Resume re-injection was a real bug.** A takeover resumed with
`--conversation <id>` kept its history, but the explicit `start(resume=...)` path
never set `resumed`, so the handoff was injected a second time — invariant 7
broken. The registry path covered it; the explicit path did not. Regression test
added. The lesson generalises: "resume does not replay history" is a property of
the runtime, while "we do not re-inject" is a property of *every* resume entry
point in our code, and only tests can hold the second one.

## 16. External processes and the SDK: `attach` (2026-10-19)

**The problem.** A harness runs the agent loop in its own process. The handoff
document is not enough: that process needs the SDK, and a bare
`python -c "import meiva; meiva.tasks.get(...)"` raises
`RuntimeError: event emission requires an initialized process pipeline or
producer`. Measured both inside the dsh sandbox and on the host, so it is not a
sandbox property: `meiva.*` needs an initialized process context.

**The seam already exists.** `meiva.impersonation.attach(session_id,
agent_id=...)` is a context manager that boots config, machine, database and
client set, binds a borrowed `MeivaContext`, initializes the telemetry pipeline
and the SDK state, gates every SDK call, and flushes plugin state on exit.
`meiva impersonate exec --file` is the CLI wrapper around it. A harness needs the
same thing with its own identity.

**Shape.** `meiva.attach(handle=None)` — renamed from
`meiva.impersonation.harness.attach` by a user ruling on 2026-10-19: the capability
is "bind this external process to its Meiva execution identity", shared by every
harness and not an impersonation concept. It returns the same object (`flush` /
`close` / `native_snapshot`, usable as a context manager) and reads
`MEIVA_HARNESS_HANDLE` when called with no argument. The low-level constructor
`meiva.attach.open_attachment(handle, *, agent_id, session_id, store)` remains for
the controller-facing CLI (`meiva harness exec`), which validates the row itself.

**The handle.** An opaque `hx_<random>` string whose SHA-256 the registry stores;
the bridge passes the plain value to the harness child through
`MEIVA_HARNESS_HANDLE`. Deliberately **not** a workspace file: the sandbox can
read everything and write only the workspace, so a file would hand the token to
the agent it authorises. Validation is threefold on every SDK call — the row is
active and unexpired, the machine matches, and the calling process descends from
the recorded controller pid (the harness analogue of the takeover's
process-tree rule).

**What an attached harness agent may do.** The same as a native agent: there is
no capability gate. The user ruled on 2026-10-19 that a stored declaration is a
record, never a check, and the takeover path's gate was reverted; giving the
harness path the same abstraction under a new name would restore what was
removed. Admission therefore keeps only the two rejections main already applies
to a borrowed identity — the registration is no longer attached or has expired,
and the attachment is closed — and they live in one place (`admit_sdk_call`),
not in a per-call capability table.

Lifecycle self-calls are a **convention, not a rule**: `compact`, `restart` and
`terminate` belong to the bridge that owns the session, and adapter authors
should not issue them. What enforces that is not our code but the SDK's own
`assert_self_action`, which requires the caller to own the agent's turn loop — a
provider harness process never does, so the call is refused on that ground.
Spawning agents, scheduling and spending on `understand` are likewise review
conventions for adapter authors, not runtime boundaries.

**Why not MCP, and why not a sidecar.** MCP would add a process, a tool
description and a second interaction model for the same capability, and the
sidecar bridge adds a service, an auth scheme and its own version surface. The
kernel gives the harness the same `meiva.*` calls the native agent makes, with
the same gate and the same telemetry, which is the point of having one logic with
two entry points.
