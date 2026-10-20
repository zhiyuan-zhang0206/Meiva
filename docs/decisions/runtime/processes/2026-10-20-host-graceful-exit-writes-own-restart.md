# A SIGTERMed agent-host writes its running turns' restarts and drains them itself

## Context

The maintenance hold (`base/deploy/maintenance/`, `ops/agent_pause/`) carries one
load-bearing guarantee: every running turn stops at its claim boundary and leaves a
restart pointer in Postgres that the next host's scan wakes. Everything else in it —
the home-local journal, cohort receipts, the admission gates, the quiesced window —
exists to let a command outside the host prove that the host did this.

Without a hold, a SIGTERM became a KeyboardInterrupt and the host cancelled its
turns. A cancelled or crashed turn is not continued: the pending scan wakes only
rows with pending input, a lifecycle pointer, a compact command or an impersonation
lease (it never revives an expired turn), and the next cold build consumes the
claimed chat as an unknown result. So "kill it and let crash recovery continue" is
not equivalent to the hold; only a turn given a restart is continued.

The units were stopped in unit-id order, which happened to put `agent-host` first.

## Decision

1. **The host performs the hold's guarantee on SIGTERM.** Once its dispatcher
   serves, SIGTERM closes the scheduler's admission, writes one `kind='restart'`,
   `source='system:restart'` inbound per agent with a running turn task through the
   ordinary inbound writer, and waits up to `HOST_GRACEFUL_EXIT_DRAIN_S` (30 s, writes included) for
   those turns to settle at it. The existing claim priority, checkpoint flush,
   lifecycle apply and ownership release do the rest; the applied restart's pointer
   is what the next host wakes. Then the ordinary shutdown runs and the process
   exits 0. Idle agents get nothing, and an agent that already has an unfinished
   restart or terminate keeps that one instead of getting a second.
2. **Every running turn is owed a restart, not only admitted ones.** A turn still
   before admission may, once admitted, observe an older restart pointer; a fresh
   restart is the only thing that puts one back. No admission flag is added to the
   host for this.
3. **A drain that times out names each unsettled agent at ERROR and falls back to
   the cancel path.** The restart is already durable, so the next host wakes the
   agent anyway; the cut-short step is lost as in a crash. A crash or SIGKILL writes
   nothing: no restart is fabricated on the crash path.
4. **The host's stop ceiling is a lattice clock.** `HOST_STOP_CEILING_S` (50 s)
   covers the drain plus the cancel unwind, clock read, resource join and release
   (45 s together) and leaves room for the closes without a bound of their own; the
   roster declares it as the agent-host's `stop_ceiling_s`, so root's TERM window
   (ceiling + `STOP_MARGIN_S`, 55 s) is derived from it. The lattice requires both
   sums to stay strictly below their bound. Root caps any unit's window at its 60 s
   health round, which is why the drain is 30 s and not the several minutes a
   hold-driven stop allowed.
5. **The agent-host is stopped explicitly first.** The generated manifest declares it
   first, and both root's whole-tree stop and `meiva stop` go down the units in
   declaration order, one at a time, continuing only after root reports each unit
   exited; its draining turns still call ops and the gateway. The order has that one
   source; startup is concurrent and unaffected.
6. **The hold stays for now.** Both mechanisms work side by side: a stop that runs
   the hold drains turns before the host is signalled, and the host then has nothing
   to drain. Removing the hold is a later change.

## Consequences

- A turn inside a long `execute_code` or model step that outlives the drain is
  cancelled (and, past the ceiling, killed), where a hold-driven stop would have
  failed and waited for an operator. Its next host still wakes it through the
  restart.
- The plugin watcher's self-SIGTERM now runs the same exit, so a plugin reload no
  longer cancels running turns.
- A wake that arrives during the drain stays pending for the next host, as do
  chats arriving while the runner is down.

Forward link (2026-10-20): the lifecycle pointer this decision relies on was removed with the
host's self-fencing. A restart now interrupts what its turn is doing after a grace and resumes an
unfinished turn from a queued note; see
[the restart that interrupts and resumes](2026-10-20-restart-interrupts-after-a-grace-and-resumes-by-a-note.md).
