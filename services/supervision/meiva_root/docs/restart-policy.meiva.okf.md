---
type: doc
title: Meiva Root — restart policy, backoff and circuit breaker
description: How root spaces replacements of a dying unit, when its circuit breaker opens, and what closes it again.
tags: [services, lifecycle]
---

# Automatic restart policy

Each ordinary health round can restart a DOWN unit once, and consecutive
attempts are spaced by one ladder (`HealthConfig.restart_backoff_s`, default
60s → 5m → 15m). The first replacement of a fresh outage stays immediate — a
crashed unit is replaced promptly — and only a unit that keeps dying waits:
attempt *n* is due `ladder[n-2]` after the previous one, capped at the last
entry. Three actual attempts exhaust the continuous-outage budget and open the
unit's **circuit breaker**: from then on only the ladder's capped cadence (15m)
replaces it, and the per-round failure-state signal reports it as
`restarts_exhausted` (the alert rule also accepts historical `breaker_open`).

An open breaker closes on the first fresh ALIVE observation — of any generation,
including an operator's `up` — or on an observed operator stop. It also closes
through the **dependency-recovery path**: while any unit's breaker is open, the
monitor makes one bounded `SELECT 1` against the cluster database per round, and
a database it observed unavailable and now sees answering pulls the next attempt
forward immediately instead of waiting out the 15-minute cap. That is the path
the 2026-10-18 outage needed: the database stalled, four machines' `agent_host`
exited on ownership-renewal failures, each spent its restarts in minutes, and
nothing brought them back after the database recovered (task #5312/#5315). A
database never observed failing never closes a breaker, so an unrelated crash
loop keeps the slow cadence instead of restarting every round.

`root_unit_breaker_open` (attempts, last error) and `root_unit_breaker_closed`
(time held, last error) mark the two transitions once each; the sustained
condition remains `root_unit_failure_state`, which the alert rule debounces.
Both events carry the machine in the log envelope.

The next ordinary round observes readiness: spawn acceptance is not ALIVE. There
is still no confirmation poll and no failure threshold across units.
Generation startup grace remains.

ALIVE or an observed operator stop resets the count and the pending delay.
UNKNOWN, foreign owners, NEVER policy, probe-only units and maintenance holds
neither spend nor replenish it. A `CODE_BEHIND_MINIMUM_EXIT_CODE` exit requires
updating the host; root warns without restarting it. The count is process-local
and resets across root restarts. Explicit operator up/restart retains authority.
Service restart never authorizes replaying agent execution or UNKNOWN work.
