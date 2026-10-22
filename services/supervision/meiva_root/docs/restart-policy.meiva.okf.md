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
`kind=breaker_open` (the alert rule also accepts the historical
`restarts_exhausted`). The breaker outranks a recorded replacement failure in that
signal: it is the state an operator must act on, and the only one whose alert
reaches a machine whose database is down.

An open breaker closes on the first fresh ALIVE observation — of any generation,
including an operator's `up` — or on an observed operator stop. While it is open,
the **dependency-recovery path** may replace the unit early: the monitor makes one
bounded `SELECT 1` against the cluster database per round (only while an open
breaker belongs to a unit that is not held down), and a usable database grants the
unit one immediate attempt: on the first observation, which cannot distinguish a
recovered database from one that never failed, and again on every
unavailable-to-answering edge. The grant is a latch, not a round's flag: a round
that cannot act on it (a terminal verdict, a starting generation's grace, a
maintenance gate) leaves it unspent, so a database that recovers inside one of
those rounds still restarts the unit at the first moment it may. A database
observed failing withdraws an unspent grant; it never closes the breaker itself.

That is the path the 2026-10-18 outage needed: the database stalled, four
machines' `agent_host` exited on ownership-renewal failures, each spent its
restarts in minutes, and nothing brought them back after the database recovered
(task #5312/#5315). An unrelated crash loop whose database is healthy pays one
extra attempt and then keeps the slow cadence, instead of restarting every round.

`root_unit_breaker_open` (attempts, last error) and `root_unit_breaker_closed`
(time held, last error) mark the two transitions once each; the sustained
condition remains `root_unit_failure_state`, which the alert rule debounces.
Both events carry the machine in the log envelope.

The next ordinary round observes readiness: spawn acceptance is not ALIVE. There
is still no confirmation poll and no failure threshold across units.
Generation startup grace remains.

ALIVE or an observed operator stop resets the count, the pending delay and any
unspent grant.
UNKNOWN, foreign owners, NEVER policy and probe-only units
neither spend nor replenish it. A `CODE_BEHIND_MINIMUM_EXIT_CODE` exit requires
updating the host; root warns without restarting it. The count is process-local
and resets across root restarts. Explicit operator up/restart retains authority.
Service restart never authorizes replaying agent execution or UNKNOWN work.

## A wrapped launch that dies early

Deployment wiring may install one opaque argv transform (`set_argv_transform`);
the generic root names no platform and reads no ambient configuration to decide.
The rewrite is a preference, not a requirement: a launcher that refuses the
transformed argv costs one failed attempt, after which the manifest's argv is
tried unchanged and the unit runs unbounded rather than missing.

A wrapper that *starts* and then dies is invisible as a launch failure — the
generation ends like any other, and the ladder above would replay the same
rewrite forever. A rewritten generation that exits with a positive code inside
ten seconds therefore retires the rewrite for the rest of this root's life, and
the warning says so: memory cap not applied, the unit runs unbounded. Negative
codes are signal deaths, so an ordinary stop never costs the cap. Each unit's
status row reports `launch_rewrite` (`on` | `off` | `off(retired)`).
