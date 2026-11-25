# Native epoch slice

The built-in harness keeps one conversation per agent in its checkpoint, whatever harness ran in between. An agent that ran as `meiva`, then on a CLI, then as `meiva` again has two native stretches that share one checkpoint, so a reader cannot tell them apart by position: `state.messages` resets at a compact, and nothing in it names the stretch. The slice tells them apart by time.

A stretch is the half-open interval `[started_at, ended_at)` of its epoch row. A rendered item belongs to the stretch that holds its `created_at`. An item without one (the system prompt and the standing head notes carry none) takes the time of the item before it, or of the next one when none comes before; a segment with no time at all belongs to the first native stretch. The first native stretch has no lower bound.

An agent with one native stretch has nothing to tell apart, so it gets no slice and its reads are unchanged.

Message times come from the host that ran the agent and epoch times from the database, so an item written within the clock skew of a switch can land on either side of it. A switch happens while the agent is idle, so the items near its boundary are the switch note itself and nothing a reader needs.

The gateway reads every native stretch through the slice, and the agent builds its live snapshots through the same function, so the cold read and the live view agree on what the current stretch holds.
