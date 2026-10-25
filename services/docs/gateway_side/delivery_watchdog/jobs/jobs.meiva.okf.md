---
type: doc
title: Delivery Watchdog Jobs
description: Delivery observation, the stranded-wake fallback and stale-input retirement.
tags: []
---

# Delivery watchdog jobs

One resident loop alerts on stale pending delivery and retires stale claimed
or terminated-owner inputs to `done`; a second, independent loop runs the
stranded-wake fallback. A running turn may legitimately hold a queue behind it.
Consuming a stale row does not certify successful execution.

The fallback (`stranded_wake.py`) covers the one order the delivery path cannot:
a message that commits while its owner still reads `running` and that the owner
never claims before dying. While a terminated owner holds pending work that
passes the resurrection fence (`TRIGGER_AFTER_KILL_COMMAND` — newer than every
kill command, and either newer than the death or from a life a command ended),
the loop runs the delivery path's own wake, `ops.lifecycle.resurrect_if_terminated`.
It is never primary: a message written to an already-terminated owner is woken at
delivery time. One attempt per owner per cooldown, at most
`MEIVA_DELIVERY_WATCHDOG_STRANDED_WAKE_MAX_PER_ROUND` owners per round, and past
the dead-letter threshold the row is retired instead. The recovery breaker and an
active suppression window exclude an owner, and the cooldown never replaces the
until-human halt. See
[the decision](../../../../../docs/decisions/agents/messages/2026-10-25-stranded-inbound-wake-fallback.md).

There are no wake re-dispatch, poison budgets, persistent alert dedup, harvest or
wedged-turn recovery jobs. Healthy hosts discover fresh pending inputs through
their ordinary polling and claim CAS. Message senders retain finite same-key
retries and the ordinary delivery-time attempt to wake a terminated agent. Task
notices retain their ordinary enqueue and best-effort wake; see
[[task-notifications.meiva.okf.md]].
