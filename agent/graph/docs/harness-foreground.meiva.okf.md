---
type: doc
title: Harness foreground input and cancellation
description: Provider acceptance, queued steering, and turn-scoped cancellation.
tags:
- agent
- graph
- external-agents
---

# Foreground input and cancellation

An external claim prepares the initial input and keeps its chat rows visible in
the pending strip. The timeline and inbox are committed together after the
executor accepts the input. Codex reports acceptance from its correlated start
response. For adapters without a start receipt, the first attributable
activity or terminal result is evidence that the input reached execution.

While Codex is active, a queued text chat can steer the same foreground. It
stays pending until the matching user message is recorded by Codex. A definite
rejection leaves the chat for the next claim. An unconfirmed delivery leaves
an uncertainty marker and is not automatically sent a second time. Other
adapters and unsupported input forms leave the chat queued.

Cancel targets the foreground. The graph continues reading activity until the
executor reports the terminal outcome. A normal finish that races a cancel
remains a normal finish. The cancel request does not retire a healthy session
or stop independent background work. A provider that lacks scoped cancellation
reports it as unavailable and the foreground continues. Restart and terminate
remain separate lifecycle commands and can still interrupt a graph invocation.

Parent: [[agent/graph/docs/harness-turn.meiva.okf.md|Harness turn]].
