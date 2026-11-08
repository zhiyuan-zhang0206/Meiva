---
type: doc
title: Generated-job heartbeats
description: What a generated OS job promises, how the definition carries it, and who reads it.
tags: [base]
---

# Generated-job heartbeats

A launchd plist or crontab line that writes nothing is indistinguishable from one that was
removed: both are silence. Four generated jobs (`logs-maintenance`, `packages-refresh`, `pr-flow`,
`walg`) therefore declare what they promise, and the definition that runs them carries it.

`base/host/system/job_values.py` owns the declaration values — the heartbeat file the run
writes, the owner to call when it stops moving, and how late it may be before someone is called —
and `cron/heartbeat.py` turns them into the definition's `MEIVA_JOB_HEARTBEAT` variable plus one
`meiva-heartbeat:` artifact line. The render refuses a job that declares no heartbeat, an
exception (`heartbeat: none`) without a reason, an owner and an `until`, and two jobs declaring
one heartbeat name on one machine; an expired exception still renders, marked `expired`, because
a refused render would fail a converge on the day of expiry and hand that failure to whoever is
deploying. `*_content.py` builds the declaration's cadence and the plist trigger from the same
`HOUR`/`MINUTE`/tick values, so the two cannot describe different schedules.

`cron/heartbeat_population.py` owns the declared population of the four generated jobs and the
document `meiva jobs heartbeats` prints, which is what makes "declared but not installed"
observable to a reader outside this checkout. `cron/heartbeat_record.py` writes the file on the
way out of the process that ran the job — the CLI entry for three of them, the PR-flow script for
the fourth — recording the command's own exit code with `ok`, and exits 3 when it cannot write
one: a run must not report a success it could not prove.
