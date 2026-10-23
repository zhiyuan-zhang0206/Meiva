---
type: doc
title: Schedule router contracts
description: The gateway schedules router's write-side behavior — script checks, projections, log reads, the catalog-dependent draft — and its SQL RETURNING invariant.
tags: [gateway]
---

# Schedule router contracts

The HTTP router syntax-checks scripts before writing them. Its list projection
omits script bodies; full reads retain them. Newly enabled rows are picked up by
reconciliation without a separate launch call. Log reads remove trailing blank
PTY screen padding before selecting the requested content tail. The saved PTY
transcript preserves output after a session is reaped; if neither live capture nor
transcript is available, the stored crash error remains a distinct fallback.
Only the agent-backed natural-language draft operation requires a model catalog;
ordinary schedule management keeps its existing independent requirements.

What an edit, a start / stop / restart and a logs request decide from a row (what changed, whether the
session must converge now, which source answers the logs) is a pure function in `plans.py`; the
router reads the row and performs the write. A start / stop / restart / edit / delete commits its
sync request with the desired state, then waits `schedule_consume_wait_seconds` (a `GatewayConfig`
value) for the manager to consume it. Tests make a write fail with a database trigger, and give
the client a short wait.

Schedule values remain bound SQL parameters; interpolated projection columns are the module-owned fixed column list. Inserts and updates use RETURNING after creating or locking their target, so non-null row assertions express database postconditions rather than validating user input. Full reads keep a separate missing-row 404.

## Relationship to Other Nodes

- [[gateway/schedules/docs/schedules.meiva.okf.md|Schedule Manager & Runner]] — the overview these route contracts belong to.
