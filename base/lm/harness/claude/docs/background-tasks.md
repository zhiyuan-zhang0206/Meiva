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
  ([attribution](../../docs/attribution.md)).
- The CLI then answers the notification itself, in a turn this session did not start, and closes
  it with a `ResultMessage` whose `origin.kind` is `task-notification`. That turn is observed like
  any other ([delivery](../../docs/delivery.md)): `TurnStarted(origin=HARNESS,
  cause=BACKGROUND_TASK)` when the notification preceded it, its text and tools attributed to it,
  and one `TurnCompleted`. It is not read as our turn's boundary and not recorded as unsolicited
  output. The report itself still goes to the sink while no turn runs, as above, because the
  graph does not yet surface observed turns; that delivery goes when it does.
- Dispatch is held for the CLI's own turn so a turn of ours does not start in the middle of it;
  `injected_dispatch_wait_seconds` bounds the wait, and the end of the observed turn releases it.

## Stopping them

`TaskStartedMessage` and a terminal `TaskUpdatedMessage` keep the set of tasks the
CLI says are running. `close()` asks the CLI to stop each one before it disconnects,
which is also why the close path's residue check no longer reports a background
command's children as leftovers. `interrupt()` stays what it was — it ends the
running turn, not a background task — and a task stopped that way reports `killed`
through the update message, sometimes with no notification behind it. That case is
cleared from the set but not delivered as a report: the report the agent would have
read is the notification's.
