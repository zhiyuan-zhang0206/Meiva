type: Fixed

- `cli/commands/lifecycle/tests/test_start_host_dependencies.py` refused-tool test now
  runs the gate over an injected Linux `Host` instead of the platform of the machine
  running it, so it asserts `✗ crontab` and its repair everywhere; a second case proves
  the macOS table (`✗ launchctl`, no `crontab`) from the same Linux-run assertion. It
  was red on macOS, where `Host.system()` selected the launchctl rows
  ([the note it generalises](cli/commands/lifecycle/tests/test_start_host_dependencies.py)).
