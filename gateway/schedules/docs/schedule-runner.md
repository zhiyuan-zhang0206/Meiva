# Schedule runner process entries

The schedule manager uses one static module entry for each due fire.

## Declarative single-round entry

Each due fire launches `runner --single-round` without a PTY. That wrapper writes
the runner identity, starts `runner --payload run.py` in its own process group,
waits for the schedule's finite timeout, and leaves an atomic outcome file for
manager recovery. TERM/INT cancellation propagates to the payload; timeout escalates
to process-group KILL. The payload entry boots the ordinary SDK context from
`MEIVA_SCHEDULE_ID` and optional `MEIVA_AGENT_ID`, then executes `run.py` once.

The manager owns the run-history row, output file, and schedule-state transition.
The payload owns only its Python process and ordinary files in the current working
directory.

The payload sees `sys.argv = [str(payload_path)]`. The runner's `--payload`
arguments and custody descriptor stay outside the script's argument parser.
The original process argument list is restored after execution.

The module entry passes arguments after the program name explicitly to `main`.
The entry boots the ordinary SDK context with the schedule actor and optional
owner attribution, while the manager retains admission and settlement ownership.
