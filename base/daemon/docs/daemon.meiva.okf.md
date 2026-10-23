---
type: doc
title: Daemon contracts
description: Shared daemon HTTP transport, health, shutdown contracts and schedule vocabulary.
tags: [base]
---

# Daemon contracts

`base/daemon/` owns shared daemon HTTP transport, health, shutdown contracts and schedule vocabulary.
Its component nodes describe the current contracts and implementation.

`ServiceEndpoints.from_boot(config, runtime=...)` constructs a daemon endpoint
table from a required configuration owner and runtime directory. Port fields
remain live until each table is built; an existing table retains its captured
values. The registered health-port resolver owns default ports and unknown-name
failures for both this factory and the remaining legacy `from_settings` roots.
The factory does not create a boot or infer a home. Process roots own runtime
directory creation at their existing pidfile/health resource decision.

When a daemon health response matches the requested name and home but its
pidfile is missing, the existing DOWN verdict includes the local port's
listener PIDs, process names and executable paths. Listener discovery uses
`base.cluster.port_preflight.strict_listeners_on`; unavailable discovery leaves
the diagnostic as `listening: unknown`. This enrichment does not establish
ownership or change restart eligibility. Other probe outcomes do not perform
the listener lookup, and the root failure-state signal retains the detail.

`cancel_and_drain(runner, interrupted_by=...)` cancels one snapshot of loop tasks
and joins one retained gather. A caller may identify the exact `KeyboardInterrupt`
it already caught: a TaskGroup replay of that same object resumes the existing
join without cancelling cleanup again. Another interrupt, `SystemExit`, and
unknown loop failures still propagate. The default `None` consumes no replay.
Ordinary cleanup exceptions retain their identities in the returned failure
list; callers report them and choose their error exit code. No executor join or
new cleanup deadline is introduced. A caller reaches `hard_exit` only after this
drain returns and its other owned cleanup has completed.

## Documented components

- [[base/daemon/docs/http-transport.meiva.okf.md]] — Settings-free daemon HTTP transport.
- [[base/daemon/schedules/docs/completion-notices.meiva.okf.md]] — Completion Notice Vocabulary.

## Loop liveness ownership

`LoopProgress` and `LivenessGroup` require the caller's existing `Timers` port.
A group shares its timer with every loop it registers. Service composition roots
supply `SYSTEM_TIMERS`; deterministic tests supply `ManualTimers` and advance it
between completed units. No default argument captures a monotonic function at
import time. A completed unit refreshes progress, while a permanent wedge stays
failed even after another beat. Wall-clock success/error timestamps retain their
existing owner and are separate from monotonic staleness.

Endpoint tests use real `ConfigBoot` values and a clean child environment for
legacy settings roots. Health-probe tests serve real loopback HTTP, malformed
status lines and actual pidfile errors, so the public fail-closed wrapper and
its narrow inner parser are exercised together. A temporary Git repository
advances after the loaded image is captured to prove health responses retain the
process image, including an explicit unknown image; probes report that evidence
without treating checkout changes as an unhealthy daemon.
