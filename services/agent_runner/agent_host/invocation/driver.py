# meiva:note
# The owned invocation context: the event publisher's lifecycle around one hosted turn.

import asyncio
from collections.abc import Awaitable, Callable

from base.api_contracts.agent_contract import DrivenContext
from services.agent_runner.agent_host import TurnOutcome
from services.agent_runner.agent_host.invocation.failure_shapes import (
    cleanup_failure,
    sole_invocation_error,
)


# meiva:note
# The publisher and explicit context belong to this invocation, not a cached runtime.
async def drive_context[C: DrivenContext](
    agent_id: int,
    ctx: C,
    invoke: Callable[[int, C], Awaitable[TurnOutcome]],
) -> TurnOutcome:
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
                    # meiva:note
                    # TaskGroup may cancel this parent while a failed invocation is
                    # draining. Keep the original failure alongside its worker error.
                    raise primary from None
                except BaseException as cleanup:
                    raise cleanup_failure(primary, cleanup) from None
                raise
            else:
                await publisher.aclose()
                return outcome
    except BaseExceptionGroup as failures:
        original = sole_invocation_error(failures, invocation_error)
        if original is not None:
            raise original from None
        raise
