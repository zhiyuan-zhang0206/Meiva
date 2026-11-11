# A session holding background work is not idle

Status: decided 2026-11-11 by the project owner.

## Decision

1. The CLI's own background tasks, timers and monitors are supported on external
   harnesses. Meiva provides no scheduling, wake or background-process mechanism of its
   own to them. A mechanism of the CLI that stops working under Meiva is a Meiva bug.
2. A hosted runtime whose external session holds pending background work (a background
   command or sub-agent, a timer or cron job, a turn the CLI started itself) does not
   age out by the idle TTL, and the LRU bound takes it only after every idle runtime is
   gone. The cache size is the ceiling; no new setting.
3. When a CLI process is nevertheless closed with work pending (host restart, update,
   forced eviction, harness switch, an Antigravity respawn), the agent is told what was
   lost, once, in the first input after the session resumes.
4. Where an adapter cannot tell whether work is pending, it errs towards keeping the
   session, bounded in time.

## Why

Closing an idle session discarded the agent's own timers and unfinished commands with no
signal to the agent or the user. Measured with real CLIs: Claude timers fire only while
the process lives and are gone after a disconnect and resume; Codex background commands
survive the turn and vanish with the app-server.

## Cost

A kept session is one CLI process (a Claude CLI, a Codex app-server, an `agy`) held beyond
the idle TTL. The number is visible as `kept_alive_sessions` in the host's `/stats`, and is
bounded by `MEIVA_HOST_AGENT_CACHE_SIZE`. A cancelled Antigravity schedule keeps its session
up to a day because cancellation is not observable.

## Supersedes

The statement in
[2026-10-19-external-harness](2026-10-19-external-harness.md) that the existing idle
TTL/LRU is what closes an external session, for sessions with pending background work.

See [background work](../../../../base/lm/harness/docs/background-work.md).
