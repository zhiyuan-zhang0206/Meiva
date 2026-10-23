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
and handles the committed terminated state. A failed session kill leaves the
force fence durable; force does not inherit a superseded graceful kill request.

The native PTY case closes the owner's older shell sessions while preserving
page, foreign and post-acceptance replacement sessions. Its child job writes a
marker on ordinary exit or a handled signal. Absence of that marker after the
hard kill models suppression of an exit callback; it does not exercise watcher
notification delivery. Session IDs are fixture allocations, not reserved roles.

`terminate_agent_op` requires the host-turn cancellation and shell-session killer.
`lifecycle_op` supplies the host implementations at dispatch. Tests control those
two host effects without replacing SQL, status reads, audit writes or event
publication. The force-cancel assertion re-reads the agent row from another
connection before accepting the cancellation call, proving commit precedes it.

Launch validation tests preserve and re-read a real agent birth row on rejection;
repeatable launches are observed through a Redis subscription. Restart overlay
validation runs in separate gateway and runner processes, asserts the selected
profile and its absent agent domain, and accepts/rejects both agent and sandbox
overlays. The gateway also asserts its sandbox domain is absent.
