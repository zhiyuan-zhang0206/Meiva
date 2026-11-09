# Self — Agent Self-Presentation

## Responsibility

meiva_fleet injects the `set_label` fleet member into `meiva.self` (declared as an `SdkMember` in `plugins/meiva_fleet/plugin.py`) — letting agents report their role to the fleet monitoring view and to their own agent-ID context note. The label that humans see for each agent when scanning the fleet view is provided by this member.

> **Boundary**: The **core** members of `meiva.self` (AGENT_ID / MACHINE_SPEC / pause_heartbeat / compact / restart / terminate) are provided by `meiva/self/__init__.py` and remain even if fleet is disabled — see documentation at [meiva.self](../../../../meiva/self/README.md). Disabling fleet only removes the members below.

## API

### `meiva.self.set_label(text: str) -> None`

Sets the agent's role label — the name displayed in the fleet graph.

- Label should be stable (role name, e.g., "health steward", "Memory pool maintenance"), not a task summary (changing every few minutes) and not a status line (current status goes in replies / task results)
- Task names are legitimate labels for ephemeral workers — a worker's "role" is its task
- Other agents discover and locate peers by label through `get_neighbors`, and read the responsibility chain above you through `get_ancestors`
- Once set, it is not automatically replaced; explicit call needed to change
- The call goes through the gateway (`PATCH /api/agents/{id}`, source `self`), so it needs the
  gateway reachable like every other `meiva.*` call that touches the cluster
- The label is stated in the agent's own agent-ID context note at every window
  establishment — agents read their current label there, and it is how a peer
  renders your role without a getter call

## Data Model

```
agents (table)
├── label         — role label (mutable by the agent itself)
└── label_user_set — whether explicitly set by agent (when TRUE, won't be auto-replaced)
```

(`agent_activity` is retired — no SDK writer since `meiva.self.log` was removed 2026-08-02, no read surface since 2026-10-03; the table's own removal is a separate migration.)

## Dependencies

- `meiva.gateway_client.patch_label` → `PATCH /api/agents/{id}`: the gateway writes the label and
  the sticky bit, records the `label_change` audit fact in the same transaction (attributed to
  `agent:<id>`, so a label set while a lease borrows the agent also lands in the lease's event
  log), and publishes `label_updated` so the fleet monitoring view refreshes

## Relationship to Other Subsystems

- [Ask — durable questions requiring a user response](notify.md) — notify's notices have agent_id as owner
- [Neighbors — Inter-Agent Communication and Discovery](neighbors/neighbors.md) — other agents discover this agent by label
- [meiva_fleet — Fleet Plugin Overview](meiva_fleet.md) — overview
