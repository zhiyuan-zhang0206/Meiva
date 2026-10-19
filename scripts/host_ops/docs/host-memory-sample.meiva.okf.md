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
