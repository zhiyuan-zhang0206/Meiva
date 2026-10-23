---
type: doc
title: PTY path and signal inputs
description: Explicit homes, process control and native identity guarantees.
tags: [base, pty]
---

# PTY path and signal inputs

Allocation marker operations receive their home explicitly; the PTY service
retains the home supplied by its process root. Known-group signalling and closure
receive the existing required `ProcessControl`, propagated through the service,
ledger sweep and stop root. Tests can leave or refuse a known group through that
port while the real birth checks and survivor decisions still run. A target in
the caller's own group continues through `OwnedProcess.send_signal`, including
Linux pidfd custody; it never becomes an unchecked numeric-PID signal. The
force-kill test runs the unchanged stubborn-group behavior from a repository
child script and observes forwarded real signals.
