---
type: doc
title: Self-evolution schedules
description: Script source, timeouts and trigger thresholds of the daily and weekly self-evolution loops.
tags:
- schedules
- self-evolution
---

# Self-evolution schedules

Both loops wake the `self-evolution` agent by label and run no work of their own. What each
outcome does is decided in `schedules/plans/scan_outcome.py` and
`schedules/plans/weekly_trigger.py`; `schedules/jobs/self_evolution_scan.py` and
`schedules/jobs/self_evolution_weekly.py` run the scan, count the events and wake the agent.

## Daily scan

`daily_scan.py` ships with the `meiva-self-evolution` skill, and the schedule
runs the skill's load-directory copy. That copy is converge-managed but
bootstrap-only (R5): converge lands it once and the product rollout's update legs
refresh it to the landed revision. Before that wiring the copy stayed at its
first-landing version, so a script added to the skill later never arrived
(issue #1289); `meiva skill update` is the manual equivalent. A missing file must
not read as an ALERT — `python` exits 2 when it cannot open the script, which the
ALERT branch would read as "bad runs found" — so the schedule reports the missing
script and wakes the agent instead.

The scan timeout is 10800 s. A measured full day takes about 113 minutes while
two collect streams run (2026-09-25, task #4743 log and #4750 tracker), so the
bound is about 1.6x the measured contended time: it separates a slow scan from a
wedged one, and the extra margin only delays wedge detection.

The daily report goes to agent 228 unless a `MEIVA_SELF_EVOLUTION_DAILY_REPORT_AGENT`
override names another recipient (2026-08-09 ruling: daily reports replaced the
weekly ones); an explicitly empty value skips it and a malformed one stops the
schedule at its start. A timed-out or missing scan wakes the agent but sends no report;
every finished scan sends one, whatever its exit code.

## Weekly trigger

The main check fires Tuesday 00:00 cluster time and acts only when the past
week's agent-event volume reaches `MIN_WEEKLY_EVENTS`. A busy main trigger
(volume at or above `HIGH_WEEKLY_EVENTS`) also enables a Friday 00:00 follow-up,
whose own bar is half that high threshold measured from the main trigger rather
than from the whole week.

The count comes from the live `/api/events` stream with `with_total=1`: the
2026-08-18 contract change made `meta.total` opt-in, and without the flag the
gateway returns no total — which crashed the trigger on `int(None)`, while a
silent 0 would have skipped the week's deep run.

Both loops run the shared loop (`schedules/jobs/loop.py`): the next slot is searched
from the later of the loop's floor and `now - 2 minutes`. Cron expressions are
strictly greater than their base, so a sleep that lands a fraction of a second past
the hour would otherwise jump to the next day — the deterministic miss observed on
2026-08-06. The daily loop claims a slot up to 60 seconds early and the weekly loop
up to 90; the weekly follow-up slot is searched only after a main fire armed it, and
that flag lives in the running process alone.

The shared claim semantics and the report-recipient variables are in
`schedules/README.md`.
