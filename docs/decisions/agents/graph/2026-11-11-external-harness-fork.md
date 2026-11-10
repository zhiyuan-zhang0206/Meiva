# Forking Claude Code and Codex agents

Component doc: [forking an external harness agent](../../../../base/lm/harness/docs/fork.md).

## Context

A person who raises many things at once either spawns new agents, which must re-explore the context,
or forks, which inherits all of it and (on a provider cache hit) is faster and cheaper. Meiva wants
every collaborator to be a persistent peer, so a fork is the only way to start a persistent peer that
needs no re-exploration. The
[2026-10-19 external harness record](2026-10-19-external-harness.md) assumed an external CLI session
could not be cloned and limited `fork_from` to the native harness. Experiments on the real CLIs
showed both Claude Code and Codex can clone a session.

## Decision

- `fork_from` is allowed for `claude_code` and `codex` sources when the child is born on the same
  machine. Cross-harness, cross-machine and Antigravity forks are refused with errors that name the
  alternative.
- The clone is made by the child's runner at its first session start, not in the birth transaction.
  Claude Code uses `resume + fork_session + session_id` with an id derived from the child's agent id
  (so a retry asks whether the transcript exists and never makes a second clone). Codex uses
  `thread/fork` with `excludeTurns` and accepts an orphan thread after a crash in the one-write
  window.
- The fork point is the source's history when the child first starts. It is not pinned to the birth
  instant: that would need per-turn provider ids Meiva does not record, and recording them touches
  the in-turn delivery path.
- The overlay rule of a native fork is unchanged: only added skills; no model change, because the
  cache is per model.
- The child's timeline starts as a copy of the source's `agent_harness_messages` rows
  (`inbound_id` cleared, no delivery records, so no unread). Graph checkpoints are not copied.

## Measured on the real CLIs (2026-11-11, Claude Code 2.1.301, claude-haiku-4-5)

A source session in directory A read a 77 KB file, then a fork was started from sibling non-git
directory B with `resume=<sid>, fork_session=True, session_id=<fixed uuid>`.

| Question | Result |
|---|---|
| Does the fork start from another directory? | Yes, with both system prompts. The CLI finds the source by id. |
| Where does the clone land? | `~/.claude/projects/<B escaped>/<fixed id>.jsonl`, the child's own project directory. |
| Plain resume of the fixed id in B afterwards | Works; the answer still knew the inherited marker. |
| First-call cache, minimal prompt | source prefix 73,450 tokens (45,022 read + 28,428 created); fork first call read 73,450 and created 905. |
| First-call cache, `claude_code` preset | source prefix 79,321 (25,269 read + 54,052 created); fork first call read 79,321 and created 999. |
| What the fork believes its directory is | B, under both prompts. The CLI appends an `environment` note on resume; asked, the model stated B and a relative `note.txt` landed in B (A unchanged). |
| Same fork arguments again with the fixed id present | The CLI exits with "Session ID ... is already in use", so presence of the transcript is the retry test. |

Same-directory results and Codex results are from the earlier experiments (Codex first call: 13-16k of
about 42k input tokens cached; `thread/fork` accepts `cwd`). Evidence files:
`/private/tmp/claude-501/-Users-zhiyuanzhang--claude-spares-spare-ZsCiLs/d5a5cacf-64b2-4d12-9f37-54d594ce30a8/scratchpad/harness-fork/`
(`xfork.py`, `xfork-none.jsonl`, `xfork-preset.jsonl`).

## Consequences

- Cost to tell callers: a Claude Code fork's first call pays only new tokens; a Codex fork's first
  call is near full price for the history, later calls are cached. This is in the `spawn` docstring.
- A source forked mid-turn carries the unfinished tool call as interrupted (CLI behavior, as native).
- Superseded in the 2026-10-19 record: the statements that an external session cannot be cloned and
  that fork is native-only (§0 table row, §6).

## Manual verification (preview cluster, real CLIs)

1. Start a preview cluster with its own home, data plane, gateway and frontend. Never the production
   home.
2. For each of `claude_code` and `codex`: spawn an agent, have it read a large file, then
   `meiva.agents.spawn(fork_from=<id>, prompt=...)` on the same machine.
3. Check the child's first-turn usage: Claude Code cache read about the size of the source's context;
   Codex cache read only the static prefix on the first turn, more on the second.
4. Tell the source and the child different markers; confirm neither sees the other's.
5. Ask the child for its working directory and have it write a relative file: it must be the child's
   own workspace. Check the child's registry record points at its own session id.
6. Open the child in the UI: the inherited timeline shows, no unread dot, the fork marker follows it.
7. Kill the child's runner between the clone and the first turn and restart: the same single clone is
   resumed (Claude Code: one transcript with the fixed id).
8. Refusals: fork with another harness, fork from another machine, fork an Antigravity agent.
