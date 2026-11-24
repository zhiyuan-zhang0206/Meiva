# Self-evolution schedules

Both schedules wake the `self-evolution` agent by label and run no work of their own. What each
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
week's audit-event volume reaches `MIN_WEEKLY_EVENTS`. A busy main trigger
(volume at or above `HIGH_WEEKLY_EVENTS`) also enables a Friday 00:00 follow-up,
whose own bar is half that high threshold measured from the main trigger rather
than from the whole week.

The count comes from the audit record, `/api/events?category=audit` with `with_total=1`. It is
the permanent record of what agents did, in one store; the telemetry and log stream is Loki's
seven days, which is the week this reads, and mostly background state samples. When the count
moved from the whole stream to audit alone (2026-11-24: about 77,000 audit events and 286,000
telemetry events in a week, against the 30,000 and 200,000 bars), `HIGH_WEEKLY_EVENTS` went from
200,000 to 60,000 so that the follow-up still arms at the volume that armed it before. The
2026-08-18 contract change made `meta.total` opt-in, and without the flag the
gateway returns no total — which crashed the trigger on `int(None)`, while a
silent 0 would have skipped the week's deep run.

The daily and weekly entries each execute one declarative slot and exit. The
schedule manager owns cron timing, single-flight admission, timeout, run history,
and `last_error`. The weekly entry persists the Tuesday result in its workspace
`state.json`; Friday performs the follow-up only when that ordinary state file says
Tuesday armed it. A manager restart therefore preserves the arm without retaining
a sleeping Python process.

The shared claim semantics and the report-recipient variables are in
`schedules/README.md`.
