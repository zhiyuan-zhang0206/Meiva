"""Framework lifecycle signals — the exceptions that drive turn-stopping.

These live in `base` (below both the agent kernel and the `meiva` SDK) so the
kernel can import them without depending on the agent-facing `meiva.self` module.
`meiva.self` re-exports them and its `restart()` / `terminate()` / `compact()` /
`update()` functions raise them; the exec node catches them to end a turn. That
separation lets a context like a benchmark runner disable `meiva.self` wholesale
(via MEIVA_SDK_DISABLE) without breaking the kernel's import of these signals.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from base.host.exit_codes import IDLE_EXIT_CODE, SYSTEM_HALT_EXIT_CODE
from base.log import logger
from base.paths import meiva_home

__all__ = [
    "AgentRestart",
    "AgentTermination",
    "LifecycleExit",
    "SystemHalt",
    "release_agent_chrome_pages",
    "release_browser_pages",
    "release_default_chrome_pages",
]

_RELEASE_STREAM_LIMIT = 64 * 1024 * 1024
_RELEASE_TIMEOUT_S = 3.0


class LifecycleExit(SystemExit):
    """Base for framework lifecycle signals — inherits SystemExit
    (not Exception), so broad try/except Exception won't accidentally
    swallow lifecycle transitions."""


class AgentTermination(LifecycleExit):
    """Raised on the success path of `terminate()`. Your process exits and
    has to be woken up by sending it a chat message (auto-resurrect)."""

    def __init__(self) -> None:
        super().__init__(IDLE_EXIT_CODE)


class AgentRestart(LifecycleExit):
    """Raised on the success path of `restart()` and `update()`. Your
    process exits and a fresh one comes up under the same agent id."""

    def __init__(self) -> None:
        super().__init__(IDLE_EXIT_CODE)


class SystemHalt(LifecycleExit):
    """Raised by `compact()` — framework takes over history compaction. Agent doesn't raise directly."""

    def __init__(self) -> None:
        super().__init__(SYSTEM_HALT_EXIT_CODE)


# meiva:note
# Close one browser-mcp socket connection; the service reads this as the
# connection ending (releasing its page-affinity context for this agent).
async def _close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with suppress(OSError):
        await writer.wait_closed()


# meiva:note
# One dial -> request -> reply round-trip; the caller owns the timeout.
async def _release_roundtrip(sock: str, agent_id: int) -> bool:
    reader, writer = await asyncio.open_unix_connection(path=sock, limit=_RELEASE_STREAM_LIMIT)
    try:
        writer.write(
            (
                json.dumps({"id": 1, "method": "release_agent_page", "agent_id": agent_id}) + "\n"
            ).encode()
        )
        await writer.drain()
        line = await reader.readline()
        if not line:
            return False
        resp: dict[str, Any] = json.loads(line)
        return resp.get("ok") is True
    finally:
        await _close_writer(writer)


# meiva:note
# Best-effort: ask the browser-mcp service of `home` to close this agent's pages.
#
# Called after an agent's termination commits so a terminated worker does not
# leave its tabs (or its `Meiva #<id>` tab group) in the user's shared Chrome.
# The service closes every page created by `agent_id` — never another
# agent's or the user's — and is idempotent.
# A machine without the browser service (no socket), a daemon that is down
# (`OSError`) or one that does not answer in time returns False; a protocol
# error propagates. Deaths that never reach termination cleanup are left to
# the service's page TTL sweep (24h by default).
async def release_agent_chrome_pages(agent_id: int, home: Path) -> bool:
    try:
        from base.config import settings
        from base.paths import chrome_mcp_socket

        if settings.has_domain("services"):
            return await asyncio.wait_for(
                _release_roundtrip(
                    str(chrome_mcp_socket(home, settings.services.browser_cdp_port)), agent_id
                ),
                timeout=_RELEASE_TIMEOUT_S,
            )
        return False
    except (OSError, TimeoutError):
        return False


# meiva:note
# Release this agent's browser-mcp pages in the active Meiva home.
async def release_default_chrome_pages(agent_id: int) -> bool:
    return await release_agent_chrome_pages(agent_id, meiva_home())


# meiva:note
# Best-effort close of this agent's browser-mcp pages after termination commits.
async def release_browser_pages(
    agent_id: int,
    *,
    page_releaser: Callable[[int], Awaitable[object]] = release_default_chrome_pages,
) -> None:
    try:
        released = await page_releaser(agent_id)
        if not released:
            logger.debug(
                "agent {agent_id} browser-page release skipped (no service / failed)",
                agent_id=agent_id,
            )
    except Exception:
        logger.opt(exception=True).warning(
            "agent {agent_id} browser-page release failed (non-fatal)",
            agent_id=agent_id,
        )
