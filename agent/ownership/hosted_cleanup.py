"""Independent notification and best-effort browser/shell cleanup after hosted death."""

import asyncio
from collections.abc import Awaitable, Callable

from base.agents.lifecycle import (
    release_browser_pages,
    release_default_chrome_pages,
)
from base.events.live.announce import publish_agent_updated
from base.events.live.bus import EventBus
from base.log import logger


async def finish_termination(
    bus: EventBus,
    agent_id: int,
    cutoff: int | None,
    killer: Callable[[int, int], None] | None,
    *,
    page_releaser: Callable[[int], Awaitable[object]] = release_default_chrome_pages,
) -> None:
    """Attempt every post-commit effect and preserve the first failure, including cancellation."""
    primary_error: BaseException | None = None
    try:
        await publish_agent_updated(bus, agent_id)
    except BaseException as exc:
        primary_error = exc
    try:
        await release_browser_pages(agent_id, page_releaser=page_releaser)
    except BaseException as browser_error:
        if primary_error is None:
            primary_error = browser_error
        else:
            primary_error.add_note(f"Post-commit browser cleanup also failed: {browser_error!r}")
            logger.opt(exception=browser_error).error(
                "termination committed but browser cleanup also failed after publication failure",
                agent_id=agent_id,
            )
    try:
        if cutoff is not None and killer is not None:
            await asyncio.to_thread(killer, agent_id, cutoff)
    except BaseException as cleanup_error:
        if primary_error is None:
            raise
        primary_error.add_note(f"Post-commit shell cleanup also failed: {cleanup_error!r}")
        logger.opt(exception=cleanup_error).error(
            "termination committed but shell cleanup also failed after an earlier failure",
            agent_id=agent_id,
            cutoff=cutoff,
        )
    if primary_error is not None:
        raise primary_error
