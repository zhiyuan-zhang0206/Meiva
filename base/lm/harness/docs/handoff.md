# The handoff document

Part of [Provider harness](harness.md).


The handoff is a wire format, not an internal dataclass: the waking agent reads
it as the first message of a fresh session, and its rendered size is a per-wake
cost the host reports (`Handoff.size_bytes()`, and a `HandoffInjected` event).

Section order is fixed — identity, cluster, entry points, boundaries, plugin sections,
reporting, instructions — because an agent that skims it still meets the instructions last.
`HANDOFF_VERSION` pins the dataclass field set across adapters (3: `home` and `sections` were added).

Section 3 names where to start, not an inventory: `DEFAULT_ENTRIES` holds the
`meiva` entry — which attaches this session's identity on first use — plus
`meiva.agents` and `meiva.self` (`pause_heartbeat`, `terminate`), while plugins
contribute their own cluster namespaces (`meiva.tasks`, `meiva.memory`,
`meiva.schedules`). External harnesses use their own native file, shell, search,
and in-turn background tools, so `meiva.files` is not exposed in `DEFAULT_ENTRIES`.
Listing every namespace there would become a second copy of `meiva.help()` that
drifts from the SDK; the entry point and its pointer are enough to start.

## Plugin sections

A plugin that has facts an external agent needs in a section of their own, beyond a one-line entry
point or rule, contributes a `HarnessContribution.sections` builder: a function from the `Handoff`
being built to a `HandoffSection(title, lines)`, run by `Handoff.with_contributions` and rendered
between the boundaries and the reporting rules. It takes the handoff because what such a section
states is mostly absolute paths under this agent's `home` and `workspace`, which only the handoff
knows. The memory plugin uses it for `Meiva Memory`: both memory folders, their indexes and what
each is for. Core `handoff.py` holds no memory knowledge.

## Where the CLI's own tools end

The harness owns the work inside one context; Meiva owns identity, persistence, peers and
the human's attention. Section 4 therefore tells an external agent, by preference and with a
reason for each, to use Meiva rather than the CLI's own feature in two places and to split a
third by purpose. Durable memory (shared and personal memory under `meiva.memory`, contributed
by the memory plugin) replaces the CLI's memory or notes file, since peers and later agents never
read the latter; questions for the user (`meiva.ui.ask`, contributed by the fleet plugin) replace
the CLI's ask-user tool, since a CLI prompt never reaches the person in Meiva. Delegation is split
by purpose, in two lines of the default section: a one-off delegation within a turn, whose result
returns to the current context (searching code, reading a batch of files, checking several things
in parallel), uses the CLI's own subagent or task tool; work that needs its own identity, should
keep going, or should be seen and talked to by people or other agents uses a Meiva agent
(`meiva.agents.spawn`, `meiva.agents.send_message`). A CLI subagent's text and tool calls do
appear on the timeline, as work detail under the call that started it
([sub-agent text](subagent-text.md)), but it has no agent id or inbox, nothing can message it, and
it ends with the session. A Meiva agent is a long-lived peer and does not notify whoever spawned
it when it finishes; its result arrives only if it messages them.
The same section says the CLI's own shell, background commands, monitors and timers stay in
use. This is prompt guidance only: no CLI tool is disabled and no CLI system prompt is
replaced, so the agent can still judge the edge cases. A guidance line lives with the plugin
that owns the entry it names, so it renders only when that entry does.

## The saved copy and compaction

An external CLI compacts its own context, and a compaction can summarize the first
message, standing instructions included. So the host also saves the document it is
about to send as `meiva_instructions.md` directly under the agent's workspace
(`INSTRUCTIONS_FILENAME`, `instructions_path`), and the message ends with a pointer
that names the file by absolute path and says to read it first when the instructions
can no longer be seen in full. The pointer is tool-neutral wording and renders only
when `Handoff.reference_file` is set.

- **What the file holds:** the whole rendered document without the pointer — identity,
  cluster, SDK entry points, boundaries, reporting and the standing instructions — since
  every section stays true after a compaction and the first message and the file never
  disagree. Nothing first-turn-only is in it: the inbound tail is not part of the document.
- **Name and place:** a Meiva-named file at the workspace top level, not `CLAUDE.md`,
  `AGENTS.md` or `GEMINI.md`, and not inside `.claude/`, `.agents/` or any directory a CLI
  loads as project configuration, so no CLI injects it twice or shadows a repository's or
  a user's own file.
- **When it is written:** each time the graph builds a handoff (a fresh start, a harness
  switch, a restart that rebuilds the session), by the runner on the agent's machine,
  atomically, replacing the previous content. A warm session builds none, so its file keeps
  matching what that session was told.
- **A failed write** (`OSError`) logs a `handoff_instructions_file_failed` warning and builds
  the handoff without `reference_file`: the message then makes no claim about the file and the
  compaction reminder below is not sent. The file is a safety net for the agent, not input to
  the turn, so it does not fail the wake.
- **Observing compaction:** the Claude adapter maps the CLI's `compact_boundary` system
  message and the Codex adapter maps a completed `contextCompaction` item to
  `ContextCompacted`. Antigravity's stream-json output carries no compaction signal, so it
  emits none. `HarnessBridge` remembers the event and puts `compaction_reminder(path)` in
  front of the next batch's first input; it is not part of the mid-turn
  delivery path, and a turn that carries the full handoff needs none. A compaction the CLI
  runs outside a turn this session is running is not seen.

Changing the field set or the section order means bumping the version.
`tests/test_handoff.py` pins both: the field tuple is compared against
`PINNED_HANDOFF_FIELDS` and the version against a literal, so the edit that
changes the shape fails until the version moves with it.

Completed-key replay preserves the original reply and usage but sets
`HarnessTurn.replayed`; the graph does not log another provider charge for it.
This cache is local to an adapter session, not durable across session retirement.
