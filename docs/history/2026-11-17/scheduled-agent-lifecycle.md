# Scheduled agent lifecycle

Added one-shot appointments for transfer and restart to let agents arrange a
future quiescent attempt while finishing their current turn. Gateway schedules
own these appointments independently of the initiating agent's lifetime.

The agreed design uses a single strict attempt and initiator-routed notices.
Polling grace queues, inbox locks and speculative post-launch harness rollback
were rejected: the actors coordinate retries through their normal inboxes.
Predictable harness and model failures are rejected before source shutdown.


## Review update

Actual-fire review found that ordinary same-harness restart short-circuits a
terminated target. Scheduled restarts now use a dedicated runner action and the
existing resurrection flow, retaining the ordinary synchronous contract. The
strict check also runs under the termination row lock before any host stop;
a deterministic claim-race test verifies that a winning claim prevents the
restart mutation and all host settlement calls.
