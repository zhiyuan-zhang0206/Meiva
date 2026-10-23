---
type: doc
title: Completion digest rounds
description: Hourly durable digest windows and the heartbeat loop that flushes them.
tags:
- heartbeat
- lifecycle
---

# Completion digest rounds

Background-command and watcher completion events wait in PostgreSQL until their
hour has ended. `CompletionDigestRound` owns the pool, lazy database, live bus,
machine identity and required Clock for one daemon generation. Each round reads
the clock once and gives the entire flush that same instant, including retention
pruning. Connections belong to the flush transactions; none stays open during
an interval wait.

The frozen `DigestConfig` retains the sixty-second flush interval. The root
constructs the real round and gives it to `completion_digest_loop`, which keeps
the existing shared `run_rounds` policy: flush immediately, wait after a completed
or operationally unavailable round, and propagate an unexpected error or
cancellation to the owning TaskGroup. The dispatcher and liveness loop have
different failure budgets; digest does not acquire their retry policy.

Delivery still uses the actual shared chat-inbound operation, with the stable
`completion-digest:<agent>:<hour>` receipt key. Durable delivery precedes marking
the authoritative completion rows. A restart after delivery but before marking
recovers the same inbound rather than delivering twice. Unknown failures end the
round; later agents remain pending. System notices retain their existing policy
against resurrecting terminated owners.

Tests use a complete scripted round for pacing and immediate failure, and actual
PostgreSQL delivery for event marking, receipt recovery and hourly boundaries.
The delivery failure scenario installs a trigger only for its blocked agent and
removes the trigger and function in a finally block, including setup failure.
Its real transaction error must propagate while both agents' completion rows
remain unmarked. Manual wall time proves the current hour waits until its end
and a repeated completed window still produces one inbound.
