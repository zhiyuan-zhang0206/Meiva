# Task list data flow

`useTasks` chooses the task projection at each caller. The Task Graph and
Kanban board fetch `GET /api/tasks?fields=full`, retaining description and
results for their rendered text. The `summary` projection
(`GET /api/tasks?window=7d&fields=summary`, task metadata only) has no frontend
caller since the Inbox page was removed.

The Task Graph persists a graph-view window (default 24h).

`task_created` and `task_updated` SSE events invalidate the task cache after a
two-second debounce. A constant 30-second reconciliation poll continues after
errors; loaded task data remains visible while the hook separately reports the
stale state.
