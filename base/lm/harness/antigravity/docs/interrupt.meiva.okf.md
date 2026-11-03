---
type: doc
title: Antigravity interrupt and process-tree reaping
description: How `AgySession.interrupt` and `close` reap tool child trees under `agy` and respawn with `--conversation`.
tags:
- base
- library
- llm-inference
- antigravity
---

# Antigravity interrupt and process-tree reaping

Part of [[base/lm/harness/antigravity/docs/antigravity.meiva.okf.md|Antigravity (agy) session bridge]].

`SIGINT` stops `agy` but not its tool tree. In a live run the turn was asked to
`run_command: sleep 90`; after the interrupt the CLI had exited while both
`bash -c sleep 90` and `sleep 90` were still running. Those pids are known only
inside this call — the model can no longer track them and the caller never saw
them — so the adapter reaps them itself, by default and without a switch.

The report still has to be honest: `settled` is true only when every observed pid
is gone after the reap. A process that survives (permission, pid reuse, a reaper
that refuses) leaves `settled=False` and names the survivors in `remaining`;
`killed` lists what the adapter actually signalled.

Closing an active session uses the same reaping path. The session snapshots its
tree before cancelling the stream reader, whose finalizer can close and detach
the transport. This keeps a hosted cancellation or termination from losing its
observed children and claiming an unobserved close. Remaining children fail the
close; the final CLI exit status survives transport detachment.

Same live run after reaping:

```
CancelReport(settled=True, remaining=(),
                observed=(bash -c sleep 90; echo done, sleep 90),
                killed=(2615594, 2615595), cli_alive=False, waited_ms=8039)
turn: interrupted, is_error=True
next: AFTER-CANCEL          # respawned with --conversation
```

The session itself recovers: the next turn respawns `agy` with
`--conversation <id>` and completed normally in the same measurement.
