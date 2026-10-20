---
type: doc
title: Start phase timings
description: Where one `meiva start` spent its wall clock, recorded on demand and never used as a gate.
tags: [base, deploy, lifecycle]
---

# Start phase timings

`base/deploy/start_trace.py` records one span per phase of a single `meiva start`
and prints them before the command exits. It exists to answer "why did start take
that long" from one run instead of from guesses.

## Enabling it

`MEIVA_START_TIMING=1` (also `true`, `yes`, `on`). The CLI entry reads the
environment and hands the answer to `start_trace.begin`, so no module below the
CLI consults the environment on its own.

```bash
MEIVA_START_TIMING=1 meiva start --all-services
```

## What a run prints

```
start timing (MEIVA_START_TIMING=1; seconds from the CLI entry)
    start     took  phase
     0.00     0.72  admission
     0.72     0.31  identity
     ...
    12.40    13.19  readiness wait
    12.40     0.02  ready: gateway
    25.36     0.00  ready: frontend
    25.90             total
  saved: /…/.meiva/logs/start-timing/20261019T080000Z-4242.json
```

`start` is the offset from the CLI entry, `took` the phase's own duration. Phases
may nest, so a parent's duration includes its children; the table is sorted by
start offset. Each service the run launched gets one `ready:` line at the moment
it first passed the readiness probe, measured from the start of the readiness
wait — that line is what distinguishes "the frontend was still building" from
"the gateway was slow".

The JSON copy under `<home>/logs/start-timing/` carries the same record for
before/after comparisons. It is written only when the run asked for a report.

## Boundaries

- The origin is the CLI entry (`cli.main.main`), not the interpreter's first
  instruction: the cost of importing the CLI is outside every span. Compare a
  report's `total` against the command's own wall clock to see that remainder.
- Recording changes no decision. A phase runs exactly as it would without the
  recorder; a raising phase still unwinds through its `with` block, and the span
  closes in `finally`.
- Nothing here is a readiness signal. Readiness stays `SERVICE_READY_TIMEOUT_S`
  and the service probes' own verdicts; the trace only observes them.
