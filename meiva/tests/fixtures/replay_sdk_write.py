# meiva:note
# Run one public SDK write as the first call of a checkpointed execution.

from collections.abc import Callable
from dataclasses import replace
from uuid import NAMESPACE_URL, uuid5

from base.agents.sdk.operation_ids import ExecutionIdentity, OperationIds
from meiva.sdk_surface import process_context


def replay_uid(execution: str) -> str:
    identity = ExecutionIdentity(uuid5(NAMESPACE_URL, execution), None)
    return str(OperationIds(identity).next_uid())


def replay_sdk_write[**P, R](
    execution: str, write: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs
) -> R:
    context = process_context.current_context()
    identity = ExecutionIdentity(uuid5(NAMESPACE_URL, execution), None)
    process_context.bind_context(replace(context, operation_ids=OperationIds(identity)))
    try:
        return write(*args, **kwargs)
    finally:
        process_context.bind_context(context)
