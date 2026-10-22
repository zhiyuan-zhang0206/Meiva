type: Fixed

- `meiva stop --stop-extras` now refuses, before any unit stops, a command that also keeps a unit (`--keep-shell-sessions`, `--keep-browser`, `--keep-service`): retiring the permissions helper needs the service root stopped, and the root exits only when no unit stays behind, so the old path stopped every unit and then failed in the extras phase, leaving the host down with the helper still loaded. `stop_extras()` refuses the same combination itself, so no caller can bypass the rule.
