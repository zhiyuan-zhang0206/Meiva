# Forking an external harness agent

Part of [Provider harness](harness.md). Decision record:
[external harness fork](../../../../docs/decisions/agents/graph/2026-11-11-external-harness-fork.md).

`meiva.agents.spawn(fork_from=...)` works for `claude_code` and `codex` agents when the child is
born on the source's machine. The child inherits the source's CLI conversation, so it neither
re-explores the context nor rereads it at full price (Claude Code) or at full price on the first
call only (Codex).

## Who clones, and when

The birth transaction cannot clone a CLI session: the session is a file or process on the agent's
machine. So birth only records the intent and the clone happens on the child's **first session
start**, in the runner that hosts it:

1. Birth (gateway, one transaction) writes the child's `agents_meta` row with
   `fork_source_agent_id`, inserts the `kind='fork'` inbound (as for a native fork), and copies the
   source's `agent_harness_messages` rows to the child (`copy_harness_timeline`): same `seq`, same
   items, `inbound_id` cleared, no `agent_user_messages` (so no unread), and no graph checkpoints
   (the source's graph state holds no conversation and may sit mid-turn).
2. The host reads `fork_source_agent_id` with the stored config and hands it to the bridge through
   `SessionBinding`.
3. `BaseHarnessSession._fork_origin` runs after the registry lookup: the child has no record of its
   own, so it reads the **source's** record from the same `FileSessionRegistry` (same machine by
   construction). No record is `ForkSourceUnavailableError`, surfaced as `HarnessSetupError`; a fork never
   silently becomes a fresh session. A clone counts as a resume, so the handoff is not co-delivered:
   the inherited conversation already holds it.
4. The adapter clones through the CLI's own primitive, then the ordinary resume path takes over.
5. The child's first input is the fork marker, the fork notes and the tail skills (`_handle_fork`),
   appended to the inherited conversation. The marker adds the new workspace path for external
   harnesses.

## Crash safety

| Harness | Primitive | Idempotency |
|---|---|---|
| Claude Code | `resume=<source sid>`, `fork_session=True`, `session_id=uuid5(NAMESPACE, agent_id)` in the same options a session always starts with | The clone's id is fixed by the agent id. The start asks the SDK whether that id has a transcript: present means resume it (a crash after the CLI wrote it, before the registry did), absent means fork. Starting a fork under an existing id is refused by the CLI ("Session ID ... is already in use"), so two clones cannot exist. |
| Codex | `thread/fork {threadId, cwd, excludeTurns}` then the normal `thread/resume` | The new thread id is registered as soon as the app-server answers. A crash between the answer and the write leaves one thread nobody resumes; the next start clones again and registers that one. The orphan costs disk only and is accepted because Codex offers no way to name the clone in advance. |

## Cache and cost

Measured on the real CLIs (Claude Code 2.1.301, Haiku 4.5; Codex per the earlier experiment):

- Claude Code: the first call of a fork reads the source's whole prefix from cache, also across
  working directories, with the default minimal system prompt and with the `claude_code` preset.
- Codex: only the static prefix (about 13-16k of 42k input tokens) hits on the first call; the
  history is billed close to full price once, then cached.

Nothing in the clone's first input may change the prefix: the options come from `option_values` (the
one owner of the session options; only the two fork keys are added), model and system prompt are the
source's, and the overlay rule of a native fork applies: a fork may only add skills, anything else is
`ForkConfigChangeNotAllowed`, which is also why a fork cannot switch model (the cache is per model).

## Working directory

Claude Code files transcripts by working directory, but resuming by session id from another
directory works: the clone is written under the child's own directory, the child's tools run there,
and the CLI appends an `environment` note with the new directory (the model answers with it and
writes relative paths there). Measured 2026-11-11 with sibling non-git directories; evidence is in
the decision record. Codex takes `cwd` on `thread/fork`.

## Where the fork point lands

The fork point is the source's history **when the child first starts**, not the instant of birth.
Turns the source finishes between the two are included. Pinning it would need Meiva to record the
provider's last message uuid (Claude `resume_session_at`) or turn id (Codex `lastTurnId`) per turn,
which it does not. A source fork taken mid-turn carries the unfinished tool call; the CLI treats it
as interrupted, as a native fork does.

## Refusals

| Case | Error |
|---|---|
| Fork names a different harness than its source | `ForkHarnessMismatch`: spawn instead, or restart the source onto that harness and fork there |
| External source on another machine | `ForkHarnessMismatch`: the session is a local file; fork on the source's machine |
| Antigravity source | `ForkHarnessMismatch`: agy has no supported fork entry (the in-process RPC `ForkConversation` is not public and a fork made with it gets no cache hit) |
| Source never started a session here | `ForkSourceUnavailableError` at the child's first start |

## Defaults and assumptions

- **Default machine.** The SDK sends no machine for a fork that names none, and the gateway decides
  (`default_spawn_target`): a fork of a `claude_code` or `codex` agent lands on the **source's**
  machine, the only one it can be born on; a fork of a native agent lands on its spawner's machine
  when the spawner is an agent, else the gateway's, as before. Naming another machine for an external
  source still gets the cross-machine refusal. The gateway also reads the source's harness for a fork
  (`_fork_source_harness`), so an external fork is validated and born on the source's harness even
  when the request names none.
- **Claude home.** The "does the clone already exist" check reads `~/.claude` (or the runner's
  `CLAUDE_CONFIG_DIR`) through the SDK in the runner process, so it assumes the runner and the CLI
  session share that directory, as they do by default. A session given a different `CLAUDE_CONFIG_DIR`
  would not find an existing clone, and the retry would meet the CLI's "already in use" error.
- **Harness switches.** The registry keeps one record per (agent, harness) and a switch deletes none.
  The fork source is read together with the child's stored config and counts only while the source
  runs on the child's current harness (`read_stored_config`); a child switched to another harness has
  no source session to clone and starts fresh there. A fork clones only the source's session on the
  child's current harness, never an older record the source kept on another harness. "Has the child
  its own session" is asked per harness, so a child that switches away and back finds its old record
  and resumes it instead of cloning again.
- **No unread.** The delivery records (`agent_user_messages`) are not copied, and the seen-position
  table has no row for the child, which reads as position 0.
