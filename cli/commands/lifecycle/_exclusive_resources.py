# meiva:note
# Whole local start/stop operations run one at a time under the home's lifecycle lock.

from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps


# meiva:note
# A different process owns startup, including readiness.
@dataclass(frozen=True)
class StartDelegation:
    run: Callable[[], int]


# meiva:note
# Serialize whole local start/stop operations, beyond short journal writes.
def exclusive_resources[**P, R](operation: Callable[P, R]) -> Callable[P, R]:

    @wraps(operation)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        from base.deploy.lifecycle.home_lifecycle_locks import resource_lock

        with resource_lock(purpose=f"cli.{operation.__name__}"):
            return operation(*args, **kwargs)

    return wrapped


# meiva:note
# Run a start exclusively; a start that delegates runs its child after the lock is released.
def delegable_start[**P](start: Callable[P, int | StartDelegation]) -> Callable[P, int]:

    @exclusive_resources
    def start_locked(*args: P.args, **kwargs: P.kwargs) -> int | StartDelegation:
        return start(*args, **kwargs)

    @wraps(start)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> int:
        result = start_locked(*args, **kwargs)
        # meiva:note
        # The GUI child takes this same lock. The observer releases it before
        # kicking/waiting.
        return result.run() if isinstance(result, StartDelegation) else result

    return wrapped
