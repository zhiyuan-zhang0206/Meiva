---
type: doc
title: Termination test evidence
description: Transaction fallback, force fences and native shell-session proof.
---

# Termination test evidence

`ops/lifecycle/tests/test_termination_message.py` exercises a real database pair
rollback through a trigger that rejects the completed chat/command pair, then
observes the terminate-first fallback. A temporary check constraint rejects both
the initial chat and its retry while the terminate command survives. Audit failure
uses a real database constraint and verifies rollback of the terminated state.
The locked-death race verifies that a shell-kill request waits for the agent row
and handles the committed terminated state. Force does not inherit a
superseded graceful kill request.

Every shell-session case runs real shells in the test's own pty-sessions service, and each
terminate request reaches the real `kill_agent_shells`: a force or an already-terminated agent
reports the ids it killed and leaves none of them alive; a graceful request of a live agent only
records the request and kills nothing. The native PTY case closes the owner's older shell sessions
while preserving page, foreign and post-acceptance replacement sessions. Its child job writes a
marker on ordinary exit or a handled signal. Absence of that marker after the hard kill models
suppression of an exit callback; it does not exercise watcher notification delivery. Session IDs
are fixture allocations, not reserved roles.

`terminate_agent_op` and `cancel_agent_op` take the `LocalAgentHost` that names this machine's agent
host (its data home and loopback health port); `lifecycle_op` takes the data home from the op entry
that serves it and reads the health port from the settings once per request. In the tests the host
is a loopback server answering `POST /cancel-turn` through the same HTTP client production uses, so
the force-cancel case asserts on a real request: its route re-reads the agent row over another
connection before it answers, proving commit precedes the cancellation, and the late shell session
of the replacement case is allocated by that same route, after the force fence and before the kill.
A host that cannot be reached leaves a force termination durable and fails a cancel request. A kill
the session service could not confirm cannot be produced with a real service, because the pty client
reports a service that is down as an empty listing, so that failure path is not exercised: the kill
is the last step of the request and nothing wraps it.

Launch validation tests preserve and re-read a real agent birth row on rejection;
repeatable launches are observed through a Redis subscription. Restart overlay
validation runs in separate gateway and runner processes, asserts the selected
profile and its absent agent domain, and accepts/rejects both agent and sandbox
overlays. The gateway also asserts its sandbox domain is absent.
