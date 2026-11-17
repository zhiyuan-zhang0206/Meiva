# The agent a schedule speaks as

`schedules.owner_agent_id` names the agent a schedule acts for. `NULL` names a
system-level schedule.

## Assignment and locality

The declarative SDK and HTTP API accept an owner. An owner supplied without an
explicit machine follows the agent's current machine and places its execution files
under:

`$MEIVA_HOME/workspaces/<owner>/schedules/<schedule>/`

A system schedule uses `$MEIVA_HOME/schedules/<schedule>/`. The same value is stamped
on `schedule_runs.agent_id` when the fire is claimed, so later owner edits cannot
rewrite the attribution or locality of an already-open run.

Owner changes are recorded in `schedule_owner_changes`. API mutations rely on the
ordinary operation receipt and audit surfaces.

## Process identity

Declarative payloads are child processes. Their environment carries
`MEIVA_SCHEDULE_ID` and, when owned, `MEIVA_AGENT_ID`. SDK bootstrap binds one
`MeivaContext` whose actor is `schedule:<id>` and whose optional agent id is the
owner. A script therefore retains schedule provenance while calls that require an
agent identity can attribute to the owner.

An owned schedule is a script carrying an agent's identity, not that agent's turn
path. `owns_loop` is false, so `meiva.self.compact`, `restart`, and `terminate` stay
unavailable. Cross-agent operations remain ordinary peer operations under the
cluster credential.

## Lifecycle policy

`on_owner_terminate='cancel'` is the owner default. When the agent first becomes
terminated, a database lifecycle trigger disables every unfinished matching
schedule, records `cancelled`, and clears its next fire. The schedule manager then
terminates any active no-PTY process and closes its open run. `keep` makes the
schedule independent of owner termination.

## Authority boundary

Owner identity grants no additional permission. Schedule processes already use the
cluster's service credential; the owner determines attribution, placement, workspace
locality, and lifecycle coupling. Capability restriction belongs to the credential
boundary rather than this column.
