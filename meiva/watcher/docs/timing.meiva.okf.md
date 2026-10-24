---
type: doc
title: Watcher time snapshots
description: Relative watcher deadlines and session TTL use one captured time snapshot.
tags:
- sdk
- agent-lifecycle
---

# Watcher time snapshots

Relative `at(when=timedelta(...))` and `cron(end_time=timedelta(...))` resolve against the
single UTC snapshot captured by their SDK call. Their generated absolute deadline and
session TTL use that same snapshot; the shared normalization/cron-validation helpers
receive it as a required value. A zero relative `at` is immediate and retains its delivery
grace; a zero relative cron has no future session lifetime and is refused by the existing
TTL guard. Generated watcher processes still read the live clock while waiting and firing.

Elapsed durations normalize the captured aware time to UTC before addition, so a relative
wait spans the requested elapsed time across daylight-saving transitions. A naive snapshot
is rejected for relative inputs; absolute datetime and ISO inputs keep their existing
timezone requirements.

## Cron lifetime

`end_time` sets expiry; it **defaults to now + 7 days** (standing cap, task #2617) —
a longer schedule passes an explicit `end_time`. Calling `cron()` again with the same
expression + timezone does **not** replace or renew anything — it starts another,
independent session; kill the old one yourself (`meiva.shell.sessions.kill`) if you
don't want both running.
