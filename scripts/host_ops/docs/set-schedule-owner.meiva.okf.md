---
type: doc
title: Assigning a schedule's owner
description: Why `schedules.owner_agent_id` is set only from an operator host, what the tool writes beside it, and what the audit row is for.
tags: [schedules, operations]
---

# Assigning a schedule's owner

`schedules.owner_agent_id` decides which agent a schedule speaks as: the runner binds that identity
into the launched process, and every write the schedule makes carries it. It is a provisioning fact,
not a setting a client may change.

The HTTP API refuses the field on create and update, an explicit null included. The gateway cannot
tell its callers apart — one shared administrator credential, caller labels deliberately absent — so
an exposed owner field would let any caller make an unattended script write under another agent's
name. Whoever runs this tool is on the gateway host with the home's database, which is the only place
that distinction exists.

## What the tool does

```
python scripts/host_ops/set_schedule_owner.py <schedule-id> --owner <agent-id> --reason "<why>"
python scripts/host_ops/set_schedule_owner.py <schedule-id> --clear --reason "<why>"
```

One transaction writes both rows:

- `schedules.owner_agent_id` — the current value, `NULL` when cleared;
- `schedule_owner_changes` — the old value, the new value, `ops:<hostname>`, the reason, and the time.

`--reason` is required and must survive `strip()`: it is the only evidence an assignment will ever
have. An unchanged owner writes nothing and reports `unchanged`. An unknown agent id stops before any
row changes. `--dry-run` prints the change and rolls back.

## What an owner does not grant

The session already holds the cluster's agent API token, so an owner adds no permission. A schedule
process cannot end, restart or compact its own loop (`owns_loop = false` refuses `meiva.self.*`); the
cross-agent calls stay available to it exactly as they are to any process holding the token. Read the
column as "who this schedule speaks for", never as an authorization boundary.
