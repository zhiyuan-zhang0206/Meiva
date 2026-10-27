---
type: doc
title: Graph child process boundary
description: One required child execution operation, bound by graph composition.
tags: []
---

# Graph child process boundary

The compiled graph binds one immutable `SubprocessExecProcess` through a required
`ExecProcess.run` operation. Each invocation supplies its own context, state,
overlay, cancellation event and output publisher. The node captures overlay before
subscribing to durable interrupts and retains shield timeout, cancellation
attribution and payload validation. The operation returns the existing result
sum and raw envelope; unknown SDK tally remains absent, while an empty tally
remains an explicit zero. It does not own attachment formatting or graph state
reducers.

The real operation is synchronous until it returns the backend awaitable: inner
execution limits are read before the outer shield timeout. State serialization
and those reads occur without suspension; serialization now precedes the inner
reads. No live configuration is frozen at graph construction; the only values handed in then
are the process's `ParentFacts` and the generator the envelope identities are drawn from.

The production adapter delegates to the existing subprocess backend. It is a frozen
value holding the `ParentFacts` of this process (environment, cwd, interpreter,
checkout, editable roots), which the agent host's root reads once with
`services.agent_runner.agent_host.daemon.read_parent_facts` and passes to `build_graph`, and the `random.Random` it draws
each call's request and result envelope identities from (`req-<uuid>.json`,
`res-<uuid>.json`; the root builds a cryptographically secure one, a test a seeded one).
Before it writes a request, `run_in_subprocess` prunes the agent's stale result files
against its context's clock (`protocol.prune_stale`). After editable-install admission and writing the
request envelope, `run_in_subprocess` connects to the per-home `exec-host` service
over `<home>/run/exec-host.sock` (`_remote.py` / `base.sessions.exec_host`) when
available, streaming output frames into `StreamingTextIO`, forwarding cancellation
in-band, and letting `exec-host` own child spawning and process-group supervision;
when `exec-host.sock` is not listening (such as in standalone unit tests), it falls
back to spawning `agent.execution.child` directly. Both routes converge on the same
result-envelope decode, `result_from_payload` mapping, and request/result file
cleanup. A test that must change what the child inherits
constructs the adapter with its own `ParentFacts` (a changed copy of
`live_parent_facts()` from `tests/process_inputs.py`) or hands one to the backend; none subclasses the adapter or
patches the environment. Graph tests can supply scripted
sum/envelope outcomes without replacing interruption, payload validation, elapsed
measurement or reducer commits. A plugin's base messages channel still requires
its real registry declaration; a scripted result cannot bypass that validation.

The process operation does not enter the shared context schema or public SDK.
No arbitrary writer, attachment builder or bootstrap callback belongs to it.

Cancellation tests keep graph dispatch and the frontend cancellation event while
supplying a cancelled child outcome. A turn without an inbound queue
(`ops_pool=None`) runs its exec uninterruptibly, so tests that do not interrupt
do not replace the durable-interrupt subscription; the one that does inserts a
real pending row. The outer-timeout test uses a cancellable future to model a
framework wait that the inner execution timeout missed; the turn's `AgentSlices`
supplies the short outer deadline. The scripted process keeps no call record: a
test that needs to know what reached the process lets the outcome report it, so
the assertion reads the committed tool result.

Parent: [[exec.meiva.okf.md]].
