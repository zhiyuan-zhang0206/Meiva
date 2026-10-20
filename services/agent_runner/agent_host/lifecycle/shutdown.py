# meiva:note
# Release a joined host's clients without losing primary errors.

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from base.agents.context.clients import ClientSet
from base.native_process.turn_identity import HostedServiceResources

__all__ = ["close_host_resources"]


# meiva:note
# Join native resources before releasing clients.
#
# An unfinished resource join keeps clients and pools live. Once joined, every
# cleanup runs and the first original error retains later cleanup as notes.
# Ownership needs no release: the next host boot settles this machine's rows.
async def close_host_resources(
    service: HostedServiceResources,
    clients: ClientSet,
    *,
    close_runtimes: Callable[[], Awaitable[None]],
    resource_deadline: float | None,
) -> None:
    primary: BaseException | None = None
    try:
        await service.aclose(deadline=resource_deadline)
    except BaseException as exc:
        primary = exc
    if service.joined:
        try:
            await close_runtimes()
        except BaseException as secondary:
            if primary is None:
                primary = secondary
            else:
                primary.add_note(f"Retiring cached runtimes also failed: {secondary!r}")
        try:
            # meiva:note
            # Unfinished ordinary delivery retains its ClientSet owner until hard exit.
            await asyncio.to_thread(clients.close)
        except BaseException as secondary:
            if primary is None:
                primary = secondary
            else:
                primary.add_note(f"Host client cleanup also failed: {secondary!r}")
    if primary is not None:
        raise primary
