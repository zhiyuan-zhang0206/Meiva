# meiva:note
# How a launch loop paces itself: how long it keeps looking at its session, how long it waits between looks, and how long it lets a freshly rendered screen settle before trusting it. The values are the launch's tuning constants; a test hands in short ones.
# meiva:link meiva/shell/coding_tools/docs/coding-tools.meiva.okf.md
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Pacing:
    timeout: float
    poll: float
    settle: float


# meiva:note
# Claude Code startup takes 5-10 seconds: poll once a second for 30 seconds, and look again two seconds after the panel first renders.
CLAUDE_READY = Pacing(timeout=30.0, poll=1.0, settle=2.0)


# meiva:note
# The four waits of a Codex launch. The shared app server's socket is probed twice a second for 20 seconds (startup measured about 1-2 seconds on codex 0.153.4, so an absent or older app server fails the launch loudly instead of hanging it). The TUI renders its frame during MCP startup, so readiness polls once a second for 90 seconds and waits two seconds while MCP starts and after the ready screen. The `/status` card is read once a second within 20 seconds. The submission check looks every three seconds for a minute and gives an Enter retry five seconds to take.
@dataclass(frozen=True)
class CodexWaits:
    server: Pacing = Pacing(timeout=20.0, poll=0.5, settle=0.0)
    ready: Pacing = Pacing(timeout=90.0, poll=1.0, settle=2.0)
    status: Pacing = Pacing(timeout=20.0, poll=1.0, settle=0.0)
    submit: Pacing = Pacing(timeout=60.0, poll=3.0, settle=5.0)
