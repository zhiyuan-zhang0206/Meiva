---
type: doc
title: Watchdog sentinel (outside Grafana)
description: The alert-path sentinel that asks Prometheus how old the newest accepted ingest is and reports over Telegram itself, so a dead alert engine cannot silence it.
tags: [scripts, operations, alerting]
---

# Watchdog sentinel

`scripts/host_ops/watchdog_sentinel.py` observes the alert path from outside the alert engine
(task #6220). The two rules that prove the path works (R27 and R28) live inside Grafana: when the
alert engine stops evaluating, they stop with it and nothing reports the outage. The sentinel asks
their question directly -- how old is the newest accepted alert ingest -- against Prometheus and
posts to Telegram itself, so the engine that stopped cannot also silence its observer.

## The two rules

**Absence is the failure.** A missing sample is not health: it means the ingest stopped, and no
other rule can see that from inside the engine. The one exception is a metric that has never
existed, which is what a fresh install looks like; the first sighting is recorded in the state
file and until then the sentinel logs `unarmed` and reports nothing. The state file is what turns
"never pushed" into a silence instead of a page.

**One incident is one message.** The state file also holds the last send. A tick every fifteen
minutes therefore produces one alert per outage and at most one repeat per `--repeat-hours`
(default six), and the log keeps the streak and its length.

## The two legs

- **Telegram** carries the alert. The Bot API's answer is the only receipt of a delivery:
  `message_id` and `date` are what the log cites, and a caller that never saw them has proved
  nothing about delivery.
- **The agent channel** carries an escalation, because the suspected leg cannot report that it is
  broken. It runs the installed `meiva agents send` with a fixed argv and no shell; `agents send`
  auto-resurrects a terminated target, so naming the owner is enough and no liveness is assumed.

Every tick also checks delivery *capability* before judging the pipeline: a `getMe` call costs one
request and fails on exactly the two permanent breaks (401 a bad token, 403 the bot was blocked)
and on network loss. That turns the send leg's six-hour sampling into a fifteen-minute one. It is
a warning, not proof -- a bot removed from the conversation can still answer `getMe` -- so a real
send's `message_id` stays the only evidence that the action path works.

Failure classification is what decides how long to wait: status 0 is transient (the network), 401
and 403 are permanent. Three failures in a row escalate, and any permanent one escalates at once.
One escalation latch is shared by both legs, so a broken delivery leg produces one message and not
two; when the leg comes back, a recovery message says so, because otherwise the operator only ever
learns it broke.

## State, log and credentials

Both files live under `$MEIVA_HOME/ops/`:

- `watchdog-sentinel.state` -- `first_seen`, `last_sent`, the two failure streaks with their
  start times, and `escalated_at`.
- `watchdog-sentinel.log` -- one line per tick: `ok`, `unarmed`, `SILENT`, `ERROR ...`,
  `RECOVERED ...`, `sent message_id=...`.

Credentials come from `$MEIVA_HOME/.env`. Both spellings are read, because the names this home
actually defines are `MEIVA_TELEGRAM_BOT_TOKEN` / `MEIVA_TELEGRAM_OWNER_ID`, while the
`MEIVA_ALERTS_*` pair exists only inside the Grafana contact point's own environment.

## Running it

```bash
python scripts/host_ops/watchdog_sentinel.py                 # one tick, report through Telegram
python scripts/host_ops/watchdog_sentinel.py --dry-run       # say what it would send, send nothing
python scripts/host_ops/watchdog_sentinel.py --silence-seconds 900 --repeat-hours 2
```

`--home` names the home to observe (default `~/.meiva`), and `--prom-url`, `--escalate-agent` and
`--silence-seconds` exist because a probe pointed at the wrong port or at a stale threshold is a
silent false green. The script depends only on the standard library, so it runs on a host whose
virtualenv is exactly what broke.

The deployment on the host that carries the ingest is a crontab entry that runs one tick every
fifteen minutes and appends stderr to `ops/watchdog-sentinel.cron.log`; the entry is one of the
declared lines in `~/.meiva/ops/crontab-expected.txt` (task #6234).

## Why outside Grafana at all

Every alerting component that reports on itself shares a failure with the thing it watches. The
ingest path is the one link the rules cannot test from inside, because a rule that fires rides the
same route to the same table: if the route breaks, the rule that would report it breaks first.
The sentinel keeps its own clock, its own state file and its own outbound leg, so it is wrong in
different ways than the path it watches.
