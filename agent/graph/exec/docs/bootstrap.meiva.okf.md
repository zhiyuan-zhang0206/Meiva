---
type: doc
title: Execution Child Bootstrap Ownership
description: Configuration, SDK capabilities and admission belong to the execution entry.
tags: []
---

# Execution Child Bootstrap Ownership

The formal `agent.execution.child` interpreter entry owns its SDK bootstrap.
It validates the request before installing plugins and applies framework pins
to the same `ConfigBoot` supplied to its SDK authority, Clock factory, and
Gateway transport readers. Package import does not install a second owner for
this entry. Bare agent-launched Python retains its existing import-time plugin
surface contract; a request-path environment value alone cannot select the
formal execution posture.

The `agent.graph.exec` package initializer imports no graph or LM machinery:
the child must be able to import `agent.execution.protocol` before user code without loading
the node stack. `agent/tests/execution/test_lazy_child_imports.py` checks this
lightweight import boundary. Keep `agent.execution.child` recognizable as a
`python -m` process entry when tracing callers; another checkout's parent can
name it without a Python import in that parent's source.

## Direct-child test premises

The child's signal handlers belong to `main()` alone: the entry installs them
before anything else and `run_child` does not install them again. A real child
whose request file is a pipe nobody writes shows that a SIGTERM during boot
becomes a catchable `TimeoutError` and a crash envelope.

`agent/tests/execution/test_exec_child.py` launches the real interpreter entry
with a private home and request/result files. It tests the child's envelopes,
runtime SDK tally and config/state round trips; parent orchestration is separate.
The eager-config failure case removes `MEIVA_CONFIG_FETCH`, pins eager boot and
supplies a runner-profile database URL so authority validation does not mask the
intended timeout-settings failure. The lite case removes both boot overrides
and uses an invalid boot-path timezone. Both must report failure before user code.

Help tests declare machine identity in the private home's `.env`, since an
inherited identity without that declaration is dropped. Overlay-order is
shown by real children: framework configuration applies before plugin loading
and plugin configuration afterward (the probe plugin's pinned marker reaches the
child), and the installed surface then receives the additive SDK disables and
the eval isolation, observed as namespaces that are unavailable to agent code.

The direct-child helper's explicit launcher environment is a complete selected
baseline, copied before child fields are added. An empty mapping stays empty;
only a missing baseline selects a copy of the test process environment. Eager
and lite failure cases remove their boot/fetch overrides on that private copy,
not in the test process, and verify helper augmentation leaves the input intact.
Their real interpreter entry, error kinds and pre-code failure assertions remain.
