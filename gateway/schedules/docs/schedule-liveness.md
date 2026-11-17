# Schedule liveness

A waiting schedule has no resident process. During an open run, the manager
tracks the runner's PID and native birth identity. PID reuse never inherits
execution custody.

After a manager restart, an atomic outcome file settles the original run. A
verified live runner is adopted under its original timeout. A runner that
vanishes without an outcome closes the run as an error. A process table read
that cannot establish identity does not authorize killing an unrelated process.

Logs remain in the schedule workspace as `latest.log`, independently of the
runner's lifetime. See [schedule runner](schedule-runner.md) for process custody
and [Schedule Manager & Runner](schedules.md) for state transitions.
