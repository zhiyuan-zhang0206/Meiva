---
type: doc
title: Background tasks in a Claude session
description: 'What a `Bash` command started with `run_in_background=true` does to a claude harness session: the notification the CLI injects after its turn ends, the turn the CLI runs over it, dispatch held until that turn reports, the report delivered as inbound content, and stopping the tasks a closing session still tracks.'
tags:
- base
- library
- llm-inference
- claude-sdk
---

# Background tasks in a Claude session

A `Bash` command the model starts with `run_in_background=true` outlives its turn:
the agent answers and goes idle, and the CLI reports the command's end later. Two
messages carry that report, and the adapter treats them differently because they
mean different things.

- `TaskNotificationMessage` is the report itself. It maps to
  `UnsolicitedOutput(source="task-notification", text=...)` — the task id, its
  status, the provider's summary and the output file's path — and the session
  routes it by whether a turn is running. While one is, the running turn already
  received the notification in its own context, so the event is only recorded.
  While none is, nobody has read it: the session holds dispatch (below) and hands
  it to the sink, which the host writes as a `system:harness` inbound so the agent
  takes a turn over it
  ([[../../docs/attribution.meiva.okf.md|attribution]]).
- The CLI then answers the notification itself, in a turn this session did not
  start, and closes it with a `ResultMessage` whose `origin.kind` is
  `task-notification`. `_claim` gives that result no owner — the session never
  names its turns, so no name can match — and it is recorded as
  `UnsolicitedOutput` rather than read as our turn's boundary or as a failure.
  `origin.kind == "human"`, and a CLI too old to report an origin at all, still
  claim the running turn.
- Dispatch is held for the CLI's own turn so its text cannot be collected into
  ours; `injected_turn_timeout_seconds` bounds the wait. The first unclaimed
  `ResultMessage` releases it.

## Stopping them

`TaskStartedMessage` and a terminal `TaskUpdatedMessage` keep the set of tasks the
CLI says are running. `close()` asks the CLI to stop each one before it disconnects,
which is also why the close path's residue check no longer reports a background
command's children as leftovers. `interrupt()` stays what it was — it ends the
running turn, not a background task — and a task stopped that way reports `killed`
through the update message, sometimes with no notification behind it. That case is
cleared from the set but not delivered as a report: the report the agent would have
read is the notification's.
