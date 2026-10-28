---
type: doc
title: "Reading a past window"
description: "How to read host metrics back after an incident: the PromQL shape, why a first look reads load average, and the timer-driven per-process sample a Linux host keeps on disk."
tags:
- telemetry
- observability
- metrics
- runbook
---

# Reading a past window

The series are Prometheus's OTLP translation, and the way to read a window back is the
Prometheus HTTP API on the machine that holds the store (the ubuntu gateway,
`127.0.0.1:59090`; its retention is `--storage.tsdb.retention.time=180h` and
`--storage.tsdb.retention.size=8GB`, decided by the deployment's config, not here):

```
/api/v1/query_range?query=system_cpu_load_average_1m{machine_name="ubuntu"}&start=...&end=...&step=60
```

**A first look reads load, not utilization.** In the 2026-10-29 stall the 1-minute load
average for a 16-core machine read 101.9 and 285.2 on two consecutive minutes, while
`system_cpu_utilization_ratio` read 0.02–0.03 for the same minutes. The ratio is computed by the
collector from counter deltas; a host that starves the collector can leave it stale or
misleading. That is one window's observation, not a proven mechanism, and it does not make
utilization useless: before drawing a conclusion from either series, check the sample's
freshness and the aggregation behind it — a `query_range` that returns every step says the store
answered every step, not that every scrape happened. Group by `machine_name`, not `host`.

The same asymmetry applies to per-process attribution, which is why a Linux host also runs a
timer-driven sampler (`~/.meiva/hostwatch/sample.py`, a systemd timer every 30 s, a JSON path
`~/.meiva/state/hostwatch/<UTC date>.jsonl`, 14 days kept): one short-lived process per trigger
— never a second resident binary — that records load, memory, io/net counters and the top five
processes by CPU measured with a 0.5 s in-run delta. 30 s because a stall that costs a lease
lasts one to two minutes and a 60 s grid can hold a single misleading point; 14 days because
every investigation of this class happens inside two weeks. Its samples are ordinary files, so
they are readable when the collector, Prometheus and the database are all unavailable.
