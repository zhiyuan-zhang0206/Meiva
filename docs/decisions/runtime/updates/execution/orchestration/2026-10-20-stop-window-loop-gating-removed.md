# Resident loops no longer gate on a stop window

## Context

The 2026-09-12 PgBouncer stop hang came from paused runner processes that stayed
alive and kept idle pooled clients on PgBouncer, so its shutdown waited for
clients that never left. The stop-window contract answered with a database-quiet
window, and the 2026-10-02 decision extended it to every resident loop: the shared
round loop and about thirty hand-written sites consulted `admission.quiesced()`,
and `lint_quiesced_loops.py` enforced it.

The same incident has two other fixes that hold without the window: PgBouncer is
stopped with SIGINT, which disconnects clients and waits only for in-flight
queries, and the stop orders services before the data plane, so every pool dies
with its process before the data-plane phase begins.

## Decision

Remove the quiesced window as a loop-gating mechanism: `admission.quiesced()`, the
gate in `run_rounds`, every hand-written gate, the lint and its
`# quiesce-exempt:` markers. A loop does its work every round; the stop ordering
and the PgBouncer stop mode are what keep a stop from hanging.

## Alternatives rejected

- Keep the lint and only drop the gates: the lint exists solely to require them.
- Keep the gates until the maintenance hold itself is removed: the gates add
  about thirty places that must be read when the hold goes, and they protect
  nothing the two surviving fixes do not already protect.

## Consequences

- Between a completed drain and the services stop, loops keep doing database work
  against a live data plane; that work is harmless.
- A service kept running across a stop with its data plane stopped is refused by
  the data-plane stop, not by the loop.
- Event-driven acceptance gates (`business_paused`) are separate and unchanged.

Forward link (2026-10-20): the `business_paused` gate left unchanged here is deleted with the
hold. See [delete the maintenance hold](../../../processes/2026-10-20-delete-maintenance-hold.md).
