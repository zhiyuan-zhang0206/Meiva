---
type: doc
title: Schedule entry composition
description: Current ownership boundaries and their executable evidence.
---

# Schedule entry composition

Runner-hosted scripts borrow their explicit ScheduleInputs without rebinding the shared SDK. A script run by hand owns a fresh entry: `open_schedule_entry` (in `schedules/jobs/entry.py`) opens process logging and calls `fresh_schedule_entry(image=..., process=...)` with the commit this process loaded and the process name, both values the caller supplies. The fresh entry owns ConfigBoot, one lazy database admission gate named after that image, and the client set; it binds an SDK context with that boot's clock factory and restores the previous context on exit. A checkout that moves on underneath a running schedule never relabels its database sessions. A primary callback failure survives a secondary client-close failure; normal close failure remains visible.

The entry is the only place a script reads the world. Its `__main__` block reads `MEIVA_SCHEDULE_ID` (validated by `parse_schedule_id`), the operator's report-recipient variable (validated when the recipient is built), the cluster timezone (`cluster_timezone()`, once) and the start time, and hands them down as values. Nothing below the entry reads the environment, the settings or the clock to decide what to do.

Executable evidence: `schedules/tests/test_entry.py` (a real git checkout that moves after capture, writer lifetime, borrowed inputs) and `schedules/tests/test_scripts_entry.py` (every script imports from a materialized copy and reaches its loop as `__main__`).
