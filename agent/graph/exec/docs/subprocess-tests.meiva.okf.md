---
type: doc
title: Subprocess test assumptions
description: Timing and process-state premises of real execution-child tests.
---

# Subprocess test assumptions

`agent/graph/exec/tests/real_child/test_exec_subprocess.py` measures timeout from spawn. Cases that
assert interruption of user code or a spawned descendant must leave enough
budget for child imports and descendant creation. After SIGKILL, a descendant
can remain a zombie until its new parent reaps it; `pid_exists()` alone cannot
distinguish that state from a live process. Cleanup assertions check live members.

`test_exec_protocol.py` checks that a failed envelope write leaves nothing at
the destination (it makes the destination a directory, a fault the disk can
produce). Commit, cleanup and durability faults of the atomic write itself, such
as a failed `replace` or `fsync`, are proved once in `base/host/atomic_io`'s own
tests; the protocol writes through `write_bytes_atomic`.

Real-child tests hand the spawn its environment and working directory as a
`ParentFacts` (`process_inputs.real_parent`), so the isolation checks (a stale
`PYTHONPATH`, a foreign cwd, ignored encoding variables) change the value, not the
test process. They also drop the editable-install guard (`source_root=None`):
the guard's own tests cover it. Streaming and keepalive checks read the published
events back from a real subscriber (`event_capture.py`).
