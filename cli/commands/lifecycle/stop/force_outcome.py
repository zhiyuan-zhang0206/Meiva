# meiva:note
# How a force stop ends when a step reports failure through its return code.
#
# The cluster instance stop prints its own reason and returns non-zero when Postgres or Redis
# still answers afterwards. Success of the stop is not the absence of an exception: a non-zero
# code is a failed stop, reported like an incomplete normal stop but without claiming that
# nothing was force-killed.

from __future__ import annotations

__all__ = ["DataPlaneStopError", "data_plane_failure", "force_incomplete_report"]


# meiva:note
# A force stop's data-plane step reported through its return code that the plane did not stop.
#
# Only this failure is reported as an incomplete force stop. Any other failure of a force
# step (a root that cannot be reached, say) is the command's own: a force stop has no
# journal to report into.
class DataPlaneStopError(RuntimeError):
    pass


def data_plane_failure(returncode: int) -> str | None:
    if returncode == 0:
        return None
    return f"the data plane did not stop (exit code {returncode}); its output above names why"


def force_incomplete_report(error: str) -> str:
    return (
        f"Force stop incomplete: {error}. "
        "The steps before it were performed and stay done. Retry the command, or use meiva start "
        "(with this unit's explicit service selection) to resume."
    )
