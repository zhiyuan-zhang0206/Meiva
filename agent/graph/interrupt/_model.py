import asyncio
from collections.abc import Coroutine
from typing import Any

from base.agents.messages.inbound import InterruptReason


# meiva:note
# An abort event retaining the first observed cause through cleanup. Setting the event never
# claims or applies the durable command, and a later command cannot relabel an execution that
# was already interrupted.
class InterruptEvent(asyncio.Event):
    def __init__(self) -> None:
        super().__init__()
        self.reason = InterruptReason.USER

    def set(self, reason: InterruptReason = InterruptReason.USER) -> None:
        if not self.is_set():
            self.reason = reason
            super().set()


# meiva:note
# The state update of a node an interrupt ended: halted, and why when a restart did. The one
# place that says a restart's interrupt leaves an unfinished turn halted
# (`AgentState.interrupted_by_restart`). Cancel and terminate need no mark: the claim that
# takes them decides what follows.
def interrupt_halt(reason: InterruptReason) -> dict[str, bool]:
    if reason is InterruptReason.RESTART:
        return {"halted": True, "interrupted_by_restart": True}
    return {"halted": True}


# meiva:note
# A model operation settled without a result; claim still owns the command.
class ModelInterruptedError(Exception):
    def __init__(self, reason: InterruptReason) -> None:
        super().__init__(f"interrupted by {reason.value}")
        self.reason = reason


# meiva:note
# Races side-effect-free model work against a durable interruption. The same-tick race belongs
# to cancellation. The owned operation settles before control returns to claim; the host's
# external stop boundary owns escalation if a provider refuses to unwind.
async def interruptible_model[T](
    operation: Coroutine[Any, Any, T],
    event: asyncio.Event,
) -> T:
    # meiva:note
    # The bounded owner inspects the original error after both tasks settle. Letting TaskGroup
    # raise it would cancel its sibling and wrap its identity.
    async def observed_operation() -> tuple[T] | BaseException:
        try:
            return (await operation,)
        except BaseException as error:
            return error

    async with asyncio.TaskGroup() as tasks:
        task = tasks.create_task(observed_operation())
        interrupted = tasks.create_task(event.wait())
        try:
            done, _ = await asyncio.wait({task, interrupted}, return_when=asyncio.FIRST_COMPLETED)
            cancelled = interrupted in done
        finally:
            task.cancel()
            interrupted.cancel()
    outcome = task.result()
    if isinstance(outcome, BaseException) and (
        not cancelled or not isinstance(outcome, asyncio.CancelledError)
    ):
        raise outcome
    if cancelled:
        raise ModelInterruptedError(
            event.reason if isinstance(event, InterruptEvent) else InterruptReason.USER
        )
    if isinstance(outcome, BaseException):
        raise outcome
    return outcome[0]
