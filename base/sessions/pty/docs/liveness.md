# PTY session liveness

A matching PID/start-time is not sufficient for liveness: the service's
`PtySession.pid_matches` rejects `STATUS_ZOMBIE`. A zombie cannot execute, even
when its parent has not reaped its PID yet (the service is the shell's parent
and reaps it on its next pass). Start-time identity still rejects recycled PIDs.
`has`, `list` and every op that needs a live shell read the table through this
rule, so a session whose shell exited reads as gone before the service has torn
it down.

Closure requires the known shell to be gone or zombie. Known job leftovers
are diagnostic; no all-descendant absence proof is made. PID disappearance
alone measures the parent's
reap timing, not whether the child can keep executing.

## Dependencies

- [PTY sessions — every agent interactive shell lives in the pty-sessions service](pty_sessions/pty_sessions.md) — the service and session lifecycle.
