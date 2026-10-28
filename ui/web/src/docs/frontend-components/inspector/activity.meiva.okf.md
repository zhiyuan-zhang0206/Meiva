---
type: doc
title: Inspector Activity section — cost, TPS and durations
description: The panel's last section — the cost cell's billing presentation and its four metrics, then TPS and the duration cells with their external-harness and no-life cases.
tags: [frontend]
---

# Inspector Activity section

The panel's last section and its two grids. The shell (layout, order, close
surfaces) is in [[ui/web/src/docs/frontend-components/inspector/inspector.meiva.okf.md|Inspector Panel]],
the section list it hangs off in [[ui/web/src/docs/frontend-components/inspector/section-details.meiva.okf.md|section details]],
and the reads behind it in [[ui/web/src/docs/frontend-components/inspector/queries-and-refresh.meiva.okf.md|reads and refresh]].

Two grids. Cost: total `$` (four decimals); a subscription harness appends its
billing label in parentheses on the amount's own run — `$0.42 (Subscription)` —
because the label qualifies the number, while a metered agent, or one whose harness
the roster has not resolved yet, shows the amount alone, so the cell never names a
mode it cannot know (user ruling 2026-10-28). An `N unpriced` sub-line appears when
calls without a stored usage-time price snapshot exist — those contribute 0 and are
surfaced rather than hidden, never silently partial; LLM calls; tokens in / out
combined; cache-hit percent (`N/A` when unmetered). Activity: TPS (the LLM stage's),
then the absolute durations the header window covers — LLM output, code execution,
and idle (`alive − active`). For external harnesses (`harness !== "meiva"`), TPS,
LLM output, and code execution are omitted because those belong to the native
`execute_code` loop. Without life (`alive_seconds = 0`) the duration cells show em
dashes while TPS stays visible. Durations use the shared tiering — `45s` / `15m` /
`1h` / `1h 30m` / `24d 3h` (day tier past 24h) — and TPS renders one decimal, `0` →
em dash. Token counts roll `≥1000M` to `B` and past `1000B` to `T`. The section
header carries the window picker and the ⟳
([[ui/web/src/docs/frontend-components/inspector/queries-and-refresh.meiva.okf.md|reads and refresh]]).

Parent: [[ui/web/src/docs/frontend-components/inspector/inspector.meiva.okf.md]].
