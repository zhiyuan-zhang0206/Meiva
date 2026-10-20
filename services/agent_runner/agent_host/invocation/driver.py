# meiva:note
# The owned invocation context: the event publisher's lifecycle around one hosted turn.

import asyncio
from collections.abc import Awaitable, Callable

from base.agents.context import MeivaContext
from services.agent_runner.agent_host.runtime import TurnOutcome


async def drive_context(
    agent_id: int,
    ctx: MeivaContext,
    invoke: Callable[[int, MeivaContext], Awaitable[TurnOutcome]],
) -> TurnOutcome:
    """The publisher and explicit context belong to this invocation, not a cached runtime."""
    publisher = ctx.event_publisher
    if publisher is None:
        raise RuntimeError("hosted invocation requires its event publisher")
    invocation_error: BaseException | None = None
    try:
        async with asyncio.TaskGroup() as tasks:
            await publisher.start(tasks)
            try:
                outcome = await invoke(agent_id, ctx)
            except BaseException as primary:
                invocation_error = primary
                try:
                    await publisher.aclose()
                except asyncio.CancelledError:
                    # TaskGroup may cancel this parent while a failed invocation is
                    # draining. Keep the original failure alongside its worker error.
                    raise primary from None
                except BaseException as cleanup:
                    raise BaseExceptionGroup(
                        "invocation and publisher cleanup failed", [primary, cleanup]
                    ) from None
                raise
            else:
                await publisher.aclose()
                return outcome
    except BaseExceptionGroup as failures:
        # TaskGroup wraps a body failure even when its worker exits cleanly.
        # Keep host lifecycle/stall classification on the original exception;
        # a worker failure or multiple failures still leave as the full group.
        if (
            invocation_error is not None
            and len(failures.exceptions) == 1
            and failures.exceptions[0] is invocation_error
        ):
            raise invocation_error from None
        raise
