---
type: doc
title: Graph child process boundary
description: One required child execution operation, bound by graph composition.
tags: []
---

# Graph child process boundary

The compiled graph binds one stateless `SubprocessExecProcess` through a required
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
reads. No live configuration is frozen at graph construction.

The production adapter delegates to the existing subprocess backend. Its editable
installation admission, process domain, native signals, output pipe, grace/reap
and artifact cleanup contracts remain there. Graph tests can supply scripted
sum/envelope outcomes without replacing interruption, payload validation, elapsed
measurement or reducer commits. A plugin's base messages channel still requires
its real registry declaration; a scripted result cannot bypass that validation.

The process operation does not enter the shared context schema or public SDK.
No arbitrary writer, attachment builder or bootstrap callback belongs to it.

Cancellation tests keep graph dispatch and the frontend cancellation event while
supplying a cancelled child outcome. The outer-timeout test uses a cancellable
future to model a framework wait that the inner execution timeout missed; its
explicit configuration owner supplies the short outer deadline.

Parent: [[exec.meiva.okf.md]].
