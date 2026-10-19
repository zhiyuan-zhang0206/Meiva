---
type: doc
title: Incomplete converge marker
description: The per-home record of a converge that aborted, and why `meiva status` prints it.
tags: [cli, lifecycle, converge]
---

# Incomplete converge marker

`meiva converge` is fail-fast: the first failing step ends the run. `meiva start`
then continues into service bring-up, so a host can serve with one step never
applied — a converge step aborted while every service reported healthy
(task #5323).

The aborted converge writes `$MEIVA_HOME/state/converge-incomplete.json` —
`{at, step, error}` — and `meiva status` prints it above the service table:

```
converge: INCOMPLETE 2026-10-18T10:02:33+00:00 at step 'permissions helper build + sign + load': …
```

The marker is per home, and only a converge that completes **every** applicable
step on that home removes it. Another host's success never clears it, and a
`meiva start` that skips a step because the service is not selected does not
count as having applied it.

`cli/commands/converge/incomplete.py` owns the file; `converge_host.py`
writes it on the failure path and clears it after the last step;
`status.py` reads it. Only the host's prod install records it — a converge from
a dev worktree leaves the prod home's marker untouched.

`up` in `cli/fleet_update.py` reads the same file over SSH after a failed start,
so its per-host failure line names the step and reason instead of the last line
of console output; a failed host no longer stops the remaining hosts, and the run
exits `3` (partial) rather than `1`.

Parent: [[cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md]].
