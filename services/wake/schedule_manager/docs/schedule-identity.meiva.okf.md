---
type: doc
title: The agent a schedule speaks as
description: Why a schedule carries `owner_agent_id`, why it must be written in two places (the launch environment and the runner's bound identity), and what it deliberately does not grant.
tags: [gateway, schedules]
---

# The agent a schedule speaks as

`schedules.owner_agent_id` names the agent a schedule acts for; NULL is an
operator-created schedule that only runs a command (a backup, a sweep, a probe).

## How the value is assigned

Provisioning, never the API. `POST` and `PUT /api/schedules` refuse the field with
a 422 that names the operator path, an explicit null included — clearing an owner is
as much an assignment as naming one. The reason is that the gateway cannot tell its
callers apart (one shared administrator credential; caller labels are deliberately
absent), so an exposed owner field would let any caller make an unattended script
write under another agent's name.

`scripts/host_ops/set_schedule_owner.py` is that path. It runs on the gateway host
against the home's database, requires `--reason`, and writes the `schedules` row and
a `schedule_owner_changes` row in one transaction: old owner, new owner,
`ops:<hostname>`, the reason and the time. That row is the only audit an assignment
will ever have.

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
child script gets). That refuses `meiva.self.compact`, `meiva.self.restart` and
`meiva.self.terminate` — the three calls that end or reshape the loop the identity
belongs to — and a test pins all three.

It does **not** reach the cross-agent calls. `meiva.agents.terminate(<owner>)` is an
ordinary peer action and stays available, exactly as it is for any process holding
the cluster token. Reads that treat `owns_loop = False` as "this schedule cannot
touch its owner" overstate it.

## Run history

`schedule_runs.agent_id` is the owner at the moment the run opened, written in the
same statement that inserts the row, so "which run" and "for whom" never drift
apart. The column existed reserved for this since the run history was added.
