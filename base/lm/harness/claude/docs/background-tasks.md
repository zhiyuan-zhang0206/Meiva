# Background tasks in a Claude session

A `Bash` command the model starts with `run_in_background=true` outlives its turn:
the agent answers and goes idle, and the CLI reports the command's end later. Two
messages carry that report, and the adapter treats them differently because they
mean different things.

- `TaskNotificationMessage` is the report itself. It maps to
  `UnsolicitedOutput(source="task-notification", text=...)` — the task id, its
  status, the provider's summary and the output file's path — and the session always records it in
  the event stream. What else happens depends on whether a turn is running:
  - While one is, the running turn already received the notification in its own context, so
    nothing more happens (recorded: a `TaskStop` inside the model's own turn).
  - While none is, the CLI is expected to read it and answer in a turn of its own (below), so the
    session keeps the report and waits for that turn instead of delivering it.
- **The CLI's own turn.** For a finished command, a failed command (non-zero exit) and a Monitor that
  ended by itself, all idle, the CLI answered the notification: `task_updated`, `task_notification`,
  `background_tasks_changed`, a fresh `init`, the answer's content, and a `ResultMessage` with
  `origin = {kind: task-notification, producer: session-task, runId}`, with no echoed user message
  (`tests/fixtures/background_bash.json`, `background_ends.json`; `subagent_background.json` shows it for
  an async sub-agent). That turn is observed like any other ([delivery](../../docs/delivery.md)):
  `TurnStarted(origin=HARNESS, cause=BACKGROUND_TASK)`, its text and tools attributed to it, one
  `TurnCompleted`. It is not read as our turn's boundary and not recorded as unsolicited output.
- **The wait.** The gap between the notification and the first frame of that turn is a model call
  (about 15 s on the recordings, longer when the CLI adds its own nudge). The task is already reported
  and no observed turn exists, so for that gap the session counts one `turn` item per notification
  ("the CLI did not answer yet: ...") in `background_work()`, and the idle TTL cannot recycle a session
  whose command ran longer than the TTL before the CLI answers. A turn ends the wait of the oldest item
  when the CLI opens it with no origin or with the `task-notification` origin; a turn of ours or a timer's
  does not. After `ANSWER_WAIT_SECONDS` (120 s, several times the recorded wait) with no such turn, the
  kept report is handed to the sink (written by the host as a `system:harness` inbound) and the item
  is gone, so a notification the CLI does not answer is never lost and never keeps the session alive.
- **A task stopped while idle is neither waited for nor delivered.** Stopped through the SDK's
  `stop_task` with the session idle, the CLI sent `task_updated(killed)` and `task_notification(stopped)`
  and then nothing for 32 s; the next input's turn knew the task had been stopped, so the CLI carries the
  notification into the next turn's context without a turn of its own (`background_ends.json`,
  `stop_idle_*`). The session only records the report: delivering it would repeat what the CLI says
  itself and wake the agent for a task stopped on purpose.
- **Persistence.** The wait item is written to the session record, without an end time, with the other
  pending work, and stays there until the CLI answers or the fallback delivers the report. A process
  lost at any point before that, inside the wait or after it lapsed, leaves it: the next session tells
  the agent once what was pending (the summary line of the report) and clears the record, and it sets no
  timer, so the report is not also delivered to the sink. Once answered or delivered the item is removed.
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
