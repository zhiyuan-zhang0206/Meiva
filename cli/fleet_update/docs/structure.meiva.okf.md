---
type: doc
title: Fleet update structure
description: How cli.fleet_update separates what a window decides from what it does, and where each piece lives.
tags: [cli, updates, architecture]
---

# Fleet update structure

`main` is a shell around decisions. Every ambient fact reaches it as a value read once by the
entry (`__main__`): the repository the NEW commit is resolved in, the environment (the `PATH`
that finds `ssh`, and the agent marker that refuses a run from inside an agent), the time the log
and the one-shot job names are stamped with, and the pacing of its waits. Nothing below reads the
environment, the clock or `sys.argv`.

- `session.py` owns the connection to the hosts: the operator's own `ssh`, found on the `PATH` the
  process was started with, and the one redacted log. `FailedError` is a step that cannot
  complete; `RefusedError` is a window that refused before touching anything (exit 2).
- `choices.py` parses and validates every explicit choice. `down.py` and `up.py` are the two
  halves; `fleet_gate.py` and `fleet_verify.py` are the reads and the verification they run.
- `plan/` holds what the window decides, as strict pure modules: the order of the stops and the
  flags each host gets (`stop_wave`, `wave_order`), the journal reading that says whether a stop
  finished (`journal`), what a failed stop brings back (`recovery`), what the probes refuse on
  (`host_probe`, `helper_gate`, `old_target`), how the shipped programs' replies are read
  (`stored_schedules`, `lease_announcement`, `silence_replies`), the roster gate and the
  verification steps (`roster`, `verify_steps`, `host_failures`), and the command text each host
  receives (`host_commands`, `gateway_commands`, `oneshot`). The shell reads facts, runs the
  commands and prints the lines these return.

## Contract

Nothing in cli.fleet_update reads the environment, the clock or sys.argv below the entry point: main receives the repository, the environment, the stamp time and the pacing as values, every host command goes through the ssh found on the PATH main was given, and each decision the window makes is a pure function in plan/ of the text a host printed.

Parent: [[cli/docs/fleet-update.meiva.okf.md]].
