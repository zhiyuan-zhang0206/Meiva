---
type: doc
title: Session Builds and Upper-Level Rebuilds
description: Manual per-session understanding builds over chosen compaction segments and queued upper-level tree rebuilds once an agent's chunk jobs drain.
tags:
- hierarchy
- understanding
- chunks
- rebuild
---

# Session Builds and Upper-Level Rebuilds

## Manual build per session

`GET /api/agents/{id}/sessions` and `POST /api/agents/{id}/understanding/build`
(`gateway/agents/history/understanding.py`; `sessions.py`, `build.py`, `chunk_plan.py`). A
*session* is one compaction segment (1 = oldest, stable; the unclosed newest has no boundary).
`build.plan_jobs` replays the live trigger rule over the chosen sessions (`chunk_plan.plan_replay`,
threshold `chunk_threshold`, as the hook), cuts every chunk down to the runs no level-1 node covers
(one job per run; a run of framework notes only is dropped) and enqueues ordinary chunk jobs (a
closed session's name its boundary checkpoint; an ended job of the same stretch is revived, a live
one merged into). The session in progress is built to the last request sent, which is what closing
the live segment was. `dry_run` plans and prices only. Estimates are cold-cache
(`build.COST_BASIS`). A build is recorded in `understanding_builds` (its jobs, its rebuild) and
read back by `GET .../understanding/builds/{build_id}`.

## Rebuild of the upper levels (`rebuild.py`, table `understanding_rebuilds`)

A build also queues one rebuild for its agent (builds merge into the agent's pending row under an
advisory lock). The consumer loop claims it only when the agent has no chunk job pending or
running; while it runs, the agent's chunk jobs are not claimed. It drops every node above level 1
and the agent's `understanding_group_state`, then replays the level-1 nodes in message order,
running the grouping checks after each with that leaf as the horizon
(`run_group_checks(upto=...)`), as live leaves grow it. An interrupted rebuild restarts from the
lifted state; chunk jobs finishing while one is pending skip their grouping checks.

The live trigger, queue and consumer around it: [[base/agents/history/hierarchy/docs/chunks.meiva.okf.md|Chunk-triggered Understanding]] and [[base/agents/history/hierarchy/docs/groups.meiva.okf.md|Upper-level Grouping]].
