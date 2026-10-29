---
type: doc
title: Inspector liveness section
description: The Liveness cells — birth, harness and model, the next-heartbeat rule for a pause, a queued wake, a projected check-in (idling or impersonated) or nothing, last pause, machine and status.
tags: [frontend]
---

# Inspector liveness section

One merged section (user rulings Task #1195 / 2026-08-12 and 2026-09-18 field
order, Task #3952 / #3904). Cells in display order: agent birth
— `spawned_at` (the true birth, written once at spawn), rendered as
"relative, absolute" on a single line (user ruling 2026-08-24) and deliberately
not `started_at`, which refreshes on every restart; harness (roster's display
label, em dash until the roster loads) and the model the agent was born on
(`config_overlay.llm_model`, user 2026-10-21); next heartbeat — the cell
answers "when does the heartbeat look at this agent again", so an active pause
renders labelled, `paused · <time>`, on ONE line rather than as a bare clock
time that reads like a scheduled check-in. The relative span that used to sit
under the value as the cell's sub line is gone — it read as a second, smaller
line (user 2026-10-29) — and the time itself follows the reader's
`display.date_format`: the date-context absolute form by default
(`Tomorrow 15:21`), or `in 22h`, which counts down with the panel's one-second
clock, for a relative reader. That holds for a
running agent too: the pause is the daemon's floor whatever the status is, and
reading it only while idling hid a day-long pause behind an em dash for as long
as the agent kept working (user report 2026-10-28). Both projected instants (the
end of an active pause and the check-in time) render through `formatFuture`, so
under the default absolute form the day boundary is part of the value: clock
time later today, `Tomorrow 15:21` across local midnight, a weekday name two to
six days out, a date countdown from seven days, and a plain date from a month
on. A bare clock time is never shown for a future day — `15:21` read at 16:26 looks like a time that already passed
(user report 2026-10-17, in the 12-hour wording then in use). A queued check-in
renders "pending" (the daemon will not send another while an inbound is pending),
a running agent with no active pause and no impersonation lease, and any terminated
agent, an em dash — while a running agent under a lease projects a real check-in from the
lease start, since the daemon does select it between takeover turns and the cell used to
show nothing there (task #5723). A projected time already in the past renders "due" (a past "*next*" time must never
read "Xm ago"); a pause that ran out between the last live read and the current
tick also reads "due" instead of counting down at zero; last pause (relative time
· duration, or "never paused"); then machine and lifecycle status. The status cell reuses the fleet's active-lease projection so an
impersonated agent reads "Impersonated" while its native lifecycle stays idling;
a terminated panel keeps "Terminated" even if a stale roster card is cached. The
gateway's derived liveness state colors the heart icon when offline. The "every
N" interval badge, the old "Last judged" cell and all observation details stay
omitted.

The section's cells read the live projection only; the reads and their refresh
policy belong to [[ui/web/src/docs/frontend-components/inspector/queries-and-refresh.meiva.okf.md|reads and refresh]].
