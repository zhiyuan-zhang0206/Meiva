# Background tasks in a Claude session

A `Bash` command the model starts with `run_in_background=true` outlives its turn:
the agent answers and goes idle, and the CLI reports the command's end later. Two
messages carry that report, and the adapter treats them differently because they
mean different things.

- `TaskNotificationMessage` is the report itself. It maps to
  `UnsolicitedOutput(source="task-notification", text=...)` — the task id, its
  status, the provider's summary and the output file's path — and the session only
  records it in the event stream. It is never handed to the sink: the CLI reads the
  notification itself, in the running turn's context when a turn runs, and otherwise in a
  turn of its own (below). Delivering it too would give the agent the same report twice and
  run a second turn.
- The CLI answers the notification in a turn this session did not start, and closes
  it with a `ResultMessage` whose `origin.kind` is `task-notification`. That turn is observed like
  any other ([delivery](../../docs/delivery.md)): `TurnStarted(origin=HARNESS,
  cause=BACKGROUND_TASK)` when the notification preceded it, its text and tools attributed to it,
  and one `TurnCompleted`. It is not read as our turn's boundary and not recorded as unsolicited
  output. The order was recorded from a real CLI (`tests/fixtures/background_bash.json`, a
  background `Bash` that ended while the agent was idle, twice with the same result;
  `subagent_background.json` records the same for an async sub-agent): `task_updated`,
  `task_notification`, `background_tasks_changed`, a fresh `init`, the answer's content, and a
  `ResultMessage` with `origin = {kind: task-notification, producer: session-task}`. No user message
  is echoed for it, and the CLI started the turn on its own after the command ended.
- The gap between the notification and the first frame of that turn is a model call (about 15 s on
  the recording, longer when the CLI adds its own nudge). The task is already reported and no
  observed turn exists, so for that gap the session claims one `turn` item ("the CLI is answering the
  end of task ...") in `background_work()`, and the idle TTL cannot recycle a session whose command ran
  longer than the TTL before the CLI answers. It ends when any turn opens (the observed-turn item takes
  over, never counted twice) or after `ANSWER_WAIT_SECONDS` (120 s, several times the recorded wait), so
  a notification the CLI never answers cannot keep the session forever. A task the model stopped itself
  (`stopped`) is not waited for. The item is persisted with the other pending work, so a process lost
  inside the window is told to the agent on the next session while the item is unexpired; once the 120 s
  have passed it is left out like any item past its end time, and the report is lost with nothing said.
- Nothing is held for the CLI's own turn: an input delivered meanwhile is written to the CLI, which
  runs it behind its own turn, and the events say which turn it entered.

## Stopping them

`TaskStartedMessage` and a terminal `TaskUpdatedMessage` keep the set of tasks the
CLI says are running. `close()` asks the CLI to stop each one before it disconnects,
which is also why the close path's residue check no longer reports a background
command's children as leftovers. `interrupt()` stays what it was — it ends the
running turn, not a background task — and a task stopped that way reports `killed`
through the update message, sometimes with no notification behind it. That case is
cleared from the set but not delivered as a report: the report the agent would have
read is the notification's.

## Counting them as pending work

The same task set backs `ClaudeSession.background_work()`, with each task's description
and kind (command or sub-agent). A `Monitor` call is one of those commands: the CLI announces it with
`TaskStarted(task_type=local_bash)`, ends it with `TaskUpdated(completed)` and a `TaskNotification`
(`TaskStop` gives `killed` and `stopped`), so a session whose only work is a Monitor stays out of
the idle TTL until its source ends or is stopped
(fixture `tests/fixtures/monitor_background.json`, recorded on CLI 2.1.322). Each output line wakes
the CLI as a turn of its own (`origin.kind` `task-notification`) with no Task frame, so those lines
neither start nor end anything here. The timers the model sets with `CronCreate`,
`CronDelete`, `CronList` and `ScheduleWakeup` are followed from those tool calls and their
results (`ScheduledWork` in `inbound.py`; fixture
`tests/fixtures/scheduled_work.json` holds the recorded shapes). They live only in the CLI
process and do not survive a resume. No event says one fired, so a one-shot timer stops
counting a few minutes after its due time and a recurring cron at `CronDelete` or the CLI's
seven-day expiry; `CronList` replaces the cron set when it is in the stream. While any of
this is pending, the host keeps the session out of the idle TTL
([background work](../../docs/background-work.md)).
