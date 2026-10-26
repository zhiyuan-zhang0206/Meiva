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
from contextlib import suppress
from pathlib import Path
from typing import Any

from base.agents.lifecycle_signals import (
    AgentRestart as AgentRestart,
)
from base.agents.lifecycle_signals import (
    AgentTermination as AgentTermination,
)
from base.agents.lifecycle_signals import (
    LifecycleExit as LifecycleExit,
)
from base.agents.lifecycle_signals import (
    SystemHalt as SystemHalt,
)
from base.log import logger

__all__ = [
    "AgentRestart",
    "AgentTermination",
    "LifecycleExit",
    "SystemHalt",
    "release_agent_chrome_pages",
    "release_browser_pages",
]

_RELEASE_STREAM_LIMIT = 64 * 1024 * 1024
_RELEASE_TIMEOUT_S = 3.0


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
# Locate the unit home's active chrome MCP socket under `<home>/run/`, or default to 9222 when absent.
def _chrome_socket(home: Path) -> Path:
    from base.paths import chrome_mcp_socket, run_dir

    try:
        matches = sorted(run_dir(home).glob("chrome-mcp.*.sock"))
    except OSError:
        matches = []
    return matches[0] if matches else chrome_mcp_socket(home, 9222)


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
        return await asyncio.wait_for(
            _release_roundtrip(str(_chrome_socket(home)), agent_id),
            timeout=_RELEASE_TIMEOUT_S,
        )
    except (OSError, TimeoutError):
        return False


# meiva:note
# Best-effort close of this agent's browser-mcp pages in `home` after termination commits.
async def release_browser_pages(agent_id: int, *, home: Path) -> None:
    try:
        released = await release_agent_chrome_pages(agent_id, home)
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
