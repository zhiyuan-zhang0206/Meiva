---
type: doc
title: Host memory sample
description: Read-only cgroup pressure/events/top-unit sampler for a host that also runs the data plane.
tags: [scripts, operations, memory]
---

# Host memory sample

`scripts/host_ops/host_memory_sample.py` appends one JSON line per run to
`$MEIVA_HOME/logs/host-memory.jsonl` (rotating to `.1` at 64 MiB), so a host's
memory history survives the incident it describes. Each line holds the kernel's
`/proc/pressure/memory` stall averages, the user slice's `memory.current` /
`memory.max` / swap pair, its `memory.events` counters (including cumulative
`oom_kill`), and the largest descendant `.scope`/`.service` units by current
usage with their own limits.

Fields exist so a budget can be justified from measurement rather than from an
estimate. `machine.charged` is the hierarchy's `memory.current` — the same basis
as any unit limit, page cache included — while `machine.total` / `available` /
`swap_total` / `swap_free` come from `/proc/meminfo` and answer the different
question of what a new workload could take without swapping; the two are
reported side by side, never mixed. `machine.anon`, `file`, `slab`,
`slab_reclaimable`, `slab_unreclaimable` and `kernel` come from `memory.stat`
and decide whether a limit means anything: most of `charged` is reclaimable.
Those columns are nested — `kernel` contains every slab, `slab` contains its
reclaimable and unreclaimable halves — so they are exported for diagnosis but
never meant to be added up by the reader. `machine.unreclaimable` is the one
figure a budget is written against, computed as `anon + kernel -
slab_reclaimable`; adding `slab_unreclaimable` on top of it counts unreclaimable
slab twice and reads as roughly double. `capped` lists every descendant unit
that carries a limit right now, with its current charge, that limit, the
kernel's own `memory.peak` and the unit's own `max`/`oom_kill` counters, so
"what does everything outside the limits use at its worst" is `machine.charged`
minus the sum of `capped`, and stays answerable for a limit that was set
yesterday. Those counters are recorded per sample because a transient scope
takes them with it when it exits: "was this budget ever hit" is only answerable
while the scope is alive, and only if someone wrote it down then.

It reads the cgroup tree and never writes to it, signals a process, or raises an
alert: the reason to cap a job is that the data plane shares its kernel, and an
alarm threshold needs a baseline before it means anything. The runbook owns the
rule it supports ([heavy ad-hoc jobs on a data-plane host](../../../docs/conventions/runbook.md#heavy-ad-hoc-jobs-on-a-data-plane-host);
task #5351).

```bash
python scripts/host_ops/host_memory_sample.py --echo          # one sample on stdout
python scripts/host_ops/host_memory_sample.py --cgroup-root DIR --output PATH
```

`--cgroup-root` defaults to this user's slice, so the same script samples a
runner host without arguments. Missing kernel files degrade to empty values:
a partial sample beats a dead timer.
