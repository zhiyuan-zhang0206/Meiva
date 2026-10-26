---
type: doc
title: Async test stall diagnostics
description: Await-chain capture before the existing faulthandler deadline.
---

# Async test stall diagnostics

`tests/asyncio_stall_probe.py` walks `cr_await` and `ag_await` because a thread
stack or suspended task's outer frame cannot name every nested await. It writes
to pytest's faulthandler stash descriptor, duplicated before fd capture. Writing
to captured fd 2 can hide the diagnostic if the test is killed before playback.

pytest-asyncio builds the event loops of its tests through the
`pytest_asyncio_loop_factories` hook, and the probe implements it: every such loop
comes from `probing_loop_factory`, which schedules the dump on the loop itself
`DUMP_MARGIN_SECONDS` before the faulthandler deadline. Nothing in asyncio is
replaced, and a loop owns its timers, so closing the loop discards them.

The deadline runs from the start of the test's protocol, which is when pytest arms
faulthandler, and the loop is created later (when the first async fixture or the
test itself asks for it). A `pytest_runtest_protocol` wrapper therefore records the
protocol's start and the test's runtime node id, and the factory takes the time
already spent in setup off the delay, so the chain dump still lands before the
thread dump when module-scoped or session-scoped setup used part of the deadline. The
runtime node id also carries the `[param]` of a parametrized test, which the
collection-time hook cannot see. With no deadline the factory is plain
`asyncio.new_event_loop`. The callback diagnoses suspended tasks when the loop can
service a timer, not an indefinitely blocked event-loop thread. The margin
separates it from the watchdog's thread dump; it does not change the timeout itself.

## What it does not see

Only loops pytest-asyncio creates are probed. A loop a test builds for itself is
not: about 180 `asyncio.run(` sites and 5 `new_event_loop(` calls in tests, and the
loop an anyio portal creates for a Starlette `TestClient` (19 test files), because
those loops never go through the hook. The stalls this probe was written for (backend
shards whose faulthandler dump showed a worker's loop parked in `select()` with no
test frame) do not say which kind of loop was parked, so a stall in one of those
tests still produces only the thread dump.
