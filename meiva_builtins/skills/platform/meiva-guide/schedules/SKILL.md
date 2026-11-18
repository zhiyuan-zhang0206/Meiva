---
name: schedules
description: "Creates declarative Meiva schedules for one-shot, cron, or fixed-interval work."
---

# Declarative Schedules

Use `meiva.schedules` inside an agent and `meiva schedules` from a shell.
A schedule runs a Python payload once per fire and records the outcome. The
manager owns timing, recovery and process lifetime. Choose the trigger, skip
condition, error policy, machine, owner and stopping condition from the request.
Reuse existing schedules and event delivery before creating another monitor.

## Choose a trigger and payload

- `at(aware_datetime, ...)`: one fire; a missed instant catches up once.
- `cron("0 3 * * *", ...)`: wall-clock cadence; `timezone` defaults to the
  configured timezone. Missed slots are skipped and fires never overlap.
- `every(60, ...)`: fixed interval of at least 30 seconds, measured after completion.
- Supply exactly one of `script` or `message`. A message wakes `target_agent_id`,
  defaulting to the owner; a system message schedule needs an explicit target.

```python
from datetime import datetime, timedelta, timezone
import meiva

reminder = meiva.schedules.at(
    datetime.now(timezone.utc) + timedelta(hours=1),
    name="review-result", message="Review the saved result and resume the task.",
)
check = meiva.schedules.every(
    60, name="check-result",
    script="from pathlib import Path\nprint(Path('state.json').exists())\n",
    timeout_seconds=30,
)
nightly = meiva.schedules.cron(
    "0 3 * * *", name="nightly-review", message="Review the nightly evidence.",
    on_owner_terminate="keep",
)
```

## Single-round contract and local state

Write one bounded round and exit 0 on success. Never put `while True`, sleeping
until the next fire, `write_beat`, or a resident daemon inside a schedule. Raise
on failure so the run and `last_error` capture the error. A skipped round can
simply return. For event/threshold work, use an `every` trigger whose script checks
once and sends a message only when action is needed. Short-lived event-stream
waits belong in `meiva.shell.run_background(..., ttl=...)` instead.

The payload runs on its selected machine in
`$MEIVA_HOME/workspaces/<owner_agent_id>/schedules/<schedule_id>/`.
System schedules use `$MEIVA_HOME/schedules/<schedule_id>/`.
`run.py` is the materialized payload and `latest.log` contains its latest output.
Ordinary local files such as `state.json` persist between fires: use them for
baselines, debouncing and delivery receipts. Persist progress before a terminal
notification, record successful delivery, then disable the schedule when finished:

```python
import os
import meiva

# After the terminal result and successful notification are durably recorded:
meiva.schedules.disable(int(os.environ["MEIVA_SCHEDULE_ID"]))
```

Disable can terminate an active fire during reconciliation, so put it last.
Local files belong to that machine; do not assume shared storage across hosts.

## Ownership and lifecycle

SDK creation defaults to `owner_agent_id="self"`. Pass an explicit agent id to
place work with another owner, or `None` for a system schedule. Owner placement
follows the owner's machine unless `machine` is supplied. Owned schedules default
to `on_owner_terminate="cancel"`: owner termination cancels unfinished work and
stops active fires. Choose `"keep"` explicitly when work must outlive that owner.
An owned payload retains schedule provenance and owner identity; it does not
own the agent's turn and cannot restart or terminate the owner through `meiva.self`.

## Inspect and control

`meiva.schedules.list()` and `get(id)` return durable schedule state.
`enable(id)` calculates the next eligible fire; `disable(id)` stops future fires
and any active fire; `cancel(id)` cancels unfinished work; `run_now(id)` enables
an idle schedule and makes one fire immediately eligible. Inspect `last_error`,
the run outcome and logs before claiming the work succeeded.

## CLI

`meiva schedules create NAME` takes exactly one trigger (`--at` with an aware
ISO timestamp, `--cron` expression, or `--every` seconds) and exactly one payload
(`--script` source, `--script-file` path or `-` for stdin, or `--message` text).
Use `--owner-agent-id`, `--target-agent-id`, `--on-owner-terminate cancel|keep`,
`--timeout-seconds`, `--machine`, and `--timezone` to make placement and lifetime
explicit. CLI creation without an owner creates a system schedule.

```bash
meiva schedules create nightly-review --cron '0 3 * * *' \
  --message 'Review the nightly evidence.' --owner-agent-id 42 \
  --target-agent-id 42 --on-owner-terminate keep --timeout-seconds 60
meiva schedules create check-result --every 60 --script-file check.py \
  --owner-agent-id 42 --machine ubuntu --on-owner-terminate cancel
```

Use `meiva schedules ls`, `get`, `logs`, `runs`, `enable`, `disable`, `cancel`,
and `run-now` to inspect and control a schedule. CLI help owns exact flag syntax.
After creation, retain the id and read back its trigger, owner, machine and next
fire. Apply the user's existing authorization; ask only for unsettled decisions.
