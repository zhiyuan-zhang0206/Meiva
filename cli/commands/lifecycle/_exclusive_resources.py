# meiva:note
# Whole local start/stop operations run one at a time under the home's lifecycle lock.

from collections.abc import Callable
from functools import wraps


# meiva:note
# Serialize whole local start/stop operations, beyond short journal writes.
def exclusive_resources[**P, R](operation: Callable[P, R]) -> Callable[P, R]:

    @wraps(operation)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        from base.deploy.lifecycle.home_lifecycle_locks import resource_lock

        with resource_lock(purpose=f"cli.{operation.__name__}"):
            return operation(*args, **kwargs)

    return wrapped
