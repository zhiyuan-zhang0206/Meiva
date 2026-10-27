# meiva:note
# Which failure leaves a driven invocation, as pure functions of the exceptions the driver
# collected. `drive_context` raises what these answer; it picks nothing itself.

from __future__ import annotations


# meiva:note
# The invocation's own error when the task group wrapped nothing but it. A task group wraps a body
# failure even when its worker exits cleanly; host lifecycle and stall classification must keep
# seeing the original exception then. A worker failure, or several failures, leave as the group.
def sole_invocation_error(
    failures: BaseExceptionGroup[BaseException], invocation_error: BaseException | None
) -> BaseException | None:
    if (
        invocation_error is not None
        and len(failures.exceptions) == 1
        and failures.exceptions[0] is invocation_error
    ):
        return invocation_error
    return None


# meiva:note
# The group raised when the invocation failed and closing its publisher failed too.
def cleanup_failure(
    invocation_error: BaseException, cleanup_error: BaseException
) -> BaseExceptionGroup[BaseException]:
    return BaseExceptionGroup(
        "invocation and publisher cleanup failed", [invocation_error, cleanup_error]
    )
