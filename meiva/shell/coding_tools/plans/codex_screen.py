# meiva:note
# What a Codex launch can tell from the screen it captures: whether the TUI finished MCP startup, whether the first message was submitted, which session id the `/status` card shows, and what the launch tells the operator when a check cannot finish. The launcher captures and waits; these decide what a capture means.
# meiva:link meiva/shell/coding_tools/docs/coding-tools.meiva.okf.md
from __future__ import annotations

import re
from typing import Literal

_SESSION_ID = re.compile(
    r"Session:\s+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)

UiState = Literal["blank", "starting_mcp", "ready"]


# meiva:note
# `len(output) > 50` alone is a FALSE ready signal: the TUI renders its frame during "Starting MCP servers (0/2)", and a message sent then has its Enter swallowed, so the text parks in the composer and the session looks alive but never works (empirical 2026-08-26 #3438, 2026-09-02 #5655, 2026-09-03 #5779). "Starting MCP" therefore counts as not ready until it disappears.
def ui_state(output: str) -> UiState:
    if len(output) <= 50:
        return "blank"
    return "starting_mcp" if "Starting MCP" in output else "ready"


# meiva:note
# Submission signal: the capture shows "Working", Codex's busy state line.
def is_working(output: str) -> bool:
    return "Working" in output


# meiva:note
# The Codex session id shown on the `/status` card, the id `codex resume <id>` takes; None when the card is not on the screen.
def parse_session_id(screen: str) -> str | None:
    found = _SESSION_ID.search(screen)
    return found.group(1) if found is not None else None


# meiva:note
# What the launch says when the readiness wait ran out. `rendered` is whether the TUI ever drew a frame.
def ready_timeout_line(timeout: float, *, rendered: bool) -> str:
    state = "rendered, still starting MCP" if rendered else "never rendered"
    return f"  -> timeout after {timeout:.0f} s ({state}), sending anyway"


# meiva:note
# Warn, never raise, when a receipt checkpoint meets a dead session. The checkpoint's contract is loud-not-fatal: the operator sees the warning and checks the session. An escaped ValueError is read as launch failure by the caller, which kills the session and rolls the generation back.
def dead_session_warning(action: str, error: str) -> str:
    return (
        "  -> WARNING: submission unverified "
        f"({action} failed: {error}); the session may have ended. "
        "Check the session before relying on it."
    )
