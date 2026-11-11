# Neighbors — Inter-Agent Communication and Discovery

## Responsibility

Inter-agent interaction is the communication layer of the fleet — how agents discover each other, send messages, and check status. Mechanically there is no relay chain: any agent can directly contact any other agent. But reporting to humans has direction (#620 dichotomy): authorization/decisions go directly to the user; progress/conclusions roll up to the delegator along the delegation tree (see [the interrupt discipline skill](../../skills/reduce-context-switch-for-human/README.md)).

## Relationship to Other Subsystems

- [Spawn](../spawn.md) — spawn/fork/resurrect establish ties
- [Tasks](../tasks/tasks.md) — system-note notification (NoteTag `task`) on task owner change
- [meiva.self](../../../../../meiva/self/README.md) — discover peers via label
- [Meiva Fleet](../meiva_fleet.md) — overview
