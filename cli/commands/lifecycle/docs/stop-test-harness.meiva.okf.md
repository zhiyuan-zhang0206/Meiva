---
type: doc
title: Stop and restart test harness
description: How native stop and restart tests exercise real PTY sessions, private process groups, database notices and cold gateway starts.
tags: [cli, lifecycle, stop, testing]
---

# Stop and restart test harness

The native stop tests create fresh PTY sessions and wait for the requested job's bare readiness
output (`job-ready`, or `stubborn-ready` once a stubborn job has installed its handlers) before
treating a shell session as busy: a login-profile child can exist before the initial command runs,
so child presence is not readiness. `cli/commands/lifecycle/tests/stop/stop_support.py:busy_session`
keeps cleanup custody. The plan tests assert the order of the steps, the real-shell tests assert
the closure, and the notices a stop wrote are read back from the suite's Postgres, as are audit
rows (`cli/database/tests/lifecycle_rows.py`). A gateway's cold preparation runs in a fresh
interpreter (`tests/start_storage/test_gateway_cold_start.py`), because its birth adopts a database
login into the process environment.

The stop tests' private non-PTY sessions run `tests/stop/_child.py`, which takes only SIGTERM
behavior, an optional ignored SIGHUP and an optional armed-marker path, as argv data. The ready
marker follows signal setup, and the fixture records the native session identity before returning
and closes that exact private group and pipes when the test exits, assertion failure included.

Parent: [[cli/commands/lifecycle/docs/stop-and-restart.meiva.okf.md]].
