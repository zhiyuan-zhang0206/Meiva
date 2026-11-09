# Session primitives

`base/sessions/` owns native session backends, coding-session owners and PTY lifetimes.
Its component nodes describe the current contracts and implementation.

## Documented components

- [Coding Session Owner](coding-session-owner.md) — Coding Session Owner.
- [macOS Firewall Manifest](firewall-audit.md) — macOS Firewall Manifest.
- [Process Supervision](process-supervision.md) — Process Supervision.
- [Session backend and process supervision](session-backend.md) — Session backend & process supervision.
- [Stopping a process — the kill contract and the non-session trio](stopping.md) — Stopping a process — the kill contract and the non-session trio.
- [PTY allocation generation boundary](../pty/docs/generation-boundary.md) — PTY allocation generation boundary.
- [PTY session liveness](../pty/docs/liveness.md) — PTY session liveness.
- [PTY sessions — every agent interactive shell lives in the pty-sessions service](../pty/docs/pty_sessions/pty_sessions.md) — PTY sessions — every agent interactive shell lives in the pty-sessions service.
- [PTY session kill — bounded best-effort closure](../pty/docs/session-kill.md) — PTY session kill — bounded best-effort closure.

Native fixture evidence: [Session test process evidence](test-process-evidence.md).
