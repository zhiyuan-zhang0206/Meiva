---
type: doc
title: Readiness verdicts and recovery authority
description: Four evidence states determine whether root may consider replacement.
tags:
- ops
---

# Readiness verdicts and recovery authority

| Verdict | Evidence | Root action |
|---|---|---|
| ALIVE | Owned generation passed its real protocol | Clear the continuous-outage restart budget and close an open breaker. |
| DOWN | Owned protocol failed, or known expected generation/listener is absent | Consider one process restart within intent, startup grace and the backoff ladder. |
| PORT_TAKEN | Another generation owns the responding endpoint | Report; do not kill or spawn into the occupied endpoint. |
| UNAVAILABLE | Inspection, identity, owner status, or probe execution is uncertain | Report; do not infer absence or permission to restart. |

Root's `HealthMonitor` allows three actual restart calls during a continuous
outage, spaced by one ladder (60s, 5m, then 15m); the first replacement of a
fresh outage is immediate. Spending that budget opens the unit's circuit breaker:
later rounds only replace it on the ladder's capped cadence, or on a
dependency-recovery grant — one immediate attempt on the first usable database
the monitor observes (it cannot tell a recovered one from one that never failed)
and again on every unavailable-to-answering edge. That is the breaker that keeps
a fleet-wide dependency outage from becoming a restart storm, and the path that
brings those machines back without an operator (2026-10-18, task #5315). There is
no replacement confirmation loop. A new ALIVE observation or observed operator
stop resets the count and closes the breaker; terminal observations and
maintenance holds do not replenish it. A code-version-gate exit requires updating
the host, so root does not automatically restart that process.

Status and readiness use the same probe evidence. No pidfile, session record,
matching home, or matching executable can substitute for captured ownership.
Root retains known direct-child identities for bounded stop signals and reap;
this does not certify closure of all descendants. A probe worker deadline yields
unavailable and retains the worker to prevent concurrent attempts; late green
results are discarded. The next ordinary round observes a replacement's readiness.

[[services/supervision/meiva_root_glue/docs/meiva_root_glue.meiva.okf.md|Root wiring]] separates this policy
from read-only diagnostics and external transitions.
