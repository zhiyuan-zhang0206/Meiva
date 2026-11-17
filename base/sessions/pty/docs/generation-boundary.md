# PTY allocation generation boundary

## Desired-state effect

A successful `meiva pty freeze` changes the desired-state generation at the
freeze acknowledgement, not at `resume`. Allocation itself does not directly
kill a PTY, but reconcilers apply the boundary on their next pass:

- ScheduleManager reaps every earlier-generation schedule PTY on its next tick
  and records an open schedule run as `interrupted`. An enabled schedule remains
  current desired state and can later be rebuilt under the new generation.
- Ordinary shell sessions keep their TTL and explicit lifecycle controls.

An inspection freeze has the same boundary semantics. Fence reconcilers before
freezing when an operation requires selective cleanup rather than this complete
generation sweep.

## Corrupt marker repair

A corrupt allocation marker fails closed. Never delete it: absence changes the
current generation to `None`, which can make reconciliation classify every
generation-bound desired record as superseded. Recover the original generation
UUID from a known-live session (the service's `list` reports each session's
generation), rebuild a valid marker with that exact UUID, and then permit
reconciliation. If no session establishes the UUID, leave allocation
fail-closed until an operator makes the boundary explicit.

Operator commands and their coordinated cleanup entry are in [PTY allocation operations](operations.md).
