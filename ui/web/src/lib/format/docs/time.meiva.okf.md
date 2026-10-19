---
type: doc
title: Timestamp display
description: The shared timestamp formatters, the clock-format and zone-name settings, and the day-context rules for an upcoming instant.
tags: [frontend, contracts]
---

# Timestamp display

`time.ts` is the single owner of turning an ISO instant into text a person
reads. Views see browser-local time — the viewer's zone, deliberately unlike the
cluster timezone an agent sees. Four shapes, one job each: `formatAbsolute`
(full precision, zone name optional), `formatShort` (compact rows),
`formatRelative` (one signed wording, `in 4m` / `5m ago`) and `formatUpcoming`
(an upcoming instant with its day context).

## Clock format and zone name

Two `display.*` user settings drive every clock render. Both defaults are what a
viewer who never opened Display settings sees.

| Setting               | Values          | Default | Effect                                                              |
| --------------------- | --------------- | ------- | ------------------------------------------------------------------- |
| `display.clock_format` | `24h` \| `12h` | `24h`   | `15:45` vs `3:45 PM` on every clock surface                          |
| `display.show_timezone` | boolean       | `false` | `formatAbsolute` appends `GMT+8` / `PDT` / `UTC`; off shows the clock alone |

`formatClockText` owns the 12/24-hour choice for the viewer's local zone;
`formatShort` and the run-timeline axis labels compose it. `formatUpcoming`
renders through `Intl` because it accepts an explicit zone, and takes the same
`clockFormat`. Components read both keys through `useTimeFormat` — a literal
`hour12` at a call site is a bug. A missing key means the default, not an error:
`user_settings` stores only what the viewer changed, so an old row renders
24-hour with no zone name.

The zone name is noise in a single-zone deployment (the viewer's own clock is
implied context), which is why it is opt-in. `GMT+8` appears only when the
viewer turns it on.

## Why day context

A clock reading alone cannot say which day it means. The Inspector's "Next
heartbeat" showed `3:21 PM` while the clock read 4:26 PM: the value was
tomorrow's, but it read as a time that had already passed (user report
2026-10-17). `formatUpcoming` therefore renders an upcoming instant as a clock
time plus the minimum day context, and never as a bare clock for a future day:

| Calendar days from now | Rendering                       | Example                |
| ---------------------- | ------------------------------- | ---------------------- |
| later today            | clock                           | `15:21`                |
| tomorrow               | `Tomorrow` + clock              | `Tomorrow 15:21`       |
| 2–6                    | weekday + clock                 | `Tue 15:21`            |
| 7–29                   | day count + date                | `In 7 days (Oct 24)`   |
| 30 and beyond          | date, year only when it differs | `Nov 5`, `Jan 5, 2027` |
| already past           | the shared relative wording     | `5m ago`               |

Examples use the default 24-hour clock; the 12-hour setting renders the same
rows as `3:21 PM`, `Tomorrow 3:21 PM`, `Tue 3:21 PM`.

**Boundaries.** The day changes at the viewer's local midnight, not after 24
elapsed hours: a target 20 minutes away at 00:01 is "Tomorrow". The day count is
calendar arithmetic over the local date fields, so a DST day (23 or 25 hours)
still counts as exactly one day. Seven days is the first day of the day-count
tier; 30 days is the first day of the date tier, because beyond roughly a month
a day count is harder to place than the date itself. The weekday tier stops at
six days — a seventh day repeats the weekday name, so the name would stop
distinguishing anything.

**Rendering details.** A date carries its year only when it falls in another
calendar year, so a target twelve months out cannot repeat the same ambiguity.
The clock is `formatClockText` output: `15:21` in the default 24-hour format,
`3:21 PM` when the viewer picks 12-hour. The 12-hour form replaces ICU's narrow
no-break space before the day period with a plain space, so the string is
identical across Node/ICU versions and copies cleanly. That normalization is its
own helper (`normalizeDayPeriodSpace`) tested directly: today's ICU already
emits a plain space, so asserting it through a rendered clock would pass with the
normalization deleted.

Consumers: timeline card stamps (`formatItemTime`), the Inspector's birth cell,
notice details, run-timeline detail panes and axis labels, sidebar absolute
times, the permissions "checked …" line, and alert hover titles. The Inspector's
Liveness cells are the first `formatUpcoming` consumer; see
[[ui/web/src/docs/frontend-components/inspector/section-details.meiva.okf.md|Inspector section details]].
