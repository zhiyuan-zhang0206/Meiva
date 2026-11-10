# A harness switch keeps the session records

Supersedes the "old harness's session record is removed" step of
[an agent restarts itself, and switches harness, at the end of its turn](2026-11-11-self-restart-at-turn-end.md)
and the same deletion in the operator restart route.

## Context

An external CLI (Claude Code, Codex, Antigravity) keeps the conversation in its own storage; Meiva
keeps one pointer from the agent to that session. The pointer was a single document per agent and a
switch deleted it, so the transcript stayed on disk but could never be found again. The project
owner's ruling: a switch must not throw conversations away.

## Decision

- The registry keeps one record per (agent, harness): `<agent_id>.<harness>.json`. An adapter loads
  and saves only its own harness's record.
- No switch path deletes a record: not the operator route, not the host's apply of a self-restart,
  not `prepare-harness-switch`. A restart that keeps the harness keeps its record too, so the
  operator route no longer reopens a fresh session on a same-harness restart.
- Switching to a harness that has a record resumes that session, through the ordinary resume path:
  the switch note (`[Harness switched from 'a' to 'b']` and the reason) is delivered as the first
  input, the handoff is not repeated (the saved instructions file and the compaction reminder
  already cover instructions the CLI lost), and a failed resume fails fast like any other.
- Moving an agent to another machine still deletes the agent's records on the machine it leaves
  (`delete(agent_id)` removes all of them): the transfer carries one session, the most recently
  updated one.
- Native history lives in the database checkpoint and is untouched; no switch to or from the native
  harness removes a record.

## Data migration

This change has a data migration. Session records are host-local files, so it is not SQL: the
`harness session records layout` step of host converge, which `meiva start` runs before any service
of the upgraded host starts, converts each `<agent_id>.json` to `<agent_id>.<harness>.json` (write,
then remove the old file), names a record without a harness field `meiva`, is idempotent, and prints
how many records it moved. The registry has no read path for the old layout: a load that meets an
unconverted file refuses and names it. To confirm it ran, `$MEIVA_HOME/harness/sessions` holds no
file named `<digits>.json`.

## Consequences

- Transcripts are never deleted by Meiva on a switch, restart or transfer: the code removes registry
  pointers only, never files under a CLI's own directories.
- There is no interface for "start this harness over with a fresh session". Terminating an agent
  does not remove records either. Both are left for a separate decision.
