---
type: doc
title: The agent a schedule speaks as
description: Why a schedule carries `owner_agent_id`, why it must be written in two places (the launch environment and the runner's bound identity), and what it deliberately does not grant.
tags: [gateway, schedules]
---

# The agent a schedule speaks as

`schedules.owner_agent_id` names the agent a schedule acts for; NULL is an
operator-created schedule that only runs a command (a backup, a sweep, a probe).

The value must reach **two** places, because a schedule's script runs in one of two
ways:

- **A `.py` command** is materialized under `$MEIVA_HOME/schedules/<id>/` and run
  in-process with `runpy`. Its identity is the `MeivaContext` the runner binds
  (`runner._bind_schedule_actor`) — the launch environment is never read, so writing
  `MEIVA_AGENT_ID` alone changes nothing for it.
- **Any other command** (`bash run.sh`, `curl …`) runs as a child process and takes
  its identity from the environment, so `_launch` writes `MEIVA_AGENT_ID` beside
  `MEIVA_SCHEDULE_ID`.

Both write the same column, so the two branches cannot disagree. A NULL owner omits
`MEIVA_AGENT_ID` entirely rather than writing an empty value: absence means "no
agent", and `""` would be a third state that never means anything.

`MEIVA_SCHEDULE_ID` stays in the environment in both cases. `require_actor()`
derives the provenance principal `schedule:<id>` from the bound identity, which is
what a message this schedule sends records as its `source` — so a write is traceable
to the schedule whether or not it has an owner.

## What an owner does not grant

The session already holds the cluster's agent API token (it is in a schedule
process's environment today), so an owner adds **no permission**: this column
decides *whose* identity the schedule speaks with, not *what* it may do. Anyone
reading it as an authorization boundary has misread it; narrowing what a schedule
may do belongs to the restricted-credential work.

An owned schedule is a script carrying an agent's identity, not that agent's turn
path, so its identity binds `owns_loop = False` (the same posture a shell session's
child script gets). It therefore cannot `meiva.self.compact`, `restart` or
`terminate` the agent it speaks for — the refusal is explicit, and a test pins all
three.

## Run history

`schedule_runs.agent_id` is the owner at the moment the run opened, written in the
same statement that inserts the row, so "which run" and "for whom" never drift
apart. The column existed reserved for this since the run history was added.
